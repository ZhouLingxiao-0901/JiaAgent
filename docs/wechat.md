# Jia-WeChat

微信是 Jia 的**入口**，不是第二个助手。

- 聊天：微信消息交给现有的 `Agent.run_turn()`，回复在原会话返回。
- 任务：`/task` 交给现有的 `workflow_service.create_and_start()`；拆计划、选模型、
  调工具、读文件、执行任务仍然由 Jia 现有体系完成。
- 授权：计划批准、工具确认、选择仍然落到现有的 `manager.approve()`、
  `pending.store.answer()/choose()`。工具能不能执行，仍由 `app/agent/gate.py` 决定。
- 微信模块**没有**调用 `tool.execute()`，没有 subprocess，没有鼠标键盘。

当前状态：离线实现与离线测试完成；已用测试账号完成扫码登录、文字收发和只读任务
验收。文件修改、命令执行、鼠标/键盘操作尚未验收。
未验证项在文末列出，不把“代码写完”当成“真实接入完成”。

---

## 1. 架构

```
微信服务端
   │  getUpdates 长轮询
   ▼
WeChatMonitor ──► WeChatService ──► JiaRuntimeBridge ──► Agent.run_turn / workflow
   │                │                     │
   │                ├─ 严格解析            ├─ manager.approve / pause / resume / cancel
   │                ├─ 允许名单            ├─ pending.store.answer / choose
   │                ├─ 原子去重            └─ workflow.store 只读状态
   │                └─ 速率限制
   │
   └─ 游标 / 暂停状态（SQLite: data/wechat/state.db）

TaskWatcher ──► WeChatService.notify ──► sendMessage
   └─ 读 workflow store；计划/确认/状态/终态各只通知一次
```

所有网络请求都经过 `app/wechat/transport.py` 的 `Transport` 接口。默认是标准库
`urllib.request`，测试和本地调试可以注入假实现；**构造对象本身不会联网**。

---

## 2. 协议调查结果

依据（2026-10-24 阅读）：

- `https://github.com/Tencent/openclaw-weixin`
  - `README.zh_CN.md`
  - `docs/protocol.md`、`docs/protocol_zh_CN.md`
  - `src/api/api.ts`、`src/api/types.ts`
  - `src/auth/login-qr.ts`
  - `src/messaging/inbound.ts`、`src/messaging/send.ts`

该仓库是 **OpenClaw 插件**，不是可直接被 Python 项目加载的独立服务，也不是 Jia
的插件。公开文档明确说明它不是完整服务端契约，因此本项目只实现已经核实、且
第一版真正需要的接口；没有安装 OpenClaw，也没有把它的运行时代码引入 Jia。

### 2.1 已知接口

| 用途 | 方法 / 路径 | 说明 |
| --- | --- | --- |
| 获取登录二维码 | `POST https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3` | 固定登录地址 |
| 轮询扫码状态 | `GET .../ilink/bot/get_qrcode_status?qrcode=...&verify_code=...` | 固定登录地址或 `redirect_host` |
| 拉消息 | `POST {baseurl}/ilink/bot/getupdates` | 长轮询；请求体含 `get_updates_buf` 和 `base_info` |
| 发文字 | `POST {baseurl}/ilink/bot/sendmessage` | 请求体 `{"msg": {...}}` |
| 读取输入状态票据 | `POST {baseurl}/ilink/bot/getconfig` | 返回 `typing_ticket` |
| 发送“正在输入” | `POST {baseurl}/ilink/bot/sendtyping` | 第一版未启用 |
| 上下线通知 | `POST {baseurl}/ilink/bot/msg/notifystart` / `notifystop` | 尽力而为 |

认证头：

```
AuthorizationType: ilink_bot_token
Authorization: Bearer <bot_token>
X-WECHAT-UIN: base64(随机 uint32 的十进制字符串)
iLink-App-Id: bot
iLink-App-ClientVersion: 0x00MMNNPP 的十进制
SKRouteTag: <可选>
```

