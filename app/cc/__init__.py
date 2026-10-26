"""佳 Agent 与 Claude Code 的第一阶段协作：人工交接。

本包只做三件事：
  1. 生成版本化的只读任务描述（task.json）；
  2. 在用户确认后，把明确授权的文件复制成受限快照；
  3. 严格校验用户手工放回的 result.json。

它**不启动 CC、不调用 CLI、不执行 shell、不操作桌面、不修改原始项目文件**。
"""

from app.cc.protocol import (  # noqa: F401
    RESULT_STATUSES,
    RESULT_SCHEMA_VERSION,
    TASK_SCHEMA_VERSION,
    CcValidationError,
    ManifestEntry,
    ResultDocument,
    TaskDocument,
)
