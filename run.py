"""JiaAgent 启动脚本。

用法（在项目根目录 D:\\JiaAgent 下执行）：
    .venv\\Scripts\\python.exe run.py

同一项目目录只允许一个实例。和以前不同，锁或 PID 状态文件不可用时
**拒绝启动**，而不是只打印警告后继续：固定端口不是可靠的单实例保证，
而且没有 PID 文件时停止脚本无法安全核对进程身份。

Windows 登录后自动启动见 docs/autostart.md。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 尽早记录：停止脚本会把它和系统报告的进程创建时间比对（±5 秒）。
# 如果改为写 PID 文件时才取 time.time()，导入 uvicorn/应用模块的耗时
# 会算进“启动偏差”，慢机器上可能让身份核对失败。解释器启动到这里只有
# 标准库导入，偏差远小于容差。
PROCESS_STARTED_AT = time.time()

# 不管在哪个目录执行这个脚本，都能正确找到 app 包
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.api.security import is_loopback_host  # noqa: E402
from app.settings import DATA_DIR, ensure_dirs, get_config, get_secrets  # noqa: E402
from app.shutdown_channel import ShutdownChannel  # noqa: E402
from app.single_instance import (  # noqa: E402
    acquire,
    clear_pid_file,
    write_pid_file,
)

PID_FILE = DATA_DIR / "jiaagent.pid"


def _pid_state(secrets, channel: ShutdownChannel | None) -> dict[str, object]:
    state: dict[str, object] = {
        "pid": os.getpid(),
        "started_at": PROCESS_STARTED_AT,
        "host": secrets.jia_host,
        "port": secrets.jia_port,
    }
    if channel is not None:
        state["shutdown_event"] = channel.name
    return state


def main() -> int:
    try:
        ensure_dirs()
    except Exception as exc:  # noqa: BLE001 - 数据目录不可写时不能继续
        print(f"拒绝启动：无法准备数据目录（{type(exc).__name__}）。")
        return 1

    # 单实例：自动启动和手动启动可能撞在一起；第二个进程在进入 uvicorn
    # 之前就正常退出，不留下半启动的监听或后台任务。
    try:
        lock = acquire(ROOT)
    except OSError as exc:
        # 无法判断“是否已有实例”时不能继续：固定端口只能兜底，不能
        # 代替进程级互斥。错误消息只保留类型和系统错误码，不回显环境。
        print(
            "拒绝启动：无法建立单实例锁"
            f"（{type(exc).__name__}，错误码 {getattr(exc, 'errno', '?')}）。"
        )
        return 1
    if lock is None:
        print("JiaAgent 已经在运行，本次启动已跳过。")
        return 0

    channel: ShutdownChannel | None = None
    pid_written = False
    try:
        try:
            secrets = get_secrets()
        except Exception as exc:  # noqa: BLE001 - 配置不可读时不能带着半套配置启动
            # 不打印原始异常：配置库的校验信息可能带出密钥值。
            print(f"拒绝启动：读取运行配置失败（{type(exc).__name__}）。")
            return 1

        if not is_loopback_host(secrets.jia_host):
            print("=" * 56)
            print(f"  拒绝启动：JIA_HOST={secrets.jia_host!r} 不是本机回环地址。")
            print("  JiaAgent 没有多用户认证，只允许绑定 127.0.0.1、localhost 或 ::1。")
            print("  请把 .env 里的 JIA_HOST 改回 127.0.0.1 后重试。")
            print("=" * 56)
            return 2

        try:
            channel = ShutdownChannel.create(ROOT)
        except Exception as exc:  # noqa: BLE001 - 关闭通道是可选增强，不能阻止服务启动
            # 没有事件时停止脚本会在核对身份后强制结束主进程；这里仍然
            # 启动，但不把“可优雅关闭”写进 PID 状态。只记录异常类型。
            print(
                "警告：无法建立本机关闭通道"
                f"（{type(exc).__name__}）；停止时将只能强制结束主进程。"
            )
            channel = None
        try:
            write_pid_file(PID_FILE, _pid_state(secrets, channel))
        except (OSError, ValueError) as exc:
            print(
                "拒绝启动：无法写入 PID 状态文件 "
                f"{PID_FILE}（{type(exc).__name__}）。"
            )
            return 1
        pid_written = True

        return _serve(secrets, channel)
    finally:
        # 只清理由本次启动成功写下的文件；写失败时绝不动可能属于
        # 其他实例或上一次运行的旧文件。
        if pid_written:
            clear_pid_file(PID_FILE, os.getpid())
        if channel is not None:
            channel.close()
        lock.release()


def _serve(secrets, channel: ShutdownChannel | None) -> int:
    try:
        config = get_config()
        model_info = f"{config.default_provider} ({config.provider().model})"
    except Exception as exc:  # noqa: BLE001 - 配置有问题只提示，不阻止启动
        # 只记录类型，避免异常消息里的配置内容进入启动日志。
        model_info = f"读取配置失败（{type(exc).__name__}）"

    host, port = secrets.jia_host, secrets.jia_port

    # IPv6 地址在 URL 里要加方括号，否则 http://::1:8000 是无效地址。
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host

    print("=" * 56)
    print("  JiaAgent（佳）正在启动")
    print(f"  默认模型：{model_info}")
    print(f"  网页地址：http://{display_host}:{port}")
    print("  停止服务：在这个窗口按 Ctrl+C")
    print("=" * 56)

    # 用 Server 实例而不是 uvicorn.run()，这样本机关闭事件可以设置
    # should_exit，让 uvicorn 走正常的 lifespan 清理流程。
    from app.main import app as fastapi_app  # noqa: PLC0415

    server = uvicorn.Server(
        uvicorn.Config(
            fastapi_app,
            host=host,
            port=port,
            log_level=secrets.jia_log_level.lower(),
        )
    )
    if channel is not None:
        channel.attach(server)
    server.run()
    if not getattr(server, "started", False):
        # Bind failures normally raise SystemExit from uvicorn itself; this
        # also catches a server that returned without ever listening, so the
        # auto-start task does not record a false success.
        print(
            "启动失败：uvicorn 未能开始监听，请检查端口占用和 logs/jiaagent.log。"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
