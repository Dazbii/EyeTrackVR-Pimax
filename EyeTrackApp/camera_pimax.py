from time import sleep

import numpy

from config import EyeTrackConfig
import requests
from enum import Enum
import threading
import queue
import runpy
import cv2
import time

import win32gui
import win32ui
from ctypes import windll
from PIL import Image

WAIT_TIME = 0.1

width = 320
height = 240
fps = 120

borderLeft = 0
borderTop = 0
borderRight = 0
borderBot = 0

def hook_window(name):
    global borderLeft
    global borderTop
    global borderRight
    global borderBot
    hwnd = win32gui.FindWindow(None, name)
    windll.user32.SetProcessDPIAware()

    clientLeft, clientTop, clientRight, clientBot = win32gui.GetClientRect(hwnd)
    clientLeft, clientTop = win32gui.ClientToScreen(hwnd, (clientLeft, clientTop))
    clientRight, clientBot = win32gui.ClientToScreen(hwnd, (clientRight, clientBot))
    
    left, top, right, bot = win32gui.GetWindowRect(hwnd)
    
    borderLeft = clientLeft - left
    borderTop = clientTop - top
    borderRight = clientRight - right
    borderBot = clientBot - bot
    
    w = right - left
    h = bot - top

    hwndDC = win32gui.GetWindowDC(hwnd)
    mfcDC  = win32ui.CreateDCFromHandle(hwndDC)
    saveDC = mfcDC.CreateCompatibleDC()

    saveBitMap = win32ui.CreateBitmap()
    saveBitMap.CreateCompatibleBitmap(mfcDC, w, h)

    saveDC.SelectObject(saveBitMap)
    return hwnd, saveDC, saveBitMap, hwndDC, mfcDC

def hook_eye_window(index):
    window_name = "draw Image1"
    if (index > 0):
        window_name = "draw Image2"

    return hook_window(window_name)

def get_image(hwnd, saveDC, saveBitMap):
    # - The last parameter controls flags. If I say 0 or 1 it lags when capturing both eyes. 
    #   I can avoid this by calling PrintWindow multiple times (4 seems to do the trick) for some reason
    # - It also seems to work if I call it with a value of 2. Not sure what this actually is,
    #   I think it might be PW_RENDERFULLCONTENT? Still not sure why that would fix it
    windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)

    bmpinfo = saveBitMap.GetInfo()
    bmpstr = saveBitMap.GetBitmapBits(True)

    im = Image.frombuffer(
        'RGB',
        (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
        bmpstr, 'raw', 'BGRX', 0, 1)
    im = numpy.array(im)
    cropped = im[1+borderTop:im.shape[0]+borderBot,  1+borderLeft:im.shape[1]+borderRight, :]
    return cropped

class CameraState(Enum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2

class Camera:
    def __init__(
        self,
        config: EyeTrackConfig,
        camera_index: int,
        cancellation_event: "threading.Event",
        capture_event: "threading.Event",
        camera_status_outgoing: "queue.Queue[CameraState]",
        camera_output_outgoing: "queue.Queue",
    ):
        self.camera_status = CameraState.CONNECTING
        self.config = config
        self.camera_index = camera_index
        self.camera_address = config.capture_source
        self.camera_status_outgoing = camera_status_outgoing
        self.camera_output_outgoing = camera_output_outgoing
        self.capture_event = capture_event
        self.cancellation_event = cancellation_event
        self.current_capture_source = config.capture_source
        self.error_message = "Capture source {} not found, retrying"
        self.hwnd, self.saveDC, self.saveBitMap, self.hwndDC, self.mfcDC = (None, None, None, None, None)
        self.windows_hooked = False
        self.frame_number = 0
        self.fps = fps
        self.bps = 442368

    def unhook(self):
        if self.windows_hooked:
            win32gui.DeleteObject(self.saveBitMap.GetHandle())
            self.saveDC.DeleteDC()
            self.mfcDC.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, self.hwndDC)
            self.windows_hooked = False

    def set_output_queue(self, camera_output_outgoing: "queue.Queue"):
        self.camera_output_outgoing = camera_output_outgoing

    def run(self):
        while True:
            if self.cancellation_event.is_set():
                print("Exiting capture thread")
                self.unhook()
                return
            should_push = True
            # If things aren't open, retry until they are. Don't let read requests come in any earlier
            # than this, otherwise we can deadlock ourselves.
            if (
                self.config.capture_source != None and self.config.capture_source != ""
            ):
                if (
                    not self.windows_hooked
                    or self.camera_status == CameraState.DISCONNECTED
                    or self.config.capture_source != self.current_capture_source
                ):
            #         print(self.error_message.format(self.config.capture_source))
            #         # This requires a wait, otherwise we can error and possible screw up the camera
            #         # firmware. Fickle things.
            #         if self.cancellation_event.wait(WAIT_TIME):
            #             return
            #         self.current_capture_source = self.config.capture_source
            #         self.wired_camera = cv2.VideoCapture(self.current_capture_source)
                    self.hwnd, self.saveDC, self.saveBitMap, self.hwndDC, self.mfcDC = hook_eye_window(self.config.capture_source)
                    self.windows_hooked = True
                    self.current_capture_source = self.config.capture_source
                    get_image(self.hwnd, self.saveDC, self.saveBitMap)
                    self.frame_number += 1    
                    should_push = False
            # else:
            #     # We don't have a capture source to try yet, wait for one to show up in the GUI.
            #     if self.cancellation_event.wait(WAIT_TIME):
            #         self.camera_status = CameraState.DISCONNECTED
            #         return
            # Assuming we can access our capture source, wait for another thread to request a capture.
            # Cycle every so often to see if our cancellation token has fired. This basically uses a
            # python event as a contextless, resettable one-shot channel.
            if should_push and not self.capture_event.wait(timeout=0.02):
                continue

            self.get_wired_camera_picture(should_push)
            if not should_push:
                # if we get all the way down here, consider ourselves connected
                self.camera_status = CameraState.CONNECTED

    def get_wired_camera_picture(self, should_push):
        try:
            if should_push:
                image = get_image(self.hwnd, self.saveDC, self.saveBitMap)
                scale_percent = 100
                width = int(image.shape[1] * scale_percent / 100)
                height = int(image.shape[0] * scale_percent / 100)
                shrunk_image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
                self.frame_number += 1
                self.push_image_to_queue(shrunk_image, self.frame_number, fps)
        except Exception as e:
            print(
                "Capture source problem, assuming camera disconnected, waiting for reconnect."
            )
            print(e)
            self.camera_status = CameraState.DISCONNECTED
            pass

    def push_image_to_queue(self, image, frame_number, fps):
        # If there's backpressure, just yell. We really shouldn't have this unless we start getting
        # some sort of capture event conflict though.
        qsize = self.camera_output_outgoing.qsize()
        if qsize > 1:
            print(
                f"CAPTURE QUEUE BACKPRESSURE OF {qsize}. CHECK FOR CRASH OR TIMING ISSUES IN ALGORITHM."
            )
        self.camera_output_outgoing.put((image, frame_number, fps))
        self.capture_event.clear()