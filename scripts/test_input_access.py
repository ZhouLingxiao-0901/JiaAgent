"""本地输入控制接口的访问控制测试（完全离线）。

覆盖：

  - Host / 来源 IP / Origin / Sec-Fetch-Site 的本机边界
  - 跨站请求、错误端口、null Origin、非回环客户端
  - 本地会话令牌：缺失、伪造、过期
  - 恢复急停：confirmed 不能单独授权、一次性挑战、过期、重放
  - 挑战与会话、急停代次绑定；新的急停让旧挑战失效
  - 急停与恢复并发时的安全状态
  - 恢复急停不会启用真实输入
  - 合法前端的完整调用顺序

**不启动真实应用、不监听端口、不调用任何 Windows 输入 API。**
测试直接用进程内的 ASGI 客户端调用一个只挂载输入路由的小应用。
急停使用 `EmergencyStop(path=None)`，不会读写 data/ 下的状态文件。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402

from app.api import input as input_api  # noqa: E402
from app.api import security  # noqa: E402
from app.errors import JiaAgentError  # noqa: E402
from app.input import control  # noqa: E402
from app.input.windows_backend import real_mouse_enablement  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
LOCAL_HOST = "127.0.0.1:8000"
LOCAL_ORIGIN = "http://127.0.0.1:8000"


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


# ----------------------------------------------------------------------
# 假时钟与进程内 ASGI 客户端
# ----------------------------------------------------------------------
class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict:
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    @property
    def code(self) -> str:
        return str((self.json().get("error") or {}).get("code") or "")


class AsgiClient:
    """最小 ASGI 测试客户端：不建 socket、不跑 lifespan。"""

    def __init__(self, app) -> None:
        self.app = app

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: dict[str, str | None] | None = None,
        client: tuple[str, int] = ("127.0.0.1", 54321),
        with_defaults: bool = True,
    ) -> Response:
        raw: dict[str, str] = {"host": LOCAL_HOST}
        if with_defaults:
            raw.update({
                "origin": LOCAL_ORIGIN,
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
            })
        for key, value in (headers or {}).items():
            name = key.casefold()
            if value is None:
                raw.pop(name, None)
            else:
                raw[name] = value

        encoded = [
            (name.encode("latin-1"), str(value).encode("latin-1"))
            for name, value in raw.items()
        ]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": encoded,
            "client": client,
            "server": ("127.0.0.1", 8000),
        }
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        messages: list[dict] = []

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)

        status = 500
        response_headers: dict[str, str] = {}
        payload = b""
        for message in messages:
            if message["type"] == "http.response.start":
                status = int(message["status"])
                response_headers = {
                    key.decode("latin-1").casefold(): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
            elif message["type"] == "http.response.body":
                payload += message.get("body", b"")
        return Response(status, response_headers, payload)


def build_app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(JiaAgentError)
    async def handle_known_error(request, exc):  # noqa: ANN001, ANN202
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.http_status, content={"error": exc.to_dict()}
        )

    app.middleware("http")(security.local_only_middleware)
    app.include_router(input_api.router, prefix="/api")
    return app


# ----------------------------------------------------------------------
# 隔离环境
# ----------------------------------------------------------------------
class Harness:
    def __init__(self) -> None:
        self.clock = Clock()
        self.old_emergency = control.emergency
        self.old_limiter = control.limiter
        self.old_access = security.input_access
        control.emergency = control.EmergencyStop(path=None)
        control.limiter = control.ClickRateLimiter(clock=self.clock)
        security.input_access = security.InputAccessControl(clock=self.clock)
        self.client = AsgiClient(build_app())

    def close(self) -> None:
        control.emergency = self.old_emergency
        control.limiter = self.old_limiter
        security.input_access = self.old_access

    async def session(self, **kwargs) -> tuple[Response, str]:
        response = await self.client.request(
            "POST", "/api/input/session", **kwargs
        )
        return response, str(response.json().get("token") or "")

    async def stop(self, **kwargs) -> Response:
        return await self.client.request(
            "POST", "/api/input/emergency-stop", **kwargs
        )

    async def prepare(self, token: str, **kwargs) -> tuple[Response, str]:
        headers = {"x-jia-session": token}
        headers.update(kwargs.pop("headers", {}) or {})
        response = await self.client.request(
            "POST",
            "/api/input/emergency-reset/prepare",
            headers=headers,
            **kwargs,
        )
        return response, str(response.json().get("nonce") or "")

    async def reset(
        self,
        token: str,
        *,
        nonce: str = "",
        confirmed: bool = True,
        path: str = "/api/input/emergency-reset",
        **kwargs,
    ) -> Response:
        headers = {"x-jia-session": token}
        headers.update(kwargs.pop("headers", {}) or {})
        body = json.dumps({"confirmed": confirmed, "nonce": nonce}).encode()
        return await self.client.request(
            "POST", path, body=body, headers=headers, **kwargs
        )


# ----------------------------------------------------------------------
# 一、本机边界
# ----------------------------------------------------------------------
async def test_local_boundary() -> None:
    print("\n[一] 本机边界（Host / 来源 IP）")
    harness = Harness()
    try:
        response, token = await harness.session()
        check("本机 Host + 回环客户端可以建立会话",
              response.status == 200 and bool(token))
        check("会话响应不包含任何点击能力",
              set(response.json()) == {"token", "expires_in_seconds", "header"})

        response, _ = await harness.session(headers={"host": "evil.example"})
        check("非本机 Host 被拒绝", response.status == 403
              and response.code == "untrusted_request")

        response, _ = await harness.session(headers={"host": "192.168.1.20:8000"})
        check("局域网 IP 作为 Host 被拒绝", response.status == 403)

        response, _ = await harness.session(
            client=("192.168.1.20", 51000)
        )
        check("非回环客户端被拒绝（即使绑定了 0.0.0.0 也拦得住）",
              response.status == 403
              and response.code == "untrusted_request")

        response, _ = await harness.session(client=("::1", 51000))
        check("IPv6 回环客户端被允许", response.status == 200)

        response, _ = await harness.session(
            client=("::ffff:127.0.0.1", 51000)
        )
        check("IPv4-mapped IPv6 回环客户端被允许", response.status == 200)

        response, _ = await harness.session(headers={"host": "localhost:8000"})
        check("localhost Host 被允许", response.status == 200)
    finally:
        harness.close()


# ----------------------------------------------------------------------
# 二、来源与跨站请求
# ----------------------------------------------------------------------
async def test_origin_and_csrf() -> None:
    print("\n[二] 来源校验与跨站请求")
    harness = Harness()
    try:
        response, _ = await harness.session(
            headers={"origin": "http://evil.example"}
        )
        check("跨站 Origin 不能申请会话", response.status == 403
              and response.code == "untrusted_origin")

        response, _ = await harness.session(headers={
            "origin": None, "sec-fetch-site": "cross-site",
        })
        check("Sec-Fetch-Site: cross-site 被拒绝", response.status == 403)

        response, _ = await harness.session(
            headers={"origin": "http://127.0.0.1:9999"}
        )
        check("端口不同的本地来源被拒绝", response.status == 403)

        response, _ = await harness.session(
            headers={"origin": "http://localhost:9999"}
        )
        check("localhost 但端口不同也被拒绝", response.status == 403)

        response, _ = await harness.session(headers={"origin": "null"})
        check("Origin: null 被拒绝", response.status == 403)

        response, _ = await harness.session(headers={
            "origin": None, "sec-fetch-site": "same-site",
        })
        check("Sec-Fetch-Site: same-site（子域/邻站）也被拒绝",
              response.status == 403)

        response, _ = await harness.session(
            headers={"origin": None, "referer": "http://evil.example/page"}
        )
        check("没有 Origin 时校验 Referer", response.status == 403)

        _, token = await harness.session()
        check("合法同源请求仍能拿到会话", bool(token))

        response = await harness.client.request(
            "GET", "/api/input/status",
            headers={"origin": "http://evil.example",
                     "x-jia-session": token},
        )
        check("跨站来源不能读取状态（即使偷到了令牌）", response.status == 403)

        response = await harness.client.request(
            "OPTIONS", "/api/input/emergency-reset",
            headers={
                "origin": "http://evil.example",
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type,x-jia-session",
            },
        )
        check("跨站预检得不到 CORS 放行（自定义头无法跨站发送）",
              "access-control-allow-origin" not in response.headers)

        # 跨站急停：必须既不改状态，也不产生 CORS 放行头。
        response = await harness.stop(headers={"origin": "http://evil.example"})
        check("跨站急停被拒绝", response.status == 403)
        check("跨站急停没有触发急停", not control.emergency.stopped)
        check("拒绝响应没有 Access-Control-Allow-Origin",
              "access-control-allow-origin" not in response.headers)

        # 跨站恢复：带合法令牌和来源也不行。
        await harness.stop()
        prepared, nonce = await harness.prepare(token)
        check("合法前端可以取得恢复挑战",
              prepared.status == 200 and bool(nonce))
        response = await harness.reset(
            token, nonce=nonce, headers={"origin": "http://evil.example"}
        )
        check("跨站恢复被拒绝", response.status == 403)
        check("跨站恢复后急停仍然生效", control.emergency.stopped)
    finally:
        harness.close()


# ----------------------------------------------------------------------
# 三、会话与恢复授权
# ----------------------------------------------------------------------
async def test_reset_authorization() -> None:
    print("\n[三] 恢复急停的授权边界")
    harness = Harness()
    try:
        # 未授权：没有会话。
        response = await harness.reset("", nonce="whatever")
        check("没有会话令牌不能恢复", response.status == 401
              and response.code == "input_session_required")

        response = await harness.reset("not-a-real-token", nonce="whatever")
        check("伪造的会话令牌不能恢复", response.status == 401
              and response.code == "input_session_invalid")

        _, token = await harness.session()

        response = await harness.client.request("GET", "/api/input/status")
        check("没有会话不能查询状态", response.status == 401
              and response.code == "input_session_required")

        # 伪造 confirmed：有合法会话但没有挑战。
        await harness.stop()
        response = await harness.reset(token, nonce="")
        check("只有 confirmed=true 不能恢复",
              response.status == 400
              and response.code == "reset_challenge_required")
        check("伪造 confirmed 没有改变急停状态", control.emergency.stopped)

        # 没有处于急停时不能拿挑战。
        control.emergency.reset(confirmed=True)
        response, nonce = await harness.prepare(token)
        check("没有急停时不签发恢复挑战",
              response.status == 409
              and response.code == "emergency_not_active" and not nonce)

        # 合法流程。
        await harness.stop()
        prepared, nonce = await harness.prepare(token)
        check("急停生效时可以取得一次性挑战",
              prepared.status == 200 and bool(nonce)
              and prepared.json().get("expires_in_seconds", 0) > 0)
        response = await harness.reset(token, nonce=nonce)
        check("合法前端可以恢复急停", response.status == 200)
        check("恢复后急停状态解除", not control.emergency.stopped)

        # 重放同一个请求。
        response = await harness.reset(token, nonce=nonce)
        check("重复使用旧挑战被拒绝", response.status == 409
              and response.code == "emergency_not_active")

        # 再次急停后用旧挑战：必须被拒绝，且不能清除新的急停。
        await harness.stop()
        response = await harness.reset(token, nonce=nonce)
        check("旧挑战不能清除新的急停", response.status == 409
              and control.emergency.stopped)
        check("旧挑战失败原因是无效或状态变化",
              response.code in {"reset_challenge_invalid", "emergency_not_active"},
              response.code)

        # 新急停会让已经准备好的挑战失效。
        control.emergency.reset(confirmed=True)
        await harness.stop()
        _, stale = await harness.prepare(token)
        await harness.stop()
        response = await harness.reset(token, nonce=stale)
        check("新的急停让已签发的挑战失效",
              response.status == 409 and control.emergency.stopped)

        # 重新确认后仍然可以恢复。
        _, fresh = await harness.prepare(token)
        response = await harness.reset(token, nonce=fresh)
        check("重新取得挑战后可以恢复", response.status == 200
              and not control.emergency.stopped)

        # confirmed=false 不消耗挑战，之后仍可用于一次真正的确认。
        await harness.stop()
        _, nonce = await harness.prepare(token)
        response = await harness.reset(token, nonce=nonce, confirmed=False)
        check("confirmed=false 被拒绝", response.status == 400
              and response.code == "input_control_rejected")
        check("confirmed=false 没有恢复急停", control.emergency.stopped)
        response = await harness.reset(token, nonce=nonce, confirmed=True)
        check("同一个挑战在真正确认后仍可使用", response.status == 200)
    finally:
        harness.close()


# ----------------------------------------------------------------------
# 四、会话/挑战过期、绑定与边界
# ----------------------------------------------------------------------
async def test_expiry_and_binding() -> None:
    print("\n[四] 会话过期、挑战过期与会话绑定")
    harness = Harness()
    try:
        response, token_a = await harness.session()
        _, token_b = await harness.session()
        check("可以建立多个独立会话",
              bool(token_a) and bool(token_b) and token_a != token_b)

        # 挑战绑定会话。
        await harness.stop()
        _, nonce_a = await harness.prepare(token_a)
        response = await harness.reset(token_b, nonce=nonce_a)
        check("别的会话不能使用这个挑战", response.status == 409
              and "不属于当前会话" in str(response.json()))
        check("会话不匹配没有消耗合法用户的挑战",
              control.emergency.stopped)
        response = await harness.reset(token_a, nonce=nonce_a)
        check("挑战所属会话仍然可以使用", response.status == 200)

        # 挑战过期。
        await harness.stop()
        _, nonce = await harness.prepare(token_a)
        harness.clock.advance(security.RESET_CHALLENGE_TTL_SECONDS + 1)
        response = await harness.reset(token_a, nonce=nonce)
        check("过期挑战不能恢复", response.status == 409
              and control.emergency.stopped)
        check("过期原因是凭证过期", "过期" in str(response.json()))

        # 会话过期：状态、挑战、恢复都要 401。
        harness.clock.advance(security.SESSION_TTL_SECONDS + 1)
        response = await harness.client.request(
            "GET", "/api/input/status",
            headers={"x-jia-session": token_a},
        )
        check("过期会话不能查询状态", response.status == 401
              and response.code == "input_session_invalid")
        response, nonce = await harness.prepare(token_a)
        check("过期会话不能取得恢复挑战", response.status == 401 and not nonce)
        response = await harness.reset(token_a, nonce="anything")
        check("过期会话不能恢复", response.status == 401)

        # 重新申请会话后仍可继续（后台急停不受会话过期影响）。
        response, fresh = await harness.session()
        check("可以重新建立会话", response.status == 200 and bool(fresh))
        check("会话过期不会清除急停", control.emergency.stopped)
        prepared, nonce = await harness.prepare(fresh)
        response = await harness.reset(fresh, nonce=nonce)
        check("新会话可以完成恢复", prepared.status == 200
              and response.status == 200)
    finally:
        harness.close()


# ----------------------------------------------------------------------
# 五、急停优与并发
# ----------------------------------------------------------------------
async def test_stop_priority_and_concurrency() -> None:
    print("\n[五] 急停优先与并发")
    harness = Harness()
    try:
        # 急停不要求会话：令牌过期时也必须能立刻停。
        response, token = await harness.session()
        harness.clock.advance(security.SESSION_TTL_SECONDS + 1)
        response = await harness.stop()
        check("会话过期时仍可以急停", response.status == 200
              and control.emergency.stopped)

        # stop 与 prepare 的竞态：prepare 之后、reset 之前又急停。
        response, fresh = await harness.session()
        _, nonce = await harness.prepare(fresh)
        await harness.stop()
        response = await harness.reset(fresh, nonce=nonce)
        check("确认期间再次急停会阻止恢复", response.status == 409
              and control.emergency.stopped)

        # HTTP 层同时发出急停和恢复：无论谁先完成，最终都必须是急停。
        async def race_once() -> tuple[int, int]:
            await harness.stop()
            prepared, nonce = await harness.prepare(fresh)
            assert prepared.status == 200 and nonce
            stop_task = asyncio.create_task(harness.stop())
            reset_task = asyncio.create_task(
                harness.reset(fresh, nonce=nonce)
            )
            stop_response, reset_response = await asyncio.gather(
                stop_task, reset_task
            )
            return stop_response.status, reset_response.status

        race_statuses = []
        race_safe = True
        for _ in range(10):
            stop_status, reset_status = await race_once()
            race_statuses.append((stop_status, reset_status))
            if stop_status != 200 or reset_status not in {200, 409}:
                race_safe = False
            if not control.emergency.stopped:
                race_safe = False
        check("HTTP 层急停与恢复并发后仍然保持急停",
              race_safe, str(race_statuses[:3]))

        # 直接验证 reset_if_generation 的原子语义。
        emergency = control.EmergencyStop(path=None)
        emergency.trigger("初始急停")
        generation = emergency.generation
        emergency.trigger("又急停一次")
        ok, _ = emergency.reset_if_generation(
            generation, confirmed=True, source="test"
        )
        check("代次变化后 reset_if_generation 拒绝恢复",
              not ok and emergency.stopped)

        # 真正的并发：触发和恢复同时发生。无论谁先得到锁，只要触发发生了，
        # 最终状态必须是"已急停"。
        survived = 0
        for _ in range(100):
            emergency = control.EmergencyStop(path=None)
            emergency.trigger("起始")
            generation = emergency.generation
            barrier = threading.Barrier(2)

            def do_stop():
                barrier.wait()
                emergency.trigger("并发急停")

            def do_reset():
                barrier.wait()
                emergency.reset_if_generation(
                    generation, confirmed=True, source="test"
                )

            first = threading.Thread(target=do_stop)
            second = threading.Thread(target=do_reset)
            first.start()
            second.start()
            first.join()
            second.join()
            if emergency.stopped:
                survived += 1
        check("并发急停与恢复后最终一定是急停状态", survived == 100,
              f"{survived}/100")

        # 静态状态文件损坏时仍按已急停处理（沿用上一阶段的规则）。
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stop.json"
            path.write_text("{ 坏掉的 JSON", encoding="utf-8")
            broken = control.EmergencyStop(path=path)
            check("状态文件损坏时失败关闭", broken.stopped)
            check("损坏状态下真实输入被阻止",
                  bool(broken.block_reason()))
            ok, _ = broken.reset_if_generation(
                broken.generation, confirmed=False, source="test"
            )
            check("没有显式确认时损坏状态也不会被清除",
                  not ok and broken.stopped)
            ok, _ = broken.reset_if_generation(
                broken.generation, confirmed=True, source="test"
            )
            check("显式确认后可以清除损坏状态", ok and not broken.stopped)
            check("清除损坏状态不会启用真实输入",
                  not real_mouse_enablement().allowed)
    finally:
        harness.close()


# ----------------------------------------------------------------------
# 六、恢复不等于启用真实输入
# ----------------------------------------------------------------------
async def test_reset_does_not_enable_input() -> None:
    print("\n[六] 恢复不会启用真实输入")
    harness = Harness()
    try:
        _, token = await harness.session()
        await harness.stop()
        _, nonce = await harness.prepare(token)
        response = await harness.reset(token, nonce=nonce)
        check("恢复请求成功", response.status == 200)

        decision = real_mouse_enablement()
        check("恢复后双重启用条件仍未满足", not decision.allowed)

        from app.settings import get_tools_config

        check("config 的 real_mouse_enabled 仍为 false",
              get_tools_config().input.real_mouse_enabled is False)

        from app.tools.registry import registry

        tool = registry.get("click_element")
        check("登记处的真实输入后端仍然为 None",
              tool is not None and getattr(tool, "_backend", None) is None)

        status = await harness.client.request(
            "GET", "/api/input/status",
            headers={"x-jia-session": token},
        )
        data = status.json()
        check("状态接口报告真实输入未启用",
              status.status == 200
              and data.get("real_mouse", {}).get("allowed") is False
              and data.get("real_mouse", {}).get("real_input_active") is False)
        check("状态接口报告急停已解除",
              data.get("emergency", {}).get("stopped") is False)
    finally:
        harness.close()


# ----------------------------------------------------------------------
# 七、合法前端流程
# ----------------------------------------------------------------------
async def test_frontend_flow() -> None:
    print("\n[七] 合法前端流程")
    harness = Harness()
    try:
        session, token = await harness.session(
            headers={"sec-fetch-site": "same-origin", "origin": LOCAL_ORIGIN}
        )
        check("1. 页面初始化拿到会话", session.status == 200 and bool(token))

        status = await harness.client.request(
            "GET", "/api/input/status",
            headers={"x-jia-session": token},
        )
        check("2. 用会话查询状态", status.status == 200
              and "real_mouse" in status.json())

        stop = await harness.stop(
            headers={"sec-fetch-site": "same-origin", "origin": LOCAL_ORIGIN}
        )
        check("3. 点击急停", stop.status == 200
              and control.emergency.stopped)

        prepared, nonce = await harness.prepare(token)
        check("4. 用户确认后取得一次性挑战",
              prepared.status == 200 and bool(nonce))

        reset = await harness.reset(token, nonce=nonce)
        check("5. 用挑战恢复急停", reset.status == 200
              and not control.emergency.stopped)

        status = await harness.client.request(
            "GET", "/api/input/status",
            headers={"x-jia-session": token},
        )
        check("6. 状态刷新为未急停",
              status.status == 200
              and status.json().get("emergency", {}).get("stopped") is False)

        check("合法流程没有开启真实输入",
              not real_mouse_enablement().allowed)
    finally:
        harness.close()


# ----------------------------------------------------------------------
def main() -> int:
    print("=" * 72)
    print("输入控制接口访问控制测试（离线，不启动服务，不产生输入事件）")
    print("=" * 72)

    asyncio.run(test_local_boundary())
    asyncio.run(test_origin_and_csrf())
    asyncio.run(test_reset_authorization())
    asyncio.run(test_expiry_and_binding())
    asyncio.run(test_stop_priority_and_concurrency())
    asyncio.run(test_reset_does_not_enable_input())
    asyncio.run(test_frontend_flow())

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 72)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for note, _, detail in failed:
        print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
