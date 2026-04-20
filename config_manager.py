# config_manager.py

import json

class ConfigManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance.load_config()
        return cls._instance

    def load_config(self, config_file='config.json'):
        with open(config_file, 'r') as f:
            self.config = json.load(f)

    def get_camera_ip(self, camera_num):
        return self.config['cameras'][f'camera{camera_num}']['ip']

    def get_camera_host_lan(self, camera_num):
        return self.config['cameras'][f'camera{camera_num}']['host_lan']

    def is_camera_enabled(self, camera_num):
        """默认 True, 即不写 enabled 字段 = 启用"""
        return self.config['cameras'][f'camera{camera_num}'].get('enabled', True)

    def get_camera_roi(self, camera_num):
        """返回 {'width', 'height', 'offset_x', 'offset_y'} 或 None.
        未配置就返回 None, 走相机全帧默认模式.
        """
        roi = self.config['cameras'][f'camera{camera_num}'].get('roi')
        if roi is None:
            return None
        if 'width' not in roi or 'height' not in roi:
            raise ValueError(f"camera{camera_num}.roi 必须包含 width + height")
        return {
            'width': int(roi['width']),
            'height': int(roi['height']),
            'offset_x': int(roi.get('offset_x', 0)),
            'offset_y': int(roi.get('offset_y', 0)),
        }

    def get_plc_ip(self):
        return self.config['plc']['ip']

# 创建一个全局实例
config = ConfigManager()