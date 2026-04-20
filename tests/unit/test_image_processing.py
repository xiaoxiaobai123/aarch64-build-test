"""单元测试: image_processing.ImageProcessor

覆盖:
  - 参数校验 / ROI 默认值 / 边界调整 (纯逻辑)
  - _fill_holes / _inner_circle / _smallest_circle / _orientation (算法原语)
  - process_big_circle / process_small_circle (核心流程)
  - process_big_circle_image / process_small_circle_image (高阶接口)
  - convert_to_center_coordinates (坐标变换)
  - add_result_bar / combine_images (显示链路)

所有用例用合成图像，不依赖真实相机数据.
"""
import math

import cv2
import numpy as np
import pytest

from image_processing import ImageProcessor, ProcessResult


# =====================================================================
# 1. 纯参数逻辑
# =====================================================================
class TestGetRoiInfo:
    def setup_method(self):
        self.p = ImageProcessor()

    def test_all_zero_returns_defaults(self):
        x, y, r = self.p.get_roi_info([0, 0, 0], image_width=1000, image_height=800)
        assert x == 500
        assert y == 400
        assert r == min(1000, 800) // 4 + 200

    def test_user_values_override_defaults(self):
        x, y, r = self.p.get_roi_info([100, 200, 50], image_width=1000, image_height=800)
        assert (x, y, r) == (100, 200, 50)

    def test_partial_override(self):
        x, y, r = self.p.get_roi_info([0, 200, 0], image_width=1000, image_height=800)
        assert x == 500        # 默认
        assert y == 200        # 用户值
        assert r == 400        # 默认 (1000 / 4 + 200 = 450? 不对 min(1000,800)/4=200+200=400)


class TestValidateAndAdjust:
    def setup_method(self):
        self.p = ImageProcessor()

    def test_zero_returns_default(self):
        assert self.p.validate_and_adjust_param(0, default=42, min_val=0, max_val=100) == 42

    def test_in_range_returns_value(self):
        assert self.p.validate_and_adjust_param(50, default=42, min_val=0, max_val=100) == 50

    def test_above_range_clamps_to_max(self):
        assert self.p.validate_and_adjust_param(200, default=42, min_val=0, max_val=100) == 100

    def test_below_range_clamps_to_min(self):
        assert self.p.validate_and_adjust_param(-5, default=42, min_val=0, max_val=100) == 0


class TestAdjustBounds:
    def setup_method(self):
        self.p = ImageProcessor()

    def test_lower_greater_than_upper_returns_defaults(self):
        lo, hi = self.p._adjust_bounds(50, 10, 'area', (5, 100))
        assert (lo, hi) == (5, 100)

    def test_normal_returns_unchanged(self):
        lo, hi = self.p._adjust_bounds(10, 50, 'area', (5, 100))
        assert (lo, hi) == (10, 50)


class TestGetImageParams:
    def setup_method(self):
        self.p = ImageProcessor()

    def test_all_zeros_returns_big_circle_defaults(self):
        params = self.p.get_image_params([0] * 6, is_small_circle=False)
        assert params['gray'] == self.p.DEFAULT_PARAMS['gray']
        assert params['area'] == self.p.DEFAULT_PARAMS['area']
        assert params['circularity'] == self.p.DEFAULT_PARAMS['circularity']

    def test_all_zeros_returns_small_circle_defaults(self):
        params = self.p.get_image_params([0] * 6, is_small_circle=True)
        assert params['area'] == self.p.SMALL_CIRCLE_PARAMS['area']


# =====================================================================
# 2. 坐标变换
# =====================================================================
class TestCoordinateConvert:
    def setup_method(self):
        self.p = ImageProcessor()

    def test_center_is_origin(self):
        assert self.p.convert_to_center_coordinates((500, 400), (1000, 800)) == (0, 0)

    def test_y_is_flipped(self):
        # 图像中 y 向下, 输出坐标系 y 向上 → 上方的 y 是正值
        _, y = self.p.convert_to_center_coordinates((500, 300), (1000, 800))
        assert y == 100  # 比中心高 100

    def test_top_left_corner(self):
        x, y = self.p.convert_to_center_coordinates((0, 0), (1000, 800))
        assert (x, y) == (-500, 400)


# =====================================================================
# 3. 算法原语
# =====================================================================
class TestInnerCircle:
    def test_ideal_circle_inner_equals_radius(self):
        # 填充圆的最大内切圆 = 原圆
        mask = np.zeros((500, 500), dtype=np.uint8)
        cv2.circle(mask, (250, 250), 100, 255, -1)
        row, col, radius = ImageProcessor._inner_circle(mask)
        assert abs(row - 250) <= 2
        assert abs(col - 250) <= 2
        assert abs(radius - 100) <= 2


