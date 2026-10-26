"""查看屏幕的工具。

这个工具只做"看"：截图 → 交给视觉模型 → 返回结构化描述。
**不点击、不输入、不按键**。

捕获范围（scope）：
  auto    默认。最近使用过的、不是佳自己的那个应用窗口。
  window  用户明确指定的应用窗口（按进程名或标题关键词匹配）。
  desktop 整个桌面。只有用户明确要求看整个桌面时才用。

三条安全约定：
  1. requires_confirmation = True，且**没有配置开关能关掉**。
     截图本身是只读的，但画面会离开这台电脑，必须由你本人点头。
  2. 确认卡片上写清楚：看哪个窗口（或整个桌面）、实际范围、发给哪个模型、
     以及隐私风险。
  3. 落库只用安全摘要（persist_text / persist_error）。
     完整的界面分析只给当前这次执行流程用，不进数据库。

目标固定：
  prepare() 在**用户确认之前**确定 HWND / 进程 / 标题 / 范围，并通过
  Resolution.injected 交给闸门。确认之后只用这个固定的 HWND；它失效、
  最小化或身份变化就中止并要求重新确认，绝不悄悄换一个目标。
"""

from __future__ import annotations

import logging
from typing import Any

from app.errors import JiaAgentError, VisionError
from app.settings import get_config, get_tools_config
from app.tools.base import Resolution, Tool, ToolResult
from app.vision import capture, focus, observation, wincapture
from app.vision import window as window_mod
from app.vision.analyze import analyze_screen
from app.vision.client import build_client

logger = logging.getLogger(__name__)

_SCOPES = ("auto", "window", "desktop")


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _vision_cfg():
    return get_tools_config().vision


def _foreground_label() -> str:
    """一句话描述当前前台窗口。读不到就老实说读不到。"""
    try:
        window = window_mod.get_foreground_window()
    except Exception:  # noqa: BLE001
        return "（读不到）"
    if window is None:
        return "（没有前台窗口）"
    title = window.title or "（无标题窗口）"
    process = window.process or "进程未知"
    return f"{title}（{process}）"


def _vision_model_label() -> str:
    """确认卡片上显示的"画面会发给谁"。

    **只读 models.yaml，不创建适配器、不检查 Key**——
    因为 describe_action 是在用户点确认之前调用的，不能有副作用。
    """
    try:
        cfg = get_tools_config().vision
        provider = get_config().provider(cfg.provider or None)
        return f"{provider.label or provider.name} / {provider.model}"
    except Exception:  # noqa: BLE001 - 配置有问题也不能让授权卡片画不出来
        return "视觉模型（未配置）"


def _focus_filters() -> tuple[list[str], list[str]]:
    cfg = _vision_cfg()
    patterns = list(getattr(cfg, "jia_title_patterns", []) or [])
    processes = list(getattr(cfg, "jia_process_names", []) or [])
    return patterns, processes


def _is_jia(info) -> bool:
    patterns, processes = _focus_filters()
    return focus.is_jia_window(
        info, title_patterns=patterns, process_names=processes
    )


def _guard_reason(info) -> str:
    """目标窗口现在能不能截。返回空串表示可以。"""
    if info is None or not getattr(info, "hwnd", 0):
        return "目标窗口已失效（句柄不存在或窗口已关闭）"
    if not getattr(info, "visible", False):
        return "目标窗口当前不可见"
    if getattr(info, "minimized", False):
        return "目标窗口已最小化，请先恢复它"
    if not getattr(info, "is_valid", False):
        return "目标窗口尺寸无效"
    return ""


def _matches_selector(info, key: str) -> bool:
    title = (getattr(info, "title", "") or "").casefold()
    process = (getattr(info, "process", "") or "").casefold()
    return key in title or key in process or key == process.removesuffix(".exe")


