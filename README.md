# 测试分支（无 Halcon，用 OpenCV 替代）

本分支是生产主分支的**无 halcon 版本**，完整保留相机（Hikvision）和 PLC（Modbus）功能，**圆形识别用 OpenCV 重写**。

## 和主分支的差异

| 模块 | 主分支 | 本分支 |
|------|-------|-------|
| 相机 SDK (Hikvision MVS) | ✅ | ✅ 一致 |
| PLC (Modbus TCP) | ✅ | ✅ 一致 |
| 任务调度 (task_manager) | ✅ | ✅ 一致 |
| 显示链路 (rgb565 + C) | ✅ | ✅ 一致 |
| **圆形识别算法** | **Halcon** | **OpenCV** |
| deploy/ 部署脚本 | ✅ | ✅ 一致 |
| 独立显示测试 (test_display.py) | ❌ | ✅ 额外 |

**本分支不合并回主分支**，仅用于测试机（无 halcon 环境）跑完整生产流程。

## OpenCV 算法映射表

Halcon 调用 → OpenCV 等价实现：

| Halcon | OpenCV 等价 |
|--------|-----------|
| `gen_circle` + `reduce_domain` | `cv2.circle` 画 mask + `cv2.bitwise_and` |
| `rgb1_to_gray` | `cv2.cvtColor(..., COLOR_BGR2GRAY)` |
| `threshold(img, low, high)` | `cv2.inRange` |
| `erosion_circle(1.5)` | `cv2.erode` + 圆形 kernel |
| `connection` + `fill_up` | `cv2.connectedComponentsWithStats` + `floodFill` 补洞 |
| `select_shape` | 手动遍历按 area+circularity 筛 |
| `inner_circle` (最大内切圆) | `cv2.distanceTransform` 取最大值点 |
| `smallest_circle` (最小外接圆) | `cv2.minEnclosingCircle` |
| `orientation_region` | `cv2.moments` + `atan2(2μ11, μ20-μ02)/2` |
| `area_center` / `circularity` | 直接从 stats / moments 算 |

**注意**：
- OpenCV 的 **"圆度" 定义** 用的是 `4π·area / perimeter²`
- Halcon 的 **"圆度" 定义** 用的是 `area / (π · max_radius²)`
- 两者在接近完美圆时都是 1.0，但**偏离时数值不同**
- 客户 PLC 设的圆度阈值（比如 0.5~1.0）可能需要**在现场调一下**

## 部署流程

### 1. 开发机编译（需要装 opencv 但不需要 halcon）

```bash
pip install pyinstaller numpy opencv-python-headless Pillow pyModbusTCP
pyinstaller --clean -y main.spec
```

产物：`dist/main`

### 2. 传到测试机

```bash
scp dist/main pi@<测试机>:/home/pi/
rsync -av deploy/ pi@<测试机>:/home/pi/deploy/
```

### 3. 部署（测试机）

```bash
ssh pi@<测试机>
cd /home/pi/deploy
chmod +x *.sh
./install.sh
```

## 运行两种模式

### 模式 A：完整生产模式（相机 + PLC + OpenCV 识别）

跟主分支部署方式完全一样：
```bash
sudo systemctl start main.service
sudo systemctl status main.service
tail -f /home/pi/my_app.log
```

日志里的 `[大圆]` / `[小圆]` 参数都一致，但算法后端换成了 OpenCV。

### 模式 B：纯显示链路测试（不需要相机 / PLC）

```bash
python3 test_display.py --interval 0 --count 30 --profile
```

用 OpenCV 画假图走完整显示管线，用来验证 tmpfs + inotify + C image_updater 这条链路。

## 算法精度 —— 关键注意事项

OpenCV 版和 Halcon 版在**理想输入**（清晰、对比度高、ROI 准确）下结果**几乎一致**。但下面几种情况可能有差异，**要去现场调**：

1. **圆度阈值**：定义不同，**OpenCV 的 0.7 ≈ Halcon 的 0.8**（粗略）
2. **`erosion_circle` 半径**：OpenCV `erode` 的 kernel 大小是 `2r+1`，Halcon 是连续半径，**相似但不完全等价**
3. **连通域数量**：OpenCV 8-连通（默认）≈ Halcon 默认，但边界处理可能不同
4. **`inner_circle`**：OpenCV 用距离变换，对**非凸形状**估算可能和 Halcon 略有差异

**建议测试步骤**：
- 先用同一张工件图，两边都跑一遍，对比 `[大圆] ROI=... gray=... area=...` 输出的 `center/area/circularity`
- 如果差距在 5% 以内，直接上生产
- 如果差距大，到 `image_processing.py` 调 `_select_and_measure` 里的圆度判据

## 主分支背景文档

显示链路改动、部署步骤、踩过的坑见 [`DISPLAY_CHANGES.md`](./DISPLAY_CHANGES.md)。

## FAQ

**Q: 怎么切回主分支（halcon 版）**
```bash
git checkout claude/fix-camera-exposure-timing-wxLbn
```

**Q: test_display.py 还能用吗**
能，没改过，跟主分支的 test_display.py 独立。适合**没接相机时**验证显示链路。

**Q: 这个分支能当生产用吗**
**目前不建议**。OpenCV 算法部分没经过产线真实工件校验。先当作"halcon 不可用时的降级方案"或"算法对比基准"用。
