import cv2

import numpy as np
import log_config
logger = log_config.setup_logging()
from camera_environment import setup_camera_environment

setup_camera_environment()  # 确保在导入 MvCameraControl_class 之前调用

from MvCameraControl_class import *
import time
class CameraBase:
    def __init__(self, device_ip, net_ip, camera_num=None, roi=None):
        """roi = {'width', 'height', 'offset_x', 'offset_y'} 或 None.
        设置后相机只传该区域, 数据量下降, cap+algo 显著加速.
        注意: 算法层 ROI (D25-27 / D45-47) 的坐标变成相对这块小图.
        """
        self.device_ip = device_ip
        self.net_ip = net_ip
        self.camera_num = camera_num
        self.roi = roi
        self.cam = None
        self.nPayloadSize = 0

    @property
    def _tag(self):
        return f"[Cam{self.camera_num}]" if self.camera_num else f"[{self.device_ip}]"

    def init_camera(self):
        try:
            self.cam = MvCamera()
            if not self._create_and_open_device():
                logger.error(f"{self._tag} failed to create/open device ({self.device_ip})")
                return False
            self._set_common_parameters()

            logger.info(f"{self._tag} initialized ({self.device_ip})")
            return True
        except Exception as e:
            logger.error(f"{self._tag} init error: {e}")
            self.close_camera()
            return False

    def _create_and_open_device(self):
        stDevInfo = self._create_device_info()
        if not self._create_handle(stDevInfo):
            return False
        if not self._open_device():
            return False
        return True

    def _create_device_info(self):
        stDevInfo = MV_CC_DEVICE_INFO()
        stGigEDev = MV_GIGE_DEVICE_INFO()
        deviceIpList = self.device_ip.split('.')
        stGigEDev.nCurrentIp = (int(deviceIpList[0]) << 24) | (int(deviceIpList[1]) << 16) | (
                    int(deviceIpList[2]) << 8) | int(deviceIpList[3])
        netIpList = self.net_ip.split('.')
        stGigEDev.nNetExport = (int(netIpList[0]) << 24) | (int(netIpList[1]) << 16) | (
                    int(netIpList[2]) << 8) | int(netIpList[3])
        stDevInfo.nTLayerType = MV_GIGE_DEVICE
        stDevInfo.SpecialInfo.stGigEInfo = stGigEDev
        return stDevInfo

    def _create_handle(self, stDevInfo):
        ret = self.cam.MV_CC_CreateHandle(stDevInfo)
        if ret != 0:
            logger.error(f"Create handle fail! ret[0x{ret:x}]")
            return False
        return True

    def _open_device(self):
        ret = self.cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
        if ret != 0:
            logger.error(f"{self._tag} open device failed, ret=0x{ret:x}")
            return False
        logger.info(f"{self._tag} opened ({self.device_ip})")
        return True

    def _set_common_parameters(self):
        self.cam.MV_CC_SetEnumValue("AcquisitionMode", 2)
        self._set_packet_size()
        self._apply_roi()       # ROI 必须在 GetPayloadSize 之前, 否则缓冲区按全帧算
        self._get_payload_size()

    def _apply_roi(self):
        """根据 self.roi 设置相机硬件 ROI. 未配置则跳过, 走全帧默认.

        顺序很重要: 先把 offset 归零 -> 设 width/height -> 再设 offset.
        否则如果新 size + 旧 offset 超出最大分辨率, 相机会拒绝.
        """
        if not self.roi:
            return True

        w, h = self.roi['width'], self.roi['height']
        ox, oy = self.roi.get('offset_x', 0), self.roi.get('offset_y', 0)

        # 1. offset 先清零
        self.cam.MV_CC_SetIntValue("OffsetX", 0)
        self.cam.MV_CC_SetIntValue("OffsetY", 0)

        # 2. 设 Width / Height (注意: 海康要求多为 4 的倍数)
        ret = self.cam.MV_CC_SetIntValue("Width", w)
        if ret != 0:
            logger.error(f"{self._tag} set Width={w} failed, ret=0x{ret:x} (必须 4/8 倍数?)")
            return False

        ret = self.cam.MV_CC_SetIntValue("Height", h)
        if ret != 0:
            logger.error(f"{self._tag} set Height={h} failed, ret=0x{ret:x}")
            return False

        # 3. 设 Offset
        if ox:
            ret = self.cam.MV_CC_SetIntValue("OffsetX", ox)
            if ret != 0:
                logger.warning(f"{self._tag} set OffsetX={ox} failed, ret=0x{ret:x}")
        if oy:
            ret = self.cam.MV_CC_SetIntValue("OffsetY", oy)
            if ret != 0:
                logger.warning(f"{self._tag} set OffsetY={oy} failed, ret=0x{ret:x}")

        logger.info(f"{self._tag} ROI applied: {w}x{h} @ ({ox},{oy})")
        return True

    def _set_packet_size(self):
        nPacketSize = self.cam.MV_CC_GetOptimalPacketSize()
        if int(nPacketSize) > 0:
            ret = self.cam.MV_CC_SetIntValue("GevSCPSPacketSize", nPacketSize)
            if ret != 0:
                logger.warning(f"Warning: Set Packet Size fail! ret[0x{ret:x}]")
        else:
            logger.warning(f"Warning: Get Packet Size fail! ret[0x{nPacketSize:x}]")

    def _get_payload_size(self):
        stParam = MVCC_INTVALUE()
        memset(byref(stParam), 0, sizeof(MVCC_INTVALUE))
        ret = self.cam.MV_CC_GetIntValue("PayloadSize", stParam)
        if ret != 0:
            logger.error(f"Get payload size fail! ret[0x{ret:x}]")
            return False
        self.nPayloadSize = stParam.nCurValue
        return True

    def update_trigger_mode(self, is_hardware_trigger):
        # 设置触发模式为开启
        ret = self.cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_ON)
        if ret != 0:
            logger.error(f"Set trigger mode fail! ret[0x{ret:x}]")
            return False

        if is_hardware_trigger:
            # 硬件触发的配置
            ret = self.cam.MV_CC_SetEnumValue("TriggerSource", 0)
            if ret != 0:
                logger.error(f"Set trigger source fail! ret[0x{ret:x}]")
                return False

            ret = self.cam.MV_CC_SetEnumValue("LineSelector", 0)
            if ret != 0:
                logger.error(f"Set line selector fail! ret[0x{ret:x}]")
                return False

            ret = self.cam.MV_CC_SetEnumValue("TriggerActivation", 1)
            if ret != 0:
                logger.error(f"Set trigger activation fail! ret[0x{ret:x}]")
                return False

            ret = self.cam.MV_CC_SetIntValue("LineDebouncerTime", 20)
            if ret != 0:
                logger.error(f"Set line debouncer time fail! ret[0x{ret:x}]")
                return False

        else:
            # 软件触发的配置
            ret = self.cam.MV_CC_SetEnumValue("TriggerSource", MV_TRIGGER_SOURCE_SOFTWARE)
            if ret != 0:
                logger.error(f"Set trigger source fail! ret[0x{ret:x}]")
                return False

        mode = 'hardware' if is_hardware_trigger else 'software'
        logger.info(f"{self._tag} trigger mode → {mode}")
        return True

    def start_grabbing(self):
        ret = self.cam.MV_CC_StartGrabbing()
        if ret != 0:
            logger.error(f"{self._tag} start grabbing failed, ret=0x{ret:x}")
            return False
        logger.info(f"{self._tag} grabbing started")
        return True

    def stop_grabbing(self):
        ret = self.cam.MV_CC_StopGrabbing()
        if ret != 0:
            logger.error(f"{self._tag} stop grabbing failed, ret=0x{ret:x}")
            return False
        logger.info(f"{self._tag} grabbing stopped")
        return True

    def capture_image(self, is_hardware_trigger, max_retries=3):
        attempts = 1 if is_hardware_trigger else max_retries

        for attempt in range(attempts):
            if not is_hardware_trigger:
                ret = self.cam.MV_CC_SetCommandValue("TriggerSoftware")
                if ret != 0:
                    logger.error(f"{self._tag} software trigger failed, ret=0x{ret:x}")
                    if attempts > 1:
                        time.sleep(1)
                    continue

            stOutFrame = MV_FRAME_OUT_INFO_EX()
            memset(byref(stOutFrame), 0, sizeof(stOutFrame))
            data_buf = (c_ubyte * self.nPayloadSize)()
            ret = self.cam.MV_CC_GetOneFrameTimeout(byref(data_buf), self.nPayloadSize, stOutFrame, 1000)
            if ret == 0:
                return self.convert_and_save_image(stOutFrame, data_buf)
            else:
                logger.error(
                    f"{self._tag} get frame failed, ret=0x{ret:x} "
                    f"(attempt {attempt + 1}/{attempts})"
                )

            if attempt < attempts - 1:
                time.sleep(1)

        logger.error(f"{self._tag} capture failed after {attempts} attempt(s)")
        return None

    def convert_and_save_image(self, stOutFrame, data_buf):
        nRGBSize = stOutFrame.nWidth * stOutFrame.nHeight * 3
        stConvertParam = MV_CC_PIXEL_CONVERT_PARAM()
        memset(byref(stConvertParam), 0, sizeof(stConvertParam))
        stConvertParam.nWidth = stOutFrame.nWidth
        stConvertParam.nHeight = stOutFrame.nHeight
        stConvertParam.pSrcData = data_buf
        stConvertParam.nSrcDataLen = stOutFrame.nFrameLen
        stConvertParam.enSrcPixelType = stOutFrame.enPixelType
        stConvertParam.enDstPixelType = PixelType_Gvsp_RGB8_Packed
        stConvertParam.pDstBuffer = (c_ubyte * nRGBSize)()
        stConvertParam.nDstBufferSize = nRGBSize
        ret = self.cam.MV_CC_ConvertPixelType(stConvertParam)
        if ret != 0:
            logger.error(f"Convert pixel type fail! ret[0x{ret:x}]")
            return None

        img_buff = (c_ubyte * nRGBSize)()
        memmove(byref(img_buff), stConvertParam.pDstBuffer, nRGBSize)
        img = np.frombuffer(img_buff, dtype=np.uint8).reshape(stConvertParam.nHeight, stConvertParam.nWidth, 3)
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        return img

    def write_exposure_time(self, exposure_value):
        ret = self.cam.MV_CC_SetFloatValue("ExposureTime", float(exposure_value))
        if ret != 0:
            logger.error(f"{self._tag} set exposure failed, ret=0x{ret:x}")
            return False

        time.sleep(0.1)

        target = float(exposure_value)
        actual = None
        for _ in range(5):
            actual = self.get_exposure_time()
            if actual is not None and abs(actual - target) <= 1.0:
                break
            time.sleep(0.05)
        else:
            logger.warning(
                f"{self._tag} exposure readback mismatch: expected {target}, got {actual}"
            )

        try:
            clear_ret = self.cam.MV_CC_ClearImageBuffer()
            if clear_ret != 0:
                logger.warning(f"{self._tag} clear buffer ret=0x{clear_ret:x}")
        except Exception as e:
            logger.warning(f"{self._tag} clear buffer unsupported: {e}")

        logger.info(f"{self._tag} exposure → {int(exposure_value)}us")
        return True

    def flush_one_frame(self, timeout_ms=1000):
        """软触发模式下：主动触发一帧并丢弃，确保下一帧使用最新参数。"""
        ret = self.cam.MV_CC_SetCommandValue("TriggerSoftware")
        if ret != 0:
            logger.warning(f"{self._tag} flush trigger failed, ret=0x{ret:x}")
            return False

        stOutFrame = MV_FRAME_OUT_INFO_EX()
        memset(byref(stOutFrame), 0, sizeof(stOutFrame))
        data_buf = (c_ubyte * self.nPayloadSize)()
        ret = self.cam.MV_CC_GetOneFrameTimeout(
            byref(data_buf), self.nPayloadSize, stOutFrame, timeout_ms
        )
        if ret != 0:
            logger.warning(f"{self._tag} flush frame timeout, ret=0x{ret:x}")
            return False

        logger.debug(f"{self._tag} discarded 1 stale frame")
        return True

    def close_camera(self):
        self.stop_grabbing()
        self.cam.MV_CC_CloseDevice()
        self.cam.MV_CC_DestroyHandle()
        logger.info(f"{self._tag} closed")

    def reinitialize_camera(self):
        logger.info(f"{self._tag} reinitializing")
        self.close_camera()
        if self.init_camera():
            logger.info(f"{self._tag} reinitialized")
            return True
        else:
            logger.error(f"{self._tag} reinitialize failed")
            return False

    def read_enum_value(self, strKey):
        enum_value = MVCC_ENUMVALUE()
        try:
            ret = self.cam.MV_CC_GetEnumValue(strKey, enum_value)
            if ret == 0:
                return enum_value.nCurValue
            else:
                logger.error(f"Failed to read enum value from IP {self.device_ip}: Error code {ret}")
                return None
        except Exception as e:
            logger.error(f"Exception occurred while reading enum value for IP {self.device_ip}: {e}")
            return None

    def get_float_value(self, strKey):
        float_value = MVCC_FLOATVALUE()
        try:
            ret = self.cam.MV_CC_GetFloatValue(strKey, float_value)
            if ret == 0:
                return float_value.fCurValue
            else:
                logger.error(f"Failed to read float value for key '{strKey}' from IP {self.device_ip}: Error code {ret}")
                return None
        except Exception as e:
            logger.error(f"Exception occurred while reading float value for key '{strKey}' from IP {self.device_ip}: {e}")
            return None

    def get_trigger_source(self):
        return self.read_enum_value("TriggerSource")

    def get_exposure_time(self):
        return self.get_float_value("ExposureTime")