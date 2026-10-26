# Windows 登录后自动启动

本文说明如何让 Jia 在**当前用户登录 Windows 后**自动启动，包括可选的
微信监听。默认使用任务计划程序（Task Scheduler），不要求管理员权限，
不以最高权限运行。

## 1. 设计原则

- **当前用户、交互式会话**：任务以 `LogonType Interactive` 运行，和手动
  打开终端启动是同一个用户上下文。这是 Windows DPAPI（CurrentUser）
  解密微信凭证、以及截图/鼠标等桌面能力所必需的。
- **有限权限**：`RunLevel Limited`，脚本里没有任何 `-Verb RunAs`，也不
  修改系统级配置。
- **只用项目虚拟环境**：启动器直接调用
  `.venv\Scripts\python.exe`，不依赖 `activate`，也不改全局 PATH。
- **单实例，失败即拒绝启动**：`run.py` 在进入 uvicorn 之前获取本会话
  命名互斥体（`Local\JiaAgent-<项目路径哈希>`）。第二个实例打印“已经在
  运行”并以退出码 0 结束。若锁机制本身出错，或 PID 状态文件无法可靠
  写入，`run.py` **拒绝启动**并返回非 0，不会只打印警告后继续——固定
  端口不能代替进程级互斥，没有可核对的 PID 状态也无法安全停止。
- **只清理自己的状态**：启动失败时不会删除可能属于其他实例或上一次运行
  的 PID 状态；只有本次启动成功写下的状态才会在退出时按 PID 清理。
- **先优雅关闭，再只强制结束主进程**：停止脚本先设置本机命名事件
  （`Local\JiaAgent-Shutdown-<项目哈希>`），让 uvicorn 走正常 lifespan
  清理，等待 15 秒；仍未退出时，才在**重新核对身份**后结束这一个已核实
  的主进程。没有 `taskkill /T`，没有父子进程树遍历。
- **不误伤用户应用**：Jia 通过 `app/apps/launcher.py` 启动的应用是独立
  进程。停止 Jia 不会连带结束 Claude Code、编辑器或用户打开的其他程序。
- **日志默认脱敏**：启动器把 Python 的 stdout/stderr 交给
  `scripts/run_jia_redacted.py`，按行移除 token、API key、验证码、授权码、
  二维码 URL 等敏感值，再写入 `logs/autostart.log`。
- **健康检查有明确契约**：只有 HTTP 200 且响应体是 Jia 的
  `{"status":"ok","name":"JiaAgent","version":"x.y.z"}` 才算健康；任何
  本地 JSON 都不会被当成 Jia。`JIA_HOST` 只允许回环地址，IPv4/IPv6
  都按正确格式探测。
- **微信默认不授权**：启动器只可能设置 `JIA_WECHAT_ENABLED`。它**不会**
  调用 `wechat_cli.py`，不会写允许名单，不会给任何人加 `task` 或
  `approve`。权限仍然只由你显式执行过的 `allow` 决定。

## 2. 相关文件

| 文件 | 作用 |
|---|---|
| `scripts/windows/start_jia.ps1` | 启动器：定位项目根目录、检查虚拟环境、可选启用微信、写日志 |
| `scripts/run_jia_redacted.py` | 启动 `run.py` 并逐行脱敏 stdout/stderr；`run.py` 仍是 PID 记录的进程 |
| `scripts/windows/autostart_common.ps1` | 安装/卸载共用的任务与快捷方式所有权检查；只读判断，不直接改资源 |
| `scripts/windows/install_autostart.ps1` | 注册“登录时启动”任务；可选 Startup 文件夹回退；写入前核对所有权 |
| `scripts/windows/uninstall_autostart.ps1` | 只删除已核实属于本项目的任务/快捷方式；外部或不可读资源拒绝删除 |
| `scripts/windows/jia_common.ps1` | 状态/停止脚本共享的回环地址、健康检查和进程身份校验 |
| `scripts/windows/status_jia.ps1` | 只读状态：任务、PID、严格健康检查、微信运行状态 |
| `scripts/windows/stop_jia.ps1` | 命名事件优雅关闭；必要时只强制结束已核实的主进程 |
| `scripts/windows/test_windows_scripts.ps1` | 脚本自身的离线安全回归测试 |
| `run.py` | 单实例锁 + PID 状态 + uvicorn 启动 |
| `app/single_instance.py` | 命名互斥体与 JSON PID 状态实现 |
| `app/shutdown_channel.py` | 本机命名关闭事件 |
| `app/log_redaction.py` | 启动日志脱敏规则 |