二维码请求只带 `AuthorizationType` 和 `X-WECHAT-UIN`，不带 Bearer；二维码状态
轮询只带公共头。

### 2.2 二维码状态

已处理：`wait`、`scaned`、`confirmed`、`expired`、`need_verifycode`、
`verify_code_blocked`、`scaned_but_redirect`、`binded_redirect`。

- 二维码 5 分钟过期，最多刷新 3 次。
- `scaned_but_redirect` 只接受 `redirect_host` 仍在 `weixin.qq.com` 域内。
- `binded_redirect` 表示账号已绑定到别处，**不接管**。
- `confirmed` 必须同时拿到 `bot_token` 和 `ilink_bot_id` 才保存；
  `baseurl` 缺失时回退到固定登录地址（这是实现假设，真实账号需验证）。
- 验证码最多尝试 3 次；验证码、二维码、token 都不写日志。

### 2.3 消息

入站 `msgs[]` 关键字段：`message_id`、`seq`、`from_user_id`、`to_user_id`、
`group_id`、`message_type`、`message_state`、`item_list`、`context_token`。

第一版**只接受**：

- 单聊（`group_id` 为空）；
- 用户消息（`message_type` 为 1 或未提供；机器人消息拒绝）；
- 已完成消息（`message_state` 为 0/2 或未提供）；
- 恰好一个 `type=1` 的文字 item。

群聊、图片、语音、文件、视频、多 item、控制字符、超过 8000 字、空文本、孤立
代理项一律拒绝，不进入模型。

发送文字时使用 `message_type=2`、`message_state=2`、单个 `text_item`，并回传入站
`context_token`（如果内存里还保留着）。回复按 1800 字分片，最多 4 片。

---

## 3. 安全边界

### 3.1 谁能用

- **允许名单**（`data/wechat/allowlist.json`）按 `(account_id, user_id)` 精确匹配，
  不按昵称。没有“扫码者自动授权”的回退逻辑。
- 每个条目有作用域：
  - `chat`：普通聊天、`/status`、`/tasks`；
  - `task`：`/task`、`/pause`、`/resume`、`/cancel`，以及接收任务通知；
  - `approve`：`/approve`、`/reject`、`/choose`，以及接收一次性授权码。
- **默认只给 `chat`**。需要派任务或远程审批时必须由管理员显式追加；
  非法、未知或空作用域整次拒绝，不会静默过滤。
- 任务通知和授权码在统一出口再次检查当前允许名单：没有 `task` 不发任务内容，
  没有 `approve` 不发新授权码。取消配对后不会继续发送，也不会因为发送被拒而
  无限重试。
- 未授权消息在允许名单这一步结束：不回复、不进入 Agent、不进入工作流、不调工具。
- 第一版不处理群聊。即使群里 @ 了机器人，也会在解析阶段丢弃。
- 允许名单或凭证文件损坏时**失败关闭**（按空名单/拒绝启动处理）。

### 3.2 微信端授权不等于绕过工具闸门

工作流等确认时，微信收到的是**一次性授权码**：

- 绑定 `account_id + user_id + task_id + action_type + action_id`；
- 默认 10 分钟有效；
- 只存 HMAC-SHA256 指纹，不存明文；
- 消费时必须同时匹配账号、用户、任务、动作类型和动作编号，任何一项不符都失败；
- 消费是原子的，第二次一定失败；两个并发请求同时消费同一个码，也只有一次成功；
- 过期码不能消费；同一绑定重新发码会让旧码立即失效；
- 消费后只调用 `manager.approve()` 或 `pending.store.answer()/choose()`；
- 真正的工具执行仍然要经过 `gate.authorize_tool()`，原有确认机制继续生效。

普通聊天**禁用工具**。即使模型在聊天回合里产出 `need_confirm`，桥接层会按拒绝
处理并报错，不会把聊天变成绕过工作流的工具入口。

### 3.3 凭证与日志

