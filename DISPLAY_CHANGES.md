# 显示链路改动记录

**分支**：`claude/fix-camera-exposure-timing-wxLbn`

本文记录从视觉程序到屏幕显示这一段的改动，包括代码、部署配置和现场踩的坑。

---

## 目标

视觉程序每次拍照完生成一张 rgb565 图，C 端 `image_updater` 通过 `inotify` 监听文件变化并把图画到 framebuffer（headless 系统）。原来的实现有三个问题：

1. **eMMC 每秒写入数 MB**，长期运行会加速磨损
2. **Python 每帧做重复劳动**（反复读公司 logo、反复分配结果条内存、BGR→RGB 多一次拷贝）
3. **合图和写盘阻塞 asyncio 事件循环**，双相机并发时互相卡

---

## 代码改动

### 1. `image_processing.py` — 类级缓存 + 跳转换

| 方法 | 改动 | 说明 |
|------|------|------|
| `ImageProcessor` 类属性 | 新增 `_company_bar_cache` `_result_bar_cache` 字典 | 类级缓存，所有实例共享 |
| `_get_company_bar(width)` | 新增 | 按目标宽度缓存已 resize 的公司条，首帧加载一次之后零开销 |
| `add_company_name(image)` | 从 `@staticmethod` 改为 `@classmethod`，走缓存 | 不再每帧 `cv2.imread` + `cv2.resize` |
| `_get_result_bar(width, dtype, result)` | 新增 | 缓存 OK/NG/EXCEPTION 三种颜色条 |
| `add_result_bar(image, result)` | 改用 `_get_result_bar`，去掉 3 条噪音 debug 日志 | 每次不再 `np.full` 新建数组 |
| `convert_to_rgb565(image)` | 跳过 `cv2.cvtColor(BGR→RGB)`，直接按 BGR 通道拆位 | 省一次全图内存拷贝 |

**单帧后处理耗时**：~200ms → ~100ms

### 2. `task_manager.py` — 全线程化 + 并行化

| 位置 | 改动 | 说明 |
|------|------|------|
| `process_combined_results()` | 整体包进 `asyncio.to_thread(self._combine_and_save, ...)` | 原先 `convert_to_rgb565` 和 `save_rgb565_with_header` 是**同步**调用，会阻塞 asyncio 事件循环，双相机并发时另一路协程被卡住 |
| 新增 `_combine_and_save()` | 把 合图 + 转 rgb565 + 存盘 三步打包 | 一次 `to_thread` 切换代价被多次操作摊薄 |
| `process_single_capture` 和 `process_continuous_capture` | `write_result_to_plc` 和 `process_combined_results` 改成 `asyncio.gather(...)` 并行 | 写 PLC（Modbus 网络）和合图存盘（本地磁盘）是独立链路，并行省 ~20~30ms/次 |

### 3. `log_config.py` — 日志格式清晰化

```python
formatter = logging.Formatter(
    fmt='%(asctime)s.%(msecs)03d | %(levelname)-5s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
```

毫秒精度 + 等级左对齐 + 管道分隔，便于 `grep` 和目视扫读。

### 4. 其他相关（camera_base / camera_manager / plc_manager / plc_base）

- 所有日志加 `[Cam1]` `[Cam2]` `[PLC]` `[SYS]` `[大圆]` `[小圆]` 前缀，双相机并发日志一眼分辨
- 原来每次曝光变更打 3 条 INFO（camera_base + camera_manager + task_manager 各一条），去重到只保留 camera_base 最底层那条
- `image_processing.add_result_bar` 里 3 条 `Entering add_result_bar method` / `Image shape` / `Result type` DEBUG 噪音删除

---

## 部署配置改动

### 1. 文件存储改到 tmpfs（`/dev/shm`）

**为什么**：原来 `output_image.rgb565` 落在 eMMC，每小时 ~20GB 写入，长期运行会磨损存储。挪到 `/dev/shm` 后写 RAM，零磁盘 I/O。

**做法**：在 `/home/pi/output_image.rgb565` 位置建软链接指向 `/dev/shm/output_image.rgb565`，Python 和 C 都不用改代码，透明生效。

```bash
# 首次部署（或手动恢复）
sudo pkill -f main
sudo pkill image_updater
cd /home/pi/
[ -f output_image.rgb565 ] && [ ! -L output_image.rgb565 ] && mv output_image.rgb565 output_image.rgb565.bak
sudo touch /dev/shm/output_image.rgb565
sudo chmod 666 /dev/shm/output_image.rgb565
ln -sfn /dev/shm/output_image.rgb565 output_image.rgb565
```

### 2. `/etc/systemd/system/main.service` —— 开机自愈

