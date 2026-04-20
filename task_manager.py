import asyncio
import time
from typing import Dict, Any, Tuple
from datetime import datetime
import cv2
from plc_manager import CameraStatus, ProductType, SystemStatus, CameraTriggerStatus, CameraResult
from image_processing import ProcessResult

class TaskManager:
    def __init__(self, plc_manager, camera_manager, image_processor, config, logger):
        self.plc_manager = plc_manager
        self.camera_manager = camera_manager
        self.image_processor = image_processor
        self.config = config
        self.logger = logger
        # 只给实际在用的相机开结果槽，单相机时不浪费合图开销
        active = camera_manager.active_camera_nums() if camera_manager else [1, 2]
        self.camera_results = {n: None for n in active} or {1: None, 2: None}
        self.logger.info(f"[SYS] camera_results 初始化: {list(self.camera_results.keys())}")

    async def run(self):
        active = self.camera_manager.active_camera_nums()
        if not active:
            self.logger.error("[SYS] 没有相机成功初始化，任务管理器不启动")
            return
        self.logger.info(f"[SYS] Task Manager started (active cameras: {active})")
        tasks = [asyncio.create_task(self.camera_task(n)) for n in active]
        await asyncio.gather(*tasks)

    async def camera_task(self, camera_num: int):
        while True:
            try:
                await self.process_camera(camera_num)
            except Exception as e:
                self.logger.error(f"[Cam{camera_num}] task loop exception: {str(e)}")
            await asyncio.sleep(0.1)  # 避免过于频繁的读取

    async def process_camera(self, camera_num: int):
        settings = await self.read_plc_settings(camera_num)
        # PLC 读失败时（网络抖动等）settings 为空，跳过本轮处理，下次再试
        if not settings:
            return
        status = settings['status']

        current_trigger_source = self.camera_manager.get_trigger_source(camera_num)

        if status == CameraStatus.START_LOOP:
            # 循环模式：强制使用软件触发
            if current_trigger_source != 7:  # 7 表示软件触发
                await self.update_camera_trigger_mode(camera_num, is_hardware_trigger=False)
            is_hardware_trigger = False
        elif status == CameraStatus.START_TASK:
            # 单次任务：根据设置决定触发模式
            is_hardware_trigger = settings.get('trigger_mode') == CameraTriggerStatus.HARDWARE_TRIGGER
            new_trigger_mode = 0 if is_hardware_trigger else 7  # 0 表示硬件触发，7 表示软件触发
            if current_trigger_source != new_trigger_mode:
                await self.update_camera_trigger_mode(camera_num, is_hardware_trigger)
        else:
            # IDLE 等状态：不改变触发模式，但仍然允许 PLC 提前下发曝光参数
            is_hardware_trigger = (current_trigger_source == 0)

        # 设置曝光时间（IDLE 时也执行，实现 PLC 参数预下发，触发时零延迟应用）
        await self._ensure_exposure(camera_num, settings, is_hardware_trigger)

        if status == CameraStatus.START_TASK:
            await self.process_single_capture(camera_num, settings, is_hardware_trigger)
        elif status == CameraStatus.START_LOOP:
            await self.process_continuous_capture(camera_num, settings, is_hardware_trigger)

    async def _ensure_exposure(self, camera_num: int, settings: Dict[str, Any], is_hardware_trigger: bool):
        new_exposure_time = settings.get('exposure_time')
        if not new_exposure_time:  # 0 或 None 都跳过
            return

        current_exposure_time = self.camera_manager.get_exposure_time(camera_num)
        # 容差比较：相机会把设置值吸附到最近合法步长，严格 != 会导致反复重设
        if current_exposure_time is not None and abs(current_exposure_time - new_exposure_time) <= 1.0:
            return

        await self.set_camera_exposure(camera_num, new_exposure_time)
        # 软触发模式下主动丢弃一帧，避免取到旧曝光的过渡帧
        if not is_hardware_trigger:
            await asyncio.to_thread(self.camera_manager.flush_one_frame, camera_num)

    async def read_plc_settings(self, camera_num: int) -> Dict[str, Any]:
        try:
            return await asyncio.to_thread(self.plc_manager.read_camera_settings, camera_num)
        except Exception as e:
            self.logger.error(f"[Cam{camera_num}] PLC read exception: {str(e)}")
            return {}

    async def process_single_capture(self, camera_num: int, settings: Dict[str, Any], is_hardware_trigger: bool):
        self.logger.info(f"[Cam{camera_num}] capture start")
        await asyncio.to_thread(self.plc_manager.write_camera_status, camera_num, CameraStatus.IDLE)

        result = await self.capture_and_process_image(camera_num, settings, is_hardware_trigger)

        self.camera_results[camera_num] = result
        # PLC 写结果和合图存盘是独立链路，并行执行省 20~30ms
        await asyncio.gather(
            self.write_result_to_plc(camera_num, result),
            self.process_combined_results(),
        )
        await asyncio.to_thread(self.plc_manager.write_camera_status, camera_num, CameraStatus.TASK_COMPLETED)
        self.logger.info(f"[Cam{camera_num}] capture done")

    async def process_continuous_capture(self, camera_num: int, settings: Dict[str, Any], is_hardware_trigger: bool):
        self.logger.info(f"[Cam{camera_num}] continuous capture start")

        loop_start = time.time()
        frame_count = 0
        last_report_time = loop_start
        last_report_frames = 0
        REPORT_INTERVAL = 2.0  # 每 2 秒汇总一次

        while True:
            try:
                t0 = time.time()

                new_settings = await self.read_plc_settings(camera_num)
                if not new_settings:
                    await asyncio.sleep(0.1)
                    continue

                if new_settings['status'] != CameraStatus.START_LOOP:
                    self.logger.info(f"[Cam{camera_num}] continuous capture stopping")
                    await asyncio.to_thread(self.plc_manager.write_camera_status, camera_num, CameraStatus.IDLE)
                    break

                t_plc = time.time()
                await self._ensure_exposure(camera_num, new_settings, is_hardware_trigger)
                t_exp = time.time()
                result = await self.capture_and_process_image(camera_num, new_settings, is_hardware_trigger)
                t_cap = time.time()

                self.camera_results[camera_num] = result
                await asyncio.gather(
                    self.write_result_to_plc(camera_num, result),
                    self.process_combined_results(),
                )
                t_out = time.time()

                frame_count += 1
                total_ms = (t_out - t0) * 1000

                # 每 2 秒打一条汇总：即时 FPS + 分段耗时 + 总帧耗时
                now = time.time()
                if now - last_report_time >= REPORT_INTERVAL:
                    fps = (frame_count - last_report_frames) / (now - last_report_time)
                    self.logger.info(
                        f"[Cam{camera_num}] ⏱ {fps:4.1f}fps frame#{frame_count:04d} | "
                        f"plc={(t_plc - t0) * 1000:3.0f}ms "
                        f"exp={(t_exp - t_plc) * 1000:3.0f}ms "
                        f"cap+algo={(t_cap - t_exp) * 1000:3.0f}ms "
                        f"write+combine={(t_out - t_cap) * 1000:3.0f}ms "
                        f"total={total_ms:3.0f}ms"
                    )
                    last_report_time = now
                    last_report_frames = frame_count

                await asyncio.sleep(0.01)

            except Exception as e:
                self.logger.error(f"[Cam{camera_num}] continuous capture exception: {str(e)}")
                await asyncio.sleep(1)

        total_run = time.time() - loop_start
        avg_fps = frame_count / total_run if total_run > 0 else 0
        self.logger.info(
            f"[Cam{camera_num}] continuous capture ended "
            f"| 跑了 {frame_count} 帧 / {total_run:.1f}s = 平均 {avg_fps:.1f} FPS"
        )

    async def capture_and_process_image(self, camera_num: int, settings: Dict[str, Any], is_hardware_trigger: bool):
        image = await asyncio.to_thread(self.camera_manager.capture_image, camera_num, is_hardware_trigger)
        if image is None:
            self.logger.error(f"[Cam{camera_num}] no image captured")
            return None

        # OpenCV 版本: image 已经是 numpy BGR 数组, 无需再转换
        camera_info = self.convert_settings_to_camera_info(settings)

        if settings['product_type'] == ProductType.LARGE_CIRCLE:
            self.logger.info(f"[Cam{camera_num}] algo: large circle")
            result = await asyncio.to_thread(self.image_processor.process_big_circle_image, image, camera_info)
        elif settings['product_type'] == ProductType.SMALL_CIRCLE:
            self.logger.info(f"[Cam{camera_num}] algo: small circle")
            result = await asyncio.to_thread(self.image_processor.process_small_circle_image, image, camera_info)
        else:
            self.logger.error(f"[Cam{camera_num}] unknown product_type: {settings.get('product_type')}")
            return None

        return result

    async def write_result_to_plc(self, camera_num: int, result: Tuple[ProcessResult, Any, Tuple[float, float], float]):
        if result is None:
            self.logger.error(f"[Cam{camera_num}] no valid result to write")
            return

        process_result, _, center, degree = result
        camera_result = CameraResult(
            x=center[0],
            y=center[1],
            angle=degree,
            result=process_result == ProcessResult.OK,
            area=0,
            circularity=0.0
        )

        try:
            await asyncio.to_thread(self.plc_manager.write_camera_result, camera_num, camera_result)
            self.logger.info(
                f"[Cam{camera_num}] result={process_result.name} "
                f"x={center[0]:.1f} y={center[1]:.1f} angle={degree:.2f}"
            )
        except Exception as e:
            self.logger.error(f"[Cam{camera_num}] write result exception: {str(e)}")

    async def update_camera_trigger_mode(self, camera_num: int, is_hardware_trigger: bool):
        try:
            await asyncio.to_thread(self.camera_manager.update_trigger_mode, camera_num, is_hardware_trigger)
        except Exception as e:
            self.logger.error(f"[Cam{camera_num}] trigger mode update exception: {str(e)}")

    async def set_camera_exposure(self, camera_num: int, exposure_time: float):
        try:
            await asyncio.to_thread(self.camera_manager.set_exposure, camera_num, exposure_time)
        except Exception as e:
            self.logger.error(f"[Cam{camera_num}] set exposure exception: {str(e)}")

    async def process_combined_results(self):
        # 合成 + 转 RGB565 + 存盘全放线程池，避免阻塞事件循环（以前
        # convert_to_rgb565 和 save_rgb565_with_header 是同步调用，
        # 会卡住另一个相机任务的曝光/触发协程）
        await asyncio.to_thread(self._combine_and_save, self.camera_results)

    def _combine_and_save(self, results):
        combined_image = self.image_processor.process_and_combine_images(results)
        rgb565_image = self.image_processor.convert_to_rgb565(combined_image)
        self.image_processor.save_rgb565_with_header(rgb565_image, 'output_image.rgb565')
        # 保存合并后的图像
        # timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # output_filename = f'combined_output_{timestamp}.jpg'
        # await asyncio.to_thread(cv2.imwrite, output_filename, combined_image)
        # self.logger.info(f"Saved combined result image as {output_filename}")
        #
        # # 记录处理的摄像头情况
        # total_cameras = len(self.camera_results)
        # valid_cameras = sum(1 for result in self.camera_results.values() if result is not None)
        # self.logger.info(f"Processed images: {valid_cameras} valid out of {total_cameras} total cameras")

        # 重置结果
        # self.camera_results = {1: None, 2: None}

    def convert_settings_to_camera_info(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        camera_info = {
            'roi': [
                settings.get('roi_x', 0),
                settings.get('roi_y', 0),
                settings.get('roi_diameter', 0)
            ],
            'params': [
                settings.get('gray_lower', 0),
                settings.get('gray_upper', 255),
                settings.get('area_lower', 0),
                settings.get('area_upper', 0),
                settings.get('circularity_lower', 0),
                settings.get('circularity_upper', 1)
            ],
            'pixel_distance': settings.get('pixel_distance', 1)  # 添加pixel_distance
        }
        return camera_info

    async def update_system_status(self, status: SystemStatus):
        try:
            await asyncio.to_thread(self.plc_manager.write_system_status, status.value)
            self.logger.info(f"System status updated to {status.name}")
        except Exception as e:
            self.logger.error(f"Error updating system status: {str(e)}")

    async def handle_error(self, error_code: int):
        try:
            await asyncio.to_thread(self.plc_manager.write_error_code, error_code)
            await self.update_system_status(SystemStatus.ERROR)
            self.logger.error(f"System error occurred. Error code: {error_code}")
        except Exception as e:
            self.logger.error(f"Error handling system error: {str(e)}")

    async def apply_camera_settings(self, camera_num: int, settings: Dict[str, Any], is_hardware_trigger: bool = False):
        # 向后兼容入口，复用统一的曝光处理逻辑
        await self._ensure_exposure(camera_num, settings, is_hardware_trigger)