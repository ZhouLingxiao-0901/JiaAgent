# capturekit 依赖记录

`inspect_screen` 的 `window` / `auto` 范围用 Windows Graphics Capture
按 HWND 捕获单个应用窗口。实现这一点的库是 **capturekit**。

## 固定版本

| 项目 | 值 |
| --- | --- |
| 包名 | `capturekit` |
| 版本 | `0.1.0`（固定，不使用 `>=`） |
| 来源 | PyPI：<https://pypi.org/project/capturekit/0.1.0/> |
| 许可证 | MIT |
| Python | `>=3.10`（本项目 `.venv` 是 3.13） |
| 运行时依赖 | `numpy>=1.24` |
| 本机锁定的 sdist | `capturekit-0.1.0.tar.gz` |
| sdist SHA256 | `c251b9a9d2089a1bdef0836c00e2aecff0cb26d0fa72a272b638af6f23af36f3` |

安装：

```bat
.venv\Scripts\python.exe -m pip install "capturekit==0.1.0"
```

从本地 sdist 安装前可核对：

```bat
certutil -hashfile capturekit-0.1.0.tar.gz SHA256
```

## 本项目实际使用的 API

只用下面这几项，全部是「抓一帧」：

```python
import capturekit as ck

win = ck.get_window(hwnd)
with ck.ScreenCapture(ck.WindowSource(win, client_area_only=False, border=False)) as cap:
    frame = cap.grab(timeout=3.0)      # frame.data 是 (h, w, 4) uint8 BGRA
```

- `ck.get_window(hwnd)`：确认句柄有效，并读 `is_visible` / `is_minimized` /
  `capture_excluded`。
- `ck.ScreenCapture(...)`：上下文管理器，离开 `with` 一定释放捕获会话和后台线程。
- `cap.grab(timeout=...)`：只抓一帧，不循环、不录制。

## 明确不使用的部分

- **不使用 `Recorder`**：不录制视频、不生成 MKV/MP4。
- **不使用 `[ffmpeg]` 扩展**：不安装 `imageio-ffmpeg`，不启动 FFmpeg 进程。
- **不使用音频 API**：不采集麦克风、系统声音、单应用声音。
- **没有后台录屏**：每次截图都由一次 `inspect_screen` 调用触发；
  用户拒绝时连 `grab()` 都不会被调用。
- **不使用 PrintWindow**：capturekit 只走 WGC / DXGI 的官方路径。

## 已知的 Alpha 风险

1. **上游标注 Development Status 3 - Alpha**，只有 `0.1.0` 这一个发布版本。
   API 和内部实现都可能在后续版本变化，所以这里**固定精确版本**，
   升级前必须重新跑 `scripts/test_vision_wincapture.py` 和真实窗口验证。
2. **通过 ctypes 直接调用 WinRT / D3D11 ABI**（纯 Python，无编译扩展）。
   这类实现依赖 Windows 版本的具体行为，系统大版本升级后需要回归验证。
3. **WGC 的窗口捕获要求 Windows 10 1903（build 18362）及以上**；
   更老的系统会在 `ScreenCapture` 打开时失败，佳会明确报错，不会降级。
4. **最小化窗口没有新帧**：DWM 不为最小化窗口渲染，WGC 不会提供新画面。
   本项目在捕获前就拒绝最小化窗口，不依赖「返回最后一帧」的行为。
5. **声明禁止捕获的窗口**（`WDA_EXCLUDEFROMCAPTURE`，例如密码管理器、
   DRM 播放器）会返回空白。capturekit 尊重这个声明，佳也不会绕过；
   画面校验会把空白/全黑判成失败。
6. **移除捕获边框依赖系统**：`border=False` 在 Windows 11 / build 20348+
   才可能生效，系统可以拒绝；拒绝时只是仍显示系统边框，不影响内容。
7. **提权窗口、受保护内容、安全桌面、远程桌面、多显卡/混合显卡**等场景
   没有在本项目里逐一验证。失败时必须如实报错，不能假装成功。
8. **没有长期支持承诺**：包本身没有给出稳定的维护周期；本项目把它当成
   一个可替换的捕获后端，所有调用都包在 `app/vision/wincapture.py` 里。

## 隐私与失败边界

- 捕获结果只在内存里；不写磁盘、不写 SQLite、不写日志、不写调试文件。
- 全黑、可疑单色、颜色过少、尺寸异常的画面都**不算成功**。
- 窗口捕获失败时**绝不**退回 PrintWindow，也**绝不**退回整桌截图；
  只有用户明确选择 `scope=desktop` 时才走整桌截图。
- 用户确认之前不调用 `grab()`，也不会把任何画面发给视觉模型。

## 本机验证记录

- 2026-10-18：用 `D:\wgc_probe` 的独立脚本对 VS Code 和 Edge 做了
  「正常显示 / 被完全遮挡」的前后对比。遮挡后 WGC 画面与遮挡前高度一致，
  与屏幕上的遮挡物明显不同，确认拿到的仍是目标窗口自身内容。
- 该验证只用于确认 WGC 行为，不修改 JiaAgent 主项目。
