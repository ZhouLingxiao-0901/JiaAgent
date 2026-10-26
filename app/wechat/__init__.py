"""Jia-WeChat：把微信接成 Jia 的一个入口，而不是第二套 Agent。

边界（和 docs/wechat.md 一致）：

  * 这里只负责登录/连接、收消息、发送者鉴权、把消息交给 Jia 现有的聊天与
    任务入口、把任务状态和授权请求转成微信消息。
  * 任务规划、模型选择、工具调用、文件访问仍然走 app/agent 和 app/workflow。
  * 微信模块不能直接调用工具、不能执行 shell/Python、不能绕过 gate.py。
  * 所有网络操作都通过可注入的 Transport；默认实现只在上线运行时使用，
    测试和离线检查一律注入假传输。

第一版只接受已配对账号的一对一文字消息。图片、语音、文件、群聊、
远程批准聊天里的工具调用都不在范围内。
"""

from __future__ import annotations

__all__ = ["protocol", "api", "credentials", "store", "service"]