- 凭证用 Windows DPAPI（当前用户）加密，保存在 `data/wechat/credentials/<账号>/account.bin`；
  DPAPI 不可用时拒绝启动，绝不写明文。
- 授权码 HMAC 密钥同样用 DPAPI 保护。
- 写文件使用临时文件 + `os.replace`，权限 0600；拒绝符号链接/目录联接，
  并检查从目标到状态目录之间的每一级目录。
- 二维码内容打印到终端前会再次校验：必须是 HTTPS URL，只允许可打印 ASCII，
  拒绝 ANSI 转义/控制字符、空白、用户信息、IP 主机和异常端口。校验失败时
  登录安全停止，不打印原始异常内容。真实服务端若返回非 HTTPS 内容会直接失败，
  这是有意的失败关闭。
- 日志不记录：bot token、context_token、二维码内容、验证码、授权码明文、
  消息正文、完整用户 ID（只记 SHA-256 前 10 位）。需要把日志里的哈希和某个
  候选 ID 对上时，只能显式运行本机诊断命令 `sender-hash`。
- `data/` 已被 `.gitignore` 覆盖；API Key 仍只从环境变量/.env 读取。

### 3.4 消息 at-most-once

收到消息后先 `INSERT OR IGNORE` 原子 claim，再处理：

- 同一 `message_id` 重复投递只执行一次；
- 没有 `message_id` 时退化为 `seq`，再没有时用内容指纹；
- 进程在处理中途崩溃时，这条消息**不会**在重启后自动重放；
- 长轮询游标只在整页处理完后保存；没处理完的一页不前进游标，靠 claim 去重。

这是“宁可少回一次，也不重复执行任务”的选择。任务本身的状态由 workflow store
持久化，重启后的规则见下一节。

### 3.5 断线、监督与重启

- 长轮询超时是正常控制流，不算故障。
- 网络/协议错误退避：连续 1–2 次 2 秒，连续 ≥3 次 30 秒；持续失败时日志
  限流，不会每轮刷屏。
- 服务端返回 `-14` 时，监听把账号暂停 1 小时（`paused_until` 写入状态库），
  以 `paused` 退出长轮询。**等待暂停结束的是监督任务**：它每轮最多睡 30 秒，
  期间不创建监听、不发起 `getUpdates`；TaskWatcher 的 `_notifications_allowed`
  同时检查监听存活与暂停到期，所以暂停期间也不会发送任务通知。
- 暂停到期后，监督任务清除 `paused_until` 并按正常流程重新启动监听，
  **会自动恢复**，但不会提前重连，也不绕过平台限制。恢复过程中若状态库
  读写失败，状态置为 `reconnecting`，按 2–60 秒有界退避重试；只要
  `paused_until` 未到期，监听仍然不会联网（重启后的监听先检查暂停位）。
- 长轮询意外退出或抛出未预期异常时，监督任务按 2 秒起、最多 60 秒的
  有界指数退避重连；`started` 表示监督任务是否仍存活，而不是“曾经启动成功”。
- 监听断线或账号暂停期间，TaskWatcher 暂停任务通知；通知发送本身连续失败时
  也会退避，日志最多每 5 分钟一条，避免断网时形成失败通知风暴。
- 运行时状态写在 `data/wechat/runtime-status.json`（不含 token/正文），
  `wechat_cli.py status` 会显示运行中、重连中、暂停中或已退出。状态文件损坏或
  PID 无法判断时按“可能仍在运行”处理，`logout` 会拒绝执行。
- 重启时 `workflow_manager.reconcile_on_startup()` 会把残留的
  `running / waiting_confirmation` 标成失败；**绝不自动续跑**，避免非幂等副作用
  执行两遍。微信侧只按新状态通知。
- 普通聊天后台任务在停机时取消；未发出的回复不重试。
- `context_token` 只在内存里，重启后任务通知不带它；协议是否接受无 context 的
  任务通知，需要真实账号验证（本轮没有引入新的凭证持久化）。

### 3.6 任务取消与通知一致性