运行时文件（都在已被 `.gitignore` 排除的目录里）：

- `data/jiaagent.pid`：JSON 状态，包含 `pid`、`started_at`、`host`、
  `port`，以及可用时的 `shutdown_event`。**不包含任何凭证、token 或
  命令行**。`started_at` 用于防止 PID 复用。
- `logs/autostart.log`：启动器日志，5 MB 轮转，保留一份 `.1`。
- `logs/jiaagent.log`：应用日志，5 MB × 4。
- `data/wechat/runtime-status.json`：微信监听状态，不含凭证或消息正文。

## 3. 安装

在**普通（非管理员）PowerShell**里执行，路径按你的项目位置调整：

```powershell
cd D:\JiaAgent
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
```

先加 `-WhatIf` 可以只打印将要执行的命令和触发器，不创建任何东西：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -WhatIf -EnableWeChat
```

确认无误后去掉 `-WhatIf` 再执行。安装和卸载都会先核实系统自带的
Windows PowerShell 5.1 可执行文件
（`%Windows%\System32\WindowsPowerShell\v1.0\powershell.exe`）确实存在；
找不到时明确报错并退出 2，不会创建或删除指向无效路径的任务/快捷方式。
它会注册一个名为 `JiaAgent` 的任务：

- 触发器：当前用户登录时；
- 操作：隐藏窗口运行 `start_jia.ps1`；
- 主体：当前用户，交互式登录，有限权限；
- 已在运行时不再启动第二个实例（`MultipleInstances IgnoreNew`）；
- 不限制运行时长（`ExecutionTimeLimit = 0`）；
- **不配置失败自动重启**。原因是停止脚本主动结束进程时，计划任务会把
  它看成“失败”，自动重启会违背你的停止意图。失败只记录日志和状态，
  等下一条登录触发或手动启动。

### 3.1 同时启动微信监听

微信入口默认关闭。需要登录后自动监听时，安装时加参数：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -EnableWeChat
```

`-EnableWeChat` 只做一件事：给这次启动的进程设置
`JIA_WECHAT_ENABLED=1`。它不会：

- 登录微信或扫描二维码；
- 修改允许名单；
- 给用户增加 `task`、`approve`；
- 解除任何已有的暂停或平台限制。

也可以在 `.env` 里写 `JIA_WECHAT_ENABLED=1`。微信配置现在会读取
`.env` 中的 `JIA_WECHAT_*`，并且**进程环境变量优先**。两种方式选一种即可。

> 权限提醒：默认配对只有 `chat`。需要远程任务或审批时，仍要你手动执行
> `wechat_cli.py allow --scope ...`。自动启动不会改变这一点。

### 3.2 如果策略禁止创建计划任务

某些机器/域策略不允许普通用户创建计划任务。安装脚本会给出提示，
这时可以用当前用户的启动文件夹（同样不需要管理员）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -UseStartupFolder -EnableWeChat
```

它只会在 `shell:startup` 放一个 `JiaAgent.lnk`，内容与计划任务相同。
两种方式不要同时启用（`install_autostart.ps1` 只会在同名任务/快捷方式
**可核实属于本项目**时更新，外部或不可读的同名资源会拒绝覆盖并退出 1；
重复启动仍会被单实例锁挡住）。

## 4. 手动验证（不需要重启）

按顺序做，全部可以在一台机器上完成：

```powershell
cd D:\JiaAgent

# 1) 前台启动，看到 Banner 后说明启动成功
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\start_jia.ps1 -EnableWeChat

# 2) 另开一个 PowerShell，再执行一次：应打印“已经在运行”，退出码 0
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\start_jia.ps1

# 3) 查看状态（任务、PID、健康检查、微信监听）；健康时退出码 0
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\status_jia.ps1

# 4) 浏览器或命令检查
curl.exe --noproxy "*" http://127.0.0.1:8000/api/health

