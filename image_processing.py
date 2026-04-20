"""
图像处理模块 —— OpenCV 版本（不依赖 Halcon）

算法映射关系（Halcon → OpenCV）:
  gen_circle + reduce_domain  → cv2.circle 画 mask + cv2.bitwise_and
  rgb1_to_gray                → cv2.cvtColor(..., COLOR_BGR2GRAY)
  threshold                   → cv2.inRange 或 cv2.threshold
  erosion_circle              → cv2.erode(..., 圆形 kernel)
  connection + fill_up        → cv2.connectedComponentsWithStats + 闭运算
  select_shape                → 手动按 area / circularity 过滤
  inner_circle                → cv2.distanceTransform 找最大内切圆
  smallest_circle             → cv2.minEnclosingCircle
  orientation_region          → cv2.moments 算二阶矩, atan2(2μ11, μ20-μ02)/2
  area_center / circularity   → 从 stats 直接读或手算
"""
from enum import Enum

import cv2
import numpy as np
import os

import log_config
logger = log_config.setup_logging()


class ProcessResult(Enum):
    OK = 1
    NG = 2
    EXCEPTION = 3


class ImageProcessor:
    # 类级缓存（和 halcon 版完全一致）
    _company_bar_cache = {}
    _result_bar_cache = {}
    _combine_canvas_cache = {}  # combine_images 用，避免每帧 np.zeros 分配

    def __init__(self):
        # 大圆默认参数
        self.DEFAULT_PARAMS = {
            'gray': (0, 120),
            'area': (50000, 350000),
            'circularity': (0.5, 1.0)
        }
        # 小圆默认参数
        self.SMALL_CIRCLE_PARAMS = {
            'gray': (0, 120),
            'area': (2000, 50000),
            'circularity': (0.5, 1.0)
        }

    # =========================================================
    # 参数处理 —— 和 halcon 版完全一致
    # =========================================================
    def get_roi_info(self, roi, image_width, image_height):
        default_x = image_width // 2
        default_y = image_height // 2
        default_r = min(image_width, image_height) // 4 + 200
        return (roi[0] if roi[0] != 0 else default_x,
                roi[1] if roi[1] != 0 else default_y,
                roi[2] if roi[2] != 0 else default_r)

    def validate_and_adjust_param(self, value, default, min_val, max_val):
        if value == 0:
            return default
        if min_val <= value <= max_val:
            return value
        logger.warning(
            f"Parameter value {value} is outside the valid range [{min_val}, {max_val}]. "
            f"Adjusting to the nearest valid value.")
        return max(min_val, min(value, max_val))

    def get_image_params(self, params, is_small_circle=False):
        default_params = self.SMALL_CIRCLE_PARAMS if is_small_circle else self.DEFAULT_PARAMS

        gray_lower = self.validate_and_adjust_param(params[0], default_params['gray'][0], 0, 255)
        gray_upper = self.validate_and_adjust_param(params[1], default_params['gray'][1], 0, 255)
        area_lower = self.validate_and_adjust_param(params[2], default_params['area'][0], 0, float('inf'))
        area_upper = self.validate_and_adjust_param(params[3], default_params['area'][1], 0, float('inf'))
        circularity_lower = self.validate_and_adjust_param(params[4], default_params['circularity'][0], 0, 1)
        circularity_upper = self.validate_and_adjust_param(params[5], default_params['circularity'][1], 0, 1)

        gray_lower, gray_upper = self._adjust_bounds(gray_lower, gray_upper, 'gray', default_params['gray'])
        area_lower, area_upper = self._adjust_bounds(area_lower, area_upper, 'area', default_params['area'])
        circularity_lower, circularity_upper = self._adjust_bounds(
            circularity_lower, circularity_upper, 'circularity', default_params['circularity'])

        return {
            'gray': (gray_lower, gray_upper),
            'area': (area_lower, area_upper),
            'circularity': (circularity_lower, circularity_upper)
        }

    def _adjust_bounds(self, lower, upper, param_name, default_values):
        if lower > upper:
            logger.warning(
                f"{param_name.capitalize()} lower ({lower}) > upper ({upper}), using defaults.")
            return default_values
        return lower, upper

    # =========================================================
    # 核心算法 —— OpenCV 实现
    # =========================================================
    def _preprocess(self, image, roi, params, erode_radius=0):
        """通用预处理：ROI 裁剪 + 灰度 + 二值化 + (可选)腐蚀 + 连通域筛选"""
        roi_x, roi_y, roi_radius = roi
        h, w = image.shape[:2]

        # 1. ROI 圆形 mask
        roi_mask = np.zeros((h, w), dtype=np.uint8)
        cv2.circle(roi_mask, (roi_x, roi_y), roi_radius, 255, -1)

        # 2. 灰度
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image

        # 3. 阈值：灰度在 [low, high] 之间 → 前景
        #    Halcon threshold(img, low, high) 等价于 cv2.inRange
        binary = cv2.inRange(gray, params['gray'][0], params['gray'][1])

        # 4. 仅保留 ROI 内
        binary = cv2.bitwise_and(binary, roi_mask)

        # 5. (可选) 腐蚀 —— 小圆用，去噪
        if erode_radius > 0:
            k_size = max(3, int(2 * erode_radius + 1))
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
            binary = cv2.erode(binary, kernel, iterations=1)

        # 6. 填洞：用闭运算或 floodFill
        binary = self._fill_holes(binary)

        return binary

    @staticmethod
    def _fill_holes(binary):
        """填充闭合区域内部的洞（等价于 Halcon 的 fill_up）"""
        # 方法：flood fill 从图像边缘开始填背景，然后反转 + OR
        h, w = binary.shape
        flooded = binary.copy()
        mask = np.zeros((h + 2, w + 2), dtype=np.uint8)
        cv2.floodFill(flooded, mask, (0, 0), 255)
        filled = binary | cv2.bitwise_not(flooded)
        return filled

    def _select_and_measure(self, binary, params):
        """找出满足 area + circularity 约束的唯一连通域，并返回其几何属性"""
        num, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
        if num <= 1:  # 只有背景
            return None

        candidates = []
        area_lo, area_hi = params['area']
        circ_lo, circ_hi = params['circularity']

        for i in range(1, num):
            area = stats[i, cv2.CC_STAT_AREA]
            if not (area_lo <= area <= area_hi):
                continue

            # 计算圆度 circularity = 4π·area / perimeter²
            comp_mask = (labels == i).astype(np.uint8) * 255
            contours, _ = cv2.findContours(comp_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            if not contours:
                continue
            perimeter = cv2.arcLength(contours[0], True)
            if perimeter <= 0:
                continue
            circularity = 4 * np.pi * area / (perimeter * perimeter)
            # Halcon 的 circularity 定义是 area / (π * max_radius²)，和上面公式略有差异
            # 这里用标准定义，客户的阈值可能要稍微调一下
            if not (circ_lo <= circularity <= circ_hi):
                continue

            candidates.append((i, area, circularity, comp_mask, contours[0]))

        if len(candidates) != 1:
            return None  # halcon 版也是 "count != 1 就 NG"
        return candidates[0]  # (label, area, circularity, mask, contour)

    @staticmethod
    def _inner_circle(mask):
        """最大内切圆：距离变换后取最大值点"""
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
        _, max_val, _, max_loc = cv2.minMaxLoc(dist)
        # 返回 (row, col, radius) 和 halcon 的 inner_circle 一致
        col, row = max_loc
        return row, col, max_val

    @staticmethod
    def _smallest_circle(contour):
        """最小外接圆 —— cv2.minEnclosingCircle"""
        (x, y), radius = cv2.minEnclosingCircle(contour)
        return y, x, radius  # row, col, radius

    @staticmethod
    def _orientation(mask):
        """区域主方向角 —— 用二阶中心矩计算（和 Halcon orientation_region 等价）"""
        m = cv2.moments(mask, binaryImage=True)
        if abs(m['mu20'] - m['mu02']) < 1e-6 and abs(m['mu11']) < 1e-6:
            return 0.0
        # phi = 0.5 * atan2(2 * mu11, mu20 - mu02)
        phi = 0.5 * np.arctan2(2.0 * m['mu11'], m['mu20'] - m['mu02'])
        return phi

    # =========================================================
    # 主处理函数
    # =========================================================
    def process_big_circle(self, image, roi, params):
        try:
            binary = self._preprocess(image, roi, params, erode_radius=0)
            selected = self._select_and_measure(binary, params)
            if selected is None:
                return image, 999, 999, (999, 999), 999, 999

            _, area, circularity, mask, contour = selected
            row, col, radius = self._inner_circle(mask)
            phi = self._orientation(mask)
            return image, area, circularity, (row, col), radius, phi
        except Exception as e:
            logger.error(f"process_big_circle error: {e}")
            return image, 999, 999, (999, 999), 999, 999

    def process_small_circle(self, image, roi, params):
        try:
            binary = self._preprocess(image, roi, params, erode_radius=1.5)
            selected = self._select_and_measure(binary, params)
            if selected is None:
                return image, 999, 999, (999, 999), 999, 999

            _, area, circularity, mask, contour = selected
            row, col, radius = self._smallest_circle(contour)
            return image, area, circularity, (row, col), radius, 0
        except Exception as e:
            logger.error(f"process_small_circle error: {e}")
            return image, 999, 999, (999, 999), 999, 999

    # =========================================================
    # 结果绘制（和 halcon 版基本一致，删掉了 himage_as_numpy_array）
    # =========================================================
    def draw_results(self, image, center, radius, angle_rad, roi, params, selected_area, selected_circularity, result,
                     is_small_circle=False):
        opencv_image = image.copy() if image is not None else np.zeros((100, 100, 3), dtype=np.uint8)
        if len(opencv_image.shape) == 2:
            opencv_image = cv2.cvtColor(opencv_image, cv2.COLOR_GRAY2BGR)
        height, width = opencv_image.shape[:2]

        cv2.line(opencv_image, (0, height // 2), (width, height // 2), (255, 255, 0), 3)
        cv2.line(opencv_image, (width // 2, 0), (width // 2, height), (255, 255, 0), 3)
        cv2.circle(opencv_image, (roi[0], roi[1]), roi[2], (255, 0, 255), 10)

        if center != (999, 999):
            cv2.circle(opencv_image, (int(center[0]), int(center[1])), int(radius), (0, 255, 0), 2)
            cv2.circle(opencv_image, (int(center[0]), int(center[1])), 5, (0, 0, 255), -1)

            if not is_small_circle:
                angle_deg = np.degrees(angle_rad)
                arrow_length = 300
                end_point = (
                    int(center[0] + arrow_length * np.cos(angle_rad)),
                    int(center[1] - arrow_length * np.sin(angle_rad))
                )
                cv2.arrowedLine(opencv_image, (int(center[0]), int(center[1])), end_point, (255, 0, 0), 10)
                cv2.putText(opencv_image, f"{angle_deg:.2f}",
                            (int(center[0]) + 10, int(center[1]) + 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)

        self._draw_info_text(opencv_image, roi, params, selected_area, selected_circularity)
        self._draw_result_text(opencv_image, result, center)
        return opencv_image

    def _draw_info_text(self, image, roi, params, selected_area, selected_circularity):
        info_lines = [
            f"ROI: x={roi[0]}, y={roi[1]}, r={roi[2]}",
            f"Gray: {params['gray'][0]}-{params['gray'][1]}",
            f"Area: {params['area'][0]}-{params['area'][1]}",
            f"Circularity: {params['circularity'][0]:.2f}-{params['circularity'][1]:.2f}",
            f"Selected: Area={selected_area:.0f}, Circularity={selected_circularity:.4f}"
        ]
        for i, line in enumerate(info_lines):
            cv2.putText(image, line, (10, 30 + i * 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    def _draw_result_text(self, image, result, center):
        if result == ProcessResult.OK:
            result_text, result_color = "OK", (0, 255, 0)
        elif result == ProcessResult.NG:
            result_text, result_color = "NG", (0, 0, 255)
        else:
            result_text, result_color = "Exception", (255, 0, 0)

        cv2.putText(image, result_text,
                    (int(center[0]) - 20, int(center[1]) + 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, result_color, 2)

    # =========================================================
    # 高级接口（task_manager 调的就是这两个）
    # =========================================================
    def process_big_circle_image(self, image, camera_info):
        try:
            height, width = image.shape[:2]  # OpenCV: (H, W, C)
            roi = self.get_roi_info(camera_info.get('roi', [0, 0, 0]), width, height)
            params = self.get_image_params(camera_info.get('params', [0] * 6))
            pixel_distance = camera_info.get('pixel_distance', 1)
            logger.info(
                f"[大圆] ROI=(x={roi[0]},y={roi[1]},r={roi[2]}) "
                f"gray={params['gray']} area={params['area']} "
                f"circ={params['circularity']} pixel_dist={pixel_distance}"
            )

            processed_image, selected_area, circularity, (row, col), radius, phi = \
                self.process_big_circle(image, roi, params)

            if selected_area == 999:
                logger.warning(f"[大圆] NG: no region matched")
                result = ProcessResult.NG
                center = (999, 999)
                angle = 999
            else:
                center = (col, row)
                angle = phi
                result = ProcessResult.OK

        except Exception as e:
            logger.error(f"处理大圆图像时发生错误: {str(e)}")
            result = ProcessResult.EXCEPTION
            center = (999, 999)
            angle = 999
            radius = 999
            processed_image = image
            selected_area = 999
            circularity = 999
            roi = self.get_roi_info(camera_info.get('roi', [0, 0, 0]), *image.shape[1::-1])
            params = self.get_image_params(camera_info.get('params', [0] * 6))

        finally:
            result_image = self.draw_results(processed_image, center, radius, angle, roi, params,
                                             selected_area, circularity, result, is_small_circle=False)
            result_center = center
            result_angle = angle

            if result == ProcessResult.OK:
                result_center = self.convert_to_center_coordinates(center, (width, height))
                result_center = (result_center[0] * pixel_distance, result_center[1] * pixel_distance)
                result_angle = np.degrees(angle)
                if result_angle < 0:
                    result_angle += 180
            return result, result_image, result_center, result_angle

    def process_small_circle_image(self, image, camera_info):
        try:
            height, width = image.shape[:2]
            roi = self.get_roi_info(camera_info.get('roi', [0, 0, 0]), width, height)
            params = self.get_image_params(camera_info.get('params', [0] * 6), is_small_circle=True)
            pixel_distance = camera_info.get('pixel_distance', 1)
            logger.info(
                f"[小圆] ROI=(x={roi[0]},y={roi[1]},r={roi[2]}) "
                f"gray={params['gray']} area={params['area']} "
                f"circ={params['circularity']} pixel_dist={pixel_distance}"
            )

            processed_image, selected_area, circularity, (row, col), radius, angle = \
                self.process_small_circle(image, roi, params)

            if selected_area == 999:
                logger.warning(f"[小圆] NG: no region matched")
                result = ProcessResult.NG
                center = (999, 999)
                angle = 999
            else:
                center = (col, row)
                angle = 0
                result = ProcessResult.OK

        except Exception as e:
            logger.error(f"处理小圆图像时发生错误: {str(e)}")
            result = ProcessResult.EXCEPTION
            center = (999, 999)
            angle = 999
            radius = 999
            processed_image = image
            selected_area = 999
            circularity = 999
            roi = self.get_roi_info(camera_info.get('roi', [0, 0, 0]), *image.shape[1::-1])
            params = self.get_image_params(camera_info.get('params', [0] * 6), is_small_circle=True)

        finally:
            result_image = self.draw_results(processed_image, center, radius, angle, roi, params,
                                             selected_area, circularity, result, is_small_circle=True)
            if result == ProcessResult.OK:
                center = self.convert_to_center_coordinates(center, (width, height))
                center = (center[0] * pixel_distance, center[1] * pixel_distance)
            return result, result_image, center, 0

    def convert_to_center_coordinates(self, point, image_size):
        """把图像左上角原点转换成以图像中心为原点"""
        center_x = image_size[0] / 2
        center_y = image_size[1] / 2
        new_x = point[0] - center_x
        new_y = center_y - point[1]  # y 轴方向相反
        return (new_x, new_y)

    # =========================================================
    # 显示链路（和 halcon 版完全一致，这部分本来也不依赖 halcon）
    # =========================================================
    _BAR_COLORS = {
        ProcessResult.OK: (0, 255, 0),
        ProcessResult.NG: (0, 0, 255),
        ProcessResult.EXCEPTION: (128, 128, 128),
    }
    _BAR_HEIGHT = 90

    @classmethod
    def _get_result_bar(cls, width, dtype, result):
        color = cls._BAR_COLORS.get(result, (128, 128, 128))
        key = (width, dtype.str, result)
        bar = cls._result_bar_cache.get(key)
        if bar is None:
            bar = np.full((cls._BAR_HEIGHT, width, 3), color, dtype=dtype)
            cls._result_bar_cache[key] = bar
        return bar

    @classmethod
    def add_result_bar(cls, image, result):
        height, width = image.shape[:2]
        if not isinstance(result, ProcessResult):
            logger.warning(f"Unexpected result type: {type(result)}. Using EXCEPTION color.")
            result = ProcessResult.EXCEPTION
        bar = cls._get_result_bar(width, image.dtype, result)
        try:
            return cv2.vconcat([image, bar])
        except Exception as e:
            logger.error(f"add_result_bar error: {e} (image {image.shape} bar {bar.shape})")
            return image

    @classmethod
    def combine_images(cls, images):
        """两路相机图左右拼接. canvas 缓存, 避免每帧 np.zeros 分配."""
        assert len(images) == 2, "Expected exactly two images"
        height, width = images[0].shape[:2]
        out_w = width * 2 + 10

        canvas = cls._combine_canvas_cache.get((height, out_w))
        if canvas is None:
            canvas = np.empty((height, out_w, 3), dtype=np.uint8)
            canvas[:, width:width + 10] = (255, 255, 255)  # 白色分隔线只画一次
            cls._combine_canvas_cache[(height, out_w)] = canvas

        canvas[:, :width] = images[0]
        canvas[:, width + 10:] = images[1]
        return canvas

    @classmethod
    def _get_company_bar(cls, width):
        bar = cls._company_bar_cache.get(width)
        if bar is not None:
            return bar

        current_dir = os.path.dirname(os.path.abspath(__file__))
        company_name_path = os.path.join(current_dir, 'company_name.png')
        if not os.path.exists(company_name_path):
            raise FileNotFoundError(f"公司名称图片未找到: {company_name_path}")
        raw = cv2.imread(company_name_path)
        if raw is None:
            raise ValueError(f"无法读取公司名称图片: {company_name_path}")

        if raw.shape[1] != width:
            scale = width / raw.shape[1]
            new_height = int(raw.shape[0] * scale)
            raw = cv2.resize(raw, (width, new_height), interpolation=cv2.INTER_AREA)

        cls._company_bar_cache[width] = raw
        return raw

    @classmethod
    def add_company_name(cls, image):
        bar = cls._get_company_bar(image.shape[1])
        return cv2.vconcat([bar, image])

    @classmethod
    def process_and_combine_images(cls, results):
        """根据 results 生成最终合成图.
        - 1 个相机: 跳过 combine, 直接用这张图 (省 ~40ms)
        - 2 个相机: 走合图流程
        """
        images = []
        target_size = (1024, 1280)
        for camera_num, result in results.items():
            if result is None:
                img = np.full((*target_size, 3), [0, 255, 0], dtype=np.uint8)
                process_result = ProcessResult.EXCEPTION
            else:
                process_result, result_image, _, _ = result
                img = result_image

            try:
                img = cls.add_result_bar(img, process_result)
            except Exception as e:
                logger.error(f"Error in add_result_bar for camera {camera_num}: {str(e)}")

            images.append(img)

        try:
            if len(images) == 1:
                combined_image = images[0]
            elif len(images) == 2:
                combined_image = cls.combine_images(images)
            else:
                combined_image = images[0] if images else None
            if combined_image is None:
                return None
            final_image = cls.add_company_name(combined_image)
            return final_image
        except Exception as e:
            logger.error(f"Error in combining images or adding company name: {str(e)}")
            return images[0] if images else None

    def convert_to_rgb565(self, image):
        """BGR → RGB565, 用 OpenCV 内置 C 实现, 比 numpy 位运算快 3~5 倍."""
        if image is None:
            return None
        # cv2.COLOR_BGR2BGR565 输出 shape=(H, W, 2), dtype=uint8
        return cv2.cvtColor(image, cv2.COLOR_BGR2BGR565)

    def save_rgb565_with_header(self, image, filename):
        """兼容 (H, W) uint16 和 (H, W, 2) uint8 两种 shape."""
        if image.ndim == 3:
            height, width, _ = image.shape
        else:
            height, width = image.shape
        header = np.array([width, height], dtype=np.int32)
        with open(filename, 'wb') as f:
            f.write(header.tobytes())
            f.write(image.tobytes())