- `/cancel` 和 `/reject`（拒绝计划）在任务进入终态后**立即**撤销该任务所有未使用
  授权码，不等下一轮 TaskWatcher。已取消的任务不会再生成新的操作授权。
- 引擎在步骤边界、最后一步执行完、校验前后都检查取消标志；尚未执行的步骤标成
  `cancelled`，任务落到 `cancelled` 终态。最后一步执行期间到达的取消不会被收尾
  校验悄悄改成“成功”。
- **取消是逻辑取消**：协作式检查只能保证后续步骤不再执行，不能保证已经开始的
  同步工具调用立即停下。管家等待 5 秒仍不结束时强制取消执行体；此时数据库仍会
  落到取消终态，步骤说明会明确写“底层操作无法保证立即停止”，但已产生的副作用
  可能无法回滚。
- 已取消/失败的任务只发送对应的终态通知，不会随后再发“成功”或进度；终态通知
  发送前先撤销授权码。
- 通知发送失败时不会推进 `plan_notified` / `notified_action_id`，下一轮会重试；
  重试会签发新码并让旧码失效。
- 残余风险：如果消息已经发送成功、但进程在写通知进度之前崩溃，重连后可能重复
  发送一次同一条通知（或带新码的授权提示）。这不会导致任务或工具重复执行，
  因为执行仍要经过 `gate.py`，且授权码与具体任务/动作绑定。

---

## 4. 启用步骤

### 4.1 登录（需要本机人工扫码）

```bat
.venv\Scripts\python.exe scripts\wechat_cli.py login
```

- 只显示二维码，不自动登录。
- 成功后打印账号 ID；token 不打印。
- 登录成功**不等于授权**。

### 4.2 显式配对

```bat
.venv\Scripts\python.exe scripts\wechat_cli.py pair --account <账号ID> --user <ilink_user_id> --label 我
```

`--user` 必须是可靠的 `ilink_user_id`，不能填昵称。默认作用域**只有 `chat`**；
需要派任务或远程审批时必须显式追加，且每个值都会严格校验：

```bat
... pair --user <id> --scopes chat,task
... pair --user <id> --scopes chat,task,approve
```

登录时服务端返回的 `ilink_user_id` 与入站消息的 `from_user_id` 是否为同一值，
仍需真实账号验证。若日志显示发送者被拒绝，可显式运行下面这条**本机诊断**命令，
把候选 ID 换成本机日志使用的短哈希再比对；默认日志不会输出完整 ID：

```bat
... sender-hash --user <候选 ilink_user_id>
```

这只是一个本地换算工具，不会放宽允许名单，也不会假设两个 ID 一定相同。

管理命令：

```bat
... list
... unpair --user <id>
... logout --account <账号ID>
... status
```

`unpair` 会移除该用户并撤销其未使用的授权码，但保留任务归属，也不会取消
正在运行的任务。`logout` 删除本机 DPAPI 凭证并撤销该账号未使用的授权码；
它**不等于**微信服务端 token 失效或账号解绑，本工具不会猜测或调用未经确认的
服务端解绑接口。Jia 正在运行（或运行状态无法判断）时，`logout` 会拒绝执行。

### 4.3 启动

在 `.env` 中设置：

```dotenv
JIA_WECHAT_ENABLED=1
JIA_WECHAT_ACCOUNT_ID=<账号ID>
# 可选：
# JIA_WECHAT_STATE_DIR=data/wechat
# JIA_WECHAT_CHANNEL_VERSION=0.1.0
# JIA_WECHAT_CLIENT_VERSION=0.1.0
# JIA_WECHAT_BOT_AGENT=Jia-WeChat/0.1.0
# JIA_WECHAT_ROUTE_TAG=
# JIA_WECHAT_LONG_POLL_TIMEOUT_SECONDS=35
```

然后正常启动 Jia。微信入口失败不会拖垮主服务：缺凭证、缺配对、DPAPI 不可用、
多账号未指定等情况都只记日志并返回。这些 `JIA_WECHAT_*` 会从 `.env` 读取，
进程环境变量优先。

