"""输入后端。

这个包定义"怎么把一次已经校验过的点击交给后端"的抽象，并提供两种实现：

  - `FakeInputBackend`：只记录，不碰系统。测试和演练用。
  - `WindowsInputBackend`：真实 Windows 单次左键单击。默认关闭，必须
    同时满足配置 `input.real_mouse_enabled: true` 和环境变量
    `JIA_ENABLE_REAL_MOUSE=1` 才会被构造。

真实输入还必须先过 `app.vision.observation` 的坐标校验、`app.agent.gate`
的用户逐次确认，以及 `app.input.control` 的急停和速率限制。后端只负责
最后那一下，并如实报告 succeeded / failed / unknown。
"""

from app.input.backend import (
    CLICK_FAILED,
    CLICK_SUCCEEDED,
    CLICK_UNKNOWN,
    BackendStatus,
    ClickOutcome,
    FocusResult,
    InputBackend,
    identity_problem,
    unsafe_target_reason,
)
from app.input.control import (
    ClickRateLimiter,
    EmergencyStop,
    RealInputController,
    emergency,
)
from app.input.fake import FakeInputBackend

__all__ = [
    "CLICK_FAILED",
    "CLICK_SUCCEEDED",
    "CLICK_UNKNOWN",
    "BackendStatus",
    "ClickOutcome",
    "FocusResult",
    "InputBackend",
    "FakeInputBackend",
    "ClickRateLimiter",
    "EmergencyStop",
    "RealInputController",
    "emergency",
    "identity_problem",
    "unsafe_target_reason",
]
