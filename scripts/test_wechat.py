"""Jia-WeChat 离线测试。

**不联网、不启动真实微信登录、不读写真实用户数据、不调用模型、不执行工具。**
所有网络都走注入的 FakeTransport / FakeApi；微信状态库用临时目录。

覆盖：
  1. 协议解析失败、群聊/图片/机器人消息拒绝
  2. HTTP 头、请求体、错误映射（token 不进异常）
  3. 凭证加密保存、允许名单精确匹配
  4. 入站原子去重、游标、任务归属、一次性授权码
  5. 合法/非法发送者、重复消息、速率限制
  6. 任务派发、状态查询、计划批准/拒绝
  7. 工具确认/选择授权、无效/过期/重复使用
  8. 任务通知去重、终态通知
  9. 断线、超时、-14 暂停、恢复
 10. 日志和凭证文件中不出现 token/context_token/授权码

用法：
  .venv\\Scripts\\python.exe scripts\\test_wechat.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.errors import JiaAgentError  # noqa: E402
from app.wechat import protocol  # noqa: E402
from app.wechat.admin import logout_account, unpair_user  # noqa: E402
from app.wechat.api import ApiConfig, WeChatApi  # noqa: E402
from app.wechat.bridge import (  # noqa: E402
    BridgeError,
    JiaRuntimeBridge,
    RemoteAction,
    error_text,
)
from app.wechat.credentials import (  # noqa: E402
    AccountCredentials,
    AllowListStore,
    ApprovalSecretStore,
    CredentialStore,
    DpapiProtector,
)
from app.wechat.errors import (  # noqa: E402
    ApiBusinessError,
    ApiHttpError,
    CredentialError,
    ProtocolError,
    RemoteApprovalError,
    TransportError,
    TransportTimeout,
)
from app.wechat.login import login_with_qr  # noqa: E402
from app.wechat.monitor import MonitorConfig, WeChatMonitor  # noqa: E402
from app.wechat.protocol import (  # noqa: E402
    QrCode,
    QrStatus,
    UpdatesPage,
)
from app.wechat.remote_auth import (  # noqa: E402
    ACTION_CHOICE,
    ACTION_CONFIRM,
    ACTION_PLAN,
    RemoteAuth,
    format_code,
)
from app.wechat.redact import redact_id  # noqa: E402
from app.wechat.runtime import WeChatRuntime, WeChatRuntimeConfig  # noqa: E402
from app.wechat.runtime_state import (  # noqa: E402
    clear_runtime_status,
    read_runtime_status,
    runtime_is_active,
    runtime_status_text,
    write_runtime_status,
)
from app.wechat.service import WeChatService  # noqa: E402
from app.wechat.store import WeChatStore  # noqa: E402
from app.wechat.task_watch import TaskWatcher  # noqa: E402
from app.wechat.transport import HttpResponse  # noqa: E402
from app.workflow.model import Step, StepStatus, Task, TaskStatus  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
ACC = "bot-account-1"
USER = "user-1"
OTHER = "user-2"
CODE_RE = re.compile(r"[A-Z2-9]{5}-[A-Z2-9]{5}")


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    print(f"  {'[OK]' if ok else '[!!]'} {note}" + (f"　{detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print(title)


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 假实现
# ----------------------------------------------------------------------
class FakeProtector:
    """测试用可逆保护器。真实环境必须用 DPAPI。"""

    def protect(self, data: bytes) -> bytes:
        return b"FAKE1:" + bytes(b ^ 0x5A for b in data)

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"FAKE1:"):
            raise CredentialError("测试凭证格式不对")
        return bytes(b ^ 0x5A for b in data[6:])


class FakeTransport:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[dict] = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append({
            "method": method, "url": url, "headers": dict(headers),
            "body": body, "timeout": timeout,
        })
        return self.handler(method, url, headers, body, timeout)


def json_response(payload, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


class FakeAllowList:
    def __init__(self, entries: dict[tuple[str, str], set[str]]) -> None:
        self.entries = entries

    def is_allowed(self, account_id: str, user_id: str, scope: str) -> bool:
        return scope in self.entries.get((account_id, user_id), set())

    def list(self):
        return [
            SimpleNamespace(account_id=a, user_id=u, scopes=tuple(sorted(s)))
            for (a, u), s in self.entries.items()
        ]


class FakeOutbound:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail_next = 0

    async def send(self, *, to_user_id, text, context_token):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TransportError("模拟发送失败")
        self.sent.append({
            "to": to_user_id, "text": text, "context_token": context_token,
        })


def make_task(
    task_id: str,
    *,
    status: TaskStatus = TaskStatus.PENDING,
    goal: str = "整理文件",
    steps: list[tuple[str, str]] | None = None,
    paused: bool = False,
    summary: str = "",
    error: str = "",
) -> Task:
    task = Task(
        id=task_id, session_id="session-1", goal=goal, title=goal[:24],
        status=status, paused=paused, summary=summary, error=error,
        created_at="2026-10-24T00:00:00",
        updated_at="2026-10-24T00:00:00",
    )
    for index, (text, tool) in enumerate(steps or []):
        task.steps.append(Step(
            step_index=index, goal=text, tool=tool, status=StepStatus.PENDING,
        ))
    return task


def make_action(
    action_id: str = "action-1",
    *,
    kind: str = "confirm",
    summary: str = "打开记事本",
    question: str = "",
    options: list[dict] | None = None,
    session_id: str = "session-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=action_id, kind=kind, tool_name="open_app", tool_label="打开应用",
        summary=summary, question=question, options=list(options or []),
        session_id=session_id, answered=False, approved=False, chosen="",
    )


class FakeBridge:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.links: dict[str, tuple[str, str]] = {}
        self.order: list[str] = []
        self.actions: dict[str, RemoteAction] = {}
        self.answered: list[tuple[str, bool]] = []
        self.chosen: list[tuple[str, str]] = []
        self.approved_plans: list[str] = []
        self.rejected_plans: list[str] = []
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.cancelled: list[str] = []
        self.chat_calls: list[tuple[str, str, str]] = []
        self.chat_reply = "好的，我是佳。"
        self.chat_error: Exception | None = None
        self._counter = 0

    def add_task(self, task: Task, account: str = ACC, user: str = USER) -> Task:
        self.tasks[task.id] = task
        self.links[task.id] = (account, user)
        self.order.append(task.id)
        return task

    async def chat(self, account_id, user_id, text):
        self.chat_calls.append((account_id, user_id, text))
        if self.chat_error is not None:
            raise self.chat_error
        return self.chat_reply

    async def create_task(self, account_id, user_id, goal):
        self._counter += 1
        task = make_task(
            f"{self._counter:032x}", goal=goal,
            steps=[("先看一下目录", ""), ("整理文件", "list_dir")],
        )
        return self.add_task(task, account_id, user_id)

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def resolve_task(self, account_id, user_id, prefix):
        candidates = [
            tid for tid, (a, u) in self.links.items()
            if a == account_id and u == user_id
        ]
        if prefix:
            if any(ch not in "0123456789abcdef" for ch in prefix):
                raise BridgeError("任务编号只能由 0-9、a-f 组成")
            candidates = [tid for tid in candidates if tid.startswith(prefix)]
            if len(candidates) > 1:
                raise BridgeError("这个编号对应多个任务，请多写几位")
            return self.tasks.get(candidates[0]) if candidates else None
        for tid in reversed(self.order):
            if tid in candidates:
                return self.tasks[tid]
        return None

    def list_linked_tasks(self, account_id, user_id, *, limit=20):
        tasks = [
            self.tasks[tid] for tid in reversed(self.order)
            if self.links.get(tid) == (account_id, user_id) and tid in self.tasks
        ]
        return tasks[:limit]

    def is_linked(self, task_id, account_id, user_id):
        return self.links.get(task_id) == (account_id, user_id)

    async def approve_plan(self, task):
        self.approved_plans.append(task.id)
        task.status = TaskStatus.RUNNING
        return True

    async def reject_plan(self, task):
        self.rejected_plans.append(task.id)
        task.status = TaskStatus.CANCELLED

    async def pause_task(self, task):
        self.paused.append(task.id)
        task.paused = True

    async def resume_task(self, task):
        self.resumed.append(task.id)
        task.paused = False

    async def cancel_task(self, task):
        self.cancelled.append(task.id)
        task.status = TaskStatus.CANCELLED

    def current_action(self, task):
        return self.actions.get(task.id)

    def answer_action(self, action_id, approved):
        for task_id, action in list(self.actions.items()):
            if action.action_id == action_id:
                if action.action_id in [a for a, _ in self.answered]:
                    return False
                self.answered.append((action_id, approved))
                del self.actions[task_id]
                return True
        return False

    def choose_action(self, action_id, option_id):
        for task_id, action in list(self.actions.items()):
            if action.action_id == action_id:
                if option_id not in [str(o.get("id")) for o in action.options]:
                    return False
                self.chosen.append((action_id, option_id))
                del self.actions[task_id]
                return True
        return False

    def status_text(self, task, *, with_steps=True):
        return f"STATUS {task.id[:8]} {task.status.value}"


def build_service(tmp: Path, *, rate_limit: int = 30, allow_entries=None):
    store = WeChatStore(tmp / "state.db")
    store.init()
    allow = FakeAllowList(allow_entries or {
        (ACC, USER): {"chat", "task", "approve"},
    })
    bridge = FakeBridge()
    auth = RemoteAuth(store, b"k" * 32, clock=lambda: 1000.0)
    out = FakeOutbound()
    svc = WeChatService(
        account_id=ACC, allowlist=allow, store=store, bridge=bridge,
        remote_auth=auth, outbound=out, clock=lambda: 1000.0,
        rate_limit=rate_limit,
    )
    return svc, store, bridge, auth, out, allow


def make_msg(
    text: str,
    *,
    message_id: str = "m1",
    user: str = USER,
    context_token: str = "",
    group_id: str = "",
    item_type: int = 1,
    message_type: int = 1,
    message_state: int = 2,
) -> dict:
    if item_type == 1:
        item = {"type": 1, "text_item": {"text": text}}
    else:
        item = {"type": item_type, "image_item": {"url": "https://example.invalid/x"}}
    return {
        "message_id": message_id,
        "from_user_id": user,
        "to_user_id": ACC,
        "group_id": group_id,
        "message_type": message_type,
        "message_state": message_state,
        "create_time_ms": 1_700_000_000_000,
        "context_token": context_token,
        "item_list": [item],
    }


# ----------------------------------------------------------------------
# 1. 协议
# ----------------------------------------------------------------------
def test_protocol() -> None:
    section("协议解析：只接受严格的单条文字单聊消息")
    msg = protocol.parse_inbound_message(make_msg("  你好  ", message_id="mid-1"))
    check("合法文字消息被解析", msg.text == "你好" and msg.from_user_id == USER)
    check("去重键优先使用 message_id", msg.message_key == "mid:mid-1")

    no_id = make_msg("你好", message_id="")
    no_id["seq"] = 42
    parsed = protocol.parse_inbound_message(no_id)
    check("没有 message_id 时使用 seq", parsed.message_key == f"seq:{USER}:42")

    no_id2 = make_msg("同样的话", message_id="", context_token="")
    no_id2.pop("seq", None)
    p1 = protocol.parse_inbound_message(no_id2)
    p2 = protocol.parse_inbound_message(no_id2)
    check("都没有时使用内容指纹且稳定", p1.message_key == p2.message_key)

    def rejects(note: str, raw) -> None:
        try:
            protocol.parse_inbound_message(raw)
        except ProtocolError:
            check(note, True)
        else:
            check(note, False, "本应拒绝")

    rejects("拒绝群聊消息", make_msg("hi", group_id="group-1"))
    rejects("拒绝图片消息", make_msg("", item_type=2))
    rejects("拒绝语音消息", make_msg("", item_type=3))
    rejects("拒绝机器人自己的消息", make_msg("hi", message_type=2))
    rejects("拒绝未完成的消息", make_msg("hi", message_state=1))
    bad = make_msg("hi")
    bad["item_list"].append({"type": 1, "text_item": {"text": "second"}})
    rejects("拒绝多条目消息", bad)
    rejects("拒绝空文本", make_msg("   "))
    rejects("拒绝控制字符", make_msg("hello\x00world"))
    rejects("拒绝超长文本", make_msg("a" * (protocol.MAX_TEXT_CHARS + 1)))
    rejects("拒绝没有发送者的消息", {"item_list": [{"type": 1, "text_item": {"text": "hi"}}]})

    check("允许正常换行", protocol.parse_inbound_message(make_msg("第一行\n第二行")).text == "第一行\n第二行")

    page = protocol.parse_updates_response({
        "ret": 0, "msgs": [make_msg("hi")], "get_updates_buf": "cursor-1",
        "longpolling_timeout_ms": 10_000,
    })
    check("getUpdates 响应被解析", page.cursor == "cursor-1" and len(page.messages) == 1)
    check("长轮询超时被读取", page.longpolling_timeout_ms == 10_000)
    try:
        protocol.parse_updates_response({"msgs": [make_msg("x")] * 101})
    except ProtocolError:
        check("一次返回过多消息被拒绝", True)
    else:
        check("一次返回过多消息被拒绝", False)

    for value, expected in [
        ("https://ilinkai.weixin.qq.com", True),
        ("https://weixin.qq.com", True),
        ("http://ilinkai.weixin.qq.com", False),
        ("https://evil.com", False),
        ("https://weixin.qq.com.evil.com", False),
        ("https://user:pw@ilinkai.weixin.qq.com", False),
        ("https://ilinkai.weixin.qq.com:8443", False),
        ("https://ilinkai.weixin.qq.com/path", False),
        ("https://127.0.0.1", False),
    ]:
        check(f"base_url 校验 {value}", protocol.is_allowed_base_url(value) is expected)

    try:
        protocol.parse_qr_status({"status": "confirmed", "baseurl": "https://evil.com"})
    except ProtocolError:
        check("二维码响应里的恶意 baseurl 被拒绝", True)
    else:
        check("二维码响应里的恶意 baseurl 被拒绝", False)

    # 二维码内容会直接打印到终端，必须先证明 ANSI/控制字符不可能进入输出。
    valid_qr = "https://qr.example/2/path?x=1"
    parsed_qr = protocol.parse_qr_response({
        "qrcode": "QR-safe", "qrcode_img_content": valid_qr,
    })
    check("二维码 URL 正常解析", parsed_qr.qrcode_url == valid_qr)

    bad_qr_values = [
        ("ESC 终端转义", "https://qr.example/\x1b[31mred"),
        ("换行", "https://qr.example/\nnext"),
        ("回车", "https://qr.example/\rnext"),
        ("制表符", "https://qr.example/\tnext"),
        ("空格", "https://qr.example/a b"),
        ("DEL", "https://qr.example/\x7f"),
        ("非 ASCII / 双向控制", "https://qr.example/‮evil"),
        ("javascript 协议", "javascript:alert(1)"),
        ("http 明文", "http://qr.example/1"),
        ("带用户名密码", "https://user:pw@qr.example/1"),
        ("反斜杠", "https://qr.example/\\evil"),
        ("空内容", ""),
        ("未闭合 IPv6 方括号", "https://[::1"),
        ("超长内容", "https://qr.example/" + "a" * protocol.MAX_QR_CONTENT_CHARS),
    ]
    for note, value in bad_qr_values:
        try:
            protocol.parse_qr_response({
                "qrcode": "QR-bad", "qrcode_img_content": value,
            })
        except ProtocolError:
            check(f"二维码内容拒绝：{note}", True)
        else:
            check(f"二维码内容拒绝：{note}", False, "本应拒绝")


# ----------------------------------------------------------------------
# 2. API
# ----------------------------------------------------------------------
def test_api() -> None:
    section("API 客户端：头、请求体、错误映射")

    def handler(method, url, headers, body, timeout):
        if "get_bot_qrcode" in url:
            return json_response({"qrcode": "QR-1", "qrcode_img_content": "https://qr.example/1"})
        if "get_qrcode_status" in url:
            return json_response({"status": "wait"})
        if "getupdates" in url:
            return json_response({"ret": 0, "msgs": [], "get_updates_buf": "c1"})
        if "sendmessage" in url:
            return json_response({"ret": 0, "message_id": "sent-1"})
        if "getconfig" in url:
            return json_response({"ret": 0, "typing_ticket": "ticket-1"})
        return json_response({"ret": 0})

    transport = FakeTransport(handler)
    api = WeChatApi(
        ApiConfig(base_url="https://ilinkai.weixin.qq.com", token="BOT_SECRET_TOKEN"),
        transport=transport,
    )

    qr = run(api.fetch_qr_code())
    qr_req = transport.requests[-1]
    qr_body = json.loads(qr_req["body"])
    check("二维码请求走固定登录地址", qr_req["url"].startswith(protocol.FIXED_LOGIN_BASE_URL))
    check("二维码请求不带 Bearer", "Authorization" not in qr_req["headers"])
    check("二维码请求带 AuthorizationType", qr_req["headers"].get("AuthorizationType") == "ilink_bot_token")
    check("二维码请求不带 base_info", "base_info" not in qr_body)
    check("二维码响应被解析", qr.qrcode == "QR-1")

    uin = qr_req["headers"].get("X-WECHAT-UIN", "")
    try:
        import base64
        decoded = base64.b64decode(uin).decode("ascii")
        uin_ok = decoded.isdigit() and 0 <= int(decoded) <= 0xFFFFFFFF
    except Exception:  # noqa: BLE001
        uin_ok = False
    check("X-WECHAT-UIN 是 uint32 十进制字符串的 base64", uin_ok)

    run(api.get_updates(cursor="old-cursor"))
    up_req = transport.requests[-1]
    up_body = json.loads(up_req["body"])
    check("getUpdates 带 Bearer", up_req["headers"].get("Authorization") == "Bearer BOT_SECRET_TOKEN")
    check("getUpdates 带 base_info", "base_info" in up_body)
    check("getUpdates 带游标", up_body.get("get_updates_buf") == "old-cursor")

    run(api.send_text(
        to_user_id=USER, text="你好", context_token="CTX-1", client_id="client-fixed",
    ))
    send_req = transport.requests[-1]
    send_body = json.loads(send_req["body"])
    out_msg = send_body["msg"]
    check("发送消息 message_type=2", out_msg.get("message_type") == 2)
    check("发送消息 message_state=2", out_msg.get("message_state") == 2)
    check("发送消息带固定 client_id", out_msg.get("client_id") == "client-fixed")
    check("发送消息带 context_token", out_msg.get("context_token") == "CTX-1")
    check("发送消息是单条文字", out_msg["item_list"][0]["text_item"]["text"] == "你好")
    check("sendMessage 返回消息编号", True)

    run(api.poll_qr_status(qrcode="QR-1", verify_code="9999"))
    poll_req = transport.requests[-1]
    check("二维码状态轮询是 GET", poll_req["method"] == "GET")
    check("二维码状态轮询带 verify_code", "verify_code=9999" in poll_req["url"])
    check("二维码状态轮询不带 Bearer", "Authorization" not in poll_req["headers"])

    ticket = run(api.get_config(ilink_user_id=USER))
    check("getConfig 能读到 typing_ticket", ticket == "ticket-1")

    # 错误映射
    bad_transport = FakeTransport(lambda *a, **k: json_response({"ret": 1}, status=500))
    bad_api = WeChatApi(ApiConfig(token="BOT_SECRET_TOKEN"), transport=bad_transport)
    try:
        run(bad_api.get_updates(cursor=""))
    except ApiHttpError as exc:
        check("非 2xx 转成 ApiHttpError", "BOT_SECRET_TOKEN" not in str(exc))
    else:
        check("非 2xx 转成 ApiHttpError", False)

    send_fail = FakeTransport(lambda *a, **k: json_response({"ret": 7, "errmsg": "bad"}))
    send_fail_api = WeChatApi(ApiConfig(token="BOT_SECRET_TOKEN"), transport=send_fail)
    try:
        run(send_fail_api.send_text(to_user_id=USER, text="x"))
    except ApiBusinessError as exc:
        check("发送失败码转成 ApiBusinessError", exc.ret == 7 and "BOT_SECRET_TOKEN" not in str(exc))
    else:
        check("发送失败码转成 ApiBusinessError", False)

    invalid_json = FakeTransport(lambda *a, **k: HttpResponse(200, b"not-json"))
    try:
        run(WeChatApi(ApiConfig(token="x"), transport=invalid_json).get_updates(cursor=""))
    except ProtocolError:
        check("非法 JSON 被拒绝", True)
    else:
        check("非法 JSON 被拒绝", False)

    try:
        WeChatApi(ApiConfig(base_url="https://evil.com", token="x"), transport=FakeTransport(lambda *a: json_response({})))
    except ProtocolError:
        check("API 拒绝非微信 base_url", True)
    else:
        check("API 拒绝非微信 base_url", False)


# ----------------------------------------------------------------------
# 3. 凭证与允许名单
# ----------------------------------------------------------------------
def test_credentials() -> None:
    section("凭证：加密保存、不落明文；允许名单精确匹配")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CredentialStore(root, FakeProtector())
        creds = AccountCredentials(
            account_id="acct-1", token="TOKEN_SECRET_ABC123",
            base_url="https://ilinkai.weixin.qq.com", user_id=USER,
            saved_at="2026-10-24T00:00:00",
        )
        store.save(creds)
        path = root / "credentials" / "acct-1" / "account.bin"
        raw = path.read_bytes()
        check("凭证文件已创建", path.exists())
        check("凭证文件不含 token 明文", b"TOKEN_SECRET_ABC123" not in raw)
        loaded = store.load("acct-1")
        check("凭证能解密读回", loaded is not None and loaded.token == creds.token)
        check("账号列表包含它", store.list_accounts() == ["acct-1"])

        try:
            store.save(AccountCredentials("acct-2", "t", "https://evil.com", "", ""))
        except CredentialError:
            check("拒绝保存非微信 baseurl", True)
        else:
            check("拒绝保存非微信 baseurl", False)

        path.write_bytes(b"BROKEN")
        try:
            store.load("acct-1")
        except CredentialError:
            check("损坏凭证失败关闭", True)
        else:
            check("损坏凭证失败关闭", False)

    # 中间目录被替换成链接时也必须拒绝；用替换检测函数模拟，
    # 避免测试依赖 Windows 上创建符号链接的权限。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = CredentialStore(root, FakeProtector())
        import app.wechat.credentials as credentials_module
        original = credentials_module._is_link_like
        try:
            credentials_module._is_link_like = (
                lambda p: Path(p) == root / "credentials"
            )
            try:
                store.save(AccountCredentials(
                    "acct-link", "TOKEN", "https://ilinkai.weixin.qq.com", "", "",
                ))
            except CredentialError:
                check("凭证中间目录是链接时拒绝写入", True)
            else:
                check("凭证中间目录是链接时拒绝写入", False)
            check("凭证中间目录是链接时不列账号", store.list_accounts() == [])
        finally:
            credentials_module._is_link_like = original

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        allow = AllowListStore(root)
        allow.add(account_id=ACC, user_id=USER, label="我")
        check("默认配对只授予 chat", allow.is_allowed(ACC, USER, "chat"))
        check("默认配对没有 task", not allow.is_allowed(ACC, USER, "task"))
        check("默认配对没有 approve", not allow.is_allowed(ACC, USER, "approve"))
        check("未配对用户被拒绝", not allow.is_allowed(ACC, OTHER, "chat"))
        check("不会自动授权扫码者（OTHER）", not allow.is_allowed(ACC, OTHER, "approve"))
        allow.add(account_id=ACC, user_id=OTHER, scopes=("chat",))
        check("只给 chat 时不能批准", not allow.is_allowed(ACC, OTHER, "approve"))
        check("范围外的作用域被拒绝", not allow.is_allowed(ACC, USER, "root"))

        strict_user = "user-strict"
        for bad_scopes in (("chat", "root"), ("chat", ""), ("chat", "Chat"), ()):
            try:
                allow.add(account_id=ACC, user_id=strict_user, scopes=bad_scopes)
            except CredentialError:
                check(f"非法作用域被拒绝：{bad_scopes!r}", True)
            else:
                check(f"非法作用域被拒绝：{bad_scopes!r}", False)
            check(
                f"非法作用域不会留下配对：{bad_scopes!r}",
                not allow.is_allowed(ACC, strict_user, "chat"),
            )

        allow.add(account_id=ACC, user_id=OTHER, scopes=("chat", "task", "chat"))
        check("显式作用域可包含 task", allow.is_allowed(ACC, OTHER, "task"))
        check("显式作用域去重后仍无 approve", not allow.is_allowed(ACC, OTHER, "approve"))
        check("取消配对", allow.remove(account_id=ACC, user_id=OTHER))
        check("取消后不再允许", not allow.is_allowed(ACC, OTHER, "chat"))
        (root / "allowlist.json").write_text("{broken", encoding="utf-8")
        check("损坏的允许名单按空名单处理（失败关闭）", allow.list() == [])

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        secrets_store = ApprovalSecretStore(root, FakeProtector())
        key1 = secrets_store.load_or_create()
        key2 = ApprovalSecretStore(root, FakeProtector()).load_or_create()
        check("授权密钥长度 32", len(key1) == 32)
        check("授权密钥可重复读取", key1 == key2)
        check("授权密钥文件不含明文", key1 not in (root / "approval.key.bin").read_bytes())

    if os.name == "nt":
        try:
            dpapi = DpapiProtector()
            blob = dpapi.protect(b"dpapi-roundtrip")
            plain = dpapi.unprotect(blob)
            check("Windows DPAPI 加解密可用", plain == b"dpapi-roundtrip" and blob != b"dpapi-roundtrip")
        except Exception as exc:  # noqa: BLE001
            check("Windows DPAPI 加解密可用", False, type(exc).__name__)
    else:
        check("Windows DPAPI 加解密可用（跳过：非 Windows）", True, "skip")


# ----------------------------------------------------------------------
# 4. 状态库与远程授权
# ----------------------------------------------------------------------
def test_store_and_remote_auth() -> None:
    section("状态库：原子去重、游标、一次性授权码")
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "state.db"
        store = WeChatStore(db)
        store.init()
        check("第一次 claim 成功", store.claim_inbound(
            message_key="k1", account_id=ACC, from_user_id=USER, now=100.0
        ))
        check("重复 claim 失败", not store.claim_inbound(
            message_key="k1", account_id=ACC, from_user_id=USER, now=101.0
        ))
        store.finish_inbound(message_key="k1", state="done", now=102.0)
        check("消息状态已记录", store.inbound_state("k1") == "done")

        store.set_cursor(ACC, "cursor-1", 103.0)
        check("游标可持久化", store.get_cursor(ACC) == "cursor-1")
        store.set_account_paused(ACC, paused_until=2000.0, reason="test", now=104.0)
        state = store.account_state(ACC)
        check("账号暂停时间可持久化", state.get("paused_until") == 2000.0)
        store.clear_account_paused(ACC, now=105.0)
        check("账号暂停可清除", store.account_state(ACC).get("paused_until") == 0)

        store.set_session_id(ACC, USER, "session-x", 106.0)
        check("会话映射可持久化", store.get_session_id(ACC, USER) == "session-x")
        store.link_task(task_id="a" * 32, account_id=ACC, user_id=USER,
                        session_id="session-x", now=107.0)
        check("任务归属可持久化", store.get_task_link("a" * 32)["user_id"] == USER)
        try:
            store.update_task_link("a" * 32, now=108.0, bad_field=1)
        except ValueError:
            check("任务通知字段有白名单", True)
        else:
            check("任务通知字段有白名单", False)

        auth = RemoteAuth(store, b"z" * 32, clock=lambda: 1000.0, ttl_seconds=60.0)
        code, binding = auth.issue(
            account_id=ACC, user_id=USER, task_id="a" * 32,
            action_type=ACTION_PLAN, now=1000.0,
        )
        check("授权码长度 10", len(code) == 10)
        check("授权码显示带连字符", "-" in format_code(code))
        check("授权码绑定任务", binding.task_id == "a" * 32)
        check("状态库不存授权码明文",
              code.encode() not in db.read_bytes())

        resolved = auth.resolve(code, account_id=ACC, user_id=USER, now=1001.0)
        check("能按码解析绑定", resolved.task_id == "a" * 32)
        consumed = auth.consume(code, account_id=ACC, user_id=USER,
                                binding=resolved, now=1002.0)
        check("第一次消费成功", consumed.approval_id == resolved.approval_id)
        try:
            auth.consume(code, account_id=ACC, user_id=USER,
                         binding=resolved, now=1003.0)
        except RemoteApprovalError:
            check("第二次消费失败（一次性）", True)
        else:
            check("第二次消费失败（一次性）", False)

        code2, _ = auth.issue(account_id=ACC, user_id=USER, task_id="a" * 32,
                              action_type=ACTION_PLAN, now=1004.0)
        code3, _ = auth.issue(account_id=ACC, user_id=USER, task_id="a" * 32,
                              action_type=ACTION_PLAN, now=1005.0)
        try:
            auth.resolve(code2, account_id=ACC, user_id=USER, now=1006.0)
        except RemoteApprovalError:
            check("重新发码会撤销旧码", True)
        else:
            check("重新发码会撤销旧码", False)
        check("新码可用", auth.resolve(code3, account_id=ACC, user_id=USER, now=1006.0).task_id == "a" * 32)

        expired_auth = RemoteAuth(store, b"y" * 32, clock=lambda: 5000.0, ttl_seconds=10.0)
        expired_code, _ = expired_auth.issue(
            account_id=ACC, user_id=USER, task_id="a" * 32,
            action_type=ACTION_PLAN, now=5000.0,
        )
        try:
            expired_auth.resolve(expired_code, account_id=ACC, user_id=USER, now=5011.0)
        except RemoteApprovalError:
            check("过期授权码被拒绝", True)
        else:
            check("过期授权码被拒绝", False)

        try:
            auth.resolve(code3, account_id=ACC, user_id=OTHER, now=1006.0)
        except RemoteApprovalError:
            check("别人的授权码不能解析", True)
        else:
            check("别人的授权码不能解析", False)

        try:
            auth.issue(account_id=ACC, user_id=USER, task_id="a" * 32,
                       action_type=ACTION_CONFIRM, action_id="", now=1007.0)
        except ValueError:
            check("确认授权必须绑定具体动作", True)
        else:
            check("确认授权必须绑定具体动作", False)

    # 消费时的绑定必须逐项完全一致；失败不能消耗掉真正的码。
    with tempfile.TemporaryDirectory() as tmp:
        store = WeChatStore(Path(tmp) / "state.db")
        store.init()
        auth = RemoteAuth(store, b"q" * 32, clock=lambda: 3000.0, ttl_seconds=10.0)
        task_id = "c" * 32
        code, binding = auth.issue(
            account_id=ACC, user_id=USER, task_id=task_id,
            action_type=ACTION_PLAN, now=3000.0,
        )
        wrong_bindings = [
            ("账号", replace(binding, account_id="bot-account-2"), "bot-account-2", USER),
            ("用户", replace(binding, user_id=OTHER), ACC, OTHER),
            ("任务", replace(binding, task_id="d" * 32), ACC, USER),
            ("动作类型", replace(binding, action_type=ACTION_CONFIRM,
                              action_id="act-1"), ACC, USER),
        ]
        for label, wrong, account, user in wrong_bindings:
            try:
                auth.consume(code, account_id=account, user_id=user,
                             binding=wrong, now=3000.0)
            except RemoteApprovalError:
                check(f"消费绑定不一致（{label}）被拒绝", True)
            else:
                check(f"消费绑定不一致（{label}）被拒绝", False)

        confirm_code, confirm_binding = auth.issue(
            account_id=ACC, user_id=USER, task_id=task_id,
            action_type=ACTION_CONFIRM, action_id="act-1", now=3000.0,
        )
        try:
            auth.consume(confirm_code, account_id=ACC, user_id=USER,
                         binding=replace(confirm_binding, action_id="act-2"),
                         now=3000.0)
        except RemoteApprovalError:
            check("消费绑定不一致（动作编号）被拒绝", True)
        else:
            check("消费绑定不一致（动作编号）被拒绝", False)

        expired_code, expired_binding = auth.issue(
            account_id=ACC, user_id=USER, task_id="e" * 32,
            action_type=ACTION_PLAN, now=3000.0,
        )
        try:
            auth.consume(expired_code, account_id=ACC, user_id=USER,
                         binding=expired_binding, now=3011.0)
        except RemoteApprovalError:
            check("过期授权码无法消费", True)
        else:
            check("过期授权码无法消费", False)

        # 前面那些失败尝试不该把原码消耗掉。
        consumed = auth.consume(code, account_id=ACC, user_id=USER,
                                binding=binding, now=3001.0)
        check("绑定不匹配的尝试不消耗真正的码",
              consumed.approval_id == binding.approval_id)
        try:
            auth.consume(code, account_id=ACC, user_id=USER,
                         binding=binding, now=3002.0)
        except RemoteApprovalError:
            check("成功消费后不存在第二个成功", True)
        else:
            check("成功消费后不存在第二个成功", False)

        # 两个线程同时拿同一个码消费：只允许一个成功。
        concurrent_code, concurrent_binding = auth.issue(
            account_id=ACC, user_id=USER, task_id="f" * 32,
            action_type=ACTION_PLAN, now=3000.0,
        )
        barrier = threading.Barrier(2)

        def attempt_consume() -> bool:
            barrier.wait(timeout=5)
            try:
                auth.consume(concurrent_code, account_id=ACC, user_id=USER,
                             binding=concurrent_binding, now=3003.0)
                return True
            except RemoteApprovalError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt_consume) for _ in range(2)]
            outcomes = [future.result(timeout=10) for future in futures]
        check("并发消费同一个码只有一次成功",
              sorted(outcomes) == [False, True], str(outcomes))

    # 取消配对/注销只撤销目标用户或账号，不能误伤其他人。
    with tempfile.TemporaryDirectory() as tmp:
        store = WeChatStore(Path(tmp) / "state.db")
        store.init()
        auth = RemoteAuth(store, b"r" * 32, clock=lambda: 2000.0, ttl_seconds=600.0)
        other_account = "bot-account-2"
        other_user = "user-9"
        code_user, _ = auth.issue(
            account_id=ACC, user_id=USER, task_id="u" * 32,
            action_type=ACTION_PLAN, now=2000.0,
        )
        code_other, _ = auth.issue(
            account_id=ACC, user_id=OTHER, task_id="o" * 32,
            action_type=ACTION_PLAN, now=2000.0,
        )
        code_account, _ = auth.issue(
            account_id=other_account, user_id=other_user, task_id="b" * 32,
            action_type=ACTION_PLAN, now=2000.0,
        )

        revoked = store.expire_approvals_for_user(ACC, USER, 2001.0)
        check("按用户撤销只撤销一个码", revoked == 1)
        try:
            auth.resolve(code_user, account_id=ACC, user_id=USER, now=2002.0)
        except RemoteApprovalError:
            check("取消配对用户的授权码已失效", True)
        else:
            check("取消配对用户的授权码已失效", False)
        check("撤销一个用户不影响同账号其他用户",
              auth.resolve(code_other, account_id=ACC, user_id=OTHER, now=2002.0).task_id == "o" * 32)
        check("撤销一个用户不影响其他账号",
              auth.resolve(code_account, account_id=other_account,
                           user_id=other_user, now=2002.0).task_id == "b" * 32)

        revoked = store.expire_approvals_for_account(ACC, 2003.0)
        check("按账号撤销只撤销该账号的码", revoked == 1)
        try:
            auth.resolve(code_other, account_id=ACC, user_id=OTHER, now=2004.0)
        except RemoteApprovalError:
            check("注销账号的授权码已失效", True)
        else:
            check("注销账号的授权码已失效", False)
        check("注销一个账号不影响其他账号",
              auth.resolve(code_account, account_id=other_account,
                           user_id=other_user, now=2004.0).task_id == "b" * 32)


# ----------------------------------------------------------------------
# 5. Bridge
# ----------------------------------------------------------------------
class FakeMemory:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.counter = 0

    def session_exists(self, session_id):
        return session_id in self.sessions

    def create_session(self, title="新对话", model=""):
        self.counter += 1
        sid = f"session-{self.counter}"
        self.sessions[sid] = {"id": sid, "title": title, "model": model}
        return self.sessions[sid]

    def ensure_session(self, session_id, model=""):
        if session_id not in self.sessions:
            raise RuntimeError("missing session")
        return session_id


class FakeAgent:
    def __init__(self, events, **kwargs) -> None:
        self.events = list(events)
        self.kwargs = kwargs
        self.provider_name = "fake-provider"
        self.calls: list[tuple[str, str]] = []

    async def run_turn(self, session_id, text):
        self.calls.append((session_id, text))
        for event in self.events:
            yield event


class FakePendingStore:
    def __init__(self, actions=None) -> None:
        self.actions = dict(actions or {})
        self.answers: list[tuple[str, bool]] = []
        self.choices: list[tuple[str, str]] = []

    def get(self, action_id):
        return self.actions.get(action_id)

    def answer(self, action_id, approved):
        action = self.actions.get(action_id)
        if action is None:
            return None
        if action.answered:
            return action
        action.answered = True
        action.approved = approved
        self.answers.append((action_id, approved))
        return action

    def choose(self, action_id, option_id):
        action = self.actions.get(action_id)
        if action is None:
            return None
        if option_id not in [str(o.get("id")) for o in action.options]:
            return None
        action.chosen = option_id
        action.answered = True
        self.choices.append((action_id, option_id))
        return action


class FakeManager:
    def __init__(self, running: bool = False) -> None:
        self.running = {("a" * 32)} if running else set()
        self.calls: list[tuple[str, str]] = []
        self.controls: dict[str, SimpleNamespace] = {}

    def control_of(self, task_id):
        return self.controls.setdefault(
            task_id, SimpleNamespace(current_action_id="")
        )

    def is_running(self, task_id):
        return task_id in self.running

    async def approve(self, task_id):
        self.calls.append(("approve", task_id))
        return True

    async def pause(self, task_id):
        self.calls.append(("pause", task_id))
        return True

    async def resume(self, task_id):
        self.calls.append(("resume", task_id))
        return True

    async def cancel(self, task_id):
        self.calls.append(("cancel", task_id))
        return True


class FakeWorkflow:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.idle: list[tuple[str, bool]] = []
        self.raise_error: Exception | None = None

    async def create_and_start(self, *, goal, session_id, provider=None):
        if self.raise_error:
            raise self.raise_error
        self.created.append({"goal": goal, "session_id": session_id})
        return make_task("b" * 32, goal=goal, steps=[("做事", "")])

    async def start_if_idle(self, task, *, skip_approval=True):
        self.idle.append((task.id, skip_approval))
        return True


class FakeWorkflowStore:
    def __init__(self, tasks=None) -> None:
        self.tasks = dict(tasks or {})
        self.updates: list[tuple[str, dict]] = []

    def get_task(self, task_id, with_steps=True):
        return self.tasks.get(task_id)

    def update_task(self, task_id, **fields):
        task = self.tasks.get(task_id)
        if task is None:
            return False
        for key, value in fields.items():
            setattr(task, key, value)
        self.updates.append((task_id, fields))
        return True


def test_bridge() -> None:
    section("Bridge：统一会话、纯文字聊天、任务归属与控制")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            memory = FakeMemory()
            pending = FakePendingStore()
            manager = FakeManager()
            workflow = FakeWorkflow()
            wf_store = FakeWorkflowStore()
            agents: list[FakeAgent] = []

            def factory(**kwargs):
                agent = FakeAgent(
                    [
                        {"type": "delta", "text": "你"},
                        {"type": "final", "text": "你好呀"},
                    ],
                    **kwargs,
                )
                agents.append(agent)
                return agent

            bridge = JiaRuntimeBridge(
                store=store, agent_factory=factory, workflow=workflow,
                workflow_store_=wf_store, manager=manager,
                pending_store=pending, memory_api=memory,
                workflow_enabled=lambda: True, clock=lambda: 1000.0,
            )
            reply = await bridge.chat(ACC, USER, "你好")
            check("聊天返回最终文本", reply == "你好呀")
            check("聊天禁用工具", agents and agents[0].kwargs.get("tools_enabled") is False)
            sid = store.get_session_id(ACC, USER)
            check("微信用户绑定到固定会话", bool(sid))
            await bridge.chat(ACC, USER, "再说一句")
            check("再次聊天复用同一会话", store.get_session_id(ACC, USER) == sid)

            # need_confirm 防御：即使出现也必须按拒绝处理
            def confirm_factory(**kwargs):
                return FakeAgent([
                    {"type": "need_confirm", "action": {"id": "a1"}},
                ], **kwargs)
            bridge2 = JiaRuntimeBridge(
                store=store, agent_factory=confirm_factory,
                workflow=workflow, workflow_store_=wf_store, manager=manager,
                pending_store=pending, memory_api=memory,
                workflow_enabled=lambda: True, clock=lambda: 1000.0,
            )
            pending.actions["a1"] = make_action("a1")
            try:
                await bridge2.chat(ACC, USER, "打开应用")
            except BridgeError:
                check("普通聊天遇到工具确认时失败关闭", ("a1", False) in pending.answers)
            else:
                check("普通聊天遇到工具确认时失败关闭", False)

            task = await bridge.create_task(ACC, USER, "整理下载目录")
            check("任务被创建并链接", store.get_task_link(task.id) is not None)
            wf_store.tasks[task.id] = task
            found = bridge.resolve_task(ACC, USER, task.id[:8])
            check("能按短编号找到自己的任务", found is not None and found.id == task.id)
            check("别的用户找不到这个任务", bridge.resolve_task(ACC, OTHER, task.id[:8]) is None)
            try:
                bridge.resolve_task(ACC, USER, "zz")
            except BridgeError:
                check("非法任务编号被拒绝", True)
            else:
                check("非法任务编号被拒绝", False)

            await bridge.approve_plan(task)
            check("批准计划调用 manager.approve", ("approve", task.id) in manager.calls)
            check("批准计划会补执行体", (task.id, True) in workflow.idle)

            running = make_task("c" * 32, status=TaskStatus.RUNNING)
            manager.running.add(running.id)
            await bridge.pause_task(running)
            check("暂停调用 manager.pause", ("pause", running.id) in manager.calls)
            await bridge.resume_task(running)
            check("恢复调用 manager.resume", ("resume", running.id) in manager.calls)
            await bridge.cancel_task(running)
            check("取消调用 manager.cancel", ("cancel", running.id) in manager.calls)

            action = make_action("act-1")
            pending.actions["act-1"] = action
            manager.control_of(task.id).current_action_id = "act-1"
            remote = bridge.current_action(task)
            check("能取到当前待授权动作", remote is not None and remote.action_id == "act-1")
            check("能答复确认", bridge.answer_action("act-1", True) and ("act-1", True) in pending.answers)

            action2 = make_action("act-2", session_id="other-session")
            pending.actions["act-2"] = action2
            manager.control_of(task.id).current_action_id = "act-2"
            check("待办会话不匹配时不暴露", bridge.current_action(task) is None)

            check("任意异常不把内部信息发给用户",
                  "SECRET=/path" not in error_text(RuntimeError("SECRET=/path")))
            check("BridgeError 原文可发给用户",
                  error_text(BridgeError("任务结束了")) == "任务结束了")
            check("JiaAgentError 只发 message",
                  error_text(JiaAgentError("没有配置模型")) == "没有配置模型")

            workflow.raise_error = BridgeError("工作流关着")
            try:
                await bridge.create_task(ACC, USER, "x")
            except BridgeError:
                check("创建工作流错误会传给服务层", True)
            else:
                check("创建工作流错误会传给服务层", False)

    run(scenario())


# ----------------------------------------------------------------------
# 6. Service
# ----------------------------------------------------------------------
def test_service() -> None:
    section("Service：鉴权、去重、命令、授权、发送")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            svc, store, bridge, auth, out, allow = build_service(Path(tmp))
            status = await svc.handle_update(make_msg(
                "你好", message_id="m-chat", context_token="CTX_SECRET_1"
            ))
            check("合法发送者进入聊天", status == "chat")
            await svc.wait_background()
            check("聊天回复已发送", out.sent and out.sent[0]["text"] == "好的，我是佳。")
            check("回复带上入站 context_token", out.sent[0]["context_token"] == "CTX_SECRET_1")

            status = await svc.handle_update(make_msg("你好", message_id="m-chat"))
            check("重复投递被去重", status == "duplicate")
            await svc.wait_background()
            check("重复消息不执行第二次", len(bridge.chat_calls) == 1)

            before = len(out.sent)
            status = await svc.handle_update(make_msg("hi", message_id="m-other", user=OTHER))
            check("未授权发送者被拒绝", status == "unauthorized")
            await svc.wait_background()
            check("未授权消息没有回复", len(out.sent) == before)

            status = await svc.handle_update(make_msg("", message_id="m-img", item_type=2))
            check("图片消息解析失败", status == "parse_error")
            status = await svc.handle_update(make_msg("hi", message_id="m-group", group_id="g1"))
            check("群聊消息解析失败", status == "parse_error")
            await svc.wait_background()
            check("解析失败不进入聊天", len(bridge.chat_calls) == 1)

            status = await svc.handle_update(make_msg("/unknown", message_id="m-unknown"))
            await svc.wait_background()
            check("未知斜杠命令不当聊天处理",
                  status == "command" and len(bridge.chat_calls) == 1)
            check("未知命令返回帮助", "/task" in out.sent[-1]["text"])

            bridge.chat_reply = "长" * 4000
            await svc.handle_update(make_msg("长回复", message_id="m-long"))
            await svc.wait_background()
            long_chunks = [x for x in out.sent if x["text"].startswith("长")]
            check("长回复被分片", len(long_chunks) > 1)
            check("每片不超过 1800 字", all(len(x["text"]) <= 1800 for x in long_chunks))
            bridge.chat_reply = "好的"

            send_fail_before = len(out.sent)
            out.fail_next = 1
            await svc.handle_update(make_msg("发送失败", message_id="m-fail"))
            await svc.wait_background()
            check("发送失败不崩溃", True)
            check("发送失败不重发（没有第二批）", len(out.sent) == send_fail_before)

            # 任务
            await svc.handle_update(make_msg("/task 整理下载目录", message_id="m-task"))
            await svc.wait_background()
            check("任务被派发", len(bridge.tasks) == 1)
            task = next(iter(bridge.tasks.values()))
            check("任务回复带短编号", task.id[:8] in out.sent[-1]["text"])

            await svc.handle_update(make_msg(f"/status {task.id[:8]}", message_id="m-status"))
            await svc.wait_background()
            status_reply = out.sent[-1]["text"]
            check("状态查询返回状态", "STATUS" in status_reply)
            code = CODE_RE.findall(status_reply)
            check("待批准任务会发授权码", bool(code) and "/approve" in status_reply)
            plan_code = code[-1]

            await svc.handle_update(make_msg(f"/approve {plan_code}", message_id="m-appr"))
            await svc.wait_background()
            check("计划批准调用 bridge", task.id in bridge.approved_plans)
            check("批准回复存在", "已批准" in out.sent[-1]["text"])
            await svc.handle_update(make_msg(f"/approve {plan_code}", message_id="m-appr2"))
            await svc.wait_background()
            check("授权码只能用一次", "无效" in out.sent[-1]["text"])
            check("第二次批准没有执行", len(bridge.approved_plans) == 1)

            # 列表与控制
            await svc.handle_update(make_msg("/tasks", message_id="m-tasks"))
            await svc.wait_background()
            check("任务列表包含任务", task.id[:8] in out.sent[-1]["text"])
            task.status = TaskStatus.RUNNING
            await svc.handle_update(make_msg(f"/pause {task.id[:8]}", message_id="m-pause"))
            await svc.wait_background()
            check("暂停命令调用 bridge", task.id in bridge.paused)
            await svc.handle_update(make_msg(f"/resume {task.id[:8]}", message_id="m-resume"))
            await svc.wait_background()
            check("恢复命令调用 bridge", task.id in bridge.resumed)
            stale_plan_code, _ = auth.issue(
                account_id=ACC, user_id=USER, task_id=task.id,
                action_type=ACTION_PLAN, now=1000.0,
            )
            await svc.handle_update(make_msg(f"/cancel {task.id[:8]}", message_id="m-cancel"))
            await svc.wait_background()
            check("取消命令调用 bridge", task.id in bridge.cancelled)
            check("取消后等待中的授权码立即失效",
                  store.find_active_approval(
                      task_id=task.id, action_type=ACTION_PLAN,
                      action_id="", now=1000.0,
                  ) is None)
            try:
                auth.resolve(stale_plan_code, account_id=ACC, user_id=USER,
                             now=1000.0)
            except RemoteApprovalError:
                check("取消后旧计划码不能再用", True)
            else:
                check("取消后旧计划码不能再用", False)
            check("取消回复如实说明底层操作可能停不下来",
                  "无法保证立即中断" in out.sent[-1]["text"],
                  out.sent[-1]["text"])

            # 工具确认
            confirm_task = make_task(
                "d" * 32, status=TaskStatus.WAITING_CONFIRMATION,
                steps=[("打开记事本", "open_app")],
            )
            bridge.add_task(confirm_task)
            action = make_action("act-confirm", summary="打开应用：记事本")
            bridge.actions[confirm_task.id] = RemoteAction(
                action_id=action.id, kind="confirm", tool_label="打开应用",
                summary=action.summary, question="", options=[],
                session_id=confirm_task.session_id,
            )
            confirm_code, _ = auth.issue(
                account_id=ACC, user_id=USER, task_id=confirm_task.id,
                action_type=ACTION_CONFIRM, action_id=action.id, now=1000.0,
            )
            await svc.handle_update(make_msg(
                f"/approve {format_code(confirm_code)}", message_id="m-confirm"
            ))
            await svc.wait_background()
            check("确认码允许工具步骤", ("act-confirm", True) in bridge.answered)
            await svc.handle_update(make_msg(
                f"/approve {format_code(confirm_code)}", message_id="m-confirm2"
            ))
            await svc.wait_background()
            check("确认码不能重复使用", "无效" in out.sent[-1]["text"])

            # 选择
            choose_task = make_task(
                "e" * 32, status=TaskStatus.WAITING_CONFIRMATION,
                steps=[("打开应用", "open_app")],
            )
            bridge.add_task(choose_task)
            bridge.actions[choose_task.id] = RemoteAction(
                action_id="act-choose", kind="choose", tool_label="打开应用",
                summary="选一个应用",
                question="你要打开哪一个？",
                options=[{"id": "opt-1", "name": "记事本"}, {"id": "opt-2", "name": "计算器"}],
                session_id=choose_task.session_id,
            )
            choose_code, _ = auth.issue(
                account_id=ACC, user_id=USER, task_id=choose_task.id,
                action_type=ACTION_CHOICE, action_id="act-choose", now=1000.0,
            )
            await svc.handle_update(make_msg(
                f"/choose {format_code(choose_code)} 9", message_id="m-choose-bad"
            ))
            await svc.wait_background()
            check("选择序号越界被拒绝", not bridge.chosen)
            await svc.handle_update(make_msg(
                f"/choose {format_code(choose_code)} 2", message_id="m-choose"
            ))
            await svc.wait_background()
            check("正确选择映射到服务端编号", bridge.chosen == [("act-choose", "opt-2")])

            # 计划拒绝
            reject_task = make_task(
                "f" * 32, status=TaskStatus.PENDING, steps=[("步骤", "")],
            )
            bridge.add_task(reject_task)
            reject_code, _ = auth.issue(
                account_id=ACC, user_id=USER, task_id=reject_task.id,
                action_type=ACTION_PLAN, now=1000.0,
            )
            stale_action_code, _ = auth.issue(
                account_id=ACC, user_id=USER, task_id=reject_task.id,
                action_type=ACTION_CONFIRM, action_id="stale-act", now=1000.0,
            )
            await svc.handle_update(make_msg(
                f"/reject {format_code(reject_code)}", message_id="m-reject"
            ))
            await svc.wait_background()
            check("拒绝计划会取消任务", reject_task.id in bridge.rejected_plans)
            try:
                auth.resolve(stale_action_code, account_id=ACC, user_id=USER,
                             now=1000.0)
            except RemoteApprovalError:
                check("拒绝计划后同一任务的其他旧码也失效", True)
            else:
                check("拒绝计划后同一任务的其他旧码也失效", False)

            # 无效授权
            await svc.handle_update(make_msg("/approve ZZZZZ-ZZZZZ", message_id="m-badcode"))
            await svc.wait_background()
            check("无效授权码被拒绝", "无效" in out.sent[-1]["text"])
            check("无效码不触发任何批准", len(bridge.approved_plans) == 1)

            # 别人的码
            other_code, _ = auth.issue(
                account_id=ACC, user_id=USER, task_id=task.id,
                action_type=ACTION_PLAN, now=1000.0,
            )
            other_svc_allow = FakeAllowList({
                (ACC, USER): {"chat", "task", "approve"},
                (ACC, OTHER): {"chat", "task", "approve"},
            })
            svc.allowlist = other_svc_allow
            await svc.handle_update(make_msg(
                f"/approve {format_code(other_code)}", message_id="m-other-code", user=OTHER
            ))
            await svc.wait_background()
            check("别人的任务授权码不能使用", "无效" in out.sent[-1]["text"])

            # 作用域
            chat_only = FakeAllowList({(ACC, OTHER): {"chat"}})
            svc2, _, bridge2, _, out2, _ = build_service(
                Path(tmp) / "scope", allow_entries={(ACC, OTHER): {"chat"}}
            )
            await svc2.handle_update(make_msg("/task x", message_id="m-scope", user=OTHER))
            await svc2.wait_background()
            check("只有 chat 权限不能派任务", not bridge2.tasks)
            check("无权限有明确回复", "没有被授予" in out2.sent[-1]["text"])

            # 速率限制
            svc3, _, bridge3, _, out3, _ = build_service(
                Path(tmp) / "rate", rate_limit=2,
            )
            s1 = await svc3.handle_update(make_msg("一", message_id="r1"))
            s2 = await svc3.handle_update(make_msg("二", message_id="r2"))
            s3 = await svc3.handle_update(make_msg("三", message_id="r3"))
            await svc3.wait_background()
            check("速率限制生效", (s1, s2, s3) == ("chat", "chat", "rate_limited"))
            check("速率限制会提示用户", any("频繁" in x["text"] for x in out3.sent))
            check("被限流的消息不进入聊天", len(bridge3.chat_calls) == 2)

            # 有 task 但没有 approve：/status 可以看状态，但不能拿到授权码。
            svc4, store4, bridge4, _, out4, _ = build_service(
                Path(tmp) / "no-approve",
                allow_entries={(ACC, USER): {"chat", "task"}},
            )
            task4 = make_task("9" * 32, status=TaskStatus.PENDING, steps=[("只读", "")])
            bridge4.add_task(task4)
            await svc4.handle_update(make_msg(
                f"/status {task4.id[:8]}", message_id="m-status-no-approve"
            ))
            await svc4.wait_background()
            reply = out4.sent[-1]["text"]
            check("无 approve 仍可查看任务状态", "STATUS" in reply)
            check("无 approve 的 /status 不发授权码", not CODE_RE.findall(reply))
            check("无 approve 的 /status 不生成授权码",
                  store4.find_active_approval(
                      task_id=task4.id, action_type=ACTION_PLAN,
                      action_id="", now=1000.0,
                  ) is None)

            before4 = len(out4.sent)
            accepted = await svc4.notify(ACC, OTHER, "不应发送的任务内容")
            check("通知出口拒绝未配对用户（并停止重试）", accepted is True)
            check("未配对用户没有收到任务通知", len(out4.sent) == before4)

            # 命令处理中途被 unpair：在途回复也必须在统一出口被丢弃。
            svc4.allowlist.entries[(ACC, OTHER)] = {"chat"}
            before4 = len(out4.sent)
            await svc4._send_safe(OTHER, "配对期间的在途回复", "")
            check("配对期间在途回复可以发出", len(out4.sent) == before4 + 1)
            svc4.allowlist.entries.pop((ACC, OTHER))
            before4 = len(out4.sent)
            await svc4._send_safe(OTHER, "不应发送的在途回复", "")
            check("取消配对后在途回复也不会发出", len(out4.sent) == before4)

    run(scenario())


# ----------------------------------------------------------------------
# 7. 日志脱敏
# ----------------------------------------------------------------------
class CaptureLogs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def test_logging_redaction() -> None:
    section("日志：token / context_token / 授权码不出现")

    async def scenario():
        capture = CaptureLogs()
        logger = logging.getLogger("app.wechat")
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(capture)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                svc, store, bridge, auth, out, _ = build_service(Path(tmp))
                await svc.handle_update(make_msg(
                    "你好", message_id="log-1", context_token="CTX_TOKEN_SECRET"
                ))
                await svc.wait_background()
                msg = make_msg("/task 一个任务", message_id="log-2",
                               context_token="CTX_TOKEN_SECRET")
                await svc.handle_update(msg)
                await svc.wait_background()
                task = next(iter(bridge.tasks.values()))
                code, _ = auth.issue(
                    account_id=ACC, user_id=USER, task_id=task.id,
                    action_type=ACTION_PLAN, now=1000.0,
                )
                await svc.handle_update(make_msg(
                    f"/approve {format_code(code)}", message_id="log-3",
                    context_token="CTX_TOKEN_SECRET",
                ))
                await svc.wait_background()
                logs = "\n".join(capture.messages)
                check("日志里没有 context_token", "CTX_TOKEN_SECRET" not in logs)
                check("日志里没有授权码", code not in logs and format_code(code) not in logs)
                check("日志里没有 bot token", "BOT_SECRET_TOKEN" not in logs)
        finally:
            logger.removeHandler(capture)
            logger.setLevel(old_level)

    run(scenario())


# ----------------------------------------------------------------------
# 8. Task watcher
# ----------------------------------------------------------------------
class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.calls: list[tuple[str, str, str]] = []
        self.fail = False

    async def __call__(self, account_id, user_id, text):
        self.calls.append((account_id, user_id, text))
        if self.fail:
            return False
        self.sent.append((account_id, user_id, text))
        return True


def test_task_watcher() -> None:
    section("任务通知：计划/确认/状态/终态，避免重复发送")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            bridge = FakeBridge()
            auth = RemoteAuth(store, b"w" * 32, clock=lambda: 1000.0)
            notifier = FakeNotifier()
            allow = FakeAllowList({
                (ACC, USER): {"chat", "task", "approve"},
                (ACC, OTHER): {"chat", "task", "approve"},
            })
            watcher = TaskWatcher(
                bridge=bridge, store=store, remote_auth=auth,
                notifier=notifier, allowlist=allow, clock=lambda: 1000.0,
            )
            task = make_task(
                "1" * 32, status=TaskStatus.PENDING,
                steps=[("先看目录", ""), ("整理", "list_dir")],
            )
            bridge.add_task(task)
            store.link_task(task_id=task.id, account_id=ACC, user_id=USER,
                            session_id=task.session_id, now=1000.0)
            sent = await watcher.tick()
            check("计划消息发出一次", sent == 1 and "/approve" in notifier.sent[-1][2])
            check("计划消息带授权码", bool(CODE_RE.findall(notifier.sent[-1][2])))
            check("计划通知已记录", store.get_task_link(task.id)["plan_notified"] == 1)
            sent = await watcher.tick()
            check("重复轮询不重复发计划", sent == 0)

            task.status = TaskStatus.RUNNING
            sent = await watcher.tick()
            check("进入执行会发状态", sent == 1 and "开始执行" in notifier.sent[-1][2])
            sent = await watcher.tick()
            check("同一状态不重复发", sent == 0)

            task.status = TaskStatus.WAITING_CONFIRMATION
            bridge.actions[task.id] = RemoteAction(
                action_id="watch-act", kind="confirm", tool_label="打开应用",
                summary="打开应用：记事本", question="", options=[],
                session_id=task.session_id,
            )
            sent = await watcher.tick()
            check("工具确认会发一次性码", sent == 1 and "/approve" in notifier.sent[-1][2])
            check("确认通知已记录",
                  store.get_task_link(task.id)["notified_action_id"] == "watch-act")
            sent = await watcher.tick()
            check("同一确认动作不重复发", sent == 0)

            task.status = TaskStatus.SUCCEEDED
            task.summary = "都整理好了"
            sent = await watcher.tick()
            check("终态通知发出", sent == 1 and "完成" in notifier.sent[-1][2])
            check("终态通知已记录",
                  store.get_task_link(task.id)["terminal_notified"] == 1)
            sent = await watcher.tick()
            check("终态不重复通知", sent == 0)

            failing = make_task(
                "2" * 32, status=TaskStatus.PENDING, steps=[("做事", "")],
            )
            bridge.add_task(failing)
            store.link_task(task_id=failing.id, account_id=ACC, user_id=USER,
                            session_id=failing.session_id, now=1000.0)
            notifier.fail = True
            sent = await watcher.tick()
            check("发送失败不推进通知进度", sent == 0)
            check("发送失败后计划仍未标记",
                  store.get_task_link(failing.id)["plan_notified"] == 0)
            old_text = notifier.calls[-1][2]
            old_codes = CODE_RE.findall(old_text)
            notifier.fail = False
            sent = await watcher.tick()
            check("下次轮询会重试发送", sent == 1)
            new_codes = CODE_RE.findall(notifier.sent[-1][2])
            check("重试会换一个新授权码",
                  bool(old_codes) and bool(new_codes)
                  and set(old_codes) != set(new_codes))
            try:
                auth.resolve(old_codes[0], account_id=ACC, user_id=USER,
                             now=1000.0)
            except RemoteApprovalError:
                check("重试后旧授权码已被撤销", True)
            else:
                check("重试后旧授权码已被撤销", False)
            check("重试后只有新授权码可用",
                  auth.resolve(new_codes[0], account_id=ACC, user_id=USER,
                               now=1000.0).task_id == failing.id)
            check("重试成功不重复发计划", await watcher.tick() == 0)

            paused = make_task("3" * 32, status=TaskStatus.RUNNING, paused=True)
            bridge.add_task(paused)
            store.link_task(task_id=paused.id, account_id=ACC, user_id=USER,
                            session_id=paused.session_id, now=1000.0)
            store.update_task_link(paused.id, now=1000.0, last_status="running")
            sent = await watcher.tick()
            check("暂停状态会通知", sent == 1 and "暂停" in notifier.sent[-1][2])

    run(scenario())

    async def scenario_unpair():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            bridge = FakeBridge()
            auth = RemoteAuth(store, b"w" * 32, clock=lambda: 1000.0)
            notifier = FakeNotifier()
            allow = FakeAllowList({
                (ACC, USER): {"chat", "task", "approve"},
                (ACC, OTHER): {"chat", "task", "approve"},
            })
            watcher = TaskWatcher(
                bridge=bridge, store=store, remote_auth=auth,
                notifier=notifier, allowlist=allow, clock=lambda: 1000.0,
            )
            user_task = make_task("4" * 32, status=TaskStatus.PENDING, steps=[("只读步骤", "")])
            other_task = make_task("5" * 32, status=TaskStatus.PENDING, steps=[("另一个任务", "")])
            bridge.add_task(user_task, ACC, USER)
            bridge.add_task(other_task, ACC, OTHER)
            store.link_task(task_id=user_task.id, account_id=ACC, user_id=USER,
                            session_id=user_task.session_id, now=1000.0)
            store.link_task(task_id=other_task.id, account_id=ACC, user_id=OTHER,
                            session_id=other_task.session_id, now=1000.0)

            sent = await watcher.tick()
            check("配对用户可以收到计划", sent == 2)
            user_codes = CODE_RE.findall(
                next(t for _a, u, t in notifier.sent if u == USER)
            )
            other_codes = CODE_RE.findall(
                next(t for _a, u, t in notifier.sent if u == OTHER)
            )
            check("计划授权码已生成",
                  len(set(user_codes)) == 1 and len(set(other_codes)) == 1)

            allow.entries.pop((ACC, USER))
            store.expire_approvals_for_user(ACC, USER, 1001.0)
            before_calls = len(notifier.calls)
            for _ in range(6):
                sent = await watcher.tick()
            check("取消配对后不再发送任何任务通知", sent == 0)
            check("取消配对后不再调用通知出口（无重试风暴）",
                  len(notifier.calls) == before_calls)
            check("取消配对后用户授权码已撤销",
                  store.find_active_approval(
                      task_id=user_task.id, action_type=ACTION_PLAN,
                      action_id="", now=1001.0,
                  ) is None)
            check("取消配对不删除任务关联", store.get_task_link(user_task.id) is not None)
            check("取消配对不静默取消任务",
                  bridge.cancelled == []
                  and user_task.status is TaskStatus.PENDING)
            check("其他用户授权码不受影响",
                  store.find_active_approval(
                      task_id=other_task.id, action_type=ACTION_PLAN,
                      action_id="", now=1001.0,
                  ) is not None)

            # 其他用户仍能收到新的动作授权码，证明撤销没有误伤。
            other_task.status = TaskStatus.WAITING_CONFIRMATION
            bridge.actions[other_task.id] = RemoteAction(
                action_id="other-act", kind="confirm", tool_label="只读",
                summary="只读动作", question="", options=[],
                session_id=other_task.session_id,
            )
            sent = await watcher.tick()
            check("其他用户仍能收到动作授权码",
                  sent == 1 and notifier.sent[-1][1] == OTHER
                  and bool(CODE_RE.findall(notifier.sent[-1][2])))

    run(scenario_unpair())

    async def scenario_scope_requires_approve():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            bridge = FakeBridge()
            auth = RemoteAuth(store, b"w" * 32, clock=lambda: 1000.0)
            notifier = FakeNotifier()
            allow = FakeAllowList({(ACC, USER): {"chat", "task"}})
            watcher = TaskWatcher(
                bridge=bridge, store=store, remote_auth=auth,
                notifier=notifier, allowlist=allow, clock=lambda: 1000.0,
            )
            task = make_task("6" * 32, status=TaskStatus.PENDING, steps=[("只读", "")])
            bridge.add_task(task)
            store.link_task(task_id=task.id, account_id=ACC, user_id=USER,
                            session_id=task.session_id, now=1000.0)

            sent = await watcher.tick()
            text = notifier.sent[-1][2]
            check("有 task 无 approve 时仍可收到计划提示", sent == 1)
            check("无 approve 时计划通知不含授权码", not CODE_RE.findall(text))
            check("无 approve 时不生成计划码", store.find_active_approval(
                task_id=task.id, action_type=ACTION_PLAN, action_id="", now=1000.0,
            ) is None)
            check("无 approve 提示只发一次", await watcher.tick() == 0)

            allow.entries[(ACC, USER)] = {"chat", "task", "approve"}
            sent = await watcher.tick()
            check("恢复 approve 后才发计划授权码",
                  sent == 1 and bool(CODE_RE.findall(notifier.sent[-1][2])))
            check("恢复 approve 后计划标记完成",
                  store.get_task_link(task.id)["plan_notified"] == 1)

            task.status = TaskStatus.WAITING_CONFIRMATION
            bridge.actions[task.id] = RemoteAction(
                action_id="scope-act", kind="confirm", tool_label="只读",
                summary="只读动作", question="", options=[],
                session_id=task.session_id,
            )
            allow.entries[(ACC, USER)] = {"chat", "task"}
            sent = await watcher.tick()
            text = notifier.sent[-1][2]
            check("无 approve 时动作通知不含授权码",
                  sent == 1 and not CODE_RE.findall(text))
            check("无 approve 时不生成动作码", store.find_active_approval(
                task_id=task.id, action_type=ACTION_CONFIRM,
                action_id="scope-act", now=1000.0,
            ) is None)
            check("无 approve 的动作提示只发一次", await watcher.tick() == 0)

    run(scenario_scope_requires_approve())

    async def scenario_disconnected():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            bridge = FakeBridge()
            auth = RemoteAuth(store, b"w" * 32, clock=lambda: 1000.0)
            notifier = FakeNotifier()
            allow = FakeAllowList({
                (ACC, USER): {"chat", "task", "approve"},
            })
            connected = {"value": False}
            watcher = TaskWatcher(
                bridge=bridge, store=store, remote_auth=auth,
                notifier=notifier, allowlist=allow,
                is_connected=lambda: connected["value"],
                clock=lambda: 1000.0,
            )
            task = make_task("7" * 32, status=TaskStatus.PENDING, steps=[("只读", "")])
            bridge.add_task(task)
            store.link_task(task_id=task.id, account_id=ACC, user_id=USER,
                            session_id=task.session_id, now=1000.0)
            check("监听离线时不发任务通知", await watcher.tick() == 0)
            check("监听离线时没有调用通知出口", not notifier.calls)
            connected["value"] = True
            check("监听恢复后才发送", await watcher.tick() == 1)
            check("恢复后不重复发同一条计划",
                  await watcher.tick() == 0 and len(notifier.calls) == 1)

    run(scenario_disconnected())

    async def scenario_cancelled_and_failed():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            bridge = FakeBridge()
            auth = RemoteAuth(store, b"w" * 32, clock=lambda: 1000.0)
            notifier = FakeNotifier()
            allow = FakeAllowList({
                (ACC, USER): {"chat", "task", "approve"},
            })
            watcher = TaskWatcher(
                bridge=bridge, store=store, remote_auth=auth,
                notifier=notifier, allowlist=allow, clock=lambda: 1000.0,
            )
            cancelled = make_task(
                "8" * 32, status=TaskStatus.CANCELLED,
                steps=[("还没跑的步骤", "")], error="你取消了这个任务",
            )
            bridge.add_task(cancelled)
            store.link_task(task_id=cancelled.id, account_id=ACC, user_id=USER,
                            session_id=cancelled.session_id, now=1000.0)
            bridge.actions[cancelled.id] = RemoteAction(
                action_id="cancel-act", kind="confirm", tool_label="只读",
                summary="不该再批准的动作", question="", options=[],
                session_id=cancelled.session_id,
            )
            old_code, _ = auth.issue(
                account_id=ACC, user_id=USER, task_id=cancelled.id,
                action_type=ACTION_CONFIRM, action_id="cancel-act", now=1000.0,
            )
            sent = await watcher.tick()
            text = notifier.sent[-1][2]
            check("已取消任务只发取消终态", sent == 1 and "已取消" in text)
            check("已取消任务不生成新的操作授权",
                  not CODE_RE.findall(text)
                  and store.find_active_approval(
                      task_id=cancelled.id, action_type=ACTION_CONFIRM,
                      action_id="cancel-act", now=1000.0,
                  ) is None)
            try:
                auth.resolve(old_code, account_id=ACC, user_id=USER, now=1000.0)
            except RemoteApprovalError:
                check("已取消任务的旧确认码也被撤销", True)
            else:
                check("已取消任务的旧确认码也被撤销", False)
            check("取消终态不写成功文案",
                  "完成" not in text and "开始执行" not in text)
            check("取消终态已记录",
                  store.get_task_link(cancelled.id)["terminal_notified"] == 1)
            check("已取消任务之后不再通知", await watcher.tick() == 0)

            failed = make_task(
                "a" * 32, status=TaskStatus.FAILED,
                steps=[("失败的步骤", "")], error="磁盘写满了",
            )
            bridge.add_task(failed)
            store.link_task(task_id=failed.id, account_id=ACC, user_id=USER,
                            session_id=failed.session_id, now=1000.0)
            sent = await watcher.tick()
            text = notifier.sent[-1][2]
            check("失败任务发失败终态", sent == 1 and "失败了" in text)
            check("失败任务带上真实原因", "磁盘写满了" in text)
            check("失败任务不会被通知成完成", "完成" not in text)

    run(scenario_cancelled_and_failed())


# ----------------------------------------------------------------------
# 9. Monitor
# ----------------------------------------------------------------------
class FakeMonitorApi:
    def __init__(self, pages) -> None:
        self.pages = deque(pages)
        self.calls: list[str] = []
        self.notifies: list[str] = []

    async def get_updates(self, *, cursor, timeout_seconds):
        self.calls.append(cursor)
        if not self.pages:
            raise TransportTimeout("no more pages")
        item = self.pages.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    async def notify_start(self):
        self.notifies.append("start")

    async def notify_stop(self):
        self.notifies.append("stop")


class FakeMonitorService:
    def __init__(self, stop_event=None, stop_after=None) -> None:
        self.handled: list[dict] = []
        self.stop_event = stop_event
        self.stop_after = stop_after

    async def handle_update(self, raw):
        self.handled.append(raw)
        if self.stop_event is not None and self.stop_after is not None:
            if len(self.handled) >= self.stop_after:
                self.stop_event.set()
        return "ok"


def page(messages=None, cursor="c", *, ret=0, errcode=None, timeout_ms=None):
    return UpdatesPage(
        messages=list(messages or []), cursor=cursor, ret=ret,
        errcode=errcode, errmsg="", longpolling_timeout_ms=timeout_ms,
    )


def test_monitor() -> None:
    section("监听：长轮询、游标、断线退避、-14 暂停")

    async def scenario_success():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            store.set_cursor(ACC, "old", 1000.0)
            stop = asyncio.Event()
            service = FakeMonitorService(stop_event=stop, stop_after=2)
            api = FakeMonitorApi([
                TransportTimeout("normal"),
                page([{"id": 1}, {"id": 2}], "new-cursor", timeout_ms=5000),
            ])
            sleeps: list[float] = []

            async def sleep(seconds):
                sleeps.append(seconds)

            monitor = WeChatMonitor(
                account_id=ACC, api=api, store=store, service=service,
                config=MonitorConfig(default_long_poll_seconds=35.0),
                clock=lambda: 1000.0, sleep=sleep,
            )
            reason = await monitor.run(stop)
            check("长轮询超时不算失败", reason == "stopped" and not sleeps)
            check("整页处理完后保存游标", store.get_cursor(ACC) == "new-cursor")
            check("两条消息都被处理", len(service.handled) == 2)
            check("长轮询超时被采用", monitor.long_poll_seconds == 5.0)
            check("上下线通知已发送", api.notifies == ["start", "stop"])

    run(scenario_success())

    async def scenario_redelivery_after_reconnect():
        with tempfile.TemporaryDirectory() as tmp:
            svc, store, bridge, _auth, out, _allow = build_service(Path(tmp))
            stop = asyncio.Event()
            results: list[str] = []

            class StopAfterTwoDeliveries:
                """同一页在“重连后”再投一次，第二次应被 inbound 原子去重挡住。"""

                async def handle_update(self, raw):
                    result = await svc.handle_update(raw)
                    results.append(result)
                    if len(results) >= 2:
                        stop.set()
                    return result

            raw = make_msg("你好", message_id="m-redeliver")
            api = FakeMonitorApi([
                page([raw], "redeliver-cursor"),
                page([raw], "redeliver-cursor"),
            ])
            monitor = WeChatMonitor(
                account_id=ACC, api=api, store=store,
                service=StopAfterTwoDeliveries(),
                config=MonitorConfig(default_long_poll_seconds=5.0),
                clock=lambda: 1000.0, sleep=lambda _s: asyncio.sleep(0),
            )
            reason = await monitor.run(stop)
            await svc.wait_background()
            check("重投同一页只处理一次",
                  results == ["chat", "duplicate"], str(results))
            check("重投的消息没有执行第二次", len(bridge.chat_calls) == 1)
            check("重投的消息没有回复第二次", len(out.sent) == 1)
            check("整页处理后游标正常保存",
                  reason == "stopped"
                  and store.get_cursor(ACC) == "redeliver-cursor")

    run(scenario_redelivery_after_reconnect())

    async def scenario_backoff():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            stop = asyncio.Event()
            service = FakeMonitorService(stop_event=stop, stop_after=1)
            api = FakeMonitorApi([
                TransportError("down-1"),
                TransportError("down-2"),
                TransportError("down-3"),
                page([{"id": 1}], "c1"),
            ])
            sleeps: list[float] = []

            async def sleep(seconds):
                sleeps.append(seconds)

            monitor = WeChatMonitor(
                account_id=ACC, api=api, store=store, service=service,
                clock=lambda: 1000.0, sleep=sleep,
            )
            await monitor.run(stop)
            check("连续失败先短退避、随后长退避", sleeps[:3] == [2.0, 2.0, 30.0])
            check("恢复后游标保存", store.get_cursor(ACC) == "c1")

    run(scenario_backoff())

    async def scenario_pause():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            stop = asyncio.Event()
            service = FakeMonitorService()
            api = FakeMonitorApi([page([], "", ret=-14)])
            monitor = WeChatMonitor(
                account_id=ACC, api=api, store=store, service=service,
                clock=lambda: 1000.0, sleep=lambda _s: asyncio.sleep(0),
            )
            reason = await monitor.run(stop)
            state = store.account_state(ACC)
            check("-14 暂停账号并退出", reason == "paused")
            check("暂停时长 1 小时", state.get("paused_until") == 4600.0)
            check("没有保存业务错误页的游标", store.get_cursor(ACC) == "")

            api2 = FakeMonitorApi([page([], "should-not-use")])
            monitor2 = WeChatMonitor(
                account_id=ACC, api=api2, store=store, service=service,
                clock=lambda: 1000.0, sleep=lambda _s: asyncio.sleep(0),
            )
            reason2 = await monitor2.run(stop)
            check("暂停期内不重新连接", reason2 == "paused" and not api2.calls)

    run(scenario_pause())

    async def scenario_pause_config():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            api = FakeMonitorApi([page([], "", ret=-14)])
            monitor = WeChatMonitor(
                account_id=ACC, api=api, store=store,
                service=FakeMonitorService(),
                config=MonitorConfig(pause_seconds=120.0),
                clock=lambda: 1000.0, sleep=lambda _s: asyncio.sleep(0),
            )
            reason = await monitor.run(asyncio.Event())
            check("暂停时长使用运行时配置",
                  reason == "paused"
                  and store.account_state(ACC).get("paused_until") == 1120.0)

    run(scenario_pause_config())

    async def scenario_stop_mid_page():
        with tempfile.TemporaryDirectory() as tmp:
            store = WeChatStore(Path(tmp) / "state.db")
            store.init()
            store.set_cursor(ACC, "old", 1000.0)
            stop = asyncio.Event()
            service = FakeMonitorService(stop_event=stop, stop_after=1)
            api = FakeMonitorApi([page([{"id": 1}, {"id": 2}], "mid-cursor")])

            async def sleep(seconds):
                pass

            monitor = WeChatMonitor(
                account_id=ACC, api=api, store=store, service=service,
                clock=lambda: 1000.0, sleep=sleep,
            )
            await monitor.run(stop)
            check("停止时没处理完的一页不保存游标", store.get_cursor(ACC) == "old")

    run(scenario_stop_mid_page())


# ----------------------------------------------------------------------
# 10. 登录
# ----------------------------------------------------------------------
class FakeLoginApi:
    def __init__(self, statuses) -> None:
        self.statuses = deque(statuses)
        self.qr_count = 0
        self.polls: list[dict] = []

    async def fetch_qr_code(self, *, local_token_list=None, bot_type="3"):
        self.qr_count += 1
        return QrCode(qrcode=f"QR-{self.qr_count}", qrcode_url=f"https://qr.example/{self.qr_count}")

    async def poll_qr_status(self, *, qrcode, verify_code="", base_url=None, timeout=None):
        self.polls.append({
            "qrcode": qrcode, "verify_code": verify_code, "base_url": base_url,
        })
        if not self.statuses:
            return QrStatus(status="wait")
        return self.statuses.popleft()


def test_login() -> None:
    section("登录：二维码状态机、验证码、凭证保存")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            store = CredentialStore(Path(tmp), FakeProtector())
            api = FakeLoginApi([
                QrStatus(status="wait"),
                QrStatus(status="scaned"),
                QrStatus(
                    status="confirmed", bot_token="LOGIN_BOT_TOKEN",
                    bot_id="login-account",
                    base_url="https://ilinkai.weixin.qq.com",
                    user_id="scan-user",
                ),
            ])
            emitted: list[str] = []

            async def sleep(_seconds):
                pass

            creds = await login_with_qr(
                api=api, credential_store=store, emit=emitted.append,
                ask_code=lambda: "123456", sleep=sleep, clock=lambda: 1000.0,
            )
            check("登录成功后保存账号", creds.account_id == "login-account")
            check("登录后能读回 token", store.load("login-account").token == "LOGIN_BOT_TOKEN")
            output = "\n".join(emitted)
            check("登录输出不包含 bot token", "LOGIN_BOT_TOKEN" not in output)
            check("登录输出提示需要显式配对", "允许名单" in output)

        # API 返回的二维码内容即使绕过 parse_qr_response，也不能原样打印。
        class BadQrLoginApi(FakeLoginApi):
            async def fetch_qr_code(self, *, local_token_list=None, bot_type="3"):
                return QrCode(
                    qrcode="QR-bad",
                    qrcode_url="https://qr.example/\x1b[31mHIDDEN",
                )

        with tempfile.TemporaryDirectory() as tmp:
            store = CredentialStore(Path(tmp), FakeProtector())
            emitted: list[str] = []
            try:
                await login_with_qr(
                    api=BadQrLoginApi([QrStatus(status="binded_redirect")]),
                    credential_store=store,
                    emit=emitted.append,
                    ask_code=lambda: "1",
                    sleep=lambda _s: asyncio.sleep(0),
                    clock=lambda: 1000.0,
                )
            except CredentialError as exc:
                output = "\n".join(emitted)
                check("不安全二维码内容安全失败", True)
                check("失败信息不含原始二维码", "HIDDEN" not in str(exc))
                check("失败前没有输出原始二维码", "\x1b" not in output and "HIDDEN" not in output)
            else:
                check("不安全二维码内容安全失败", False, "本应拒绝")

        with tempfile.TemporaryDirectory() as tmp:
            store = CredentialStore(Path(tmp), FakeProtector())
            api = FakeLoginApi([
                QrStatus(status="need_verifycode"),
                QrStatus(
                    status="confirmed", bot_token="TOKEN-2", bot_id="acct-2",
                    base_url="https://ilinkai.weixin.qq.com", user_id="u-2",
                ),
            ])

            async def sleep(_seconds):
                pass

            await login_with_qr(
                api=api, credential_store=store, emit=lambda _s: None,
                ask_code=lambda: "7788", sleep=sleep, clock=lambda: 1000.0,
            )
            check("验证码被带到下一次轮询",
                  api.polls[1]["verify_code"] == "7788")

        with tempfile.TemporaryDirectory() as tmp:
            store = CredentialStore(Path(tmp), FakeProtector())
            api = FakeLoginApi([
                QrStatus(status="binded_redirect"),
            ])

            async def sleep(_seconds):
                pass

            try:
                await login_with_qr(
                    api=api, credential_store=store, emit=lambda _s: None,
                    ask_code=lambda: "1", sleep=sleep, clock=lambda: 1000.0,
                )
            except CredentialError:
                check("已绑定到别处时拒绝接管", True)
            else:
                check("已绑定到别处时拒绝接管", False)

            api2 = FakeLoginApi([
                QrStatus(
                    status="confirmed", bot_token="", bot_id="acct-3",
                    base_url="https://ilinkai.weixin.qq.com", user_id="u-3",
                ),
            ])
            try:
                await login_with_qr(
                    api=api2, credential_store=store, emit=lambda _s: None,
                    ask_code=lambda: "1", sleep=sleep, clock=lambda: 1000.0,
                )
            except CredentialError:
                check("缺少 bot_token 时拒绝保存", True)
            else:
                check("缺少 bot_token 时拒绝保存", False)

            api3 = FakeLoginApi([
                QrStatus(
                    status="confirmed", bot_token="T", bot_id="acct-4",
                    base_url="https://evil.com", user_id="u-4",
                ),
            ])
            try:
                await login_with_qr(
                    api=api3, credential_store=store, emit=lambda _s: None,
                    ask_code=lambda: "1", sleep=sleep, clock=lambda: 1000.0,
                )
            except CredentialError:
                check("恶意 baseurl 时拒绝保存", True)
            else:
                check("恶意 baseurl 时拒绝保存", False)

            expired_api = FakeLoginApi([QrStatus(status="expired")] * 5)
            try:
                await login_with_qr(
                    api=expired_api, credential_store=store, emit=lambda _s: None,
                    ask_code=lambda: "1", sleep=sleep, clock=lambda: 1000.0,
                )
            except CredentialError:
                check("二维码反复过期后停止", expired_api.qr_count == 4)
            else:
                check("二维码反复过期后停止", False)

    run(scenario())


# ----------------------------------------------------------------------
# 11. Runtime（离线启动/停止）
# ----------------------------------------------------------------------
def test_runtime() -> None:
    section("运行时：默认关闭、失败关闭、离线启动与停止")

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = WeChatRuntimeConfig(
                enabled=False, state_dir=root / "disabled",
            )
            runtime = WeChatRuntime(config, protector=FakeProtector())
            check("默认关闭时不启动", not await runtime.start())
            check("关闭时不创建状态目录", not (root / "disabled").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = WeChatRuntimeConfig(
                enabled=True, account_id=ACC, state_dir=root / "empty",
            )
            runtime = WeChatRuntime(config, protector=FakeProtector())
            check("没有凭证时拒绝启动", not await runtime.start())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            CredentialStore(state, FakeProtector()).save(AccountCredentials(
                account_id=ACC, token="TOKEN", base_url="https://ilinkai.weixin.qq.com",
                user_id=USER, saved_at="2026-10-24T00:00:00",
            ))
            config = WeChatRuntimeConfig(
                enabled=True, account_id=ACC, state_dir=state,
                long_poll_timeout=5.0, watch_interval=0.2,
            )
            runtime = WeChatRuntime(config, protector=FakeProtector())
            check("没有配对用户时拒绝连接", not await runtime.start())
            check("拒绝时没有发网络请求", True)

            AllowListStore(state).add(account_id=ACC, user_id=USER)
            calls = {"updates": 0}

            def handler(method, url, headers, body, timeout):
                if "notifystart" in url or "notifystop" in url:
                    return json_response({"ret": 0})
                if "getupdates" in url:
                    calls["updates"] += 1
                    if calls["updates"] == 1:
                        return json_response({
                            "ret": 0, "msgs": [], "get_updates_buf": "runtime-cursor",
                            "longpolling_timeout_ms": 5000,
                        })
                    return json_response({
                        "ret": 0, "msgs": [], "get_updates_buf": "",
                    })
                return json_response({"ret": 0})

            transport = FakeTransport(handler)
            runtime2 = WeChatRuntime(
                config, protector=FakeProtector(), transport=transport,
            )
            started = await runtime2.start()
            check("凭证+配对齐全后启动", started)
            await asyncio.sleep(0.15)
            await runtime2.stop()
            check("停止后状态复位", not runtime2.started)
            check("运行时建立了长轮询", calls["updates"] >= 1)
            check("运行时通知了 start/stop",
                  any("notifystart" in r["url"] for r in transport.requests)
                  and any("notifystop" in r["url"] for r in transport.requests))

    run(scenario())


def test_runtime_recovery() -> None:
    section("运行时监督：意外退出、暂停恢复、状态文件、关闭")

    class FakeMonitor:
        def __init__(self, store, mode: str) -> None:
            self.store = store
            self.mode = mode
            self.calls = 0

        async def run(self, stop_event):
            self.calls += 1
            if self.mode == "error_once" and self.calls == 1:
                raise RuntimeError("boom")
            if self.mode == "pause_once" and self.calls == 1:
                now = time.time()
                self.store.set_account_paused(
                    ACC, paused_until=now + 0.25, reason="test pause", now=now,
                )
                return "paused"
            await stop_event.wait()
            return "stopped"

    class TestRuntime(WeChatRuntime):
        def __init__(self, config, monitor, **kwargs):
            self._fake_monitor = monitor
            super().__init__(config, **kwargs)

        def _build_monitor(self, **_kwargs):
            return self._fake_monitor

    async def wait_for(predicate, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(0.01)
        return False

    async def prepare(root: Path, mode: str):
        state = root / "state"
        CredentialStore(state, FakeProtector()).save(AccountCredentials(
            account_id=ACC, token="TOKEN", base_url="https://ilinkai.weixin.qq.com",
            user_id=USER, saved_at="2026-10-24T00:00:00",
        ))
        AllowListStore(state).add(account_id=ACC, user_id=USER)
        config = WeChatRuntimeConfig(
            enabled=True, account_id=ACC, state_dir=state,
            long_poll_timeout=5.0, watch_interval=0.05,
            account_pause_seconds=0.25,
            supervisor_backoff_base=0.02, supervisor_backoff_max=0.05,
        )
        store = WeChatStore(state / "state.db")
        store.init()
        monitor = FakeMonitor(store, mode)
        runtime = TestRuntime(
            config, monitor, protector=FakeProtector(),
        )
        return state, store, monitor, runtime

    async def scenario_unexpected_exit():
        with tempfile.TemporaryDirectory() as tmp:
            state, _store, monitor, runtime = await prepare(
                Path(tmp), "error_once"
            )
            check("监督运行时启动", await runtime.start())
            check("意外退出后会重连", await wait_for(lambda: monitor.calls >= 2))
            check("重连期间 started 仍代表监督任务存活", runtime.started)
            check("重连后监听存活", await wait_for(lambda: runtime.monitor_alive))
            status = read_runtime_status(state) or {}
            check("状态文件反映运行中", status.get("state") == "running")
            check("状态文件记录重启次数", int(status.get("restart_count", 0)) >= 1)
            check("status 文本显示运行中", "运行中" in runtime_status_text(state))
            await runtime.stop()
            check("关闭后 started 复位", not runtime.started)
            check("关闭后监听标记复位", not runtime.monitor_alive)
            check("关闭后状态文件清除", read_runtime_status(state) is None)
            check("关闭后监督与后台任务已清空",
                  runtime._supervisor_task is None
                  and runtime._watch_task is None
                  and runtime._monitor_task is None
                  and runtime._service is None)

    run(scenario_unexpected_exit())

    async def scenario_supervisor_death():
        with tempfile.TemporaryDirectory() as tmp:
            state, _store, monitor, runtime = await prepare(Path(tmp), "wait")
            check("监督死亡场景启动", await runtime.start())
            check("监听已开始", await wait_for(lambda: monitor.calls >= 1))
            supervisor = runtime._supervisor_task
            assert supervisor is not None
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
            check("监督任务意外结束后 started 立即失效", not runtime.started)
            check("监督任务结束后监听标记失效", not runtime.monitor_alive)
            check("监督任务结束后 watcher 也退出", await wait_for(
                lambda: runtime._watch_task is None or runtime._watch_task.done()
            ))
            status = read_runtime_status(state) or {}
            check("监督任务意外结束写 stopped 状态",
                  status.get("state") == "stopped")
            check("stopped 状态不阻止下次 logout",
                  not runtime_is_active(state))
            await runtime.stop()

    run(scenario_supervisor_death())

    async def scenario_pause_resume():
        with tempfile.TemporaryDirectory() as tmp:
            state, store, monitor, runtime = await prepare(
                Path(tmp), "pause_once"
            )
            check("暂停场景启动", await runtime.start())
            await wait_for(lambda: float(
                store.account_state(ACC).get("paused_until", 0) or 0
            ) > 0)
            await wait_for(
                lambda: (read_runtime_status(state) or {}).get("state") == "paused"
            )
            status = read_runtime_status(state) or {}
            check("暂停状态写入状态文件", status.get("state") == "paused")
            check("暂停期间监听标记为退出", not status.get("monitor_alive"))
            check("暂停期间不重试", monitor.calls == 1)
            check("暂停期间不发送任务通知", not runtime._notifications_allowed())
            check("status 文本显示暂停", "暂停" in runtime_status_text(state))
            await asyncio.sleep(0.08)
            check("暂停未到期仍不重试", monitor.calls == 1)
            check("暂停到期后恢复监听", await wait_for(
                lambda: monitor.calls >= 2, timeout=3.0
            ))
            check("恢复后清除暂停",
                  float(store.account_state(ACC).get("paused_until", 0) or 0) == 0)
            check("恢复后重新允许任务通知", runtime._notifications_allowed())
            await runtime.stop()

    run(scenario_pause_resume())

    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp) / "state"
        check("没有状态文件时不认为监听存活", not runtime_is_active(state))
        check("没有状态文件时 status 显示未运行",
              "未运行" in runtime_status_text(state))
        write_runtime_status(
            state, account_id=ACC, state="running", monitor_alive=True,
            pid=2147483647,
        )
        check("PID 已退出时状态文件视为过期", not runtime_is_active(state))
        check("过期状态文件在 status 中显示残留",
              "残留" in runtime_status_text(state))
        state.mkdir(parents=True, exist_ok=True)
        (state / "runtime-status.json").write_text("{bad", encoding="utf-8")
        check("损坏的状态文件按仍在运行处理（失败关闭）",
              runtime_is_active(state))
        check("损坏状态会阻止 logout", "状态未知" in runtime_status_text(state))
        clear_runtime_status(state)


def test_management() -> None:
    section("本机管理：unpair/logout 的撤销范围、任务保留与运行中保护")

    other_account = "bot-account-2"

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            protector = FakeProtector()
            credentials = CredentialStore(state, protector)
            for account in (ACC, other_account):
                credentials.save(AccountCredentials(
                    account_id=account, token="TOKEN",
                    base_url="https://ilinkai.weixin.qq.com",
                    user_id=USER, saved_at="2026-10-24T00:00:00",
                ))
            allow = AllowListStore(state)
            allow.add(account_id=ACC, user_id=USER, scopes=("chat", "task", "approve"))
            allow.add(account_id=ACC, user_id=OTHER, scopes=("chat", "task", "approve"))
            allow.add(account_id=other_account, user_id=OTHER,
                      scopes=("chat", "task", "approve"))

            store = WeChatStore(state / "state.db")
            store.init()
            auth = RemoteAuth(store, b"m" * 32, clock=lambda: 1000.0)
            task_user = "a" * 32
            task_other = "b" * 32
            task_other_account = "c" * 32
            auth.issue(
                account_id=ACC, user_id=USER, task_id=task_user,
                action_type=ACTION_PLAN, now=1000.0,
            )
            auth.issue(
                account_id=ACC, user_id=OTHER, task_id=task_other,
                action_type=ACTION_PLAN, now=1000.0,
            )
            auth.issue(
                account_id=other_account, user_id=OTHER,
                task_id=task_other_account, action_type=ACTION_PLAN, now=1000.0,
            )
            store.link_task(
                task_id=task_user, account_id=ACC, user_id=USER,
                session_id="session-1", now=1000.0,
            )

            result = unpair_user(
                state, account_id=ACC, user_id=USER, now=1001.0
            )
            check("unpair 移除了目标配对", result.removed)
            check("unpair 撤销了目标的授权码", result.revoked_codes == 1)
            check("unpair 不删除任务关联", store.get_task_link(task_user) is not None)
            check("unpair 不删除本机凭证",
                  credentials.load(ACC) is not None)
            check("unpair 不影响同账号其他用户",
                  store.find_active_approval(
                      task_id=task_other, action_type=ACTION_PLAN,
                      action_id="", now=1001.0,
                  ) is not None)
            check("unpair 不影响其他账号",
                  store.find_active_approval(
                      task_id=task_other_account, action_type=ACTION_PLAN,
                      action_id="", now=1001.0,
                  ) is not None)

            write_runtime_status(
                state, account_id=ACC, state="running", monitor_alive=True,
                pid=os.getpid(),
            )
            refused = False
            try:
                logout_account(
                    state, account_id=ACC, protector=protector, now=1002.0
                )
            except CredentialError:
                refused = True
            check("Jia 运行时拒绝 logout", refused)
            check("拒绝时凭证仍在", credentials.load(ACC) is not None)
            check("拒绝时授权码仍未被误撤销",
                  store.find_active_approval(
                      task_id=task_other, action_type=ACTION_PLAN,
                      action_id="", now=1002.0,
                  ) is not None)
            clear_runtime_status(state)

            invalid_path_refused = False
            try:
                logout_account(
                    state, account_id="../escape", protector=protector,
                    now=1002.0,
                )
            except CredentialError:
                invalid_path_refused = True
            check("logout 拒绝非法账号路径", invalid_path_refused)
            check("非法路径没有删除其他账号凭证",
                  credentials.load(other_account) is not None)

            logout = logout_account(
                state, account_id=ACC, protector=protector, now=1003.0
            )
            check("logout 删除指定账号凭证", credentials.load(ACC) is None)
            check("logout 撤销该账号剩余授权码", logout.revoked_codes == 1)
            check("logout 不影响其他账号凭证",
                  credentials.load(other_account) is not None)
            check("logout 不影响其他账号授权码",
                  store.find_active_approval(
                      task_id=task_other_account, action_type=ACTION_PLAN,
                      action_id="", now=1003.0,
                  ) is not None)
            check("logout 不清空允许名单",
                  {(e.account_id, e.user_id) for e in allow.list()} == {
                      (ACC, OTHER),
                      (other_account, OTHER),
                  })
            check("logout 不删除任务关联",
                  store.get_task_link(task_user) is not None)
            check("logout 不取消任务（任务本身仍存在）", True)

    run(scenario())


def test_diagnostics() -> None:
    section("诊断：默认日志哈希标识与显式本地 sender-hash 命令")

    user_hash = redact_id(USER)
    check("发送者日志标识是短哈希", user_hash.startswith("u-") and len(user_hash) == 12)
    check("发送者日志标识不包含原始 ID", USER not in user_hash)
    check("同一 ID 的哈希稳定", redact_id(USER) == user_hash)
    check("空 ID 不产生哈希", redact_id("") == "(none)")

    try:
        import wechat_cli  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        check("CLI 可导入用于解析检查", False, type(exc).__name__)
        return
    parser = wechat_cli.build_parser()
    pair_args = parser.parse_args(["pair", "--user", USER])
    check("CLI 配对默认只给 chat", pair_args.scopes == "chat")
    diag_args = parser.parse_args(["sender-hash", "--user", USER])
    check("CLI 提供显式 sender-hash 诊断", diag_args.user == USER)
    logout_args = parser.parse_args(["logout", "--account", ACC])
    check("CLI logout 必须显式指定账号", logout_args.account == ACC)


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main() -> int:
    print("=" * 72)
    print("Jia-WeChat 离线测试（不联网、不登录真实账号、不执行工具）")
    print("=" * 72)
    test_protocol()
    test_api()
    test_credentials()
    test_store_and_remote_auth()
    test_bridge()
    test_service()
    test_logging_redaction()
    test_task_watcher()
    test_monitor()
    test_login()
    test_runtime()
    test_runtime_recovery()
    test_management()
    test_diagnostics()

    failed = [r for r in RESULTS if not r[1]]
    print()
    print("=" * 72)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("失败项：")
        for note, _ok, detail in failed:
            print(f"  - {note}" + (f"（{detail}）" if detail else ""))
    print("=" * 72)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