class TestSmallestCircle:
    def test_ideal_circle_outer_equals_radius(self):
        mask = np.zeros((500, 500), dtype=np.uint8)
        cv2.circle(mask, (250, 250), 100, 255, -1)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        row, col, radius = ImageProcessor._smallest_circle(contours[0])
        assert abs(row - 250) <= 2
        assert abs(col - 250) <= 2
        assert abs(radius - 100) <= 2


class TestOrientation:
    def test_symmetric_shape_returns_zero(self):
        # 完美对称的圆 — μ20==μ02 且 μ11==0
        mask = np.zeros((500, 500), dtype=np.uint8)
        cv2.circle(mask, (250, 250), 100, 255, -1)
        phi = ImageProcessor._orientation(mask)
        assert abs(phi) < 1e-3

    def test_horizontal_ellipse_orientation_near_zero(self):
        mask = np.zeros((500, 500), dtype=np.uint8)
        cv2.ellipse(mask, (250, 250), (150, 60), angle=0, startAngle=0, endAngle=360, color=255, thickness=-1)
        phi = ImageProcessor._orientation(mask)
        # 水平椭圆主轴沿 x, 角度 ≈ 0
        assert abs(phi) < 0.1


class TestFillHoles:
    def test_donut_gets_filled(self):
        mask = np.zeros((500, 500), dtype=np.uint8)
        cv2.circle(mask, (250, 250), 100, 255, -1)  # 外圆
        cv2.circle(mask, (250, 250), 40, 0, -1)     # 挖洞
        filled = ImageProcessor._fill_holes(mask)
        # 洞应被填上
        assert filled[250, 250] == 255


# =====================================================================
# 4. 核心流程
# =====================================================================
class TestProcessBigCircle:
    def test_ideal_circle_is_detected(self, make_circle_image, big_circle_params):
        img = make_circle_image(cx=250, cy=250, radius=100, fill=50)  # 暗圆亮背景? 不对
        # gray [0,120] → 前景是暗的. 让前景 (圆内) 是 50 (暗), 背景 (外) 是 255 (亮)
        img = np.full((500, 500, 3), 255, dtype=np.uint8)
        cv2.circle(img, (250, 250), 100, (50, 50, 50), -1)
        roi = (250, 250, 200)
        _, area, circ, center, radius, phi = ImageProcessor().process_big_circle(img, roi, big_circle_params)
        assert area != 999, "应该检测到圆"
        # 面积 ≈ π r²
        assert abs(area - math.pi * 100 * 100) / (math.pi * 100 * 100) < 0.05
        # 圆度应接近 1
        assert circ > 0.9
        # 中心在 (250, 250) 附近 (注意返回是 (row, col))
        row, col = center
        assert abs(row - 250) < 5 and abs(col - 250) < 5
        # 半径 ≈ 100
        assert abs(radius - 100) < 5

    def test_blank_image_returns_sentinel(self, blank_image, big_circle_params):
        roi = (250, 250, 200)
        _, area, circ, center, radius, phi = ImageProcessor().process_big_circle(
            blank_image, roi, big_circle_params)
        assert area == 999
        assert center == (999, 999)

    def test_too_small_circle_is_filtered(self, big_circle_params):
        img = np.full((500, 500, 3), 255, dtype=np.uint8)
        cv2.circle(img, (250, 250), 20, (50, 50, 50), -1)  # 小圆 area ≈ 1256 < area_lo 10000
        roi = (250, 250, 200)
        _, area, *_ = ImageProcessor().process_big_circle(img, roi, big_circle_params)
        assert area == 999, "小圆应被面积过滤掉"


class TestProcessSmallCircle:
    def test_ideal_small_circle(self, small_circle_params):
        img = np.full((500, 500, 3), 255, dtype=np.uint8)
        cv2.circle(img, (250, 250), 40, (50, 50, 50), -1)  # area ≈ 5026
        roi = (250, 250, 100)
        _, area, circ, center, radius, _ = ImageProcessor().process_small_circle(
            img, roi, small_circle_params)
        assert area != 999
        assert abs(area - math.pi * 40 * 40) / (math.pi * 40 * 40) < 0.15  # 腐蚀后会小些
        assert circ > 0.7