# 5) 停止（先优雅关闭，必要时只强制结束核实过的主进程）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\stop_jia.ps1
```

脚本自身的离线回归测试（不启动 Jia、不结束真实进程）：

```powershell
# 155 项 PowerShell 断言：健康契约、健康探针失败与真实不可达的分类、
# IPv4/IPv6、进程身份、PID 状态分类、缺失 PID 时的健康检查/进程扫描决策、
# 临时副本上的停止脚本退出码、事件、启动命令的 stdout/stderr 合并与退出码
# 传递、任务/快捷方式所有权检查、Windows PowerShell 5.1 固定路径解析，
# 以及“停止脚本没有 taskkill / 进程树”的静态检查
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\test_windows_scripts.ps1

# Python 侧：单实例、PID 原子写入、启动失败收尾、脱敏、事件、脚本静态检查
.venv\Scripts\python.exe scripts\test_autostart.py
```

`status_jia.ps1` 会显示：

- 计划任务是否安装、上次运行时间和结果码；
- `data/jiaagent.pid` 对应进程是否还活着，以及身份核验结果：
  - 可执行文件必须是本项目 `.venv\Scripts\python.exe`；
  - 命令行必须包含完整绝对路径 `...\run.py`（不是 `run.py` 子串）；
  - JSON 状态里的启动时间必须与进程启动时间一致（±5 秒）；
  - 旧版整数 PID 文件或缺少启动时间时，只显示“身份未完全核验”，
    停止脚本会拒绝结束它。
- `/api/health` 是否是**Jia 自己的**健康响应，并打印 HTTP 状态码和实际
  的 `name/status/version`；
- 微信监听状态（`running / paused / reconnecting / stopped`、重启次数、
  最近错误）。状态文件里记录的 PID 已退出时，会明确显示 `stale`，
  不会把上一次运行的残留文件当成正在监听。

`status_jia.ps1` 在健康检查通过时退出 0，否则退出 1，便于脚本监控。

日志位置：

```powershell
Get-Content D:\JiaAgent\logs\autostart.log -Tail 50
Get-Content D:\JiaAgent\logs\jiaagent.log -Tail 50
```

安装完成后，注销并重新登录一次，确认任务真的被触发：

```powershell
# 登录后
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\status_jia.ps1
Get-ScheduledTaskInfo -TaskName JiaAgent
```

也可以在“任务计划程序”图形界面里找到 `JiaAgent`，右键“运行”做一次
手动触发（不会要求管理员）。

## 5. 退出方式与状态一致性

| 场景 | 做法 | 结果 |
|---|---|---|
| 自动启动的实例 | `stop_jia.ps1` | 设置命名事件 → 等待 15 秒优雅清理 → 必要时只强制结束已核实的主进程 |
| 前台手动启动 | 在那个窗口按 `Ctrl+C` | uvicorn 正常退出，lifespan 清理微信与窗口追踪 |
| 注销/关机 | Windows 注销 | 交互式任务随会话结束；Jia 自己的线程和后台任务一并消失 |
| 需要立即结束 | `stop_jia.ps1 -Force` | **跳过 15 秒等待**，但仍然先核对身份，只结束那一个主进程 |

`stop_jia.ps1` 的顺序：

1. 读取 JSON PID 状态（旧版整数文件没有启动时间，拒绝强制结束）；
2. 核对 PID 对应的可执行文件、绝对 `run.py` 参数、启动时间；
3. 打开并 `Set()` 本机关闭事件；uvicorn 把 `should_exit` 置位，走正常
   lifespan 清理；
4. 15 秒内退出：删除**属于本次实例**的 PID 状态，退出 0；
5. 仍未退出：再次核对身份，然后 `Stop-Process -Id <已核实 PID> -Force`，
   最多再等 10 秒；
6. 主进程退出后删除属于它的 PID 状态并退出 0；否则退出 1，保留状态文件
   供排查。

关键点：

- **没有 `taskkill`，没有 `/T`，没有父子进程树遍历。** 由 Jia 启动的
  Claude Code、编辑器或其他用户应用不会被连带结束。
- **不要用任务计划程序的“结束”按钮来停止 Jia。** 那会按任务的进程树
  结束，可能连带结束 Jia 启动的应用；请使用 `stop_jia.ps1`，它只处理
  已核实的主进程。
- 如果 PID 已被系统复用给别的进程，启动时间核对会失败，停止脚本拒绝
  结束它。
- 如果进程仍在运行、但身份无法核验（PID 复用、CIM/权限错误），停止
  脚本同样拒绝结束，并保留 PID 状态文件供排查，不会假装已经停止。
- 如果 PID 状态文件**能读出 PID**，但指向不匹配的进程，而该 PID 仍存活，
  停止脚本会拒绝结束并退出 1；如果进程已经不在，则按合法状态清理并退出 0。
- 如果 PID 状态文件**为空、JSON 损坏、PID/启动时间/端口非法或无法读取**，
  停止脚本不会把它当成“没有运行”。它会打印具体原因（corrupt / unreadable），
  拒绝猜测 PID、拒绝结束任何进程，并退出 1；损坏的状态文件**不会被自动删除**。
- 如果 PID 状态文件**不存在**，停止脚本也不会仅凭这一点退出 0：
  1. 先对回环地址执行严格健康检查（必须返回 Jia 的 200 响应）；
  2. 健康端点仍可达：报告“服务可能仍在运行，但没有可信 PID”，退出 1；
  3. 否则只读扫描 `python.exe`，核对本项目虚拟环境路径和绝对 `run.py` 参数；
  4. 找到匹配进程、扫描失败或进程信息不可读，都退出 1；
  5. 只有扫描完整完成、没有匹配进程，并且健康检查确实执行过且不可达时，
     才退出 0。
- 如果缺失 PID 时健康检查因 `.env` 配置非法或 `curl.exe` 缺失而**无法执行**，
  即使进程扫描没有发现匹配项，也会退出 1，而不是假设已经停止。
- 仍无法确认时，请用 `status_jia.ps1` 和 `/api/health` 核对，必要时在任务
  管理器里检查 `.venv\Scripts\python.exe` 与 `run.py` 的完整命令行后再手动
  处理。停止脚本不会递归结束进程树，也不会扩大身份校验范围。
- 强制结束仍可能跳过部分 lifespan 清理；下次启动时工作流对账
  （`reconcile_on_startup`）会把残留的“执行中/等待确认”标成失败，不会
  自动续跑。能优雅关闭时优先用不带 `-Force` 的方式。
- 手动启动时如果命令行是相对路径 `run.py`，身份校验会把它视为不可核实，
  `stop_jia.ps1` 不会结束它；请在那个窗口按 `Ctrl+C`。自动启动使用的
  是绝对路径，不受影响。

## 6. 卸载自动启动

```powershell
cd D:\JiaAgent
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1
```

卸载前会读取同名任务/快捷方式的实际目标、精确参数和工作目录，只有能确认
属于本项目时才删除。以下情况会**拒绝删除并退出 1**，资源保持原样：

- 同名任务或快捷方式指向其他程序；
- 任务有多个动作、不是可执行动作，或参数/工作目录与本项目不一致；
- 查询任务失败，或读取快捷方式属性失败（无法核实即按外部资源处理）。

不存在同名资源时是正常空操作（退出 0），不会枚举或删除其他任务/文件夹。

只删除已核实属于本项目的 `JiaAgent` 计划任务和 `JiaAgent.lnk`，**不会**删除：

- `data/`（微信凭证、状态库、工作流数据库）；
- `logs/`；
- `config/tools.yaml`；
- 任何其他任务或用户的启动项。

如果还想顺便停掉正在运行的实例：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1 -StopRunning
```

