"""单元测试: plc_manager.PLCManager

重点:
  - Modbus 字节序转换 (uint32 / float / double) 的正反变换是否一致
  - read_camera_settings 的块布局解析 (17 个 word 正确拆解成各字段)
  - write_camera_result 打包的 words 是否跟协议一致
  - 状态 enum 映射
  - _parse_camera_settings 把 raw dict 转成 enum

不依赖真实 PLC: 用 unittest.mock 替换 PLCBase.
"""
import struct
from unittest.mock import MagicMock, patch

import pytest

# 延迟 import, 避免 PLCBase.__init__ 真去连接
from plc_manager import (
    CameraResult,
    CameraStatus,
    CameraTriggerStatus,
    Endian,
    PLCManager,
    ProductType,
    SystemStatus,
)


@pytest.fixture
def plc_mgr_little():
    """构造一个 little-endian PLCManager, 内部的 PLCBase 全部 mock 掉."""
    with patch('plc_manager.PLCBase') as mock_base_cls:
        mock_base = MagicMock()
        mock_base_cls.return_value = mock_base
        mgr = PLCManager('127.0.0.1', endian=Endian.LITTLE)
        mgr._mock_plc = mock_base  # 方便测试里取
        yield mgr


@pytest.fixture
def plc_mgr_big():
    with patch('plc_manager.PLCBase') as mock_base_cls:
        mock_base = MagicMock()
        mock_base_cls.return_value = mock_base
        mgr = PLCManager('127.0.0.1', endian=Endian.BIG)
        mgr._mock_plc = mock_base
        yield mgr


# =====================================================================
# 1. 字节序转换 (纯函数, 最值得测)
# =====================================================================
class TestWordsToUint32:
    def test_little_endian_low_word_first(self, plc_mgr_little):
        # LE: (high<<16) | low, 所以第一个参数是 low
        assert plc_mgr_little._words_to_uint32(low=0x5678, high=0x1234) == 0x12345678

    def test_big_endian_low_word_first(self, plc_mgr_big):
        # BE: (low<<16) | high
        assert plc_mgr_big._words_to_uint32(low=0x1234, high=0x5678) == 0x12345678


class TestWordsToFloat:
    def test_round_trip_3_14(self, plc_mgr_little):
        # 把 3.14 打包成 little-endian 两个 word 再还原
        packed = struct.pack('<f', 3.14)
        low = (packed[1] << 8) | packed[0]
        high = (packed[3] << 8) | packed[2]
        result = plc_mgr_little._words_to_float(low, high)
        assert abs(result - 3.14) < 1e-5

    def test_round_trip_zero(self, plc_mgr_little):
        assert plc_mgr_little._words_to_float(0, 0) == 0.0


class TestUint32ToWords:
    def test_split_0x12345678(self, plc_mgr_little):
        assert plc_mgr_little._uint32_to_words(0x12345678) == [0x5678, 0x1234]

    def test_zero(self, plc_mgr_little):
        assert plc_mgr_little._uint32_to_words(0) == [0, 0]


class TestDoubleToWords:
    def test_outputs_four_words(self, plc_mgr_little):
        words = plc_mgr_little._double_to_words(3.14)
        assert len(words) == 4
        # 每个 word 都在 16 bit 范围
        for w in words:
            assert 0 <= w <= 0xFFFF


class TestFloatToWords:
    def test_outputs_two_words(self, plc_mgr_little):
        words = plc_mgr_little._float_to_words(1.5)
        assert len(words) == 2


