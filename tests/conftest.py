import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def circle_image():
    """一张理想白圆背景，中心 (250, 250)，半径 100，BGR 500x500."""
    img = np.zeros((500, 500, 3), dtype=np.uint8)
    cv2.circle(img, (250, 250), 100, (255, 255, 255), -1)
    return img


@pytest.fixture
def make_circle_image():
    def _make(width=500, height=500, cx=250, cy=250, radius=100, fill=255):
        img = np.zeros((height, width, 3), dtype=np.uint8)
        cv2.circle(img, (cx, cy), radius, (fill, fill, fill), -1)
        return img
    return _make


@pytest.fixture
def blank_image():
    return np.zeros((500, 500, 3), dtype=np.uint8)


@pytest.fixture
def big_circle_params():
    return {'gray': (0, 120), 'area': (10000, 100000), 'circularity': (0.7, 1.0)}


@pytest.fixture
def small_circle_params():
    return {'gray': (0, 120), 'area': (1000, 50000), 'circularity': (0.5, 1.0)}