## 7. 安全与隐私

- 脚本里没有 token、二维码、`context_token`、用户消息或账号凭证。
- `data/jiaagent.pid` 只包含 PID、启动时间、回环地址/端口和事件名。
- **两条日志链不是同一层保护：**
  - `logs/autostart.log` 保存 `run_jia_redacted.py` 捕获的 Python/uvicorn
    stdout/stderr，写入前经过 `app/log_redaction.py`。它会替换 Bearer/Basic
    凭据、`sk-` API key、`key=value`/`key: value`（大小写不限，支持
    `access_token`、`refresh_token`、`bot_token`、`client_secret`、
    `api_key`、`secret`、`password`、`token` 等标签，以及
    `JIA_WECHAT_BOT_TOKEN`、`JIA_CLIENT_SECRET` 这类带分段前缀的键名）、
    JSON 形式的 `"key": "value"`、URL 查询参数里的 `code`/`token`、
    中文“验证码/授权码/二维码/令牌/密钥/密码/凭证”，以及微信登录二维码
    URL。替换只处理敏感值，键名和同一行的其他诊断字段保留。
  - `logs/jiaagent.log` 由 `app/logging_setup.py` 的
    `RotatingFileHandler` 直接写入，**不经过** `redact_log_line`。目前只
    依赖各模块在调用日志前主动脱敏（例如 `app/wechat/redact.py`）；新增
    代码若直接把原始异常或配置值写进 `logger`，文件里仍可能是明文。
