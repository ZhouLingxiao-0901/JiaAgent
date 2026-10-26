"""微信 iLink Bot API 的原生 Python 客户端。

只实现第一版需要的接口：二维码登录、长轮询、发文字、读取 typing ticket、
发送输入状态和上下线通知。媒体上传/下载、群聊、卡片不在第一版范围内。

所有方法都通过注入的 `Transport` 发请求。默认构造不会自动发起任何请求；
只有显式调用方法才会联网。
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from app.wechat import protocol
from app.wechat.errors import (
    ApiBusinessError,
    ApiHttpError,
    ProtocolError,
    TransportError,
)
from app.wechat.transport import HttpResponse, Transport, UrllibTransport

DEFAULT_API_BASE = "https://ilinkai.weixin.qq.com"
DEFAULT_CHANNEL_VERSION = "0.1.0"
DEFAULT_BOT_AGENT = "Jia-WeChat/0.1.0"
DEFAULT_APP_ID = "bot"
_MAX_JSON_BYTES = protocol.MAX_UPDATES_RESPONSE_BYTES


@dataclass(frozen=True)
class ApiConfig:
    base_url: str = DEFAULT_API_BASE
    token: str = ""
    app_id: str = DEFAULT_APP_ID
    channel_version: str = DEFAULT_CHANNEL_VERSION
    client_version: tuple[int, int, int] = (0, 1, 0)
    bot_agent: str = DEFAULT_BOT_AGENT
    route_tag: str = ""
    timeout_seconds: float = 15.0
    long_poll_timeout_seconds: float = 35.0


def _client_version_number(parts: tuple[int, int, int]) -> int:
    major, minor, patch = (max(0, min(255, int(v))) for v in parts)
    return (major << 16) | (minor << 8) | patch


def _random_wechat_uin() -> str:
    raw = str(secrets.randbits(32)).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _sanitize_bot_agent(raw: str) -> str:
    text = str(raw or "").strip()
    safe = "".join(
        ch for ch in text if ch.isascii() and (ch.isalnum() or ch in "_.+-()/ ")
    ).strip()
    if not safe or len(safe.encode("utf-8")) > 256:
        return DEFAULT_BOT_AGENT
    return safe


def _reject_constant(value: str) -> None:
    raise ValueError(f"JSON 常量不允许：{value}")


def _parse_json(raw: bytes) -> Any:
    if len(raw) > _MAX_JSON_BYTES:
        raise ProtocolError("响应体超过上限")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("响应不是 UTF-8") from exc
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError("响应不是合法 JSON") from exc


class WeChatApi:
    def __init__(
        self,
        config: ApiConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self.transport = transport if transport is not None else UrllibTransport(
            max_response_bytes=_MAX_JSON_BYTES
        )
        if config.base_url and not protocol.is_allowed_base_url(config.base_url):
            raise ProtocolError("API base_url 不在允许的微信域名内")
        self.base_url = protocol.normalize_base_url(config.base_url) if config.base_url else ""
        self.bot_agent = _sanitize_bot_agent(config.bot_agent)

    # ------------------------------------------------------------------
    # 请求头
    # ------------------------------------------------------------------
    def _common_headers(self) -> dict[str, str]:
        headers = {
            "iLink-App-Id": self.config.app_id,
            "iLink-App-ClientVersion": str(
                _client_version_number(self.config.client_version)
            ),
        }
        if self.config.route_tag.strip():
            headers["SKRouteTag"] = self.config.route_tag.strip()
        return headers

    def _post_headers(self, *, include_auth: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _random_wechat_uin(),
            **self._common_headers(),
        }
        if include_auth:
            token = self.config.token.strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _base_info(self) -> dict[str, str]:
        return {
            "channel_version": self.config.channel_version,
            "bot_agent": self.bot_agent,
        }

    # ------------------------------------------------------------------
    # 低层请求
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        try:
            return await asyncio.to_thread(
                self.transport.request,
                method,
                url,
                headers=headers,
                body=body,
                timeout=timeout,
            )
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError("网络请求失败") from exc

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        auth: bool = True,
        timeout: float | None = None,
        include_base_info: bool = True,
        base_url: str | None = None,
    ) -> Any:
        base = base_url or self.base_url
        if not base:
            raise ProtocolError("没有可用的 API 地址")
        url = f"{base.rstrip('/')}/{endpoint.lstrip('/')}"
        body_data = dict(payload)
        if include_base_info:
            body_data["base_info"] = self._base_info()
        body = json.dumps(
            body_data, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        response = await self._request(
            "POST",
            url,
            headers=self._post_headers(include_auth=auth),
            body=body,
            timeout=self.config.timeout_seconds if timeout is None else timeout,
        )
        if response.status < 200 or response.status >= 300:
            raise ApiHttpError(response.status)
        return _parse_json(response.body)

    async def _get(
        self,
        endpoint: str,
        query: dict[str, str],
        *,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        base = base_url or self.base_url
        if not base:
            raise ProtocolError("没有可用的 API 地址")
        url = f"{base.rstrip('/')}/{endpoint.lstrip('/')}?{urlencode(query)}"
        response = await self._request(
            "GET",
            url,
            headers=self._common_headers(),
            body=None,
            timeout=self.config.timeout_seconds if timeout is None else timeout,
        )
        if response.status < 200 or response.status >= 300:
            raise ApiHttpError(response.status)
        return _parse_json(response.body)

    # ------------------------------------------------------------------
    # 二维码登录
    # ------------------------------------------------------------------
    async def fetch_qr_code(
        self, *, local_token_list: list[str] | None = None, bot_type: str = "3"
    ) -> protocol.QrCode:
        raw = await self._post(
            f"ilink/bot/get_bot_qrcode?bot_type={bot_type}",
            {"local_token_list": list(local_token_list or [])[:10]},
            auth=False,
            include_base_info=False,
            base_url=protocol.FIXED_LOGIN_BASE_URL,
        )
        return protocol.parse_qr_response(raw)

    async def poll_qr_status(
        self,
        *,
        qrcode: str,
        verify_code: str = "",
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> protocol.QrStatus:
        query = {"qrcode": qrcode}
        if verify_code:
            query["verify_code"] = verify_code
        raw = await self._get(
            "ilink/bot/get_qrcode_status",
            query,
            base_url=base_url or protocol.FIXED_LOGIN_BASE_URL,
            timeout=timeout,
        )
        return protocol.parse_qr_status(raw)

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    async def get_updates(
        self, *, cursor: str, timeout_seconds: float | None = None
    ) -> protocol.UpdatesPage:
        timeout = (
            self.config.long_poll_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        raw = await self._post(
            "ilink/bot/getupdates",
            {"get_updates_buf": cursor},
            timeout=timeout,
        )
        return protocol.parse_updates_response(raw)

    async def send_text(
        self,
        *,
        to_user_id: str,
        text: str,
        context_token: str = "",
        run_id: str = "",
        client_id: str | None = None,
    ) -> str:
        if not text.strip():
            raise ProtocolError("不能发送空文本")
        client = client_id or f"jia-wechat-{uuid.uuid4().hex}"
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client,
            "message_type": 2,
            "message_state": 2,
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        if run_id:
            message["run_id"] = run_id
        raw = await self._post("ilink/bot/sendmessage", {"msg": message})
        return protocol.parse_send_response(raw)

    async def get_config(
        self, *, ilink_user_id: str, context_token: str = ""
    ) -> str:
        payload: dict[str, Any] = {"ilink_user_id": ilink_user_id}
        if context_token:
            payload["context_token"] = context_token
        raw = await self._post("ilink/bot/getconfig", payload)
        return protocol.parse_get_config_response(raw)

    async def send_typing(
        self, *, ilink_user_id: str, typing_ticket: str, status: int
    ) -> None:
        if status not in (1, 2):
            raise ProtocolError("typing status 无效")
        await self._post(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": ilink_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
        )

    async def notify_start(self) -> None:
        await self._notify("ilink/bot/msg/notifystart")

    async def notify_stop(self) -> None:
        await self._notify("ilink/bot/msg/notifystop")

    async def _notify(self, endpoint: str) -> None:
        raw = await self._post(endpoint, {}, timeout=self.config.timeout_seconds)
        if isinstance(raw, dict):
            ret = raw.get("ret")
            if isinstance(ret, int) and not isinstance(ret, bool) and ret != 0:
                raise ApiBusinessError("notify", ret)