# =====================================================================
# 2. raw → enum 的解析
# =====================================================================
class TestParseCameraSettings:
    def test_maps_status_trigger_product_to_enums(self, plc_mgr_little):
        raw = {
            'status': CameraStatus.START_TASK.value,
            'trigger': CameraTriggerStatus.HARDWARE_TRIGGER.value,
            'exposure': 5000,
            'pixel_distance': 0.01,
            'product_type': ProductType.LARGE_CIRCLE.value,
            'gray_upper': 120,
            'gray_lower': 0,
            'area_upper': 350000,
            'area_lower': 50000,
            'circularity_upper': 1.0,
            'circularity_lower': 0.5,
            'roi_x': 640,
            'roi_y': 512,
            'roi_diameter': 400,
        }
        parsed = plc_mgr_little._parse_camera_settings(raw)
        assert parsed['status'] == CameraStatus.START_TASK
        assert parsed['trigger_mode'] == CameraTriggerStatus.HARDWARE_TRIGGER
        assert parsed['product_type'] == ProductType.LARGE_CIRCLE
        assert parsed['exposure_time'] == 5000

    def test_invalid_enum_raises(self, plc_mgr_little):
        raw = dict.fromkeys([
            'status', 'trigger', 'exposure', 'pixel_distance', 'product_type',
            'gray_upper', 'gray_lower', 'area_upper', 'area_lower',
            'circularity_upper', 'circularity_lower', 'roi_x', 'roi_y', 'roi_diameter',
        ], 0)
        raw['status'] = 999  # 非法值
        with pytest.raises(ValueError):
            plc_mgr_little._parse_camera_settings(raw)


# =====================================================================
# 3. read_camera_settings: 块布局解析
# =====================================================================
class TestReadCameraSettings:
    def test_cam1_parses_known_block(self, plc_mgr_little):
        # 造一个 27 个 word 的 block (cam1: D1 + D10..D27)
        # block[0] = status (D1)
        # block[9..26] = config (D10..D27, 18 个)
        block = [0] * 27
        block[0] = CameraStatus.START_TASK.value              # D1 status
        block[9 + 0] = CameraTriggerStatus.SOFTWARE_TRIGGER.value  # D10 trigger
        block[9 + 1] = 3000                                   # D11 exposure
        # D12-D13 pixel_distance (float32 小端)
        packed = struct.pack('<f', 0.05)
        block[9 + 2] = (packed[1] << 8) | packed[0]
        block[9 + 3] = (packed[3] << 8) | packed[2]
        block[9 + 4] = ProductType.LARGE_CIRCLE.value         # D14 product_type
        block[9 + 5] = 120                                    # D15 gray_upper
        block[9 + 6] = 0                                      # D16 gray_lower
        # D17-D18 area_upper (uint32 小端)
        block[9 + 7] = 350000 & 0xFFFF
        block[9 + 8] = (350000 >> 16) & 0xFFFF
        # D19-D20 area_lower
        block[9 + 9] = 50000 & 0xFFFF
        block[9 + 10] = (50000 >> 16) & 0xFFFF
        # D21-D22 circularity_upper (float32)
        packed = struct.pack('<f', 1.0)
        block[9 + 11] = (packed[1] << 8) | packed[0]
        block[9 + 12] = (packed[3] << 8) | packed[2]
        # D23-D24 circularity_lower
        packed = struct.pack('<f', 0.5)
        block[9 + 13] = (packed[1] << 8) | packed[0]
        block[9 + 14] = (packed[3] << 8) | packed[2]
        block[9 + 15] = 640                                   # D25 roi_x
        block[9 + 16] = 512                                   # D26 roi_y
        block[9 + 17] = 400                                   # D27 roi_diameter

        plc_mgr_little._mock_plc.read_status.return_value = block

        out = plc_mgr_little.read_camera_settings(camera_num=1)

        assert out['status'] == CameraStatus.START_TASK
        assert out['trigger_mode'] == CameraTriggerStatus.SOFTWARE_TRIGGER
        assert out['exposure_time'] == 3000
        assert abs(out['pixel_distance'] - 0.05) < 1e-5
        assert out['product_type'] == ProductType.LARGE_CIRCLE
        assert out['area_upper'] == 350000
        assert out['area_lower'] == 50000
        assert abs(out['circularity_upper'] - 1.0) < 1e-5
        assert abs(out['circularity_lower'] - 0.5) < 1e-5
        assert out['roi_x'] == 640 and out['roi_y'] == 512 and out['roi_diameter'] == 400

        # 验证 PLC 只被调了 1 次 (原子块读)
        plc_mgr_little._mock_plc.read_status.assert_called_once()
        args, kwargs = plc_mgr_little._mock_plc.read_status.call_args
        assert args[0] == 1 and kwargs.get('count') == 27

    def test_unsupported_camera_num_returns_empty(self, plc_mgr_little):
        assert plc_mgr_little.read_camera_settings(camera_num=99) == {}

    def test_read_failure_returns_empty(self, plc_mgr_little):
        plc_mgr_little._mock_plc.read_status.return_value = None
        assert plc_mgr_little.read_camera_settings(camera_num=1) == {}

    def test_short_block_returns_empty(self, plc_mgr_little):
        plc_mgr_little._mock_plc.read_status.return_value = [0] * 10  # 太短
        assert plc_mgr_little.read_camera_settings(camera_num=1) == {}