- **应用日志的直接凭证检查（本轮静态审查）：** 在 `app/` 下的
  `logger.*` 调用中，只发现 `app/api/input.py:160` 记录恢复挑战时使用了
  `session.token[:8]`（输入会话令牌的前 8 位，不是完整令牌，不能直接用于
  认证）。没有发现把完整 bot token、context token、API key 或密码直接交给
  `logger` 的调用。该截断值仍属于凭证派生信息，是否改为哈希/长度需要单独
  评估。第三方库异常、请求 URL 或未审计的新代码仍可能把原文带进
  `jiaagent.log`；这不是穷尽性证明。
- **为什么没有给全部应用日志加强制正则过滤：** 根 handler 的
  `logging.Filter` 会同时作用于异常堆栈、结构化字段和多行消息，简单的
  按行正则会误伤诊断信息，也无法可靠处理跨行密钥。若要加，最小方案是给
  `logging_setup.py` 的文件 handler 增加一个只做“单行值替换、失败时隐藏
  该行”的过滤器，并单独评估对 traceback 和多行日志的影响；本轮没有做这个
  架构改动。
- **当前脱敏规则仍有的缺口：**
  - 未列入标签的自定义键名（例如 `my_custom_credential=...`）；
  - 不带可识别键名的裸密钥；
  - 未加引号且包含空格的值（只会替换到第一个空白，剩余部分可能保留）；
  - JSON 转义、跨行拆分、非 UTF-8 字节或特殊编码的内容。
  不要把 `autostart.log` 当作完整密钥保险。
- 脱敏是**尽力而为**，不是密码学边界：同一用户下的其他进程仍然能读取
  环境变量和配置文件；极端的多行/编码格式可能无法识别。不要依赖它来
  保护高价值密钥。
- `.env` 仍然只在本地，已被 `.gitignore` 排除。
- DPAPI 凭证保存在当前 Windows 用户下；换用户或换机器无法解密是预期行为。
- 自动启动**不会**让 Jia 监听局域网或公网：`run.py` 仍然拒绝非回环
  `JIA_HOST`；状态脚本也只对回环地址做健康检查。
- 微信监听仍受平台限制：`-14` 会暂停 1 小时，监督任务到期后才恢复。

## 8. 已知限制与仍需人工完成的配置

1. **安装脚本本身需要你手动运行一次**（本轮没有替你注册任务）。
2. 如果域策略禁止创建计划任务，改用 `-UseStartupFolder`。
3. 登录触发只在**用户登录后**生效；不会替用户开机、解锁或登录 Windows。
4. 没有自动重启：启动失败后需要看 `logs/autostart.log` 修复原因，再手动
   启动或重新登录。`status_jia.ps1` 会显示任务上次运行结果码。
5. 强制结束（包括优雅等待超时后）可能跳过部分清理；见第 5 节。
6. 无法保证“关闭”时恰好没有正在执行的工具；取消语义仍遵循
   `docs/wechat.md` 的说明——逻辑取消不等于底层同步操作已停止。
7. 身份校验依赖 Windows 的 `Get-Process`/CIM 能读取启动时间和命令行；
   读不到时停止脚本按失败处理，不会猜测。
8. 健康检查使用 Windows 自带的 `curl.exe`（Windows 10 1803+ 默认提供）。
   若 `curl.exe` 缺失，状态脚本会报告健康检查失败的原因。
9. 单实例锁和关闭事件都是 `Local\`（按登录会话）。同一项目只应在一个
   交互式会话中运行；若同一用户同时用控制台和 RDP 打开同一个项目，
   端口只能让一个实例监听，但并发启动时两个会话仍可能争用同一个 PID
   状态文件。当前没有实现跨会话锁。
