"""工具登记处：所有能用的工具都在这里注册。

以后加新工具，只需要两步：
  1. 写一个新的 Tool 子类
  2. 在这里 register 一次
模型适配器和网页界面都不用改。

注意 requires_confirmation：
  get_system_info / list_directory  只读，False，不用授权
  open_application / create_text_file  会动电脑，True，必须授权
"""

from __future__ import annotations

from app.input.windows_backend import build_input_backend
from app.tools.app_discovery_tools import ListIndexedAppsTool, SearchAppsTool
from app.tools.app_tools import OpenApplicationTool
from app.tools.base import Tool, ToolRegistry
from app.tools.cc_tools import PrepareCcHandoffTool, ValidateCcResultTool
from app.tools.click_tools import ClickElementTool
from app.tools.file_tools import CreateTextFileTool
from app.tools.read_tools import ReadTextFileTool, SearchTextFilesTool
from app.tools.system_tools import GetSystemInfoTool, ListDirectoryTool
from app.tools.vision_tools import InspectScreenTool

registry = ToolRegistry()

registry.register(GetSystemInfoTool())
registry.register(ListDirectoryTool())
registry.register(OpenApplicationTool())
registry.register(CreateTextFileTool())

# 只读文件理解：正文会交给模型，所以每次都要确认，并且由 prepare 固定
# 真实路径/指纹。权限只认 config/tools.yaml 的 filesystem.allowed_roots；
# 留空时这两个工具会失败关闭，不会回退到 permissions。
registry.register(ReadTextFileTool())
registry.register(SearchTextFilesTool())

# Claude Code 人工交接：只准备受限只读快照、验证用户放回的结果。
# 不启动 CC、不调用 CLI、不执行 shell。两个工具都逐次确认、永不自动重试；
# cc.enabled 默认 false，cc.allowed_roots 默认空，失败关闭。
registry.register(PrepareCcHandoffTool())
registry.register(ValidateCcResultTool())

# 应用发现：只读，不用授权
registry.register(ListIndexedAppsTool())
registry.register(SearchAppsTool())

# 屏幕感知：只读，但画面会离开这台电脑，所以**每次都要求你确认**。
# 这个工具的 requires_confirmation 是硬编码 True，没有配置开关能关掉。
registry.register(InspectScreenTool())

# 单次点击：只接受 observation_id + element_id，逐次确认，永不自动重试。
# 真实后端只有在**配置 + 环境变量**同时满足时才会被构造；否则这里是
# None，工具在 prepare 阶段就失败关闭，不会弹确认卡片，更不会"退回"
# 另一种输入方式。测试通过构造函数注入假后端来跑完整链路。
registry.register(ClickElementTool(backend=build_input_backend()))


def all_tools() -> list[Tool]:
    return registry.all()