`/dev/shm` 在重启后会被清空，需要 systemd 在启动 main 前重建文件。

```ini
[Service]
Type=simple
WorkingDirectory=/home/pi/
ExecStartPre=/bin/sh -c 'touch /dev/shm/output_image.rgb565 && chmod 666 /dev/shm/output_image.rgb565'
ExecStartPre=/bin/sleep 4
ExecStart=/home/pi/main
StandardOutput=tty
TTYPath=/dev/tty2
TTYReset=yes
Restart=always
Environment=MVCAM_SDK_PATH=/opt/MVS
Environment=MVCAM_COMMON_RUNENV=/opt/MVS/lib
Environment=LD_LIBRARY_PATH=/opt/MVS/lib/aarch64:/opt/halcon/lib/aarch64-linux
LimitNOFILE=500000
LimitNPROC=65535
LimitSTACK=32M
```

### 3. `/etc/systemd/system/image_updater.service` —— 开机自愈 + 死循环保活

原来的 service 有两个问题：
- **image_updater 比 main 先启动**时，`/dev/shm/output_image.rgb565` 不存在，`inotify_add_watch` 失败 → 进程立即退出
- bash 包装层用 `exit 0`，systemd 以为成功退出，不会重启

```ini
[Unit]
Description=Image Updater Service
After=network.target main.service
Wants=main.service

[Service]
Type=simple
User=root
ExecStartPre=/bin/sh -c 'touch /dev/shm/output_image.rgb565 && chmod 666 /dev/shm/output_image.rgb565'
ExecStartPre=/bin/sleep 8
ExecStart=/bin/bash -c 'while true; do if [ -e /dev/fb0 ]; then /home/pi/image_updater; sleep 5; fi; done'
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

**关键点**：
- `After=main.service` + `Wants=main.service` —— 让 main 先起，它会 touch tmpfs 文件
- `ExecStartPre=touch ...` —— 自己也建一次，双保险
- `while true; ... ; sleep 5;` **去掉 exit 0** —— image_updater 挂了之后 5 秒后自动拉起，不再一次死永久死

---

## 验证方法

### 验收显示链路工作

```bash
# 1. image_updater 正在跑
sudo systemctl status image_updater.service

# 2. tmpfs 文件被写入（mtime 随拍照更新）
stat /dev/shm/output_image.rgb565

# 3. 软链接指向正确
ls -la /home/pi/output_image.rgb565
# 期望：lrwxrwxrwx ... -> /dev/shm/output_image.rgb565

# 4. main 在用新代码跑
ps -eo pid,lstart,cmd | grep /home/pi/main | grep -v grep
tail -100 /home/pi/my_app.log | grep -E "\[Cam[12]\]|\[大圆\]|\[小圆\]"
```

### 验收 eMMC 不再被写

```bash
# 安装 sysstat 后
sudo apt install -y sysstat
iostat -x 1 30 /dev/mmcblk0

