"""
------------------------------------------------------------------------------------------------------

                                               ,@@@@@@
                                            @@@@@@@@@@@            @@@
                                          @@@@@@@@@@@@      @@@@@@@@@@@
                                        @@@@@@@@@@@@@   @@@@@@@@@@@@@@
                                      @@@@@@@/         ,@@@@@@@@@@@@@
                                         /@@@@@@@@@@@@@@@  @@@@@@@@
                                    @@@@@@@@@@@@@@@@@@@@@@@@ @@@@@
                                @@@@@@@@                @@@@@
                              ,@@@                        @@@@&
                                             @@@@@@.       @@@@
                                   @@@     @@@@@@@@@/      @@@@@
                                   ,@@@.     @@@@@@((@     @@@@(
                                   //@@@        ,,  @@@@  @@@@@
                                   @@@(                @@@@@@@
                                   @@@  @          @@@@@@@@#
                                       @@@@@@@@@@@@@@@@@
                                      @@@@@@@@@@@@@(

Ellipse Based Pupil Dilation By: Prohurtz, PallasNeko (Optimization)
Algorithm App Implementations By: Prohurtz

Copyright (c) 2023 EyeTrackVR <3
LICENSE: Babble Software Distribution License 1.0
------------------------------------------------------------------------------------------------------
"""
import numpy
import numpy as np
import time
import os
import cv2

from eye import EyeId
from one_euro_filter import OneEuroFilter

os.environ["OMP_NUM_THREADS"] = "1"

# Note.
# OpenCV on Windows will generate an error if the file path contains non-ASCII characters when using cv2.imread(), cv2.imwrite(), etc.
# https://stackoverflow.com/questions/43185605/how-do-i-read-an-image-from-a-path-with-unicode-characters
# https://github.com/opencv/opencv/issues/18305


def csv2data(frameshape, filepath):
    # For data checking
    frameshape = (frameshape[0], frameshape[1] + 1)
    out = np.zeros(frameshape, dtype=np.uint32)
    xy_list = []
    val_list = []
    with open(filepath, mode="r", encoding="utf-8") as in_f:
        # Skip header.
        _ = in_f.readline()
        for s in in_f:
            xyval = [int(val) for val in s.strip().split(",")]
            xy_list.append((xyval[0], xyval[1]))
            val_list.append(xyval[2])
    xy_list = np.array(xy_list)
    val_list = np.array(val_list)
    out[xy_list[:, 1], xy_list[:, 0]] = val_list[:]
    return out


def data2csv(data_u32, filepath):
    # For data checking
    nonzero_index = np.nonzero(data_u32)  # (row,col)
    data_list = data_u32[nonzero_index].tolist()
    datalines = ["{},{},{}\n".format(x, y, val) for y, x, val in zip(*nonzero_index, data_list)]
    with open(filepath, "w", encoding="utf-8") as out_f:
        out_f.write("x,y,eyedilation\n")
        out_f.writelines(datalines)
    return


def u32_1ch_to_u16_3ch(img):
    out = np.zeros((*img.shape[:2], 3), dtype=np.uint16)
    # https://github.com/numpy/numpy/issues/2524
    # https://stackoverflow.com/questions/52782511/why-is-numpy-slower-than-python-for-left-bit-shifts
    out[:, :, 0] = img & np.uint32(65535)
    out[:, :, 1] = (img >> np.uint32(16)) & np.uint32(65535)

    return out


def u16_3ch_to_u32_1ch(img):
    # The image format with the most bits that can be displayed on Windows without additional software and that opencv can handle is PNG's uint16
    out = img[:, :, 0].astype(np.float64)  # float64 = max 2^53
    cv2.add(out, img[:, :, 1].astype(np.float64) * np.float64(65536), dst=out)  # opencv did not have uint32 type
    return out.astype(np.uint32)  # cast


def newdata(frameshape):
    print("\033[94m[INFO] Initialise data for dilation.\033[0m")
    return np.zeros(frameshape, dtype=np.uint32)