Windows 登录后自动启动（可选带微信监听）见
[docs/autostart.md](autostart.md)；自动启动只设置开关，不会修改允许名单或
授予 `task` / `approve`。

---

## 5. 微信里的命令

```text
/task 要做的事          派一个任务，返回任务短号
/status [任务号]        查看进度；待批准/待确认时会发新的一次性码
/tasks                  最近任务
/pause [任务号]         暂停正在执行的任务
/resume [任务号]        恢复
/cancel [任务号]        取消
/approve 授权码         批准计划，或允许一步工具操作
/reject 授权码          拒绝
/choose 授权码 序号     从候选中选一个
/help                   帮助
```

中文别名：`任务`、`状态`、`任务列表`、`允许`、`拒绝`、`选择`、`暂停`、`继续`、
`取消`、`帮助`。也支持“允许 ABCDE-FGHJK”这种写法。

未知的 `/xxx` 不会落到普通聊天，而是返回帮助。

---

## 6. 第一版的明确限制

- 只支持单聊文字；图片、语音、文件、视频、卡片未实现。
- 不处理群聊指令。
- 普通聊天不提供工具确认；需要动电脑的操作必须走 `/task`。
- 发送失败不重试；回复可能丢一次，但不会重复执行。
- 任务通知如果遇到可重试的发送失败，watcher 会退避重试；连续失败会拉长间隔
  并限流日志，状态类通知可能晚到。取消配对或失去 `task`/`approve` 权限是
  永久性拒绝，不会重试。
- 暂停/恢复依赖现有 `manager` 的执行体；服务重启后执行体不存在，任务会被标失败。
- 取消保证的是“后续步骤不再执行”和任务/步骤进入取消终态；已经开始的工具调用
  没有中断钩子，底层操作可能继续或已经产生部分效果，不能承诺回滚。
- `context_token` 不持久化；重启后的任务通知是否能被客户端正确归入原会话，未验证。
- 没有真实账号验证前，`channel_version` / `client_version` / `bot_agent` 的服务端
  接受度未知；这些都可以用环境变量覆盖。
- 没有实现 typing 指示（接口已具备，但第一版不调用）。

---

## 7. 测试

```bat
.venv\Scripts\python.exe scripts\test_wechat.py
```

测试**全部离线**：不构造真实网络传输、不登录真实账号、不读真实用户数据、不调用
模型、不执行工具。覆盖：

- 合法/非法发送者；未授权消息不回复、不进入聊天；
- 群聊、图片、机器人消息、未完成消息、多 item、控制字符、超长文本；
- 重复消息只执行一次；速率限制；
- 任务派发、状态查询、计划批准/拒绝、暂停/恢复/取消；
- 工具确认、选择授权、无效码、过期码、一次性、跨用户/跨任务；
- 授权码消费时账号/用户/任务/动作类型/动作编号逐项绑定，过期不能消费，
  两个并发消费只有一个成功；
- 计划/确认/状态/终态通知去重，发送失败重试并换新码；
- 取消/失败终态不会被通知成成功；已取消任务不再生成新授权码；
- `/cancel` 成功后立即撤销该任务未使用的授权码；
- 取消配对后停止任务通知、撤销该用户授权码、不误伤其他用户/账号、不无限重试；
- 没有 `approve` 时仍可查看状态，但不生成/下发授权码；
- 长轮询超时、断线退避、`-14` 暂停、停机时不前进游标；
- 重连后同一页被重复投递时，inbound 原子 claim 挡住第二次执行和第二次回复；
- 监督任务在监听意外退出后有界重连、暂停期不重试且不发任务通知、到期恢复、
  状态文件与关闭；
- `unpair`/`logout` 的撤销范围、运行中拒绝、非法账号路径、其他数据不受影响；
- 二维码 ANSI/控制字符拒绝、默认只给 `chat`、非法作用域拒绝、sender-hash 诊断；
- 凭证/允许名单/授权码/context_token 不落明文、不进日志；
- 二维码状态机、验证码、恶意 `baseurl`、缺少 token；
- 运行时默认关闭和失败关闭。

