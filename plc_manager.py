from enum import Enum
from typing import Dict, Any, NamedTuple
from plc_base import PLCBase
import struct
import threading
import log_config

logger = log_config.setup_logging()
class CameraStatus(Enum):
    IDLE = 0
    READING_DATA = 1
    PROCESSING_DATA = 2
    TASK_COMPLETED = 3
    START_TASK = 10
    START_LOOP = 11

class ProductType(Enum):
    NONE = 0
    LARGE_CIRCLE = 1
    SMALL_CIRCLE = 2

class SystemStatus(Enum):
    STARTING = 0
    IDLE = 1
    PROCESSING = 2
    ERROR = 3

class CameraTriggerStatus(Enum):
    DISCONNECTED = 0
    HARDWARE_TRIGGER = 1
    SOFTWARE_TRIGGER = 2

class ROI(NamedTuple):
    x: int
    y: int
    diameter: int

class CameraResult(NamedTuple):
    x: float
    y: float
    angle: float
    result: bool
    area: int
    circularity: float
class Endian(Enum):
    LITTLE = 'little'
    BIG = 'big'
class PLCManager:
    def __init__(self, ip: str, port: int = 502,endian: Endian = Endian.LITTLE):
        self.plc = PLCBase(ip, port)
        self.endian = endian
        self._init_registers()

        self.lock = threading.Lock()
    def _init_registers(self):
        self.camera_registers = {
            1: {
                'read': {
                    'status': 1, 'trigger': 10, 'exposure': 11, 'pixel_distance': 12,
                    'product_type': 14, 'gray_upper': 15, 'gray_lower': 16,
                    'area_upper': 17, 'area_lower': 19, 'circularity_upper': 21, 'circularity_lower': 23,
                    'roi_x': 25, 'roi_y': 26, 'roi_diameter': 27
                },
                'write': {
                    'output_x': 70,
                    'output_y': 74,
                    'output_angle': 78,
                    'result': 82,
                    'area': 83,
                    'circularity': 85
                }
            },
            2: {
                'read': {
                    'status': 2, 'trigger': 30, 'exposure': 31, 'pixel_distance': 32,
                    'product_type': 34, 'gray_upper': 35, 'gray_lower': 36,
                    'area_upper': 37, 'area_lower': 39, 'circularity_upper': 41, 'circularity_lower': 43,
                    'roi_x': 45, 'roi_y': 46, 'roi_diameter': 47
                },
                'write': {
                    'output_x': 90,  # 假设相机2的写入寄存器从170开始
                    'output_y': 94,
                    'output_angle': 98,
                    'result': 102,
                    'area': 103,
                    'circularity': 105
                }
            }
        }
        self.system_registers = {
            'plc_heartbeat': 50,
            'system_status': 120,
            'error_code': 121,
            'system_heartbeat': 122,
            'camera1_trigger_status': 123,
            'camera2_trigger_status': 124,
            'camera1_status': 1,
            'camera2_status': 2
        }

    def read_camera_settings(self, camera_num: int) -> Dict[str, Any]:
        """一次 Modbus 请求读完 status + config，真正原子快照。"""
        # 块布局（以块首地址为 0）：
        #   status 在 status_offset（Cam1=0, Cam2=0）
        #   config 从 config_offset 起 18 个字：
        #     +0: trigger     +1: exposure    +2-3: pixel_distance(float32)
        #     +4: product_type +5: gray_upper +6: gray_lower
        #     +7-8: area_upper(uint32)  +9-10: area_lower(uint32)
        #     +11-12: circularity_upper(float32) +13-14: circularity_lower(float32)
        #     +15: roi_x      +16: roi_y     +17: roi_diameter
        CONFIG_SIZE = 18

        if camera_num == 1:
            status_addr, config_addr = 1, 10   # D1 + D10..D27
        elif camera_num == 2:
            status_addr, config_addr = 2, 30   # D2 + D30..D47
        else:
            logger.error(f"Unsupported camera_num: {camera_num}")
            return {}

        block_start = status_addr
        block_size = (config_addr + CONFIG_SIZE) - block_start  # 27 or 46
        status_idx = status_addr - block_start                   # 0
        config_idx = config_addr - block_start                   # 9 or 28

        with self.lock:
            # 一次 Modbus 请求读完。Modbus 协议保证服务器在同一瞬间快照所有
            # 被请求的寄存器，因此不可能再读到"status 新、config 旧"或
            # "config 内部字段混搭"的中间状态。
            block = self.plc.read_status(block_start, count=block_size)
            if block is None or len(block) < block_size:
                logger.error(
                    f"Failed to atomic-read camera {camera_num} settings "
                    f"at D{block_start}..D{block_start + block_size - 1}, got: {block}"
                )
                return {}

            c = config_idx  # 简写
            data = {
                'status': block[status_idx],
                'trigger': block[c + 0],
                'exposure': block[c + 1],
                'pixel_distance': self._words_to_float(block[c + 2], block[c + 3]),
                'product_type': block[c + 4],
                'gray_upper': block[c + 5],
                'gray_lower': block[c + 6],
                'area_upper': self._words_to_uint32(block[c + 7], block[c + 8]),
                'area_lower': self._words_to_uint32(block[c + 9], block[c + 10]),
                'circularity_upper': self._words_to_float(block[c + 11], block[c + 12]),
                'circularity_lower': self._words_to_float(block[c + 13], block[c + 14]),
                'roi_x': block[c + 15],
                'roi_y': block[c + 16],
                'roi_diameter': block[c + 17],
            }
            return self._parse_camera_settings(data)

    def _words_to_uint32(self, low: int, high: int) -> int:
        """两个 16-bit 字按当前字节序拼成 uint32。"""
        if self.endian == Endian.LITTLE:
            return (high << 16) | low
        else:
            return (low << 16) | high

    def _words_to_float(self, low: int, high: int) -> float:
        """两个 16-bit 字按当前字节序拼成 float32（IEEE 754）。"""
        value = self._words_to_uint32(low, high)
        return struct.unpack('!f', struct.pack('!I', value))[0]

    def _parse_camera_settings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            'status': CameraStatus(data['status']),
            'trigger_mode': CameraTriggerStatus(data['trigger']),
            'exposure_time': data['exposure'],
            'pixel_distance': data['pixel_distance'],
            'product_type': ProductType(data['product_type']),
            'gray_upper': data['gray_upper'],
            'gray_lower': data['gray_lower'],
            'area_upper': data['area_upper'],
            'area_lower': data['area_lower'],
            'circularity_upper': data['circularity_upper'],
            'circularity_lower': data['circularity_lower'],
            'roi_x': data['roi_x'],
            'roi_y': data['roi_y'],
            'roi_diameter': data['roi_diameter']
        }

    def _write_double(self, register: int, value: float) -> None:
        # 将 double 转换为 IEEE 754 格式的字节（小端序）
        packed = struct.pack('<d', value)

        # 将字节转换为 16 位整数列表
        words = [
            (packed[1] << 8) | packed[0],
            (packed[3] << 8) | packed[2],
            (packed[5] << 8) | packed[4],
            (packed[7] << 8) | packed[6]
        ]

        # 将每个 16 位整数写入对应的 D 寄存器
        for i, word in enumerate(words):
            self.plc.write_status(register + i, word)

    def write_camera_result(self, camera_num: int, result: CameraResult) -> None:
        """一次 Modbus 块写 17 个寄存器 (D70-D86 for Cam1, D90-D106 for Cam2).
        原先 17 次独立写 ~20ms, 块写 ~3ms. 同时消除 PLC 读到半写状态的风险.
        """
        with self.lock:
            registers = self.camera_registers[camera_num]['write']
            start_addr = registers['output_x']  # D70 / D90

            # 按协议顺序打包 17 个 word
            words = []
            words.extend(self._double_to_words(result.x))         # 4 words: output_x
            words.extend(self._double_to_words(result.y))         # 4 words: output_y
            words.extend(self._double_to_words(result.angle))     # 4 words: output_angle
            words.append(1 if result.result else 2)               # 1 word : result
            words.extend(self._uint32_to_words(result.area))      # 2 words: area
            words.extend(self._float_to_words(result.circularity))# 2 words: circularity
            # 共 17 个 word

            ok = self.plc.write_multiple_registers(start_addr, words)
            if not ok:
                logger.error(f"[PLC] Cam{camera_num} 块写结果失败 (addr={start_addr}, n={len(words)})")

    # ---- 字节序转换 helpers (块写专用, 不碰 Modbus) ----
    def _double_to_words(self, value: float) -> list:
        packed = struct.pack('<d', value)
        return [
            (packed[1] << 8) | packed[0],
            (packed[3] << 8) | packed[2],
            (packed[5] << 8) | packed[4],
            (packed[7] << 8) | packed[6],
        ]

    def _uint32_to_words(self, value: int) -> list:
        return [value & 0xFFFF, (value >> 16) & 0xFFFF]

    def _float_to_words(self, value: float) -> list:
        packed = struct.pack('<f', value)
        return [
            (packed[1] << 8) | packed[0],
            (packed[3] << 8) | packed[2],
        ]

    # ---- 保留旧接口不影响其他代码 ----
    def _write_uint32(self, register: int, value: int) -> None:
        self.plc.write_status(register, value & 0xFFFF)
        self.plc.write_status(register + 1, (value >> 16) & 0xFFFF)

    def _write_float(self, register: int, value: float) -> None:
        packed = struct.pack('<f', value)
        self.plc.write_status(register, (packed[1] << 8) | packed[0])
        self.plc.write_status(register + 1, (packed[3] << 8) | packed[2])
     

    def read_plc_heartbeat(self) -> int:
        return self.plc.read_status(self.system_registers['plc_heartbeat'])

    def write_system_status(self, status: SystemStatus) -> None:
        self.plc.write_status(self.system_registers['system_status'], status.value)

    def write_error_code(self, error_code: int) -> None:
        self.plc.write_status(self.system_registers['error_code'], error_code)

    def write_system_heartbeat(self, value: int) -> None:
        self.plc.write_status(self.system_registers['system_heartbeat'], value)

    def write_camera_status(self, camera_num: int, status: CameraTriggerStatus) -> None:
        with self.lock:
            register = self.system_registers[f'camera{camera_num}_status']
            self.plc.write_status(register, status.value)

    def toggle_system_heartbeat(self) -> None:
        current_heartbeat = self.plc.read_status(self.system_registers['system_heartbeat'])
        self.write_system_heartbeat(1 - current_heartbeat)

    def _read_uint32(self, register: int) -> int:
        low_word = self.plc.read_status(register)
        high_word = self.plc.read_status(register + 1)
        if self.endian == Endian.LITTLE:
            return (high_word << 16) | low_word
        else:  # BIG endian
            return (low_word << 16) | high_word

 

    def _read_float(self, register: int) -> float:
        value = self._read_uint32(register)
        return struct.unpack('!f', struct.pack('!I', value))[0]


    def close(self) -> None:
        self.plc.close()