"""打开应用的工具（索引驱动）。

不再依赖 config/tools.yaml 里那 8 个手写的应用名，而是先在本地应用索引里
按名字找（中文、英文、简称、模糊都能对），找到唯一的一条后，启动它并核实。

安全边界（阶段二）：
  - 模型只能报应用名，给不出路径——工具 schema 里只有一个 app: string
  - 启动前 launcher 会再查一遍危险程序黑名单（闸门二，纵深防御）
  - 每次都要用户确认（requires_confirmation = True）
  - 启动全程不走 shell，参数用列表传
"""

from __future__ import annotations

import logging
from typing import Any

from app.apps import index, launcher, matcher
from app.apps.model import AppRecord
from app.tools.base import Resolution, Tool, ToolError, ToolResult

logger = logging.getLogger(__name__)


def _resolve(raw: str, resolved_id: str = "") -> AppRecord:
    """把名字对上索引里的记录。

    两条路：
      - 给了 resolved_id：说明用户已经在网页上从候选里挑过了。直接按编号取
        那条记录，**不再重新匹配**——否则用户挑的那个可能又被匹配成别的。
      - 没给：按名字匹配。唯一命中就返回；歧义或没找到都抛 ToolError。
    """
    if resolved_id:
        record = index.get(resolved_id)
        if record is None:
            raise ToolError("你选的那个应用已经不在索引里了，请重新说一次要打开什么。")
        return record

    text = (raw or "").strip()
    if not text:
        raise ToolError("没有说明要打开哪个应用")

    index.ensure_ready()   # 索引是空的就顺手建一次
    result = matcher.match(text, index.load_all())

    if result.status == "unique":
        assert result.best is not None
        return result.best.record

    if result.status == "ambiguous":
        names = "、".join(c.record.name for c in result.candidates[:5])
        raise ToolError(f"「{raw}」对上了好几个应用：{names}。请说得再具体一点。")

    raise ToolError(
        f"在索引里没找到「{raw}」这个应用。你可以换个说法，或者告诉我它的完整路径。"
    )


class OpenApplicationTool(Tool):
    name = "open_application"
    label = "打开应用"
    description = (
        "打开用户电脑上的一个已安装应用，例如记事本、计算器、浏览器、VS Code。"
        "拿不准应用叫什么名字时，先调用 search_apps 搜一下再打开。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "description": "要打开的应用名字，例如 notepad、记事本、calc、浏览器、vscode",
            }
        },
        "required": ["app"],
    }
    requires_confirmation = True

    async def prepare(self, args: dict[str, Any]) -> Resolution:
        """对上多个候选时，先请用户在网页上挑一个。

        唯一命中、或者压根没找到，都照常往下走——那两种情况没有"选"这回事，
        由 run() 去如实报错就行。
        """
        raw = str(args.get("app", "")).strip()
        if not raw:
            return Resolution(args=args)   # 缺参数，交给 validate() 报错

        index.ensure_ready()
        result = matcher.match(raw, index.load_all())
        if result.status != "ambiguous":
            return Resolution(args=args)

        return Resolution(
            question=f"「{raw}」对上了好几个应用，你要打开哪一个？",
            choices=[
                {
                    "id": c.record.id,
                    "name": c.record.name,
                    "source": c.record.source.label,
                    "target": c.record.display_target(),
                    "score": round(c.score, 3),
                    "reason": c.reason,
                }
                for c in result.candidates
            ],
        )

    def describe_action(self, args: dict[str, Any]) -> str:
        """授权卡片上显示解析出的真实身份：名字 + 真实路径。

        这是最后一道防线——你在点头前能看到它到底要启动哪个可执行文件。
        resolved_id 是服务端在用户选完之后补进来的，这里一并认它。
        """
        raw = str(args.get("app", "")).strip()
        resolved_id = str(args.get("resolved_id", "") or "")
        try:
            record = _resolve(raw, resolved_id)
        except ToolError:
            return f"打开应用：{raw or '（未指定）'}"
        return f"打开应用：{record.name}（{record.display_target()}）"

    async def run(self, app: str, resolved_id: str = "") -> ToolResult:
        record = _resolve(app, resolved_id)
        logger.info("准备打开应用：%s（来源 %s）", record.name, record.source.value)

        outcome = await launcher.launch(record)
        if outcome.ok:
            return ToolResult(ok=True, output=outcome.message)
        return ToolResult(ok=False, error=outcome.message)