当前结果：**359/359 通过**。Windows DPAPI 往返测试在 Windows 上真实执行。

相关回归：

```bat
.venv\Scripts\python.exe scripts\test_workflow_store.py
.venv\Scripts\python.exe scripts\test_workflow_confirm.py
.venv\Scripts\python.exe scripts\test_gate_prepare.py
.venv\Scripts\python.exe scripts\test_tools.py
.venv\Scripts\python.exe scripts\test_workflow_engine.py
```

其中 `test_workflow_engine.py` 覆盖等待确认时取消、最后一步执行期间取消、
工具不响应协作式取消时的强制取消，以及后续步骤不得继续执行。

`scripts/test_workflow_api.py` 需要先启动 `run.py` 的真实服务；本轮未启动服务，
因此没有把它算作回归通过。

---

## 8. 尚未打通的真实账号功能

以下不是“写完了”，而是**必须用真实账号确认**：

1. 二维码登录全流程、`confirmed` 返回字段、`baseurl` 是否总是返回。
2. `channel_version` / `client_version` / `bot_agent` 是否被当前服务端接受。
3. 真实入站消息的 `message_id`、`seq`、`message_type`、`message_state` 是否与
   公开文档一致；没有 `message_id` 时的去重键是否稳定。
4. `sendMessage` 是否接受回传的 `context_token`；重启后不带 context 的通知能否
   正确归入原会话。
5. `-14` 的实际触发条件和暂停恢复时间。
6. 微信客户端如何展示长文本分片、授权码和任务通知。
7. 真实网络下的长轮询超时、代理、证书行为。
8. 登录返回的 `ilink_user_id` 与入站 `from_user_id` 是否一致。代码层只能通过
   `sender-hash` 比对日志哈希；真实映射必须用测试账号确认，不能假设一定相同。
9. 服务端二维码字段是否确实是 HTTPS URL。当前实现对非 HTTPS/控制字符内容
   失败关闭，真实返回值需要在验收时确认。

在这些验证完成前，不应把 `JIA_WECHAT_ENABLED=1` 作为长期开启的远程入口。

---

## 9. 下一步：真实账号端到端验证

建议按顺序做，每一步都要有明确验收条件：

1. **只登录，不监听**
   - 运行 `wechat_cli.py login`，用测试微信扫码。
   - 验收：拿到账号 ID；`data/wechat/credentials/` 下文件不含 token 明文；
     日志无二维码/token/验证码。
2. **只配对，不启用**
   - `pair` 一个测试用户；确认 `list` 能看到，未配对用户不在名单。
   - 验收：允许名单损坏/为空时入口拒绝启动。
3. **开启监听，只发文字**
   - `JIA_WECHAT_ENABLED=1`，先用只有 `chat` 权限的配对。
   - 验收：能收到回复；重复投递只回一次；群聊/图片无响应；日志无敏感内容。
4. **派任务，但只批准计划，不批准工具**
   - `/task` 一个只读任务；观察计划通知和 `/approve`。
   - 验收：任务进入 `running`；未批准工具步骤时不会执行；`/status` 能拿到新码。
5. **工具确认与一次性**
   - 让任务走到需要确认的步骤；用 `/approve` 一次，再重复一次。
   - 验收：第一次有效，第二次提示无效；拒绝后任务不执行该步骤。
6. **断线/重启**
   - 重启 Jia 后再看任务状态和通知。
   - 验收：残留 `running/waiting_confirmation` 不自动续跑；微信不重复执行旧消息。
7. **最后再考虑更大权限**
   - 只有在 3–6 全部稳定后，才考虑给 `task`/`approve` 作用域，并保持
     `config/tools.yaml` 的现有安全控制不变。

每一步都应保留日志、消息截图和任务数据库记录，作为“真实测试结果”，不要用离线
测试结果代替真实账号结论。