# =====================================================================
# 5. 高阶接口 (task_manager 实际调用的入口)
# =====================================================================
class TestProcessBigCircleImage:
    def test_returns_ok_on_good_image(self):
        img = np.full((1024, 1280, 3), 255, dtype=np.uint8)
        # 默认面积阈值 [50000, 350000], 半径 200 对应 area ≈ 125663
        cv2.circle(img, (640, 512), 200, (50, 50, 50), -1)
        camera_info = {'roi': [640, 512, 400], 'params': [0] * 6, 'pixel_distance': 1}
        result, out_img, center, angle = ImageProcessor().process_big_circle_image(img, camera_info)
        assert result == ProcessResult.OK
        assert out_img is not None
        # center 经过 convert_to_center_coordinates, 应在原点附近
        assert abs(center[0]) < 10 and abs(center[1]) < 10

    def test_returns_ng_on_blank_image(self):
        img = np.zeros((1024, 1280, 3), dtype=np.uint8)
        camera_info = {'roi': [640, 512, 400], 'params': [0] * 6, 'pixel_distance': 1}
        result, out_img, center, angle = ImageProcessor().process_big_circle_image(img, camera_info)
        assert result == ProcessResult.NG
        assert center == (999, 999)

    def test_pixel_distance_scales_center(self):
        img = np.full((1024, 1280, 3), 255, dtype=np.uint8)
        # 故意把圆画在偏左 200 像素的位置
        cv2.circle(img, (640 - 200, 512), 200, (50, 50, 50), -1)
        camera_info = {'roi': [640 - 200, 512, 400], 'params': [0] * 6, 'pixel_distance': 0.01}
        result, _, center, _ = ImageProcessor().process_big_circle_image(img, camera_info)
        assert result == ProcessResult.OK
        # 偏左 200 像素, pixel_distance=0.01 → x ≈ -2.0 (单位) ± 一点容差
        assert -2.5 < center[0] < -1.5


class TestProcessSmallCircleImage:
    def test_returns_ok_on_good_image(self):
        img = np.full((1024, 1280, 3), 255, dtype=np.uint8)
        # 小圆默认 area [2000, 50000], 半径 50 → area ≈ 7854
        cv2.circle(img, (640, 512), 50, (50, 50, 50), -1)
        camera_info = {'roi': [640, 512, 150], 'params': [0] * 6, 'pixel_distance': 1}
        result, _, _, angle = ImageProcessor().process_small_circle_image(img, camera_info)
        assert result == ProcessResult.OK
        assert angle == 0  # 小圆不输出角度


# =====================================================================
# 6. 显示链路
# =====================================================================
class TestResultBar:
    def test_adds_90px_bar(self):
        img = np.zeros((100, 200, 3), dtype=np.uint8)
        out = ImageProcessor.add_result_bar(img, ProcessResult.OK)
        assert out.shape == (100 + 90, 200, 3)

    def test_ok_bar_is_green(self):
        img = np.zeros((100, 200, 3), dtype=np.uint8)
        out = ImageProcessor.add_result_bar(img, ProcessResult.OK)
        # 底部栏取中间像素, 应该是绿色 (0, 255, 0)
        assert tuple(out[-10, 100]) == (0, 255, 0)

    def test_ng_bar_is_red(self):
        img = np.zeros((100, 200, 3), dtype=np.uint8)
        out = ImageProcessor.add_result_bar(img, ProcessResult.NG)
        assert tuple(out[-10, 100]) == (0, 0, 255)

    def test_invalid_result_type_falls_back_to_exception(self):
        img = np.zeros((100, 200, 3), dtype=np.uint8)
        out = ImageProcessor.add_result_bar(img, "not_an_enum")
        # EXCEPTION 用灰色 (128,128,128)
        assert tuple(out[-10, 100]) == (128, 128, 128)


class TestCombineImages:
    def test_side_by_side_with_10px_separator(self):
        a = np.zeros((100, 80, 3), dtype=np.uint8)
        b = np.zeros((100, 80, 3), dtype=np.uint8)
        out = ImageProcessor.combine_images([a, b])
        assert out.shape == (100, 80 + 10 + 80, 3)
        # 分隔线是白色
        assert tuple(out[50, 85]) == (255, 255, 255)

    def test_wrong_count_raises(self):
        a = np.zeros((100, 80, 3), dtype=np.uint8)
        with pytest.raises(AssertionError):
            ImageProcessor.combine_images([a])


class TestRgb565:
    def test_shape_halves_third_dim(self):
        img = np.zeros((100, 200, 3), dtype=np.uint8)
        rgb565 = ImageProcessor().convert_to_rgb565(img)
        # COLOR_BGR2BGR565 输出 shape=(H, W, 2), dtype=uint8
        assert rgb565.shape == (100, 200, 2)
        assert rgb565.dtype == np.uint8

    def test_none_input_returns_none(self):
        assert ImageProcessor().convert_to_rgb565(None) is None