# =====================================================================
# 4. write_camera_result: 协议打包
# =====================================================================
class TestWriteCameraResult:
    def test_cam1_writes_17_words_to_D70(self, plc_mgr_little):
        result = CameraResult(x=1.5, y=-2.5, angle=45.0, result=True, area=12345, circularity=0.95)
        plc_mgr_little._mock_plc.write_multiple_registers.return_value = True

        plc_mgr_little.write_camera_result(1, result)

        plc_mgr_little._mock_plc.write_multiple_registers.assert_called_once()
        args, _ = plc_mgr_little._mock_plc.write_multiple_registers.call_args
        start_addr, words = args
        assert start_addr == 70           # Cam1 output_x 起始
        assert len(words) == 17           # 4+4+4+1+2+2
        # 第 13 个 word (index 12) 是 result: True → 1
        assert words[12] == 1

    def test_cam2_uses_D90(self, plc_mgr_little):
        result = CameraResult(x=0, y=0, angle=0, result=False, area=0, circularity=0)
        plc_mgr_little._mock_plc.write_multiple_registers.return_value = True

        plc_mgr_little.write_camera_result(2, result)

        args, _ = plc_mgr_little._mock_plc.write_multiple_registers.call_args
        start_addr, words = args
        assert start_addr == 90
        # result=False → 2
        assert words[12] == 2


# =====================================================================
# 5. 系统寄存器
# =====================================================================
class TestSystemRegisters:
    def test_write_system_status(self, plc_mgr_little):
        plc_mgr_little.write_system_status(SystemStatus.PROCESSING)
        plc_mgr_little._mock_plc.write_status.assert_called_with(120, SystemStatus.PROCESSING.value)

    def test_write_error_code(self, plc_mgr_little):
        plc_mgr_little.write_error_code(42)
        plc_mgr_little._mock_plc.write_status.assert_called_with(121, 42)

    def test_toggle_heartbeat_flips_bit(self, plc_mgr_little):
        plc_mgr_little._mock_plc.read_status.return_value = 0
        plc_mgr_little.toggle_system_heartbeat()
        # 读到 0, 应写 1
        plc_mgr_little._mock_plc.write_status.assert_called_with(122, 1)

        plc_mgr_little._mock_plc.read_status.return_value = 1
        plc_mgr_little.toggle_system_heartbeat()
        plc_mgr_little._mock_plc.write_status.assert_called_with(122, 0)

    def test_write_camera_status(self, plc_mgr_little):
        plc_mgr_little.write_camera_status(1, CameraTriggerStatus.HARDWARE_TRIGGER)
        plc_mgr_little._mock_plc.write_status.assert_called_with(
            1, CameraTriggerStatus.HARDWARE_TRIGGER.value,
        )
