import asyncio

from config_manager import config
from camera_base import CameraBase
import log_config
import threading

logger = log_config.setup_logging()

class CameraManager:
    def __init__(self):
        self.cameras = {}
        self.camera_locks = {}
        self._initialize_cameras()

    def _initialize_cameras(self):
        for i in range(1, 3):  # 假设我们有两个相机
            self.camera_locks[i] = threading.Lock()
            if not config.is_camera_enabled(i):
                logger.info(f"[Cam{i}] disabled in config.json, skipping")
                continue
            device_ip = config.get_camera_ip(i)
            net_ip = config.get_camera_host_lan(i)
            roi = config.get_camera_roi(i)   # None 或 dict
            try:
                camera = CameraBase(device_ip, net_ip, camera_num=i, roi=roi)
                if camera.init_camera():
                    self.cameras[i] = camera
                    self.start_grabbing(i)
                else:
                    logger.error(f"[Cam{i}] init failed (未连接? IP 错? 被别的程序占用?)")
            except Exception as e:
                logger.error(f"[Cam{i}] init exception: {str(e)}")

    def active_camera_nums(self):
        """返回实际初始化成功的相机编号列表"""
        return sorted(self.cameras.keys())

    def get_camera(self, camera_num):
        return self.cameras.get(camera_num)

    def get_camera_info(self, camera_num):
        camera = self.get_camera(camera_num)
        if camera:
            return {
                "device_ip": camera.device_ip,
                "net_ip": camera.net_ip
            }
        return None   
    def capture_image(self, camera_num, is_hardware_trigger=False, max_retries=3):
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return None

        with self.camera_locks[camera_num]:
            try:
                image = camera.capture_image(is_hardware_trigger=is_hardware_trigger, max_retries=max_retries)
                if image is None:
                    logger.error(f"[Cam{camera_num}] capture returned no image")
                return image
            except Exception as e:
                logger.error(f"[Cam{camera_num}] capture exception: {str(e)}")
                return None

    def set_exposure(self, camera_num, exposure_time):
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False

        with self.camera_locks[camera_num]:
            try:
                return camera.write_exposure_time(exposure_time)
            except Exception as e:
                logger.error(f"[Cam{camera_num}] set exposure exception: {str(e)}")
                return False

    def flush_one_frame(self, camera_num):
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False

        with self.camera_locks[camera_num]:
            try:
                return camera.flush_one_frame()
            except Exception as e:
                logger.error(f"[Cam{camera_num}] flush exception: {str(e)}")
                return False

    def start_grabbing(self, camera_num):
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False
        try:
            return camera.start_grabbing()
        except Exception as e:
            logger.error(f"[Cam{camera_num}] start grabbing exception: {str(e)}")
            return False

    def stop_grabbing(self, camera_num):
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False
        with self.camera_locks[camera_num]:
            try:
                return camera.stop_grabbing()
            except Exception as e:
                logger.error(f"[Cam{camera_num}] stop grabbing exception: {str(e)}")
                return False

    def update_trigger_mode(self, camera_num, is_hardware_trigger):
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return False
        with self.camera_locks[camera_num]:
            try:
                return camera.update_trigger_mode(is_hardware_trigger)
            except Exception as e:
                logger.error(f"[Cam{camera_num}] trigger mode exception: {str(e)}")
                return False

    def reinitialize_camera(self, camera_num):
        try:
            self.cameras.pop(camera_num, None)
            camera_ip = config.get_camera_ip(camera_num)
            host_lan = config.get_camera_host_lan(camera_num)
            new_camera = CameraBase(camera_ip, host_lan, camera_num=camera_num)
            if new_camera.init_camera():
                self.cameras[camera_num] = new_camera
                self.start_grabbing(camera_num)
                return True
            else:
                logger.error(f"[Cam{camera_num}] reinitialize failed")
                return False
        except Exception as e:
            logger.error(f"[Cam{camera_num}] reinitialize exception: {str(e)}")
            return False

    def close_all_cameras(self):
        for camera_num, camera in self.cameras.items():
            with self.camera_locks[camera_num]:
                try:
                    camera.close_camera()
                except Exception as e:
                    logger.error(f"[Cam{camera_num}] close exception: {str(e)}")
        self.cameras.clear()
        self.camera_locks.clear()

    def get_trigger_source(self, camera_num):
        camera = self.get_camera(camera_num)
        if camera is None:
            logger.error(f"[Cam{camera_num}] not found")
            return None
        with self.camera_locks[camera_num]:
            try:
                trigger_source = camera.get_trigger_source()
                if trigger_source is None:
                    logger.error(f"[Cam{camera_num}] get trigger source failed")
                return trigger_source
            except Exception as e:
                logger.error(f"[Cam{camera_num}] get trigger source exception: {str(e)}")
                return None

    def get_exposure_time(self, camera_num):
        with self.camera_locks[camera_num]:
            camera = self.get_camera(camera_num)

            # 如果相机未找到，尝试重新初始化
            if camera is None:
                logger.warning(f"[Cam{camera_num}] not found, attempting reinit")
                if not self.reinitialize_camera(camera_num):
                    return None
                camera = self.get_camera(camera_num)

            # 尝试获取曝光时间
            try:
                exposure_time = camera.get_exposure_time()
                if exposure_time is not None:
                    return exposure_time
            except Exception as e:
                logger.warning(f"[Cam{camera_num}] read exposure exception: {str(e)}")

            # 获取失败，尝试重新初始化
            if camera is not None:
                logger.warning(f"[Cam{camera_num}] read exposure failed, attempting reinit")
                if self.reinitialize_camera(camera_num):
                    camera = self.get_camera(camera_num)
                    try:
                        exposure_time = camera.get_exposure_time()
                        if exposure_time is not None:
                            return exposure_time
                    except Exception as e:
                        logger.error(f"[Cam{camera_num}] read exposure after reinit failed: {str(e)}")

            logger.error(f"[Cam{camera_num}] read exposure gave up")
            return None