"""单次点击工具：把一次可信观察变成一次（可模拟的）点击。

# 模型能说什么

模型只能提交两个参数：

    observation_id   上一次 inspect_screen 返回的可信观察编号
    element_id       那条记录里的元素编号（e1、e2……）

它**不能**提交屏幕坐标、窗口句柄、进程号或窗口标题。坐标和窗口身份
全部由服务端从观察记录里取，并在执行前重新校验。多写一个参数会被
`Tool.validate()` 直接拒绝。

# 执行顺序

    用户确认（gate）
      -> 取回记录、核对确认前固定的目标
      -> 观察记录校验（第 1 次）
      -> 模拟/真实后端聚焦目标窗口
      -> 观察记录校验（第 2 次，复核窗口身份、位置、DPI、坐标）
      -> 前台窗口身份核对
      -> 后端点击
      -> 报告 succeeded / failed / unknown

**关于竞态**：两次校验和"最后再查一次前台"只能把窗口移动、切走、
关闭的窗口缩到很小，不能证明不存在。真正按下鼠标之后仍然可能发生
界面变化。所以后端还必须自己再核对一次窗口身份和前台，并且把
"结果未知"如实报回来——不能把未知当成失败自动重试。

# 后端与启用条件

`ClickElementTool` 不自己决定用不用真实输入。生产登记处通过
`app.input.windows_backend.build_input_backend()` 拿后端：只有配置和
环境变量**同时**满足时才会得到真实后端，否则是 `None`。没有后端时它在
prepare 阶段就失败关闭，连确认卡片都不会弹，更不会退回到任何真实输入
方式。测试注入 `FakeInputBackend` 时，整条链路会真的跑一遍，但所有
"点击"都只记在内存里，不会产生真实鼠标事件。

真实后端下，执行前还要过急停和速率限制（见 `app.input.control`）。
"""

from __future__ import annotations

import logging
from typing import Any

from app.input import control
from app.input.backend import (
    CLICK_FAILED,
    CLICK_SUCCEEDED,
    CLICK_UNKNOWN,
    InputBackend,
    unsafe_target_reason,
)
from app.tools.base import Resolution, Tool, ToolResult
from app.vision import observation

logger = logging.getLogger(__name__)


