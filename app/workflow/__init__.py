"""自主工作流引擎。

让佳能接一句自然语言指令，先拆成有序步骤给用户过目，点头后在后台逐步执行。

这个包**自己不碰工具的执行细节**：要执行工具时，它调用 app/agent/gate.py 里
和聊天循环同一份的确认闸门——这样才不会出现「引擎那条路忘了让用户确认」这种
绕过授权的漏洞。

分层（从下往上）：
    model.py     纯数据：任务、步骤、状态机。不碰磁盘、不调模型
    store.py     持久化：建表 + 增删改查。DDL 内联在本包，测试可指向临时库
    planner.py   问模型要计划、解析 JSON、每步之后的决策、终局校验
    engine.py    后台执行循环：暂停/取消检查、重试、再规划、求助、总结
    manager.py   进程级后台任务管理：起停、控制信号、启动时对账
    trigger.py   判断一句话要不要进工作流

依赖方向是单向的：workflow -> tools / llm / memory / agent.gate。
反过来 import（比如在 app/tools 里 import workflow）会成环，不要那样做。
"""

from __future__ import annotations

__all__ = ["model", "store"]
