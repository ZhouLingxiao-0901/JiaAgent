"""工具模块的公共部分：工具长什么样、怎么执行、出错怎么报。

三条规矩（对应项目目标 6：重要操作必须用户确认）：

  1. requires_confirmation 默认 True，也就是"默认必须授权"。
     只有确定没有副作用的工具（纯读取）才允许改成 False。

  2. 工具自己不解析路径、不判断权限，一律先过 app/tools/policy.py。
     工具里出现任何自己拼路径的代码，都是 bug。

  3. 工具不允许把异常抛给上层，也不允许编造成功。
     统一走 execute()，失败时必须把真实原因带上。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.errors import JiaAgentError

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """一次工具执行的结果。ok=False 时 error 必须是真实原因。

    两套文本，用途不同：
      output / error                给模型和当前执行流程看，可以是完整内容。
      persist_text / persist_error  给**数据库**看，必须是安全摘要。

    有些输出不适合原样落库（比如屏幕分析），所以"给模型看什么"和
    "往库里写什么"必须分开。这两个字段留空时行为与以前完全一致。
    """

    ok: bool
    output: str = ""
    error: str = ""
    persist_text: str = ""    # 落库用的成功摘要；空 = 沿用 as_text()
    persist_error: str = ""   # 落库用的错误摘要；空 = 沿用 error/output
    # 有副作用、不可重复的操作要能区分「明确失败」和「结果未知」。
    # 空串 = 沿用原有语义（成功即 succeeded，失败即 failed）。
    # 点击类工具在结果未知时会写 "unknown"，调用方据此提示用户，
    # **不能**把它当成普通失败自动重试。
    outcome: str = ""
    # 给界面看的细分状态，例如 read_ok / search_truncated /
    # file_not_found / sensitive_file。空串 = 沿用原来的「执行完成/失败」。
    # 它只是展示用的标签，不改变 ok 的语义，也不能当成功证明。
    status: str = ""

    def as_text(self) -> str:
        """转成给模型看的文字。失败时明确写"执行失败"，绝不伪装成功。"""
        if self.ok:
            return self.output or "（执行完成，没有输出）"
        return f"执行失败：{self.error}"

    def as_persist_text(self) -> str:
        """给数据库看的成功结果。默认等于 as_text()，现有工具行为不变。"""
        return self.persist_text or self.as_text()

    def as_persist_error(self) -> str:
        """给数据库看的失败原因。

        默认等价于引擎原来那句 `error or output or "未知错误"`，
        所以没设置 persist_error 的工具行为完全不变。
        """
        return self.persist_error or self.error or self.output or "未知错误"


class ToolError(Exception):
    """工具内部用来中断执行的异常，会被 execute() 转成失败结果。"""


@dataclass
class Resolution:
    """prepare() 的结果：要么可以照常往下走，要么得先请用户挑一个，要么直接停。

    choices 为空 = 就绪，工具行为和以前一模一样（绝大多数工具都是这样）。
    填了 choices = 这事有歧义，先让用户在网页上选，选完再继续。
    blocked 非空 = 这次调用在弹出确认卡片**之前**就该停下，原因给用户看。
    injected = 服务端在 prepare 阶段确定、随执行一起注入的参数。

    每个候选项是一个字典，至少要有 id 和 name。那个 id 是服务端发出去的
    一次性令牌：用户选中的只会是这里列过的编号，别的编号会被直接拒掉。

    为什么 blocked 不能靠抛异常实现：闸门为了不让一次 prepare 的小毛病
    把整轮对话带崩，会吞掉 prepare 的异常并按“直接就绪”继续。只有这个
    字段能让工具**可靠地**在确认前停下来。injected 同样只信任工具自己
    返回的值，模型在 arguments 里伪造同名参数会被 validate() 拒掉。
    """

    args: dict[str, Any] = field(default_factory=dict)
    question: str = ""
    choices: list[dict[str, Any]] = field(default_factory=list)
    injected: dict[str, Any] = field(default_factory=dict)
    blocked: str = ""
    # blocked 的细分原因，会变成界面状态。空串 = 通用「未执行」。
    # 例如 "file_not_found" / "path_not_allowed" / "invalid_argument"。
    blocked_code: str = ""

    @property
    def needs_choice(self) -> bool:
        return bool(self.choices)


class Tool(ABC):
    """所有工具的父类。"""

    name: str = ""
    description: str = ""
    # 一句人话的工具名，用在授权卡片上，例如"打开应用"
    label: str = ""
    # JSON Schema，告诉模型这个工具要什么参数。留空表示不需要参数。
    parameters: dict[str, Any] = {}
    # 默认最安全：需要用户确认才能执行
    requires_confirmation: bool = True
    # True = prepare() 必须成功并固定目标。prepare 抛异常、没有返回有效
    # Resolution、或没有注入任何值时，闸门直接停止，不创建确认卡片。
    # 默认 False：现有工具的行为完全不变。
    requires_prepared_target: bool = False
    # 失败后工作流能不能自动重试。只读/可幂等的工具保持 True；
    # 将来接入的鼠标点击、键盘输入、按键这类**有副作用且不可重复**的工具
    # 必须设为 False，防止一次"超时"被引擎当成"可以再点一次"。
    # 这个标记只影响工作流重试，不能替代用户确认。
    retry_safe: bool = True

    # ------------------------------------------------------------------
    # 给模型看的格式
    # ------------------------------------------------------------------
    def openai_schema(self) -> dict[str, Any]:
        """转成工具调用协议要求的格式（OpenAI 那一套，DeepSeek 也认）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
                or {"type": "object", "properties": {}, "required": []},
            },
        }

    # ------------------------------------------------------------------
    # 参数校验
    # ------------------------------------------------------------------
    def validate(self, args: dict[str, Any]) -> dict[str, Any]:
        """检查模型给的参数。子类可以覆盖，但一定要先调用 super().validate(args)。"""
        if not isinstance(args, dict):
            raise ToolError("参数格式不对，应该是一个对象")

        properties = (self.parameters.get("properties") or {})
        unknown = set(args) - set(properties)
        if unknown:
            raise ToolError(
                f"不认识的参数：{'、'.join(sorted(unknown))}。"
                f"这个工具接受的参数是：{'、'.join(properties) or '（无）'}"
            )

        for key in self.parameters.get("required", []):
            if key not in args or args[key] in (None, ""):
                raise ToolError(f"缺少必要的参数：{key}")

        return args

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    @abstractmethod
    async def run(self, **kwargs: Any) -> ToolResult:
        """真正干活的地方。由子类实现。"""
        raise NotImplementedError

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        """执行前的可选钩子。默认"直接就绪"，不改变任何行为。

        需要"先请用户在几个候选里挑一个"的工具才覆盖它。
        注意这里拿到的是**模型给的原始参数**，还没过 validate()。
        """
        return Resolution(args=args)

    def check_injected(self, injected: dict[str, Any]) -> str:
        """检查 prepare() 注入的目标是否完整。返回空串 = 通过。

        默认不检查，现有工具行为不变。需要「确认前固定目标」的工具覆盖它；
        闸门会在创建确认卡片之前调用，所以不完整的目标连卡片都不会出现。
        """
        return ""

    async def execute(
        self, args: dict[str, Any], *, injected: dict[str, Any] | None = None
    ) -> ToolResult:
        """上层唯一允许调用的入口。

        它保证两件事：一是不管出什么事都不会抛异常出去，
        二是失败时返回的是真实原因，而不是编造的成功。

        injected 是**服务端**补进来的参数（目前只有用户在网页上选中的
        那个候选项编号）。它刻意放在 validate() **之后**才合并——
        因为 validate() 会把 schema 里没有的键判成非法参数，所以模型
        自己在 arguments 里塞 resolved_id 只会被拒掉。这两条路必须分开，
        否则"用户的选择"就退化成了模型可以自己填的普通参数。
        """
        try:
            clean = self.validate(dict(args or {}))
        except ToolError as exc:
            logger.warning("工具 %s 参数不合格：%s", self.name, exc)
            return ToolResult(
                ok=False, error=str(exc), status="invalid_argument"
            )
        except JiaAgentError as exc:
            # validate() 里也可能读取配置（比如文件读取的路径长度上限）。
            # 配置坏了要如实报错，绝不能让异常穿透 execute()。
            logger.warning("工具 %s 校验参数时发现配置问题：%s", self.name, exc.message)
            detail = f"（{exc.detail}）" if exc.detail else ""
            return ToolResult(
                ok=False,
                error=f"{exc.message}{detail}",
                status=getattr(exc, "code", "failed"),
            )

        if injected:
            clean.update(injected)

        try:
            return await self.run(**clean)
        except ToolError as exc:
            # 工具自己判断出的问题（比如"不认识这个应用"）属于正常拒绝，
            # 记一条警告就够了，没必要打印堆栈吓人。
            logger.warning("工具 %s 未能执行：%s", self.name, exc)
            return ToolResult(ok=False, error=str(exc), status="failed")
        except JiaAgentError as exc:
            # 我们自己定义的错误（路径不允许等），消息本身就是给人看的
            logger.warning("工具 %s 被拒绝：%s", self.name, exc.message)
            detail = f"（{exc.detail}）" if exc.detail else ""
            return ToolResult(
                ok=False,
                error=f"{exc.message}{detail}",
                status=getattr(exc, "code", "failed"),
            )
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不让异常穿透
            logger.exception("工具 %s 执行出错", self.name)
            return ToolResult(
                ok=False, error=f"{type(exc).__name__}: {exc}", status="failed"
            )

    def describe_action(self, args: dict[str, Any]) -> str:
        """用一句人话说明"这次到底要做什么"，会显示在授权卡片上。

        子类应该覆盖它，写成用户一眼能看懂的样子。
        """
        if not args:
            return self.label or self.name
        detail = "、".join(f"{key}={value}" for key, value in args.items())
        return f"{self.label or self.name}（{detail}）"

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "requires_confirmation": self.requires_confirmation,
            "parameters": sorted((self.parameters.get("properties") or {}).keys()),
        }


class ToolRegistry:
    """工具登记处。所有可用的工具都在这里，别处要工具就找它。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("工具必须要有 name")
        if tool.name in self._tools:
            raise ValueError(f"工具名重复了：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def openai_tools(self) -> list[dict[str, Any]]:
        """发给模型看的工具清单。"""
        return [tool.openai_schema() for tool in self._tools.values()]

    def describe(self) -> list[dict[str, Any]]:
        return [tool.describe() for tool in self._tools.values()]
