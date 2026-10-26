"""启动 run.py，并把它的 stdout/stderr 脱敏后转给启动器。

为什么需要这一层：
  ``start_jia.ps1`` 会把 Python 的输出写进 ``logs/autostart.log``。uvicorn
  和第三方库的异常、URL 可能带出 token/验证码。PowerShell 端做正则既不
  好维护，也容易和应用的脱敏规则分叉，所以用同一个 Python 环境过滤。

这一层不是安全边界：它只保护普通启动日志，不能阻止同用户进程读取配置。
子进程仍是真正的 Jia（PID 文件记录的是子进程），停止脚本不会结束这一层
的父进程树。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.log_redaction import redact_log_line  # noqa: E402

# Jia 退出后，最多再等这么久收取管道里最后几行。用户应用即使仍持有
# 写端，也不能让这一层无限等待，否则计划任务永远结束不了。
_DRAIN_SECONDS = 2.0


def _pump(stream, sink) -> None:
    """在后台线程逐行读取并脱敏，直到 EOF 或流被关闭。"""
    try:
        for line in stream:
            try:
                safe = redact_log_line(line.rstrip("\r\n"))
            except Exception:  # noqa: BLE001 - 脱敏失败时不能退回原始行
                safe = "[日志行无法脱敏，已隐藏]"
            sink(safe)
    except (OSError, ValueError):
        # 读取端被关闭（例如整个进程正在退出）时安静结束。
        return


def _run_child(child, sink, drain_timeout: float = _DRAIN_SECONDS) -> int:
    """等主进程退出，然后最多再 drain_timeout 秒收尾，绝不无限等 EOF。"""
    reader = None
    if child.stdout is not None:
        reader = threading.Thread(
            target=_pump,
            args=(child.stdout, sink),
            name="jia-log-reader",
            daemon=True,
        )
        reader.start()

    code = child.wait()
    if reader is not None:
        reader.join(timeout=drain_timeout)
    return int(code)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    run_py = ROOT / "run.py"
    if not run_py.is_file():
        print("JiaAgent 启动失败：找不到 run.py", flush=True)
        return 2

    try:
        child = subprocess.Popen(
            [sys.executable, str(run_py)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        print(
            f"JiaAgent 启动失败：无法创建子进程（{type(exc).__name__}）",
            flush=True,
        )
        return 2

    try:
        assert child.stdout is not None
        return _run_child(child, lambda text: print(text, flush=True))
    except KeyboardInterrupt:
        # 子进程在同一个控制台里，会自己收到 Ctrl+C 并走 uvicorn 的清理。
        try:
            return int(child.wait(timeout=20))
        except subprocess.TimeoutExpired:
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
