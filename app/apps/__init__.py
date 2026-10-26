"""应用发现子系统。

这个包负责"这台电脑上有哪些应用、怎么启动它们"，
是 app/tools/app_tools.py 里 open_application 工具背后的基础设施。

它本身不是一组工具，不直接面向模型。分层：

    model.py    数据结构（只定义"长什么样"）
    lnk.py      解析 .lnk 快捷方式（纯标准库）
    uwp.py      商店应用（UWP）的枚举与启动标识推导
    guard.py    安全守卫（危险程序黑名单、排除规则）
    sources.py  各个发现来源（开始菜单/注册表/安装目录/商店）
    index.py    索引的建立、保存、增量刷新
    matcher.py  名字匹配（中文/英文/简称/模糊）
"""
