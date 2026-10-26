"""Windows 自动启动的离线回归检查。

不注册任务、不启动 Jia、不联网、不读真实凭证、不结束任何真实进程。覆盖：
  1. 单实例锁：同一目录第二次获取失败，释放后可以再拿；
  2. PID 状态：JSON/旧格式/损坏文件、只清除属于自己的状态；
  3. 本机关闭事件：创建、信号、触发 uvicorn should_exit、关闭后失效；
  4. 启动失败时的状态一致性：锁错误、已有实例、配置错误、PID 写入失败、
     启动异常都必须拒绝启动或干净收尾，绝不动别的实例的状态；
  5. 自动启动日志脱敏：token、API key、二维码、验证码、授权码不落日志；
  6. `.env` 里的 JIA_WECHAT_* 会被读到，进程环境变量优先；
  7. Windows 脚本静态约束，并调用 PowerShell 回归测试（若可用）。

用法：
  .venv\\Scripts\\python.exe scripts\\test_autostart.py
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.wechat.runtime as runtime_module  # noqa: E402
import run as run_module  # noqa: E402
from app.log_redaction import redact_log_line  # noqa: E402
from app.shutdown_channel import (  # noqa: E402
    EVENT_PREFIX,
    ShutdownChannel,
    event_name,
)
from app.single_instance import (  # noqa: E402
    acquire,
    clear_pid_file,
    read_pid_file,
    write_pid_file,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


class FakeLock:
    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class FakeChannel:
    def __init__(self) -> None:
        self.name = EVENT_PREFIX + "0123456789ab"
        self.closed = False
        self.attached = False

    def attach(self, server: object) -> None:
        self.attached = True

    def close(self) -> None:
        self.closed = True


class FakeChannelFactory:
    def __init__(self) -> None:
        self.instances: list[FakeChannel] = []

    def create(self, _root: Path) -> FakeChannel:
        channel = FakeChannel()
        self.instances.append(channel)
        return channel


class RaisingChannelFactory:
    def create(self, _root: Path) -> FakeChannel:
        raise OSError(6, "intentional event failure")


def test_single_instance() -> None:
    print()
    print("单实例锁：")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        root.mkdir()
        lock_path = Path(tmp) / "project.lock"
        first = acquire(root, lock_path=lock_path)
        check("首次获取单实例锁", first is not None)
        second = acquire(root, lock_path=lock_path)
        check("同一目录第二个实例拿不到锁", second is None)
        if first is not None:
            first.release()
        third = acquire(root, lock_path=lock_path)
        check("释放后可以再次获取", third is not None)
        if third is not None:
            third.release()

        other_root = Path(tmp) / "other"
        other_root.mkdir()
        other = acquire(other_root, lock_path=Path(tmp) / "other.lock")
        check("不同项目目录互不影响", other is not None)
        if other is not None:
            other.release()


def test_pid_file() -> None:
    print()
    print("PID 状态文件：")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "jiaagent.pid"
        state = {
            "pid": 12345,
            "started_at": 1700000000.5,
            "host": "127.0.0.1",
            "port": 8000,
            "shutdown_event": EVENT_PREFIX + "0123456789ab",
        }
        write_pid_file(path, state)
        loaded = read_pid_file(path)
        check(
            "JSON 状态写入后可读回",
            loaded is not None
            and loaded.get("pid") == 12345
            and abs(float(loaded.get("started_at")) - 1700000000.5) < 0.001,
        )
        check(
            "JSON 状态保留关闭事件名",
            loaded is not None and loaded.get("shutdown_event") == state["shutdown_event"],
        )

        rejected = []
        for bad in (
            {"pid": 0},
            {"pid": True},
            {},
            {"pid": "not-a-pid"},
            {"pid": 1, "started_at": 0},
            {"pid": 1, "started_at": "not-a-number"},
            {"pid": 1, "port": 0},
            {"pid": 1, "port": 70000},
        ):
            try:
                write_pid_file(path, bad)
            except ValueError:
                rejected.append(bad)
        check("拒绝非法 PID/启动时间/端口", len(rejected) == 8)

        path.write_text("not-a-pid", encoding="ascii")
        check("损坏的 PID 文件视为无效", read_pid_file(path) is None)
        path.write_text("", encoding="ascii")
        check("空 PID 文件视为无效", read_pid_file(path) is None)

        path.write_text("12345", encoding="ascii")
        legacy = read_pid_file(path)
        check(
            "旧版整数 PID 可识别但标记为 legacy",
            legacy is not None
            and legacy.get("pid") == 12345
            and legacy.get("legacy") is True,
        )
        clear_pid_file(path, 12345)
        check("不会清除无法核对启动时间的 legacy 状态", path.exists())

        write_pid_file(path, {"pid": 999, "started_at": 1700000001.0})
        clear_pid_file(path, 12345)
        check(
            "不会删除别的实例的 PID 文件",
            (read_pid_file(path) or {}).get("pid") == 999,
        )
        clear_pid_file(path, 999)
        check("匹配的 PID 文件会被删除", not path.exists())

        bad_dir = Path(tmp) / "atomic"
        bad_dir.mkdir()
        target = bad_dir / "x.pid"
        raised = False
        try:
            with patch(
                "app.single_instance.os.replace",
                side_effect=OSError(28, "disk full"),
            ):
                write_pid_file(target, {"pid": 1, "started_at": 1700000000.0})
        except OSError:
            raised = True
        check(
            "写入失败会清理临时文件且不留下半成品",
            raised and list(bad_dir.iterdir()) == [],
        )


def test_shutdown_channel() -> None:
    print()
    print("本机关闭事件：")
    if os.name != "nt":
        check("非 Windows 平台跳过命名事件测试", True, "skip")
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "project"
        root.mkdir()
        channel = ShutdownChannel.create(root)
        check("可以创建项目专属关闭事件", channel is not None)
        if channel is None:
            return
        check("事件名由项目路径决定", channel.name == event_name(root))
        check("事件名格式符合停止脚本白名单", bool(
            re.fullmatch(r"Local\\JiaAgent-Shutdown-[0-9a-f]{12}", channel.name)
        ))
        duplicate = ShutdownChannel.create(root)
        check("第二个实例不会接管已存在的事件", duplicate is None)

        server = SimpleNamespace(should_exit=False)
        channel.attach(server)
        check(
            "事件已绑定 uvicorn 服务器",
            channel._thread is not None and channel._thread.is_alive(),
        )
        signalled = ShutdownChannel.signal(channel.name)
        check("可以打开并触发事件", signalled)
        deadline = time.monotonic() + 3.0
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.05)
        check("事件触发后设置 should_exit", bool(server.should_exit))

        channel.close()
        check("关闭后事件不再可用", not ShutdownChannel.signal(channel.name))
        check("拒绝任意非项目事件名", not ShutdownChannel.signal("Local\\Something-Else"))


def test_redaction() -> None:
    print()
    print("自动启动日志脱敏：")
    bearer = "api: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
    check("Bearer token 不落日志", "eyJhbGciOiJIUzI1NiJ9" not in redact_log_line(bearer))

    basic = "Authorization: Basic dXNlcjpwYXNzd29yZA=="
    check("Basic 凭据整段不落日志", "dXNlcjpwYXNzd29yZA" not in redact_log_line(basic))

    api_key = "loaded JIA_DEEPSEEK_API_KEY=sk-abcdefgh12345678 from env"
    check("API key 不落日志", "sk-abcdefgh" not in redact_log_line(api_key))

    token_line = 'context_token="ctx-1234567890abcdef" ok'
    check("context token 不落日志", "ctx-1234567890abcdef" not in redact_log_line(token_line))

    url_line = "GET /api/cb?code=ABC123SECRET&state=xyz"
    result = redact_log_line(url_line)
    check("URL 查询参数里的 code 不落日志", "ABC123SECRET" not in result)
    check("URL 里的非敏感参数保留", "state=xyz" in result)
    check(
        "键名和分隔符保留，便于定位",
        "token=<redacted>" in redact_log_line("token=abc&state=xyz"),
    )

    prefixed_cases = (
        ("JIA_WECHAT_BOT_TOKEN", "bot-secret-123"),
        ("refresh_token", "refresh-secret-456"),
        ("client_secret", "client-secret-789"),
        ("JIA_CLIENT_SECRET", "jia-client-secret-abc"),
        ("JIA_REFRESH_TOKEN", "jia-refresh-secret-def"),
        ("JIA_WECHAT_BOT_TOKEN", "bot-spaced-999"),
    )
    for index, (key, secret) in enumerate(prefixed_cases):
        separator = " = " if index == len(prefixed_cases) - 1 else "="
        line = f"{key}{separator}{secret} loaded"
        result = redact_log_line(line)
        check(
            f"带前缀/别名的键名脱敏：{key}",
            secret not in result and key in result and "loaded" in result,
        )

    json_line = '{"token": "json-token-123", "other": "keep"}'
    result = redact_log_line(json_line)
    check(
        "JSON 形式 token 不落日志",
        "json-token-123" not in result and '"token"' in result,
    )
    check("JSON 同一行其他字段保留", '"other": "keep"' in result)

    result = redact_log_line('"access_token": "json-access-456"')
    check(
        "JSON 形式 access_token 不落日志",
        "json-access-456" not in result and '"access_token"' in result,
    )

    result = redact_log_line('{"client_secret":"json-secret-789"}')
    check(
        "JSON 紧凑形式 client_secret 不落日志",
        "json-secret-789" not in result and '"client_secret"' in result,
    )

    result = redact_log_line("REFRESH_TOKEN: upper-refresh-111")
    check(
        "大写键名与冒号分隔仍脱敏",
        "upper-refresh-111" not in result and "REFRESH_TOKEN" in result,
    )

    chinese = "微信验证码：382910，授权码：AUTH-778899"
    result = redact_log_line(chinese)
    check("验证码/授权码不落日志", "382910" not in result and "AUTH-778899" not in result)

    qr = "open https://login.weixin.qq.com/qrcode/abc123def now"
    result = redact_log_line(qr)
    check("微信二维码 URL 整体替换", "weixin" not in result and "abc123def" not in result)

    normal = "Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)"
    check("普通诊断信息保持不变", redact_log_line(normal) == normal)
    trace = "Traceback (most recent call last):"
    check("异常类型行保持不变", redact_log_line(trace) == trace)


def test_launcher_streams() -> None:
    print()
    print("用户应用标准流隔离：")
    import app.apps.launcher as launcher_module
    from app.apps.model import AppRecord, LaunchKind, LaunchStatus, Source

    class FakeProcess:
        pid = 4242

        def poll(self):
            return None

    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "harmless-probe.exe"
        exe.write_bytes(b"")
        record = AppRecord(
            name="probe",
            source=Source.MANUAL,
            launch_kind=LaunchKind.EXE,
            target=str(exe),
            args=["--probe"],
            workdir=tmp,
            process_hint="",
        )
        popen = MagicMock(return_value=FakeProcess())
        with patch.object(
            launcher_module.guard, "check_target", return_value=(True, "")
        ), patch.object(
            launcher_module.subprocess, "Popen", popen
        ), patch.object(
            launcher_module, "_START_CHECK_SECONDS", 0
        ):
            outcome = asyncio.run(launcher_module.launch(record))

        kwargs = popen.call_args.kwargs if popen.call_args else {}
        check(
            "EXE 启动显式隔离 stdin/stdout/stderr",
            kwargs.get("stdin") == subprocess.DEVNULL
            and kwargs.get("stdout") == subprocess.DEVNULL
            and kwargs.get("stderr") == subprocess.DEVNULL,
        )
        check(
            "EXE 启动保留参数、工作目录和 close_fds",
            bool(popen.call_args)
            and popen.call_args.args[0] == [str(exe), "--probe"]
            and kwargs.get("cwd") == tmp
            and kwargs.get("close_fds") is True,
        )
        check("EXE 启动路径仍返回正常结果", outcome.status is LaunchStatus.UNCONFIRMED)


def _load_redacted_module():
    path = ROOT / "scripts" / "run_jia_redacted.py"
    spec = importlib.util.spec_from_file_location("jia_run_redacted_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _HangingStream:
    """先给一行，然后像被其他进程持有的管道一样阻塞，不产生 EOF。"""

    def __init__(self, lines, release: threading.Event) -> None:
        self._lines = list(lines)
        self._release = release

    def __iter__(self):
        return self

    def __next__(self):
        if self._lines:
            return self._lines.pop(0)
        self._release.wait(5)
        raise StopIteration


class _FakeChild:
    def __init__(self, stream, code: int, delay: float = 0.0) -> None:
        self.stdout = stream
        self._code = code
        self._delay = delay

    def wait(self, timeout=None):
        if self._delay:
            time.sleep(self._delay)
        return self._code


def test_wrapper_exit() -> None:
    print()
    print("启动包装层退出：")
    module = _load_redacted_module()
    if not hasattr(module, "_run_child"):
        check("包装层提供有界退出实现", False, "_run_child missing")
        return

    sink: list[str] = []
    code = module._run_child(
        _FakeChild(iter(["token=normal-secret\n", "line two\n"]), 0),
        sink.append,
        drain_timeout=0.5,
    )
    joined = "\n".join(sink)
    check("包装层正常退出并保留退出码", code == 0)
    check(
        "包装层正常读取并脱敏所有行",
        "token=<redacted>" in joined
        and "line two" in joined
        and "normal-secret" not in joined,
    )

    release = threading.Event()
    sink = []
    started = time.monotonic()
    code = module._run_child(
        _FakeChild(
            _HangingStream(["token=hang-secret\n"], release),
            7,
            delay=0.2,
        ),
        sink.append,
        drain_timeout=0.2,
    )
    elapsed = time.monotonic() - started
    release.set()
    check("主进程退出后包装层有界退出", elapsed < 2.0, f"{elapsed:.2f}s")
    check("输出句柄被持有时退出码仍传递", code == 7)
    check(
        "有界退出前已读到的行仍脱敏",
        any("token=<redacted>" in line for line in sink)
        and "hang-secret" not in "\n".join(sink),
    )


def test_wrapper_real_pipe() -> None:
    print()
    print("启动包装层真实管道：")
    module = _load_redacted_module()
    if not hasattr(module, "_run_child"):
        check("真实管道测试可用", False, "_run_child missing")
        return

    # The direct child starts a harmless grandchild that inherits the pipe
    # write end, prints one line, then exits. The grandchild keeps the handle
    # open for two seconds. This reproduces the Windows inheritance scenario
    # without launching Jia or any user application.
    child_code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], "
        "stdout=sys.stdout); "
        "print('child-exit', flush=True)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    sink: list[str] = []
    started = time.monotonic()
    try:
        code = module._run_child(child, sink.append, drain_timeout=0.2)
        elapsed = time.monotonic() - started
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        if child.stdout is not None:
            child.stdout.close()

    check(
        "主进程退出后真实管道不会挂住包装层",
        elapsed < 2.0,
        f"{elapsed:.2f}s",
    )
    check("真实管道退出码仍传递", code == 0)
    check("真实管道退出前的行仍被读取", any("child-exit" in line for line in sink))


def test_run_fail_closed() -> None:
    print()
    print("启动失败时的状态一致性：")
    with tempfile.TemporaryDirectory() as tmp:
        pid_path = Path(tmp) / "jiaagent.pid"
        secrets = SimpleNamespace(
            jia_host="127.0.0.1",
            jia_port=8000,
            jia_log_level="warning",
        )

        # 0) 数据目录准备失败：不拿锁、不启动。
        acquire_mock = MagicMock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
            run_module, "ensure_dirs", side_effect=OSError(13, "denied")
        ), patch.object(run_module, "acquire", acquire_mock), patch.object(
            run_module, "PID_FILE", pid_path
        ):
            code = run_module.main()
        check("数据目录准备失败时拒绝启动", code == 1 and not acquire_mock.called)
        check("数据目录错误信息不回显原始异常文本", "denied" not in output.getvalue())

        # 1) 锁机制本身出错：必须拒绝启动，不进入 uvicorn。
        serve = MagicMock(return_value=0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
            run_module, "ensure_dirs"
        ), patch.object(
            run_module, "acquire", side_effect=OSError(5, "intentional-secret")
        ), patch.object(run_module, "_serve", serve), patch.object(
            run_module, "PID_FILE", pid_path
        ):
            code = run_module.main()
        text = output.getvalue()
        check("单实例锁异常时拒绝启动", code == 1 and not serve.called)
        check("锁错误信息不回显原始异常文本", "intentional-secret" not in text)
        check("锁错误时没有写 PID 文件", not pid_path.exists())

        # 2) 已有实例：正常跳过，不碰任何状态。
        serve = MagicMock(return_value=0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
            run_module, "ensure_dirs"
        ), patch.object(run_module, "acquire", return_value=None), patch.object(
            run_module, "_serve", serve
        ), patch.object(run_module, "PID_FILE", pid_path):
            code = run_module.main()
        check("已有实例时正常跳过", code == 0 and not serve.called)
        check("已有实例时不写 PID 文件", not pid_path.exists())

        # 3) 读取配置失败：拒绝启动，释放锁，不泄露异常内容。
        lock = FakeLock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
            run_module, "ensure_dirs"
        ), patch.object(run_module, "acquire", return_value=lock), patch.object(
            run_module, "get_secrets", side_effect=RuntimeError("sk-abcdefgh12345678")
        ), patch.object(run_module, "PID_FILE", pid_path):
            code = run_module.main()
        text = output.getvalue()
        check("配置读取失败时拒绝启动", code == 1)
        check("配置错误时不泄露密钥", "sk-abcdefgh" not in text)
        check("配置错误时释放单实例锁", lock.released)
        check("配置错误时不写 PID 文件", not pid_path.exists())

        # 4) 非回环地址：拒绝启动，释放锁，不创建运行时。
        lock = FakeLock()
        bad_secrets = SimpleNamespace(
            jia_host="0.0.0.0", jia_port=8000, jia_log_level="warning"
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
            run_module, "ensure_dirs"
        ), patch.object(run_module, "acquire", return_value=lock), patch.object(
            run_module, "get_secrets", return_value=bad_secrets
        ), patch.object(run_module, "PID_FILE", pid_path):
            code = run_module.main()
        check("非回环 JIA_HOST 时拒绝启动", code == 2)
        check("非回环地址时释放单实例锁", lock.released)
        check("非回环地址时不写 PID 文件", not pid_path.exists())

        # 5) PID 写入失败：拒绝启动，绝不动已有状态。
        pid_path.write_text(
            json.dumps({"pid": 999, "started_at": 1700000000.0}),
            encoding="ascii",
        )
        lock = FakeLock()
        factory = FakeChannelFactory()
        serve = MagicMock(return_value=0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(
            run_module, "ensure_dirs"
        ), patch.object(run_module, "acquire", return_value=lock), patch.object(
            run_module, "get_secrets", return_value=secrets
        ), patch.object(
            run_module, "ShutdownChannel", factory
        ), patch.object(
            run_module, "write_pid_file", side_effect=OSError(28, "disk full")
        ), patch.object(
            run_module, "_serve", serve
        ), patch.object(run_module, "PID_FILE", pid_path):
            code = run_module.main()
        check("PID 写入失败时拒绝启动", code == 1 and not serve.called)
        check(
            "PID 写入失败时不动已有 PID 状态",
            (read_pid_file(pid_path) or {}).get("pid") == 999,
        )
        check("PID 写入失败时释放锁", lock.released)
        check(
            "PID 写入失败时关闭本次创建的事件",
            len(factory.instances) == 1 and factory.instances[0].closed,
        )
        pid_path.unlink()

        # 6) 正常启动：PID 状态可核对，退出后清理自己的状态。
        lock = FakeLock()
        factory = FakeChannelFactory()
        observed: dict[str, object] = {}

        def fake_serve(_secrets, channel):
            observed["exists"] = pid_path.exists()
            observed["state"] = read_pid_file(pid_path)
            observed["channel"] = channel
            return 0

        with patch.object(run_module, "ensure_dirs"), patch.object(
            run_module, "acquire", return_value=lock
        ), patch.object(
            run_module, "get_secrets", return_value=secrets
        ), patch.object(
            run_module, "ShutdownChannel", factory
        ), patch.object(
            run_module, "_serve", side_effect=fake_serve
        ), patch.object(run_module, "PID_FILE", pid_path):
            code = run_module.main()
        state = observed.get("state") or {}
        channel = factory.instances[0] if factory.instances else None
        check("正常启动返回 0", code == 0)
        check(
            "运行期间 PID 状态包含本进程与监听地址",
            observed.get("exists") is True
            and state.get("pid") == os.getpid()
            and state.get("host") == "127.0.0.1"
            and state.get("port") == 8000
            and float(state.get("started_at", 0)) > 0,
        )
        check(
            "运行期间 PID 状态包含关闭事件名",
            channel is not None and state.get("shutdown_event") == channel.name,
        )
        check("正常退出后删除自己的 PID 状态", not pid_path.exists())
        check("正常退出后释放锁", lock.released)
        check("正常退出后关闭事件", channel is not None and channel.closed)

        # 7) 服务启动异常：仍然清理 PID/锁/事件，不留下半启动状态。
        lock = FakeLock()
        factory = FakeChannelFactory()
        raised = False
        try:
            with patch.object(run_module, "ensure_dirs"), patch.object(
                run_module, "acquire", return_value=lock
            ), patch.object(
                run_module, "get_secrets", return_value=secrets
            ), patch.object(
                run_module, "ShutdownChannel", factory
            ), patch.object(
                run_module, "_serve", side_effect=RuntimeError("boom")
            ), patch.object(run_module, "PID_FILE", pid_path):
                run_module.main()
        except RuntimeError:
            raised = True
        check("服务启动异常会向上抛出以便记录", raised)
        check("服务启动异常后删除 PID 状态", not pid_path.exists())
        check("服务启动异常后释放锁", lock.released)
        check(
            "服务启动异常后关闭事件",
            bool(factory.instances) and factory.instances[0].closed,
        )

        # 8) 可选的关闭事件创建失败：仍应启动，但状态里不声称可优雅关闭。
        lock = FakeLock()
        serve = MagicMock(return_value=0)
        with patch.object(run_module, "ensure_dirs"), patch.object(
            run_module, "acquire", return_value=lock
        ), patch.object(
            run_module, "get_secrets", return_value=secrets
        ), patch.object(
            run_module, "ShutdownChannel", RaisingChannelFactory()
        ), patch.object(
            run_module, "_serve", serve
        ), patch.object(run_module, "PID_FILE", pid_path):
            code = run_module.main()
        check("关闭事件不可用时仍能启动", code == 0 and serve.called)
        check("事件不可用时不留下 PID 状态", not pid_path.exists())
        check("事件不可用时释放锁", lock.released)

        state_with = run_module._pid_state(secrets, FakeChannel())
        state_without = run_module._pid_state(secrets, None)
        check(
            "_pid_state 记录事件名，没有事件时不伪造",
            state_with.get("shutdown_event") == EVENT_PREFIX + "0123456789ab"
            and "shutdown_event" not in state_without,
        )

        started_at = getattr(run_module, "PROCESS_STARTED_AT", None)
        check(
            "PID 启动时间取自进程启动时刻，不是写文件时刻",
            started_at is not None
            and abs(
                float(state_with.get("started_at", 0)) - float(started_at)
            )
            < 0.001
            and 0 < (time.time() - float(started_at)) < 300,
        )


def test_serve_outcome() -> None:
    print()
    print("uvicorn 启动结果：")
    secrets = SimpleNamespace(
        jia_host="127.0.0.1", jia_port=8000, jia_log_level="warning"
    )

    class FakeServer:
        last = None

        def __init__(self, config, started: bool) -> None:
            self.config = config
            self.started = started
            self.should_exit = False
            self.run_called = False
            FakeServer.last = self

        def run(self) -> None:
            self.run_called = True

    class FakeConfig:
        def __init__(self, app, **kwargs) -> None:
            self.app = app
            self.kwargs = kwargs

    def fake_uvicorn(started: bool):
        return SimpleNamespace(
            Server=lambda config: FakeServer(config, started),
            Config=FakeConfig,
        )

    for started, expected in ((True, 0), (False, 1)):
        channel = FakeChannel()
        output = io.StringIO()
        label = "已监听" if started else "未监听"
        with contextlib.redirect_stdout(output), patch.object(
            run_module, "get_config", side_effect=RuntimeError("config unavailable")
        ), patch.object(run_module, "uvicorn", fake_uvicorn(started)):
            code = run_module._serve(secrets, channel)
        server = FakeServer.last
        check(
            f"uvicorn {label}时返回 {'0' if expected == 0 else '非 0'}",
            code == expected and server is not None and server.run_called,
        )
        check(
            f"uvicorn {label}时绑定到配置的地址/端口",
            server is not None
            and server.config.kwargs.get("host") == "127.0.0.1"
            and server.config.kwargs.get("port") == 8000,
        )
        check(f"uvicorn {label}时关闭事件已绑定", channel.attached)
        if not started:
            check(
                "未监听时明确报告启动失败",
                "未能开始监听" in output.getvalue(),
            )


def test_health_curl_end_to_end() -> None:
    print()
    print("健康检查（真实 curl + 本机夹具，不启动 Jia）：")
    if os.name != "nt":
        check("非 Windows 平台跳过真实 curl 健康检查", True, "skip")
        return
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        check("找不到 powershell.exe", False, "无法验证真实 curl 健康检查")
        return

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            body = json.dumps(
                {"status": "ok", "name": "JiaAgent", "version": "0.1.0"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        common = ROOT / "scripts" / "windows" / "jia_common.ps1"
        command = (
            f". '{common}'; "
            f"$h = Get-JiaHealth -BaseUrl 'http://127.0.0.1:{port}'; "
            "Write-Output ('{0}|{1}|{2}' -f $h.Ok, $h.StatusCode, $h.Data.name)"
        )
        proc = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        check(
            "真实 curl 能接受本机 Jia 健康响应",
            proc.returncode == 0 and "True|200|JiaAgent" in output,
            output.strip().splitlines()[-1] if output.strip() else "no output",
        )
        if proc.returncode != 0:
            print(output)
    except subprocess.TimeoutExpired:
        check("真实 curl 健康检查在 60 秒内完成", False, "timeout")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_wechat_env_file() -> None:
    print()
    print("微信开关：.env 生效，进程环境优先：")
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_path.write_text(
            "JIA_WECHAT_ENABLED=1\n"
            "JIA_WECHAT_ACCOUNT_ID=account-from-dotenv\n"
            "JIA_DEEPSEEK_API_KEY=not-used-by-this-test\n",
            encoding="utf-8",
        )
        original = runtime_module.ENV_FILE
        runtime_module.ENV_FILE = env_path
        try:
            with patch.dict(os.environ, {}, clear=False):
                for key in ("JIA_WECHAT_ENABLED", "JIA_WECHAT_ACCOUNT_ID"):
                    os.environ.pop(key, None)
                config = runtime_module.WeChatRuntimeConfig.from_env()
                check(
                    ".env 能开启微信入口",
                    config.enabled and config.account_id == "account-from-dotenv",
                    f"enabled={config.enabled}",
                )

            with patch.dict(
                os.environ,
                {
                    "JIA_WECHAT_ENABLED": "0",
                    "JIA_WECHAT_ACCOUNT_ID": "account-from-env",
                },
                clear=False,
            ):
                config = runtime_module.WeChatRuntimeConfig.from_env()
                check(
                    "进程环境变量优先于 .env",
                    (not config.enabled)
                    and config.account_id == "account-from-env",
                )
        finally:
            runtime_module.ENV_FILE = original


def test_windows_scripts() -> None:
    print()
    print("Windows 启动脚本：")
    folder = ROOT / "scripts" / "windows"
    names = [
        "jia_common.ps1",
        "autostart_common.ps1",
        "start_jia.ps1",
        "install_autostart.ps1",
        "uninstall_autostart.ps1",
        "status_jia.ps1",
        "stop_jia.ps1",
        "test_windows_scripts.ps1",
    ]
    missing = [name for name in names if not (folder / name).is_file()]
    check("启动/安装/卸载/状态/停止/测试脚本齐全", not missing, ", ".join(missing))

    texts = {}
    for name in names:
        path = folder / name
        texts[name] = path.read_text(encoding="utf-8") if path.is_file() else ""

    all_text = "\n".join(texts.values())
    start_text = texts["start_jia.ps1"]
    stop_text = texts["stop_jia.ps1"]
    status_text = texts["status_jia.ps1"]
    common_text = texts["jia_common.ps1"]
    autostart_common_text = texts["autostart_common.ps1"]
    install_text = texts["install_autostart.ps1"]
    uninstall_text = texts["uninstall_autostart.ps1"]

    check("启动脚本使用项目虚拟环境", ".venv\\Scripts\\python.exe" in start_text)
    check("启动脚本经过脱敏包装层", "run_jia_redacted.py" in start_text)
    check(
        "停止脚本不再调用 taskkill",
        "taskkill" not in stop_text.lower(),
    )
    check(
        "停止脚本不按父子关系遍历进程",
        "ParentProcessId" not in stop_text
        and "Get-CimInstance Win32_Process -Filter \"ParentProcessId" not in stop_text,
    )
    check(
        "停止脚本使用本机命名事件优雅关闭",
        "EventWaitHandle]::OpenExisting" in stop_text,
    )
    check(
        "停止脚本只强制结束核对过的 PID",
        "Stop-Process -Id $state.Pid -Force" in stop_text,
    )
    check(
        "停止脚本区分缺失/损坏/不可读的 PID 状态",
        "Read-JiaPidStateResult" in stop_text
        and "'Corrupt'" in stop_text
        and "'Unreadable'" in stop_text
        and "'Missing'" in stop_text,
    )
    check(
        "缺失 PID 时停止脚本执行健康检查与进程身份扫描",
        "Get-JiaHealth" in stop_text
        and "Find-JiaProcess" in stop_text
        and "Get-JiaNoPidStopOutcome" in stop_text,
    )
    check(
        "状态脚本区分缺失/损坏/不可读的 PID 状态",
        "Read-JiaPidStateResult" in status_text
        and "Corrupt" in status_text
        and "Unreadable" in status_text,
    )
    check("状态脚本复用严格健康检查", "Get-JiaHealth" in status_text)
    check(
        "健康检查绑定 Jia 的服务名和状态",
        "Test-JiaHealthPayload" in common_text
        and "JiaAgent" in common_text
        and "'ok'" in common_text,
    )
    check(
        "脚本不调用微信配对/授权命令",
        "wechat_cli" not in all_text and "AllowListStore" not in all_text,
    )
    check(
        "脚本不请求提权、不以最高权限运行",
        "-Verb RunAs" not in all_text and "Highest" not in all_text,
    )
    check(
        "安装脚本默认使用当前用户交互式登录",
        "LogonType Interactive" in texts["install_autostart.ps1"]
        and "RunLevel Limited" in texts["install_autostart.ps1"],
    )
    check(
        "安装脚本使用任务计划的登录触发器",
        "New-ScheduledTaskTrigger -AtLogOn" in install_text,
    )
    check(
        "安装脚本写入前核对任务和快捷方式所有权",
        "autostart_common.ps1" in install_text
        and "Read-JiaScheduledTaskInfo" in install_text
        and "Get-JiaTaskOwnership" in install_text
        and "Read-JiaShortcutInfo" in install_text
        and "Get-JiaShortcutOwnership" in install_text,
    )
    check(
        "卸载脚本删除前核对任务和快捷方式所有权",
        "autostart_common.ps1" in uninstall_text
        and "Read-JiaScheduledTaskInfo" in uninstall_text
        and "Invoke-JiaTaskUninstall" in uninstall_text
        and "Read-JiaShortcutInfo" in uninstall_text
        and "Invoke-JiaShortcutUninstall" in uninstall_text,
    )
    check(
        "安装/卸载不再按名称直接覆盖或删除计划任务",
        "Get-ScheduledTask" not in install_text
        and "Get-ScheduledTask" not in uninstall_text,
    )
    check(
        "所有权检查拒绝外部或不可读资源并只查根任务文件夹",
        "'Foreign'" in autostart_common_text
        and "'Unreadable'" in autostart_common_text
        and "GetFolder('\\')" in autostart_common_text,
    )
    check(
        "安装/卸载使用显式校验的 Windows PowerShell 路径",
        "$PSHOME" not in install_text
        and "$PSHOME" not in uninstall_text
        and "Resolve-JiaWindowsPowerShellPath" in install_text
        and "Resolve-JiaWindowsPowerShellPath" in uninstall_text
        and "$powershellResolution.Ok" in install_text
        and "$powershellResolution.Ok" in uninstall_text,
    )
    check(
        "停止/状态脚本区分健康探针失败与真实不可达",
        "$health.Checked" in stop_text
        and "$health.Checked" in status_text
        and "CommandNotFoundException" not in stop_text,
    )
    check(
        "微信开关只在显式参数下强制打开",
        "if ($EnableWeChat)" in start_text,
    )
    check(
        "脱敏包装层调用 run.py 并复用脱敏函数",
        (ROOT / "scripts" / "run_jia_redacted.py").is_file()
        and "redact_log_line" in (ROOT / "scripts" / "run_jia_redacted.py").read_text(
            encoding="utf-8"
        ),
    )

    if os.name != "nt":
        check("PowerShell 回归测试在 Windows 上执行", True, "skip (non-Windows)")
        return

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        check("找不到 powershell.exe", False, "请手工运行 scripts\\windows\\test_windows_scripts.ps1")
        return

    test_script = folder / "test_windows_scripts.ps1"
    try:
        proc = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(test_script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        check(
            "PowerShell 回归测试全部通过",
            proc.returncode == 0,
            output.strip().splitlines()[-1] if output.strip() else "no output",
        )
        if proc.returncode != 0:
            print(output)
    except subprocess.TimeoutExpired:
        check("PowerShell 回归测试在 180 秒内完成", False, "timeout")


def main() -> int:
    print("=" * 68)
    print("Windows 自动启动离线检查（不注册任务、不启动服务、不结束真实进程）")
    print("=" * 68)
    test_single_instance()
    test_pid_file()
    test_shutdown_channel()
    test_redaction()
    test_launcher_streams()
    test_wrapper_exit()
    test_wrapper_real_pipe()
    test_run_fail_closed()
    test_serve_outcome()
    test_health_curl_end_to_end()
    test_wechat_env_file()
    test_windows_scripts()

    passed = sum(1 for _note, ok, _detail in RESULTS if ok)
    total = len(RESULTS)
    print()
    print("=" * 68)
    if passed == total:
        print(f"结果：{passed}/{total} 全部通过 [OK]")
        return 0
    print(f"结果：{passed}/{total} 通过，{total - passed} 项失败 [!!]")
    for note, ok, detail in RESULTS:
        if not ok:
            print(f"  失败：{note} {detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