# EBPD
class EllipseBasedPupilDilation:
    def __init__(self, eye_id):
        # todo: It is necessary to consider whether the filename can be changed in the configuration file, etc.
        if eye_id in [EyeId.LEFT]:
            self.imgfile = "EBPD_LEFT.png"
        else:
            pass
        if eye_id in [EyeId.RIGHT]:
            self.imgfile = "EBPD_RIGHT.png"
        else:
            pass
        # self.data[0, -1] = maxval, [1, -1] = rotation, [2, -1] = x, [3, -1] = y
        self.data = None
        self.lct = None
        self.maxval = 0
        # self.img_roi = self.now_roi == {"rotation": 0, "x": 0, "y": 0}
        self.img_roi = np.zeros(3, dtype=np.int32)
        self.now_roi = np.zeros(3, dtype=np.int32)
        self.prev_val = 0.5
        self.avg_dilation = 0.0
        self.old = []
        self.color = []
        self.x = []
        self.fc = 0
        self.filterlist = []
        self.averageList = []
        self.openlist = []
        self.eye_id = eye_id
        self.maxinten = 0
        self.tri_filter = []
        min_cutoff = 0.00001
        beta = 0.05
        noisy_point = np.array([1, 1])
        self.one_euro_filter = OneEuroFilter(noisy_point, min_cutoff=min_cutoff, beta=beta)

    def check(self, frameshape):
        # 0 in data is used as the initial value.
        # When assigning a value, +1 is added to the value to be assigned.
        self.load(frameshape)
        # self.maxval = self.data[0, -1]
        if self.lct is None:
            self.lct = time.time()

    def load(self, frameshape):
        req_newdata = False
        # Not very clever, but increase the width by 1px to save the maximum value.
        frameshape = (frameshape[0], frameshape[1] + 1)
        if self.data is None:
            print(f"\033[92m[INFO] Loaded data for pupil dilation: {self.imgfile}\033[0m")
            if os.path.isfile(self.imgfile):
                try:
                    img = cv2.imread(self.imgfile, flags=cv2.IMREAD_UNCHANGED)
                    # check code: cv2.absdiff(img,u32_1ch_to_u16_3ch(u16_3ch_to_u32_1ch(img)))
                    if img.shape[:2] != frameshape:
                        print("[WARN] Size does not match the input frame.")
                        req_newdata = True
                    else:
                        self.data = u16_3ch_to_u32_1ch(img)
                        self.img_roi[:] = self.data[1:4, -1]
                        if not np.array_equal(self.img_roi, self.now_roi):
                            # If the ROI recorded in the image file differs from the current ROI
                            req_newdata = True
                        else:
                            self.maxval = self.data[0, -1]
                except:
                    print("[ERROR] File read error: {}".format(self.imgfile))
                    req_newdata = True
            else:
                print("\033[94m[INFO] File does not exist.\033[0m")
                req_newdata = True
        else:
            if self.data.shape != frameshape or not np.array_equal(self.img_roi, self.now_roi):
                # If the ROI recorded in the image file differs from the current ROI
                # todo: Using the previous and current frame sizes and centre positions from the original, etc., the data can be ported to some extent, but there may be many areas where code changes are required.
                print("[INFO] \033[94mFrame size changed.\033[0m")
                req_newdata = True
        if req_newdata:
            self.data = newdata(frameshape)
            self.maxval = 0
            self.img_roi = self.now_roi.copy()
        # data2csv(self.data, "a.csv")
        # csv2data(frameshape,"a.csv")

    def save(self):
        self.data[0, -1] = self.maxval
        self.data[1:4, -1] = self.now_roi
        cv2.imwrite(self.imgfile, u32_1ch_to_u16_3ch(self.data))
        # print("SAVED: {}".format(self.imgfile))

    def change_roi(self, roiinfo: dict):
        self.now_roi[:] = [v for v in roiinfo.values()]

    def clear_filter(self):
        self.data = None
        self.filterlist.clear()
        self.averageList.clear()
        if os.path.exists(self.imgfile):
            os.remove(self.imgfile)

    def intense(self, w, h, x, y, frame, filterSamples, outputSamples):
        # x,y = 0~(frame.shape[1 or 0]-1), frame = 1-channel frame cropped by ROI
        self.check(frame.shape)
        int_x, int_y = int(x), int(y)
        if int_x < 0 or int_y < 0:
            return self.prev_val
        upper_x = min(
            int_x + 25, frame.shape[1] - 1
        )  # TODO make this a setting NEEDS TO BE BASED ON HSF RADIUS if possible
        lower_x = max(int_x - 25, 0)
        upper_y = min(int_y + 25, frame.shape[0] - 1)
        lower_y = max(int_y - 25, 0)

        # The same can be done with cv2.integral, but since there is only one area of the rectangle for which we want to know the total value, there is no advantage in terms of computational complexity.
        pupil_area = numpy.pi * (w / 2) * (h / 2)

        if len(self.filterlist) < filterSamples:
            self.filterlist.append(pupil_area)
        else:
            self.filterlist.pop(0)
            self.filterlist.append(pupil_area)

        try:
            if pupil_area >= np.percentile(self.filterlist, 99):  # filter abnormally high values
                # print('filter, assume blink')
                pupil_area = self.maxval

        except:
            pass

        newval_flg = False
        oob = False

        if int_x >= frame.shape[1]:
            int_x = frame.shape[1] - 1
            oob = True

        if int_x < 0:
            int_x = True
            oob = True

        if int_y >= frame.shape[0]:
            int_y = frame.shape[0] - 1
            oob = True

        if int_y < 0:
            int_y = 1
            oob = True

        if oob != True and self.data.any():
            data_val = self.data[int_y, int_x]
        else:
            data_val = 0

        # max pupil per cord
        if data_val == 0:
            # The value of the specified coordinates has not yet been recorded.
            self.data[int_y, int_x] = pupil_area
            changed = True
            newval_flg = True
        else:
            if pupil_area < data_val:  # if current intensity value is less (more pupil), save that
                self.data[int_y, int_x] = pupil_area  # set value
                changed = True
            else:
                pupil_areaa = max(
                    data_val + 5000, 1
                )  # if current intensity value is not less use  this is an agressive adjust, test
                self.data[int_y, int_x] = pupil_areaa  # set value
                changed = True

        # min pupil global
        if self.maxval == 0:  # that value is not yet saved
            self.maxval = pupil_area  # set value at 0 index
        else:
            if pupil_area > self.maxval:  # if current intensity value is more (less pupil), save that NOTE: we have the
                self.maxval = pupil_area - 5  # set value at 0 index
            else:
                pupil_aread = max(
                    (self.maxval - 5), 1
                )  # continuously adjust closed intensity, will be set when user blink, used to allow eyes to close when lighting changes
                self.maxval = pupil_aread  # set value at 0 index

        if newval_flg:
            # Do the same thing as in the original version.
            eyedilation = self.prev_val  # 0.9
        else:
            maxp = float(self.data[int_y, int_x])
            minp = float(self.maxval)

            try:
                if not np.isfinite(pupil_area) or not np.isfinite(maxp) or not np.isfinite(minp) or (minp - maxp) == 0:
                    eyedilation = 0.5
                else:
                    eyedilation = (pupil_area - maxp) / (minp - maxp)

            except:
                eyedilation = 0.5
            eyedilation = 1 - eyedilation

            if outputSamples > 0:
                if len(self.averageList) < outputSamples:
                    self.averageList.append(eyedilation)
                else:
                    self.averageList.pop(0)
                    self.averageList.append(eyedilation)
                    eyedilation = np.average(self.averageList)

            if eyedilation > 1:  # clamp values
                eyedilation = 1.0

            if eyedilation < 0:
                eyedilation = 0.0

        if changed and ((time.time() - self.lct) > 15):  # save every 5 seconds if something changed to save disk usage
            self.save()
            self.lct = time.time()

        self.prev_val = eyedilation
        try:
            noisy_point = np.array([float(eyedilation), float(eyedilation)])  # fliter our values with a One Euro Filter
            point_hat = self.one_euro_filter(noisy_point)
            eyedilationx = point_hat[0]
            eyedilationy = point_hat[1]
            eyedilation = (eyedilationx + eyedilationy) / 2

        except:
            pass

        return eyedilation