# 关注 wkB/s 列（每秒写 KB）
# 期望：接近 0，只剩系统日志等后台写入
```

### 测量单次 capture 耗时

```bash
grep -E "capture start|capture done" /home/pi/my_app.log | tail -30
```

同相机的 `capture start` 和 `capture done` 时间差就是本次耗时。

### 测量显示链路耗时

```bash
sudo journalctl -u image_updater --no-pager | grep -E "Reading|Drawing|Detected" | tail -20
```

---

## 实测效果（NanoPi-R5S-LTS / 1600×900 屏 / RK3568）

### 单次 capture（Python 视觉链路）

| 场景 | 改前 | 改后 |
|------|------|------|
| 单相机 | ~310ms | **~255ms** |
| 双相机并发 | ~310~450ms | **~270~310ms** |

### 显示链路（inotify → framebuffer）

| 阶段 | 改前（eMMC）| 改后（tmpfs）|
|------|-----------|-------------|
| C 读文件（6.1MB）| ~20~30ms | **~7~9ms** |
| C 缩放 + 转 ARGB + 画屏 | ~55ms | ~55ms（未改）|
| 单帧总延迟 | ~75~85ms | **~62~64ms** |

### 其他

- **eMMC 写入量**：~20GB/小时 → **0**
- **日志可读性**：双相机日志交织问题消除，毫秒级时间戳
- **曝光切换正确性**：增加读回校验 + 清缓冲 + 丢帧，消除"旧曝光拍新帧"竞态
- **Modbus 读原子性**：单次请求读完 status + config，消除跨寄存器错配

---

## 部署 Checklist

**推荐直接用 `deploy/` 下的自动化脚本**，见 [deploy/README.md](deploy/README.md)。

首次部署：
```bash
scp dist/main pi@<NanoPi>:/home/pi/
rsync -av deploy/ pi@<NanoPi>:/home/pi/deploy/
ssh pi@<NanoPi> 'cd /home/pi/deploy && chmod +x *.sh && ./install.sh'
```

日常发版（只更 main 二进制）：
```bash
scp dist/main pi@<NanoPi>:/tmp/main.new
ssh pi@<NanoPi> 'cd /home/pi/deploy && ./update-main.sh /tmp/main.new'
```

脚本自动处理：停服务、备份、替换、启动、失败自动回滚。

如果手动部署，按下面 checklist 走：

1. **代码侧**
   - [ ] `git pull` 到最新
   - [ ] `pyinstaller --clean -y main.spec`
   - [ ] 新 `dist/main` 传到 NanoPi 替换 `/home/pi/main`

2. **首次部署时的一次性配置**
   - [ ] 建软链接 `/home/pi/output_image.rgb565 -> /dev/shm/output_image.rgb565`
   - [ ] 安装 `deploy/main.service` 到 `/etc/systemd/system/`
   - [ ] 安装 `deploy/image_updater.service` 到 `/etc/systemd/system/`
   - [ ] `sudo systemctl daemon-reload`
   - [ ] `sudo systemctl enable main.service image_updater.service`

3. **重启生效**
   - [ ] `sudo systemctl restart main.service`
   - [ ] `sudo systemctl restart image_updater.service`
   - [ ] 观察 `tail -f /home/pi/my_app.log` 无 `Permission denied`
   - [ ] 目视确认屏幕能刷新

---

## 未来可选优化（不急做）

### Phase 2 —— Python 输出目标分辨率

当前：Python 出 **2570×1254** 的图，C 缩放到 **1600×900** 显示。如果 Python 直接按屏幕尺寸出图，C 可以跳过缩放：

- 文件从 6.1MB 降到 2.88MB
- C 缩放（~30ms）跳过
- 显示延迟 62ms → ~15ms

**缺点**：Python 侧需要读 `/sys/class/graphics/fb0/virtual_size`，业务逻辑稍微和显示耦合，**通用性下降**（不同屏幕要 Python 重新识别）。

**结论**：保留当前"C 端自适应缩放"的设计，**通用性优先**，Phase 2 先不做。

### 其他小优化（可做可不做）

| 改动 | 收益 | 风险 |
|------|------|------|
| 写 PLC 块化（17 次单写 → 1 次 `write_multiple_registers`）| -15ms/次，且避免 PLC 读到半写状态 | 低 |
| `combine_images` canvas 复用（不每帧 `np.zeros`）| -5~10ms/次 | 低 |
| `pixel_distance=0` 打 WARNING | 帮客户早发现 PLC 没写该值 | 零 |
| image_updater C 代码清理（printf 宏开关、预分配 buffer、去掉每事件起线程）| -15~30ms/次 | 中（要改 C 并重新测） |

---

## 遇到过的坑（供以后排查）

### 坑 1：`Permission denied: 'output_image.rgb565'`

**症状**：软链接建好后，Python 还是写不进。

**原因**：软链接目标 `/dev/shm/output_image.rgb565` 不存在。Linux 4.19+ 默认开启 `fs.protected_regular=2`，**即使 root 也不能通过悬空软链接在 sticky world-writable 目录（`/dev/shm`）里创建文件**。

**修法**：手动 `touch /dev/shm/output_image.rgb565 && chmod 666`，或者在 systemd 服务里加 `ExecStartPre=touch ...`。

### 坑 2：image_updater 启动 20ms 就死

**症状**：`systemctl status image_updater` 显示 `Active: inactive (dead)` 但 `Main PID ... status=0/SUCCESS`。

**原因**：image_updater 启动时 `/dev/shm/output_image.rgb565` 不存在，`inotify_add_watch` 失败，C 代码里 `exit(EXIT_FAILURE)`，然后 bash 包装 `exit 0` 让 systemd 以为成功退出，不重启。

**修法**：service 里 ExecStart 改成 `while true; ...; sleep 5; done` 死循环保活；同时用 `After=main.service` 让 main 先起。

### 坑 3：inotify 只认 inode

**症状**：改软链接后 image_updater 不再收到事件。

**原因**：`inotify_add_watch()` 监听的是 inode 而不是路径。原 `output_image.rgb565` 被 `rm` 后，旧 inode 消失，watch 失效；之后创建的新文件（tmpfs 上的）是新 inode，旧 watch 感知不到。

**修法**：每次改软链接或重建 tmpfs 文件后，**必须 restart image_updater**。

---

**文档更新时间**：伴随分支 `claude/fix-camera-exposure-timing-wxLbn` 最新一次推送。