def _text(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _rect(value: Any) -> tuple[int, int, int, int]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        return (0, 0, 0, 0)
    try:
        return tuple(int(v) for v in value)  # type: ignore[return-value]
    except (TypeError, ValueError):
        return (0, 0, 0, 0)


class ClickElementTool(Tool):
    """在可信观察记录指向的元素上点击一次。

    这个类不实现任何系统输入。它只负责顺序、核对和报告；真正"按下去"
    是 `InputBackend` 的事。生产环境是 `WindowsInputBackend`（真实
    `SendInput`，默认关闭），测试注入 `FakeInputBackend`。
    """

    name = "click_element"
    label = "点击界面元素"
    description = (
        "在最近一次可信屏幕观察中点击一个元素。只能提交 observation_id 和 "
        "element_id；坐标和窗口身份由服务端从观察记录取得，并在执行前重新"
        "校验。用户必须逐次确认。真实输入默认关闭，只有在配置和环境变量"
        "同时启用时才会执行真实鼠标左键单击；未启用时失败关闭。使用模拟"
        "后端时会明确标注，不会产生真实鼠标输入。只有用户明确要求点击某个"
        "已经观察到的元素时才使用。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "observation_id": {
                "type": "string",
                "description": "上一次 inspect_screen 返回的可信观察编号。",
            },
            "element_id": {
                "type": "string",
                "description": "该观察记录里的元素编号，例如 e1、e2。",
            },
        },
        "required": ["observation_id", "element_id"],
    }

    # 点击会改变电脑状态，必须逐次确认。硬编码，没有配置开关能关掉。
    requires_confirmation = True
    # 目标必须在确认前从可信记录里固定，prepare 失败就不弹确认卡片。
    requires_prepared_target = True
    # 点击不可重复。即使后端报"超时/网络"这类看起来瞬时的错误，
    # 工作流也不许自动再点一次。
    retry_safe = False
    # 工作流在执行这一步之前会调用 emergency_block_reason()；急停生效时
    # 连确认卡片都不会出现。
    respects_emergency_stop = True

    def __init__(
        self,
        *,
        store: observation.ObservationStore | None = None,
        backend: InputBackend | None = None,
        emergency: control.EmergencyStop | None = None,
    ) -> None:
        # 生产默认用全局登记处、没有后端；测试注入假的登记处和假后端。
        self._store = store or observation.store
        self._backend = backend
        # 急停是全局的。测试注入隔离实例，避免把真实状态写到 data/。
        self._emergency = emergency if emergency is not None else control.emergency

    def emergency_block_reason(self) -> str:
        """空串 = 可以继续；非空 = 急停生效，必须停止。"""
        return self._emergency.block_reason()

    # ------------------------------------------------------------------
    # 准备：确认之前固定目标
    # ------------------------------------------------------------------
    async def prepare(self, args: dict[str, Any]) -> Resolution:
        try:
            return self._prepare(args)
        except Exception as exc:  # noqa: BLE001 - 闸门会吞 prepare 异常
            # 如果这里抛出去，gate 会按"直接就绪"继续，等于拿模型原始参数
            # 去猜一个目标。必须自己转成明确的阻止。
            logger.exception("准备点击目标时出错")
            return Resolution(
                args=args if isinstance(args, dict) else {},
                blocked=f"无法准备点击目标：{type(exc).__name__}: {exc}",
            )

    def _prepare(self, args: dict[str, Any]) -> Resolution:
        if not isinstance(args, dict):
            args = {}

        obs_id = _text(args.get("observation_id"))
        element_id = _text(args.get("element_id"))
        if not obs_id or not element_id:
            return Resolution(
                args=args,
                blocked="点击必须同时提供 observation_id 和 element_id。",
            )

        record = self._store.get(obs_id)
        if record is None:
            return Resolution(
                args=args,
                blocked=(
                    "这条观察记录不存在或已被清理。请重新查看屏幕，"
                    "不要使用模型编造的观察编号。"
                ),
            )
        if record.status == observation.STATUS_INVALIDATED:
            return Resolution(
                args=args,
                blocked=(
                    f"这条观察记录已失效：{record.status_reason or '已主动失效'}。"
                    "请重新查看屏幕后再点击。"
                ),
            )
        if record.status == observation.STATUS_EXPIRED:
            return Resolution(
                args=args,
                blocked=(
                    f"这条观察记录已过期（有效期 {record.ttl_seconds:g} 秒）。"
                    "请重新查看屏幕后再点击。"
                ),
            )
        if record.scope != "window":
            return Resolution(
                args=args,
                blocked="整桌面观察不能用于定位点击，请针对具体应用窗口重新观察。",
            )
        if not record.operable:
            return Resolution(
                args=args,
                blocked=(
                    "这条观察记录的坐标映射不可信："
                    f"{record.mapping_problem or '缺少可用的窗口上下文'}。"
                    "请重新查看屏幕。"
                ),
            )

        element = record.element(element_id)
        if element is None:
            available = "、".join(item.element_id for item in record.elements)
            return Resolution(
                args=args,
                blocked=(
                    f"观察记录里没有元素 {element_id!r}。可用编号："
                    f"{available or '（无）'}。元素编号必须来自同一次观察。"
                ),
            )

        window = record.window
        if window is None or int(window.hwnd) <= 0:
            return Resolution(args=args, blocked="观察记录缺少目标窗口，不能点击。")

        unsafe = unsafe_target_reason(window)
        if unsafe:
            return Resolution(args=args, blocked=unsafe)

        stop_reason = self._emergency.block_reason()
        if stop_reason:
            return Resolution(
                args=args,
                blocked=f"{stop_reason} 本次点击不会执行。",
            )

        backend = self._backend
        if backend is None:
            return Resolution(
                args=args,
                blocked=(
                    "没有可用的输入后端，本次点击不会执行。"
                    "佳不会自动切换到真实鼠标输入。"
                ),
            )
        status = backend.status()
        if not status.available:
            return Resolution(
                args=args,
                blocked=(
                    f"输入后端不可用：{status.reason or '未配置'}。"
                    "本次点击不会执行，也不会退回真实输入。"
                ),
            )
        secure, secure_reason = backend.secure_desktop()
        if secure:
            return Resolution(
                args=args,
                blocked=(
                    f"当前是 UAC / 安全桌面，不能点击普通窗口：{secure_reason}"
                ),
            )

        # 确认前先做一次只读校验。窗口此时已经移动/关闭/DPI 变化的记录，
        # 不该再拿去打扰用户点确认；执行前还会再校验一次。
        check = self._store.validate(obs_id, element_id)
        if not check.ok:
            return Resolution(
                args=args,
                blocked=f"这条观察现在不能用于点击：{check.reason}",
            )

        injected = {
            "_click_observation_id": record.observation_id,
            "_click_element_id": element.element_id,
            "_target_hwnd": int(window.hwnd),
            "_target_pid": int(window.pid or 0),
            "_target_process": window.process or "",
            "_target_title": window.title or "",
            "_target_class": window.class_name or "",
            "_target_rect": tuple(int(v) for v in record.window_rect),
            "_target_element_name": element.name,
            "_target_element_kind": element.kind,
        }
        return Resolution(args=args, injected=injected)

    def check_injected(self, injected: dict[str, Any]) -> str:
        """闸门在弹确认卡片前调用：目标必须完整，不能带病确认。"""
        data = injected or {}
        if not _text(data.get("_click_observation_id")):
            return "准备阶段没有固定观察编号，已停止"
        if not _text(data.get("_click_element_id")):
            return "准备阶段没有固定元素编号，已停止"
        if _int(data.get("_target_hwnd")) <= 0:
            return "准备阶段没有固定目标窗口，已停止"
        if not _text(data.get("_target_element_name")):
            return "准备阶段没有固定目标元素，已停止"
        if _rect(data.get("_target_rect")) == (0, 0, 0, 0):
            return "准备阶段没有固定窗口位置，已停止"
        return ""

    # ------------------------------------------------------------------
    # 授权卡片
    # ------------------------------------------------------------------
    def describe_action(self, args: dict[str, Any]) -> str:
        args = args or {}
        title = _text(args.get("_target_title")) or "（未知窗口）"
        process = _text(args.get("_target_process")) or "进程未知"
        element = _text(args.get("_target_element_name")) or "（未知元素）"
        kind = _text(args.get("_target_element_kind")) or "other"
        hwnd = _int(args.get("_target_hwnd"))
        simulated = self._backend is None or bool(
            getattr(self._backend, "simulated", False)
        )
        verb = "模拟点击" if simulated else "点击"

        parts = [
            f"{verb}界面元素「{element}」（{kind}）。",
            f"目标应用：{process}。",
            f"目标窗口：{title}（HWND {hwnd:#x}）。"
            if hwnd else f"目标窗口：{title}。",
            "坐标由服务端根据可信观察记录计算；你只提交了观察编号和元素编号，"
            "不能自己指定坐标或窗口。",
        ]
        if simulated:
            parts.append("本次使用模拟输入后端，不会产生真实鼠标输入。")
        else:
            backend_name = _text(getattr(self._backend, "name", ""), 40)
            parts.append(
                "本次会执行真实鼠标左键单击（真实输入），不是模拟"
                + (f"；输入后端：{backend_name}" if backend_name else "")
                + "。执行前会再次核对窗口身份、位置、前台和桌面安全，"
                "任何一项不符都会停止。"
            )
        return "｜".join(parts)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def run(
        self,
        observation_id: str = "",
        element_id: str = "",
        *,
        _click_observation_id: str = "",
        _click_element_id: str = "",
        _target_hwnd: int = 0,
        _target_pid: int = 0,
        _target_process: str = "",
        _target_title: str = "",
        _target_class: str = "",
        _target_rect: Any = None,
        _target_element_name: str = "",
        _target_element_kind: str = "",
    ) -> ToolResult:
        # ---- 0. 必须是经过确认前固定目标的调用 ----
        # 直接调用 execute()（没有注入）时，这里会停下。绝不能拿模型
        # 原始参数去猜目标。
        pinned_obs = _text(_click_observation_id)
        pinned_element = _text(_click_element_id)
        if not pinned_obs or not pinned_element:
            return self._failure(
                "这次点击没有经过确认前的目标固定，未执行。请重新发起并确认。",
                persist="点击：没有经过确认前的目标固定，未执行",
            )
        if observation_id and observation_id != pinned_obs:
            return self._failure(
                "模型给的观察编号与确认前固定的目标不一致，未执行。",
                persist="点击：观察编号与固定目标不一致，未执行",
            )
        if element_id and element_id != pinned_element:
            return self._failure(
                "模型给的元素编号与确认前固定的目标不一致，未执行。",
                persist="点击：元素编号与固定目标不一致，未执行",
            )
        obs_id = pinned_obs
        elem_id = pinned_element

        if _int(_target_hwnd) <= 0:
            return self._failure(
                "确认前没有固定目标窗口，未执行。请重新发起并确认。",
                persist="点击：没有固定目标窗口，未执行",
            )

        # 急停优先于一切。即使确认卡片已经点过，急停之后也不许再执行。
        stop_reason = self._emergency.block_reason()
        if stop_reason:
            return self._failure(
                f"{stop_reason} 未执行点击。",
                persist="点击：急停生效，未执行",
            )

        # ---- 1. 后端必须存在且可用。没有就失败关闭，绝不换真实方式 ----
        backend = self._backend
        if backend is None:
            return self._failure(
                "没有可用的输入后端，未执行任何点击。"
                "佳不会自动切换到真实鼠标输入。",
                persist="点击：没有可用的输入后端，未执行",
            )
        status = backend.status()
        if not status.available:
            return self._failure(
                f"输入后端不可用：{status.reason or '未配置'}。未执行任何点击。",
                persist=f"点击：输入后端不可用（{status.reason or '未配置'}），未执行",
            )

        # ---- 2. 取回可信记录，核对确认前固定的目标 ----
        record = self._store.get(obs_id)
        if record is None:
            return self._failure(
                "观察记录在执行前已经不存在，未执行点击。请重新查看屏幕。",
                persist="点击：观察记录不存在，未执行",
            )
        if record.status != observation.STATUS_VALID:
            reason = record.status_reason or record.status
            return self._failure(
                f"观察记录在执行前已经失效（{reason}），未执行点击。"
                "请重新查看屏幕。",
                persist=f"点击：观察记录已失效（{reason}），未执行",
            )
        if record.scope != "window" or not record.operable:
            return self._failure(
                "这条观察记录不能用于定位点击，未执行。请针对具体应用窗口重新观察。",
                persist="点击：观察记录不可操作，未执行",
            )
        window = record.window
        if window is None or int(window.hwnd) != _int(_target_hwnd):
            return self._failure(
                "观察记录里的目标窗口与确认时固定的窗口不一致，未执行。",
                persist="点击：目标窗口与确认时不一致，未执行",
            )
        if tuple(int(v) for v in record.window_rect) != _rect(_target_rect):
            return self._failure(
                "观察记录里的窗口位置与确认时不一致，未执行。请重新观察。",
                persist="点击：窗口位置与确认时不一致，未执行",
            )
        element = record.element(elem_id)
        if element is None:
            return self._failure(
                f"观察记录里已经没有元素 {elem_id!r}，未执行点击。",
                persist="点击：目标元素不存在，未执行",
            )
        if (
            _text(_target_element_name)
            and element.name != _text(_target_element_name)
        ):
            return self._failure(
                "观察记录里的目标元素与确认时不一致，未执行。请重新观察。",
                persist="点击：目标元素与确认时不一致，未执行",
            )

        unsafe = unsafe_target_reason(window)
        if unsafe:
            return self._failure(
                f"{unsafe}未执行点击。",
                persist="点击：目标是 UAC / 安全桌面，未执行",
            )
        secure, secure_reason = backend.secure_desktop()
        if secure:
            return self._failure(
                f"当前是 UAC / 安全桌面，不能执行点击：{secure_reason}",
                persist="点击：当前处于安全桌面，未执行",
            )

        # ---- 3. 点击前校验（观察记录、窗口、DPI、坐标）----
        first = self._store.validate(obs_id, elem_id)
        if not first.ok:
            return self._failure(
                f"点击前校验失败：{first.reason}",
                persist=f"点击前校验失败：{first.code}",
            )

        # ---- 4. 聚焦目标窗口 ----
        focus = backend.focus_window(
            window=window, window_rect=tuple(record.window_rect)
        )
        if not focus.ok:
            return self._failure(
                f"无法聚焦目标窗口：{focus.reason}"
                + (f"（{focus.code}）" if focus.code else "")
                + " 未执行点击。",
                persist=f"点击：无法聚焦目标窗口（{focus.code or 'unknown'}），未执行",
            )

        # ---- 5. 聚焦后立刻复核。窗口可能在聚焦过程中移动/关闭/换掉 ----
        second = self._store.validate(obs_id, elem_id)
        if not second.ok:
            return self._failure(
                f"聚焦后复核失败：{second.reason}未执行点击。",
                persist=f"点击：聚焦后复核失败（{second.code}），未执行",
            )

        # ---- 6. 前台必须是目标窗口。这是点击前最后一道竞态检查 ----
        fg_ok, fg_reason = backend.foreground_matches(window)
        if not fg_ok:
            return self._failure(
                f"前台窗口不是目标窗口：{fg_reason}未执行点击。",
                persist="点击：前台窗口身份不匹配，未执行",
            )

        secure, secure_reason = backend.secure_desktop()
        if secure:
            return self._failure(
                f"点击前检测到 UAC / 安全桌面，未执行：{secure_reason}",
                persist="点击：点击前处于安全桌面，未执行",
            )

        if second.screen_x is None or second.screen_y is None:
            return self._failure(
                "服务端没有算出可执行的屏幕坐标，未执行点击。",
                persist="点击：没有可执行坐标，未执行",
            )

        # 最后一次急停检查，尽量贴近真正发送输入的时刻。
        stop_reason = self._emergency.block_reason()
        if stop_reason:
            return self._failure(
                f"{stop_reason} 未执行点击。",
                persist="点击：发送前急停生效，未执行",
            )

        # ---- 7. 真正交给后端。这里之后就不能再自动重试 ----
        try:
            outcome = backend.click(
                window=window,
                screen_x=int(second.screen_x),
                screen_y=int(second.screen_y),
            )
        except Exception as exc:  # noqa: BLE001 - 异常时无法确认是否点上
            logger.exception("输入后端点击时抛出异常")
            self._store.invalidate(obs_id, "点击结果未知，旧画面不再可信")
            return self._failure(
                f"点击结果未知：输入后端抛出异常（{type(exc).__name__}: {exc}）。"
                "已停止，不会自动重试；请先确认目标程序现在的状态。",
                persist=f"点击结果未知：后端异常（{type(exc).__name__}），未自动重试",
                outcome=CLICK_UNKNOWN,
            )

        # 无论成功、失败还是未知，画面都可能已经变化，旧记录不再复用。
        self._store.invalidate(obs_id, "点击已尝试，旧观察不再可信")

        simulated = bool(getattr(backend, "simulated", False)) or outcome.simulated
        verb = "模拟点击" if simulated else "点击"
        title = window.title or "（无标题窗口）"
        process = window.process or "进程未知"
        target_text = f"{element.name}（{title} / {process}）"

        if outcome.status == CLICK_SUCCEEDED:
            lines = [
                f"{verb}完成：{element.name}（{element.kind}）",
                f"目标应用：{process}",
                f"目标窗口：{title}（HWND {int(window.hwnd):#x}）",
                f"服务端映射坐标：({int(outcome.screen_x)}, "
                f"{int(outcome.screen_y)}) 物理像素",
                f"输入后端：{backend.name}",
            ]
            if simulated:
                lines.append("这是模拟输入后端，没有发送任何真实鼠标输入。")
            return ToolResult(
                ok=True,
                output="\n".join(lines),
                persist_text=(
                    f"{verb}：{target_text}"
                    + ("（模拟，无真实输入）" if simulated else "")
                ),
                outcome=CLICK_SUCCEEDED,
            )

        if outcome.status == CLICK_UNKNOWN:
            return self._failure(
                f"{verb}结果未知：{outcome.reason or outcome.code}。"
                "已停止，不会自动重试；请先确认目标程序现在的状态，"
                "再决定是否重新查看屏幕并重新授权。",
                persist=(
                    f"{verb}结果未知：{target_text}；已停止，未自动重试"
                ),
                outcome=CLICK_UNKNOWN,
            )

        # 后端明确失败
        return self._failure(
            f"{verb}失败：{outcome.reason or outcome.code or '输入后端报告失败'}",
            persist=(
                f"{verb}失败：{target_text}："
                f"{outcome.reason or outcome.code or '后端报告失败'}"
            ),
            outcome=CLICK_FAILED,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _failure(
        error: str,
        *,
        persist: str = "",
        outcome: str = CLICK_FAILED,
    ) -> ToolResult:
        return ToolResult(
            ok=False,
            error=error,
            persist_error=persist or error,
            outcome=outcome,
        )