def _find_windows(selector: str) -> list:
    """按进程名或标题关键词找窗口。佳自己的界面不算候选。"""
    key = (selector or "").strip().casefold()
    if not key:
        return []

    items = window_mod.list_windows(limit=300)
    found = []
    seen: set[int] = set()
    for info in items:
        hwnd = int(getattr(info, "hwnd", 0) or 0)
        if not hwnd or hwnd in seen:
            continue
        if not _matches_selector(info, key):
            continue
        if _is_jia(info):
            continue
        seen.add(hwnd)
        found.append(info)
    return found


def _pin(scope: str, info, model_label: str) -> dict[str, Any]:
    """服务端固定下来的目标。模型无法伪造这些键。"""
    return {
        "_scope": scope,
        "_vision_model": model_label,
        "_target_hwnd": int(getattr(info, "hwnd", 0) or 0),
        "_target_pid": int(getattr(info, "pid", 0) or 0),
        "_target_process": getattr(info, "process", "") or "",
        "_target_title": getattr(info, "title", "") or "",
    }


def _target_text(args: dict[str, Any]) -> tuple[str, str]:
    title = str(args.get("_target_title", "") or "").strip() or "（无标题窗口）"
    process = str(args.get("_target_process", "") or "").strip() or "进程未知"
    return title, process


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _hwnd_text(args: dict[str, Any]) -> str:
    hwnd = _int_or_zero(args.get("_target_hwnd", 0))
    return f"HWND {hwnd:#x}" if hwnd else "HWND 未知"


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
class InspectScreenTool(Tool):
    name = "inspect_screen"
    label = "查看屏幕"
    description = (
        "截取一个应用窗口或整个桌面，交给视觉模型分析，返回界面描述、"
        "可见元素和候选操作区域。默认只看最近使用过的、不是佳自己的那个"
        "应用窗口；只有用户明确要求看整个桌面时才用 scope=desktop。"
        "当用户问「屏幕上是什么」「那个按钮在哪」「现在是什么界面」时用它。"
        "这个工具只观察，不会点击或输入任何东西。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "你希望重点看什么，例如「登录按钮在哪」。不填就整体描述界面。",
            },
            "scope": {
                "type": "string",
                "enum": ["auto", "window", "desktop"],
                "description": (
                    "捕获范围。auto=最近使用的非佳应用窗口（默认）；"
                    "window=用户明确指定的应用窗口；"
                    "desktop=整个桌面（只在用户明确要求时使用）。"
                ),
            },
            "window": {
                "type": "string",
                "description": (
                    "scope=window 时必填：应用窗口的进程名或标题关键词，"
                    "例如 vscode、Code.exe、记事本。"
                ),
            },
        },
        "required": [],
    }
    # 截图只读，但画面会离开这台电脑。硬编码为 True，**不提供关闭开关**。
    requires_confirmation = True
    # 目标必须在 prepare() 里固定。prepare 崩溃或没有注入目标时，
    # 闸门会在弹确认卡片之前停止，绝不拿模型给的参数去猜一个窗口。
    requires_prepared_target = True

    # ------------------------------------------------------------------
    # prepare：确认之前把目标固定下来，不截图
    # ------------------------------------------------------------------
    async def prepare(self, args: dict[str, Any]) -> Resolution:
        try:
            return self._prepare(args)
        except Exception as exc:  # noqa: BLE001
            # 闸门会吞掉 prepare 的异常并按「就绪」继续。为了不让一个 bug
            # 变成「悄悄改抓整个桌面」，这里自己把任何意外转成明确的阻止。
            logger.exception("确定截图目标时出错")
            return Resolution(
                args=args,
                blocked=f"无法确定截图目标：{type(exc).__name__}: {exc}",
            )

    def _prepare(self, args: dict[str, Any]) -> Resolution:
        if not isinstance(args, dict):
            args = {}

        cfg = _vision_cfg()
        if not cfg.enabled:
            return Resolution(args=args, blocked="屏幕感知没有打开")

        scope = str(args.get("scope", "auto") or "auto").strip().lower()
        if scope not in _SCOPES:
            return Resolution(
                args=args,
                blocked=f"捕获范围只能是 auto、window、desktop，收到的是 {scope!r}",
            )

        model_label = _vision_model_label()

        if scope == "desktop":
            # 明确请求整个桌面：不需要目标窗口。
            return Resolution(
                args=args,
                injected={"_scope": "desktop", "_vision_model": model_label},
            )

        available, why = wincapture.is_available()
        if not available:
            return Resolution(args=args, blocked=f"窗口捕获不可用：{why}")

        if scope == "window":
            selector = str(args.get("window", "") or "").strip()
            if not selector:
                return Resolution(
                    args=args,
                    blocked="scope=window 时必须说明要捕获哪个应用窗口（window 参数）",
                )
            matches = _find_windows(selector)
            if not matches:
                return Resolution(
                    args=args,
                    blocked=(
                        f"没有找到标题或进程名匹配「{selector}」的可见应用窗口"
                        "（佳自己的界面不会作为捕获目标）"
                    ),
                )
            if len(matches) > 1:
                names = "、".join(
                    f"{getattr(m, 'title', '') or '（无标题）'}"
                    f"（{getattr(m, 'process', '') or '进程未知'}，"
                    f"{int(getattr(m, 'hwnd', 0)):#x}）"
                    for m in matches[:6]
                )
                more = f" 等 {len(matches)} 个" if len(matches) > 6 else ""
                return Resolution(
                    args=args,
                    blocked=(
                        f"「{selector}」匹配到多个窗口：{names}{more}。"
                        "请说得更具体，佳不会随便挑一个。"
                    ),
                )
            info = matches[0]
            reason = _guard_reason(info)
            if reason:
                return Resolution(args=args, blocked=reason)
            return Resolution(
                args=args, injected=_pin("window", info, model_label)
            )

        # ---- auto：最近使用的、不是佳自己的窗口 ----
        target, reason = focus.tracker.latest()
        if target is None:
            return Resolution(
                args=args,
                blocked=f"{reason or '没有可用的最近窗口'}。佳不会改抓另一个窗口或整个桌面。",
            )
        info = window_mod.get_window_info(int(target.hwnd))
        if info is None:
            return Resolution(
                args=args,
                blocked=(
                    "最近使用的窗口已经关闭或句柄失效。"
                    "佳不会改抓另一个窗口或整个桌面，请重新发起。"
                ),
            )
        if _is_jia(info):
            return Resolution(
                args=args,
                blocked="最近使用的窗口是佳自己的界面，不能作为默认捕获目标",
            )
        reason = _guard_reason(info)
        if reason:
            return Resolution(
                args=args,
                blocked=f"{reason}。佳不会改抓另一个窗口，请先处理这个窗口或重新发起。",
            )
        return Resolution(args=args, injected=_pin("auto", info, model_label))

    def check_injected(self, injected: dict[str, Any]) -> str:
        """闸门在弹确认卡片之前调用：确认前固定的目标必须完整。

        这里挡住的是「prepare 返回了，但目标信息缺一块」的情况。
        不能等到 run() 再发现——那时用户已经为一个不完整的目标点过确认了。
        """
        data = injected or {}
        scope = str(data.get("_scope", "") or "").strip().lower()
        if scope not in _SCOPES:
            return "准备阶段没有确定有效的捕获范围，已停止"
        if not str(data.get("_vision_model", "") or "").strip():
            return "准备阶段没有确定视觉模型，已停止"
        if scope == "desktop":
            return ""
        if _int_or_zero(data.get("_target_hwnd", 0)) <= 0:
            return "准备阶段没有固定目标窗口，已停止"
        if (
            _int_or_zero(data.get("_target_pid", 0)) <= 0
            and not str(data.get("_target_process", "") or "").strip()
        ):
            return "准备阶段没有固定目标窗口的身份信息，已停止"
        return ""

    # ------------------------------------------------------------------
    # 授权卡片
    # ------------------------------------------------------------------
    def describe_action(self, args: dict[str, Any]) -> str:
        """授权卡片上的说明。

        必须同时说清楚三件事：截取范围、发给谁、隐私风险。
        对于 auto/window，目标是 prepare 阶段固定下来的那个窗口；
        没有固定目标时**绝不**声称会截整个桌面。
        """
        args = args or {}
        question = str(args.get("question", "") or "").strip()
        scope = str(args.get("_scope", "") or "").strip().lower()
        model = (
            str(args.get("_vision_model", "") or "").strip()
            or _vision_model_label()
        )

        if scope == "desktop":
            parts = [
                f"查看屏幕：将截取整个桌面，并发送给 {model} 进行分析。"
                "截图可能包含其他窗口中的信息。",
                f"当前前台窗口：{_foreground_label()}"
                "（这只是截图时的界面状态，不是截取范围）。",
            ]
        elif scope in ("auto", "window"):
            title, process = _target_text(args)
            how = "最近使用的非佳窗口" if scope == "auto" else "你指定的应用窗口"
            parts = [
                f"查看屏幕：将截取{how}「{title}」（{process}，{_hwnd_text(args)}），"
                f"并发送给 {model} 进行分析。",
                "截取范围只有这一个应用窗口（含标题栏），不包括整个桌面，"
                "也不包括其他窗口；即使它被遮挡，拿到的也是它自己的画面。"
                "窗口内仍可能有敏感信息。",
            ]
        else:
            # 没有 prepare 固定的目标：直接调用、或 prepare 被阻止。
            # 绝不能在这里声称「截整个桌面」。
            parts = [
                "查看屏幕：尚未确定要截取哪个窗口，这次不会执行截图。",
                f"如果执行，画面会发送给 {model} 进行分析。",
            ]

        if question:
            parts.append(f"重点看：{question}")
        return "｜".join(parts)

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def run(
        self,
        question: str = "",
        scope: str = "auto",
        window: str = "",
        _scope: str = "",
        _vision_model: str = "",
        _target_hwnd: int = 0,
        _target_pid: int = 0,
        _target_process: str = "",
        _target_title: str = "",
    ) -> ToolResult:
        cfg = _vision_cfg()

        if not cfg.enabled:
            return ToolResult(
                ok=False,
                error="屏幕感知没有打开",
                persist_error="屏幕感知没有打开",
            )

        active_scope = str(_scope or "").strip().lower()
        if active_scope not in _SCOPES:
            # 没有经过 prepare 注入就调用 execute：拒绝，而不是猜一个范围。
            return ToolResult(
                ok=False,
                error="这次查看屏幕没有经过确认流程，未执行截图。请重新发起。",
                persist_error="查看屏幕：没有经过确认流程，未执行",
            )
        if not str(_vision_model or "").strip():
            # 视觉模型也应在确认前固定并展示给用户。缺了就是准备结果不完整。
            return ToolResult(
                ok=False,
                error=(
                    "这次查看屏幕缺少确认前固定的模型信息，未执行截图。"
                    "请重新发起并确认。"
                ),
                persist_error="查看屏幕：缺少固定的模型信息，未执行",
            )

        # ---- 1. 按固定范围拿到截图（不落盘）----
        if active_scope == "desktop":
            try:
                snap = capture.snapshot(annotate=False, max_side=cfg.max_side)
            except VisionError as exc:
                detail = f"（{exc.detail}）" if exc.detail else ""
                return ToolResult(
                    ok=False,
                    error=f"截屏失败：{exc.message}{detail}",
                    persist_error=f"截屏失败：{exc.message}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("调用截图时出意外")
                return ToolResult(
                    ok=False,
                    error=f"截屏失败：{type(exc).__name__}: {exc}",
                    persist_error=f"截屏失败：{type(exc).__name__}",
                )
            analysis_scope = "desktop"
        else:
            hwnd = _int_or_zero(_target_hwnd)
            pinned_pid = _int_or_zero(_target_pid)
            pinned_process = str(_target_process or "").strip()
            pinned_title = str(_target_title or "").strip()
            if hwnd <= 0:
                return ToolResult(
                    ok=False,
                    error="没有固定的目标窗口，未执行截图。请重新发起并确认。",
                    persist_error="查看屏幕：没有固定目标窗口，未执行",
                )
            if pinned_pid <= 0 and not pinned_process:
                return ToolResult(
                    ok=False,
                    error=(
                        "这次查看屏幕缺少确认前固定的窗口身份信息，未执行截图。"
                        "请重新发起并确认。"
                    ),
                    persist_error="查看屏幕：缺少固定的窗口身份信息，未执行",
                )

            # ---- 1a. 批准之后，重新核对还是卡片上那个窗口 ----
            info = window_mod.get_window_info(hwnd)
            reason = _guard_reason(info)
            if reason:
                return ToolResult(
                    ok=False,
                    error=f"{reason}，未执行截图。请重新发起并确认。",
                    persist_error=f"查看屏幕：{reason}，未执行",
                )
            if _is_jia(info):
                return ToolResult(
                    ok=False,
                    error="目标窗口是佳自己的界面，未执行截图。",
                    persist_error="查看屏幕：目标是佳自己的界面，未执行",
                )
            if pinned_pid > 0 and int(info.pid or 0) != pinned_pid:
                return ToolResult(
                    ok=False,
                    error="目标窗口的进程已经变化，未执行截图。请重新发起并确认。",
                    persist_error="查看屏幕：目标进程变化，未执行",
                )
            if (
                pinned_process
                and (info.process or "").casefold() != pinned_process.casefold()
            ):
                return ToolResult(
                    ok=False,
                    error="目标窗口的进程已经变化，未执行截图。请重新发起并确认。",
                    persist_error="查看屏幕：目标进程变化，未执行",
                )
            if pinned_title and (info.title or "") != pinned_title:
                return ToolResult(
                    ok=False,
                    error="目标窗口的标题已经变化，未执行截图。请重新发起并确认。",
                    persist_error="查看屏幕：目标标题变化，未执行",
                )

            # ---- 1b. 按 HWND 捕获；失败不退回 PrintWindow / 整桌截图 ----
            try:
                frame = wincapture.capture_window(
                    hwnd,
                    max_side=cfg.max_side,
                    timeout=getattr(cfg, "capture_timeout_seconds", 3.0),
                    max_black_ratio=getattr(cfg, "max_black_ratio", 0.98),
                    max_uniform_ratio=getattr(cfg, "max_uniform_ratio", 0.995),
                    min_distinct_colors=getattr(cfg, "min_distinct_colors", 3),
                    sample_side=getattr(cfg, "validate_sample_side", 128),
                )
            except VisionError as exc:
                detail = f"（{exc.detail}）" if exc.detail else ""
                return ToolResult(
                    ok=False,
                    error=(
                        f"窗口捕获失败：{exc.message}{detail}"
                        "（不会退回整张桌面截图）"
                    ),
                    persist_error=f"窗口捕获失败：{exc.message}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("窗口捕获时出现未预期错误")
                return ToolResult(
                    ok=False,
                    error=(
                        f"窗口捕获失败：{type(exc).__name__}: {exc}"
                        "（不会退回整张桌面截图）"
                    ),
                    persist_error=f"窗口捕获失败：{type(exc).__name__}",
                )

            snap = frame.snapshot
            analysis_scope = "window"

            # ---- 1c. 捕获期间目标又变了？丢弃画面，绝不拿去分析/上传 ----
            after = window_mod.get_window_info(hwnd)
            after_reason = _guard_reason(after)
            changed = bool(
                after_reason
                or (
                    pinned_pid > 0
                    and int(after.pid or 0) != pinned_pid
                )
                or (
                    pinned_process
                    and (after.process or "").casefold()
                    != pinned_process.casefold()
                )
                or (
                    pinned_title
                    and (after.title or "") != pinned_title
                )
            )
            if changed:
                if after_reason:
                    why = after_reason
                elif pinned_pid > 0 and int(after.pid or 0) != pinned_pid:
                    why = "目标窗口的进程已经变化"
                elif (
                    pinned_process
                    and (after.process or "").casefold()
                    != pinned_process.casefold()
                ):
                    why = "目标窗口的进程已经变化"
                elif pinned_title and (after.title or "") != pinned_title:
                    why = "目标窗口的标题已经变化"
                else:
                    why = "目标窗口的身份或标题在捕获期间变化"
                return ToolResult(
                    ok=False,
                    error=f"{why}，本次画面已丢弃。请重新发起并确认。",
                    persist_error=f"查看屏幕：{why}，画面已丢弃",
                )

        # ---- 2. 视觉模型 ----
        try:
            client = build_client(cfg)
        except JiaAgentError as exc:
            detail = f"（{exc.detail}）" if exc.detail else ""
            return ToolResult(
                ok=False,
                error=f"{exc.message}{detail}",
                persist_error=exc.message,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("创建视觉客户端时出意外")
            return ToolResult(
                ok=False,
                error=f"视觉模型初始化失败：{type(exc).__name__}: {exc}",
                persist_error=f"视觉模型初始化失败：{type(exc).__name__}",
            )

        # ---- 3. 分析 ----
        try:
            reading = await analyze_screen(
                snap,
                question,
                client,
                max_elements=cfg.max_elements,
                scope=analysis_scope,
            )
        except VisionError as exc:
            detail = f"（{exc.detail}）" if exc.detail else ""
            return ToolResult(
                ok=False,
                error=f"视觉分析失败：{exc.message}{detail}",
                # 落库只留错误类型 + 模型名，不带任何画面内容
                persist_error=f"视觉分析失败：{exc.message}（模型 {client.model}）",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("视觉分析出现未预期错误")
            return ToolResult(
                ok=False,
                error=f"视觉分析失败：{type(exc).__name__}: {exc}",
                persist_error=f"视觉分析失败：{type(exc).__name__}",
            )

        # ---- 4. 登记可信观察记录（只存几何，不存图像）----
        # 只有走到这里，才说明"真实截图 + 成功分析"都已发生。记录由服务端
        # 生成编号，模型无法创建，也无法把 observation_id 写进参数来伪造。
        content_size = frame.content_size if analysis_scope == "window" else None
        try:
            record = observation.store.create_from_capture(
                snap,
                reading,
                scope=analysis_scope,
                ttl_seconds=getattr(
                    cfg,
                    "observation_ttl_seconds",
                    observation.DEFAULT_TTL_SECONDS,
                ),
                content_size=content_size,
            )
        except Exception as exc:  # noqa: BLE001 - 登记失败不能假装成功
            logger.exception("登记观察记录失败")
            return ToolResult(
                ok=False,
                error=f"观察记录建立失败：{type(exc).__name__}: {exc}",
                persist_error=(
                    "视觉分析已完成，但观察记录建立失败；"
                    "这次结果不能作为可信观察用于后续操作"
                ),
            )

        # 完整分析 + 可信编号一起给模型；数据库仍然只写安全摘要。
        output = f"{reading.render_text()}\n\n{record.describe_for_model()}"

        # ---- 5. 数据库只留安全摘要 ----
        if analysis_scope == "window":
            info = snap.window
            title = (info.title if info is not None else "") or "（无标题窗口）"
            process = (info.process if info is not None else "") or "进程未知"
            persist_text = f"查看屏幕：窗口「{title}」（{process}）"
        else:
            window_info = snap.window
            window_text = (
                f"{window_info.title or '（无标题窗口）'}"
                f"（{window_info.process or '进程未知'}）"
                if window_info is not None
                else "当前屏幕"
            )
            persist_text = f"查看屏幕：{window_text}"

        return ToolResult(
            ok=True,
            # 完整分析 + 观察编号只给模型和当前执行流程用
            output=output,
            persist_text=persist_text,
        )
