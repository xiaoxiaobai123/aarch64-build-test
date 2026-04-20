"""集成测试: task_manager.TaskManager

这里"集成"的意思是: 不真连相机/PLC, 但把 TaskManager 和它的依赖对象
(camera_manager / plc_manager / image_processor) 用 MagicMock 串起来,
验证流程编排正确 —— 调用顺序、状态转移、分支条件.

典型覆盖:
  - run() 在没相机时早退
  - process_camera 根据 CameraStatus 路由到正确的处理分支
  - 曝光容差比较 (_ensure_exposure)
  - START_TASK → 写 IDLE → 执行 → 写 TASK_COMPLETED 的状态序列
  - 产品类型路由到大圆/小圆算法
  - PLC 读失败跳过本轮
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from image_processing import ProcessResult
from plc_manager import (
    CameraResult,
    CameraStatus,
    CameraTriggerStatus,
    ProductType,
)
from task_manager import TaskManager


@pytest.fixture
def make_tm():
    """工厂: 给 TaskManager 注入可控的依赖."""
    def _make(active_cameras=(1, 2), trigger_source=0, exposure_time=None):
        camera_manager = MagicMock()
        camera_manager.active_camera_nums.return_value = list(active_cameras)
        camera_manager.get_trigger_source.return_value = trigger_source
        camera_manager.get_exposure_time.return_value = exposure_time
        camera_manager.capture_image.return_value = np.zeros((1024, 1280, 3), dtype=np.uint8)
        camera_manager.flush_one_frame.return_value = None

        plc_manager = MagicMock()
        plc_manager.read_camera_settings.return_value = {}  # 默认空
        plc_manager.write_camera_status.return_value = None
        plc_manager.write_camera_result.return_value = None

        image_processor = MagicMock()
        image_processor.process_big_circle_image.return_value = (
            ProcessResult.OK, np.zeros((10, 10, 3), dtype=np.uint8), (100.0, 50.0), 30.0,
        )
        image_processor.process_small_circle_image.return_value = (
            ProcessResult.OK, np.zeros((10, 10, 3), dtype=np.uint8), (100.0, 50.0), 0,
        )
        image_processor.process_and_combine_images.return_value = np.zeros((1024, 1280, 3), dtype=np.uint8)
        image_processor.convert_to_rgb565.return_value = np.zeros((1024, 1280, 2), dtype=np.uint8)
        image_processor.save_rgb565_with_header.return_value = None

        tm = TaskManager(plc_manager, camera_manager, image_processor, config=MagicMock(), logger=MagicMock())
        return tm, plc_manager, camera_manager, image_processor

    return _make


# =====================================================================
# 1. run() 入口
# =====================================================================
class TestRun:
    @pytest.mark.asyncio
    async def test_returns_immediately_when_no_cameras(self, make_tm):
        tm, plc, cam, _ = make_tm(active_cameras=())
        await tm.run()
        cam.active_camera_nums.assert_called()
        # 既然没相机, 不会进 camera_task, plc 也没被碰
        plc.read_camera_settings.assert_not_called()

    def test_camera_results_initialized_to_active_cameras(self, make_tm):
        tm, *_ = make_tm(active_cameras=(1,))
        assert list(tm.camera_results.keys()) == [1]

    def test_empty_active_fallback_to_both(self, make_tm):
        # active_camera_nums 返回空列表时, camera_results 回退到 {1:None, 2:None}
        tm, *_ = make_tm(active_cameras=())
        assert set(tm.camera_results.keys()) == {1, 2}


# =====================================================================
# 2. PLC 读失败 / IDLE
# =====================================================================
class TestProcessCamera:
    @pytest.mark.asyncio
    async def test_empty_settings_skips_this_round(self, make_tm):
        tm, plc, cam, img = make_tm()
        plc.read_camera_settings.return_value = {}  # PLC 挂了

        await tm.process_camera(1)

        # 没相机操作被触发
        cam.capture_image.assert_not_called()
        img.process_big_circle_image.assert_not_called()
        plc.write_camera_result.assert_not_called()

    @pytest.mark.asyncio
    async def test_idle_does_not_capture(self, make_tm):
        tm, plc, cam, img = make_tm()
        plc.read_camera_settings.return_value = {
            'status': CameraStatus.IDLE,
            'trigger_mode': CameraTriggerStatus.HARDWARE_TRIGGER,
            'exposure_time': 0,
            'product_type': ProductType.LARGE_CIRCLE,
        }

        await tm.process_camera(1)

        cam.capture_image.assert_not_called()
        img.process_big_circle_image.assert_not_called()


# =====================================================================
# 3. START_TASK 主流程
# =====================================================================
class TestStartTask:
    @pytest.mark.asyncio
    async def test_large_circle_full_flow(self, make_tm):
        tm, plc, cam, img = make_tm(trigger_source=7, exposure_time=5000)
        plc.read_camera_settings.return_value = {
            'status': CameraStatus.START_TASK,
            'trigger_mode': CameraTriggerStatus.SOFTWARE_TRIGGER,
            'exposure_time': 5000,         # 和当前相同 → 不重设
            'product_type': ProductType.LARGE_CIRCLE,
            'roi_x': 640, 'roi_y': 512, 'roi_diameter': 400,
            'gray_lower': 0, 'gray_upper': 120,
            'area_lower': 50000, 'area_upper': 350000,
            'circularity_lower': 0.5, 'circularity_upper': 1.0,
            'pixel_distance': 0.01,
        }

        await tm.process_camera(1)

        # 相机被抓了一帧
        cam.capture_image.assert_called_once_with(1, False)
        # 算法走了大圆分支
        img.process_big_circle_image.assert_called_once()
        img.process_small_circle_image.assert_not_called()
        # PLC 先被写 IDLE, 最后写 TASK_COMPLETED
        statuses = [c.args[1] for c in plc.write_camera_status.call_args_list]
        assert CameraStatus.IDLE in statuses
        assert CameraStatus.TASK_COMPLETED in statuses
        # 结果也写回 PLC 了
        plc.write_camera_result.assert_called_once()
        written = plc.write_camera_result.call_args.args[1]
        assert isinstance(written, CameraResult)
        assert written.result is True  # ProcessResult.OK → True

    @pytest.mark.asyncio
    async def test_small_circle_routes_to_small_algo(self, make_tm):
        tm, plc, cam, img = make_tm(trigger_source=7, exposure_time=5000)
        plc.read_camera_settings.return_value = {
            'status': CameraStatus.START_TASK,
            'trigger_mode': CameraTriggerStatus.SOFTWARE_TRIGGER,
            'exposure_time': 5000,
            'product_type': ProductType.SMALL_CIRCLE,
            'roi_x': 0, 'roi_y': 0, 'roi_diameter': 0,
            'gray_lower': 0, 'gray_upper': 0,
            'area_lower': 0, 'area_upper': 0,
            'circularity_lower': 0, 'circularity_upper': 0,
            'pixel_distance': 1,
        }

        await tm.process_camera(1)

        img.process_small_circle_image.assert_called_once()
        img.process_big_circle_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_ng_result_writes_result_false_to_plc(self, make_tm):
        tm, plc, cam, img = make_tm(trigger_source=7, exposure_time=5000)
        img.process_big_circle_image.return_value = (
            ProcessResult.NG, np.zeros((10, 10, 3), dtype=np.uint8), (999, 999), 999,
        )
        plc.read_camera_settings.return_value = {
            'status': CameraStatus.START_TASK,
            'trigger_mode': CameraTriggerStatus.SOFTWARE_TRIGGER,
            'exposure_time': 5000,
            'product_type': ProductType.LARGE_CIRCLE,
            'roi_x': 0, 'roi_y': 0, 'roi_diameter': 0,
            'gray_lower': 0, 'gray_upper': 0,
            'area_lower': 0, 'area_upper': 0,
            'circularity_lower': 0, 'circularity_upper': 0,
            'pixel_distance': 1,
        }

        await tm.process_camera(1)

        written = plc.write_camera_result.call_args.args[1]
        assert written.result is False


# =====================================================================
# 4. 曝光容差逻辑
# =====================================================================
class TestEnsureExposure:
    @pytest.mark.asyncio
    async def test_zero_exposure_is_skipped(self, make_tm):
        tm, _, cam, _ = make_tm()
        await tm._ensure_exposure(1, {'exposure_time': 0}, is_hardware_trigger=True)
        cam.set_exposure.assert_not_called()

    @pytest.mark.asyncio
    async def test_within_1us_tolerance_is_skipped(self, make_tm):
        tm, _, cam, _ = make_tm(exposure_time=5000.0)
        # 差 0.5us, 在容差内
        await tm._ensure_exposure(1, {'exposure_time': 5000.5}, is_hardware_trigger=True)
        cam.set_exposure.assert_not_called()

    @pytest.mark.asyncio
    async def test_change_beyond_tolerance_triggers_set(self, make_tm):
        tm, _, cam, _ = make_tm(exposure_time=5000.0)
        # 差 100us, 超容差
        await tm._ensure_exposure(1, {'exposure_time': 5100.0}, is_hardware_trigger=True)
        cam.set_exposure.assert_called_once_with(1, 5100.0)

    @pytest.mark.asyncio
    async def test_software_trigger_flushes_one_frame(self, make_tm):
        tm, _, cam, _ = make_tm(exposure_time=5000.0)
        await tm._ensure_exposure(1, {'exposure_time': 5100.0}, is_hardware_trigger=False)
        cam.set_exposure.assert_called_once()
        cam.flush_one_frame.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_hardware_trigger_does_not_flush(self, make_tm):
        tm, _, cam, _ = make_tm(exposure_time=5000.0)
        await tm._ensure_exposure(1, {'exposure_time': 5100.0}, is_hardware_trigger=True)
        cam.flush_one_frame.assert_not_called()


# =====================================================================
# 5. 触发模式切换
# =====================================================================
class TestTriggerModeSwitch:
    @pytest.mark.asyncio
    async def test_start_loop_forces_software_trigger(self, make_tm):
        # 当前是硬件触发 (source=0), 但 status 是 START_LOOP → 应切到软件触发
        tm, plc, cam, _ = make_tm(trigger_source=0, exposure_time=5000)
        plc.read_camera_settings.return_value = {
            'status': CameraStatus.START_LOOP,
            'trigger_mode': CameraTriggerStatus.HARDWARE_TRIGGER,
            'exposure_time': 5000,
            'product_type': ProductType.LARGE_CIRCLE,
            'roi_x': 0, 'roi_y': 0, 'roi_diameter': 0,
            'gray_lower': 0, 'gray_upper': 0,
            'area_lower': 0, 'area_upper': 0,
            'circularity_lower': 0, 'circularity_upper': 0,
            'pixel_distance': 1,
        }
        # 打断连续采集循环: 第一次读返回 START_LOOP, 第二次返回别的
        plc.read_camera_settings.side_effect = [
            plc.read_camera_settings.return_value,
            {'status': CameraStatus.IDLE, 'trigger_mode': CameraTriggerStatus.HARDWARE_TRIGGER,
             'exposure_time': 0, 'product_type': ProductType.LARGE_CIRCLE,
             'roi_x': 0, 'roi_y': 0, 'roi_diameter': 0,
             'gray_lower': 0, 'gray_upper': 0, 'area_lower': 0, 'area_upper': 0,
             'circularity_lower': 0, 'circularity_upper': 0, 'pixel_distance': 1},
        ]

        # 防万一死循环, 用 timeout 保护
        try:
            await asyncio.wait_for(tm.process_camera(1), timeout=5.0)
        except asyncio.TimeoutError:
            pytest.fail("process_camera didn't terminate")

        # 应切到软件触发
        cam.update_trigger_mode.assert_called_with(1, False)


# =====================================================================
# 6. settings → camera_info 转换
# =====================================================================
class TestSettingsToInfo:
    def test_roi_and_params_and_pixel_distance(self, make_tm):
        tm, *_ = make_tm()
        settings = {
            'roi_x': 640, 'roi_y': 512, 'roi_diameter': 400,
            'gray_lower': 10, 'gray_upper': 120,
            'area_lower': 50000, 'area_upper': 350000,
            'circularity_lower': 0.5, 'circularity_upper': 1.0,
            'pixel_distance': 0.01,
        }
        info = tm.convert_settings_to_camera_info(settings)
        assert info['roi'] == [640, 512, 400]
        assert info['params'] == [10, 120, 50000, 350000, 0.5, 1.0]
        assert info['pixel_distance'] == 0.01

    def test_missing_fields_fall_back_to_defaults(self, make_tm):
        tm, *_ = make_tm()
        info = tm.convert_settings_to_camera_info({})
        assert info['roi'] == [0, 0, 0]
        assert info['pixel_distance'] == 1
