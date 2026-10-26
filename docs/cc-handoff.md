# 与 Claude Code 的第一阶段协作：人工交接

这一阶段只做一件事：让佳能够**准备一个受限的只读分析任务**，由你手工交给
Claude Code（下称 CC），再把 CC 的结构化结果放回来，由佳**严格验证**后才
交给模型。

它明确**不包含**：

- 不自动启动 CC；
- 不调用 CC CLI / SDK；
- 不启动任何子进程，不执行 shell；
- 不安装新依赖；
- 不修改、删除、移动原始项目文件；
- 不操作鼠标、键盘或桌面；
- 不自动重试失败任务；
- 不向 GitHub 推送任何内容。

代码位置：

- `app/cc/protocol.py`：task.json / result.json 协议与严格校验
- `app/cc/snapshot.py`：路径过滤、受限快照、SHA-256 manifest、完整性检查
- `app/cc/service.py`：交接目录、状态、结果验证
- `app/tools/cc_tools.py`：`prepare_cc_handoff` / `validate_cc_result`
- `config/tools.yaml`：独立的 `cc:` 配置段
- `scripts/test_cc_handoff.py`：离线测试
- `web/chat.js`：新增状态的界面文案

## 一、配置：独立白名单，默认关闭

`cc:` **不沿用** `filesystem.allowed_roots`，也不沿用
`permissions.allowed_roots`。后者为了打开应用/列目录可能包含整块磁盘；
拿它来复制代码给 CC 等于把整个盘交出去。

```yaml
cc:
  enabled: false
  allowed_roots: []
  blocked_paths:
    - D:\JiaAgent\.git
    - D:\JiaAgent\data
    - D:\JiaAgent\logs
    - ~\.claude
    - ~\.ssh
  max_files: 200
  max_total_bytes: 2000000
  max_file_bytes: 200000
  max_result_bytes: 100000
  max_paths: 20
  max_goal_chars: 4000
  deadline_hours: 24
```

规则：

1. `enabled` 默认 `false`，`allowed_roots` 默认空。两者不满足就**拒绝创建
   交接任务**，不会回退到其他白名单。
2. 允许根必须是绝对路径；整块磁盘、用户主目录**及其上级目录**、系统/敏感
   目录会被 `read_policy` 拒绝。无法可靠解析用户主目录时，读取根同样
   失败关闭，不会跳过这项检查。
3. `blocked_paths` 优先于允许目录；写错会让 `cc` 交接整体失败关闭。
4. 所有上限在代码里还会做硬夹取，坏值不会放宽限制。
5. 改完要重启服务。真实鼠标输入开关 `input.real_mouse_enabled` 保持
   `false`，本功能没有任何路径能打开它。

## 二、两个工具

| 工具 | 用途 | 确认 | 自动重试 |
| --- | --- | --- | --- |
| `prepare_cc_handoff` | 准备只读任务和受限快照 | 必须 | 否 |
| `validate_cc_result` | 验证你放回的 result.json | 必须 | 否 |

### `prepare_cc_handoff`

模型只能提交两个参数：

```json
{
  "goal": "分析订单状态流转",
  "paths": ["D:\\JiaAgent\\src\\order"]
}
```

不能提交 `task_id`、`handoff_dir`、命令、CLI 参数、环境变量、输出目录或
截止时间。`task_id`、交接目录、截止时间、文件清单都由服务端生成/固定。
模型自己伪造这些字段会被 `Tool.validate()` 判为 `invalid_argument`。

`prepare()` 阶段**只做 stat 和目录枚举，不打开文件、不读正文**，然后：

- 校验路径在 `cc.allowed_roots` 内；
- 拒绝敏感/凭据类文件名、路径和密钥文件名，以及 `.git`、`.ssh`、`.claude`、
  日志、数据库、缓存依赖；
- 拒绝符号链接、目录联接和 reparse point；
- 检查文件数、单文件大小、总大小；
- 列出候选文件并在确认卡片上显示：分析目标、文件范围、文件数、只读限制、
  “文件内容可能发送给当前模型提供商”。

`prepare()` 不读取文件正文，因此不会在这个阶段扫描私钥正文；私钥正文过滤
发生在用户确认后的复制阶段（见“五、权限与敏感文件”）。

用户拒绝后，`run()` 不会执行，**不会生成任何交接包，也不会读取源文件正文**。

### `validate_cc_result`

模型只能提交：

```json
{"task_id": "cc-0123456789abcdef0123456789abcdef"}
```

不能指定任意结果路径。服务端只认：

```text
data/cc-handoff/<task_id>/result.json
```

`prepare()` 只确认任务目录、`task.json` 和 `result.json` 存在，检查结果文件
不是链接、不是目录、不超过 `max_result_bytes`，并记录指纹；**不读
`result.json` 正文**。它还会读取 `task.json`，检查结构并确认任务尚未超过
`deadline`。确认卡片会明确写出任务编号、结果文件来源（“用户放回的”）、
只读校验，以及内容可能发送给当前模型提供商。

用户确认后才读取并验证。CC 的结果被视为**不可信输入**；工具不会执行结果
里的命令、脚本或“下一步指示”。deadline 在 `prepare_result()` 和
`validate_result()` 两个服务入口都会检查；后者重新读取 `task.json` 后还会
再检查一次。

## 三、交接目录

```text
data/cc-handoff/
  cc-0123456789abcdef0123456789abcdef/
    task.json
    manifest.json
    workspace/
      root_1/
        src/
          Main.java
          sub/
            Util.java
    result.json          # 之后由你放回
```

- `workspace/` 只包含明确授权的文件副本，不包含原始仓库的任意路径。
- `root_1`、`root_2`……是服务端给允许根起的虚拟名字，用来避免把原始绝对
  路径写进交接包。manifest 和结果都使用这种工作区相对路径。
- `task.json`、`manifest.json` 的路径相对 `workspace/`，统一使用 `/`。
- 交接包在临时目录里组装完成后再整体改名发布。复制、校验、写入或发布过程中
  出现任何异常，都会清理本次调用已经成功创建、且尚未发布的临时目录，已有任务
  不受影响。临时目录创建本身因同名路径已存在而失败时，不会删除或改写那个已有
  目录。已经成功改名发布的目录不会被清理；清理本身失败会记录日志，但不会覆盖
  原始异常。
- 源文件在准备/复制窗口内发生变化、消失或被替换时，整次失败关闭
  （`cc_snapshot_changed`），不发布交接包。

### task.json 示例

```json
{
  "schema_version": 1,
  "task_id": "cc-0123456789abcdef0123456789abcdef",
  "goal": "分析订单状态流转",
  "allowed_paths": ["root_1/src"],
  "read_only": true,
  "created_at": "2026-10-21T18:00:00+08:00",
  "deadline": "2026-10-21T19:00:00+08:00",
  "manifest": [
    {
      "path": "root_1/src/Main.java",
      "size": 1234,
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

`manifest.json` 是同一份文件清单：

```json
{
  "schema_version": 1,
  "task_id": "cc-0123456789abcdef0123456789abcdef",
  "files": [
    {
      "path": "root_1/src/Main.java",
      "size": 1234,
      "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

### result.json 示例

```json
{
  "schema_version": 1,
  "task_id": "cc-0123456789abcdef0123456789abcdef",
  "status": "succeeded",
  "summary": "订单状态从 CREATED 经 PAID 到 SHIPPED；异常路径缺少回滚。",
  "files": [
    {"path": "root_1/src/Main.java", "note": "状态流转入口"}
  ],
  "error": "",
  "finished_at": "2026-10-21T18:10:00+08:00",
  "truncated": false
}
```

`status` 只能是：

| 值 | 含义 | 佳的处理 |
| --- | --- | --- |
| `succeeded` | CC 声称完成 | 还要通过全部协议/路径/完整性校验才展示 |
| `failed` | CC 明确失败 | 不当成功；返回失败状态 |
| `timeout` | CC 报告超时 | 不当成功 |
| `cancelled` | CC 报告取消 | 不当成功 |
| `invalid` | CC 自己报告结果无效 | 不当成功 |

## 四、严格验证

`result.json` 必须是一份**完整、纯 JSON 文档**。Markdown 代码围栏、前后说明、
尾随逗号、重复键、`NaN`/`Infinity` 都会被拒绝。超过 Python 整数转换上限的
超长整数也会映射为 `cc_result_invalid`，不会作为未分类异常泄露到上层。

验证顺序：

1. 校验 `task.json`、`manifest.json` 的结构、版本、`task_id`、`read_only`、
   时间、路径、大小、SHA-256；
2. 检查当前时间是否已到或超过 `deadline`；已过期返回 `cc_task_expired`，
   不读取结果正文；
3. 校验 `manifest.json` 与 `task.json` 的文件清单完全一致；
4. 按当前配置再次检查文件数、**每个文件**的 `max_file_bytes`、总大小、
   路径数和 goal 长度；这些检查发生在读取 manifest 之后、读取工作区和
   `result.json` 正文之前，不是“读取前保护”；
5. 遍历 `workspace/`，要求文件不多、不少、大小和 SHA-256 全部匹配 manifest；
   出现链接/联接、额外文件、缺失文件都失败关闭；每个实际文件读取时也会
   受单文件上限约束；
6. 检查结果文件大小、编码，读取前后用 `fstat` 核对；
7. 校验 `schema_version` 必须是 `1`；
8. `task_id` 必须与任务完全一致；
9. `status` 必须是允许的五个值之一；
10. `summary`、`error`、`note` 类型和长度必须合法；
11. `truncated` 必须是布尔值；`finished_at` 必须是 ISO 8601；
12. `files[].path` 必须是安全的工作区相对路径，并且**必须存在于 manifest**；
13. `succeeded` 时 `summary` 非空且 `error` 必须为空；否则不得当成功。

不满足任何一条就返回 `cc_result_invalid`，不会“尽量解析”或“跳过坏字段”。

### 状态

| 状态 | 含义 |
| --- | --- |
| `cc_handoff_ready` | 交接包已生成 |
| `cc_disabled` | `cc.enabled` 为 false |
| `cc_limit_exceeded` | 文件数/大小/路径数超限 |
| `cc_snapshot_changed` | 源文件在窗口内变化，未生成交接包 |
| `cc_snapshot_failed` | 快照生成失败 |
| `cc_task_not_found` | 任务目录/task.json 不存在 |
| `cc_task_expired` | 任务已到达或超过 deadline |
| `cc_result_not_found` | result.json 不存在 |
| `cc_result_too_large` | result.json 超过上限 |
| `cc_result_invalid` | 结果协议/路径/字段不合法 |
| `cc_result_changed` | 用户确认后 result.json 变化 |
| `cc_package_invalid` | 工作区与 manifest 不一致，或任务包被篡改 |
| `cc_result_ok` | 结果验证通过 |
| `cc_result_truncated` | 结果验证通过，但 CC 标记了截断 |
| `cc_result_failed` / `cc_result_timeout` / `cc_result_cancelled` | CC 报告未成功 |

## 五、权限与敏感文件

路径与敏感判断复用 `read_policy`，但使用 `cc` 自己的配置：

- 只认 `cc.allowed_roots`，留空即拒绝，**不回退**到 `filesystem` 或
  `permissions`；
- 拒绝 UNC、设备路径、盘符相对路径、ADS、`..`、空字符、超长路径；
- 顶层链接/目录联接直接拒绝；遍历时跳过所有链接，不跟随到允许范围外；
- 内置敏感名称/目录/后缀：`.env`、`keys.txt`、`id_rsa`、`.git`、`.ssh`、
  `.claude`、`AppData`、`credentials*`、`secrets*`、`*.pem`、`*.key`、
  `*.db`、`*.sqlite` 等；
- 额外排除：日志目录/`*.log`、常见二进制/压缩包/图片/音视频/办公文档；
- 单文件/总量/文件数超限都失败关闭，不会悄悄截断清单；
- 原始绝对路径不写进 `task.json`、`manifest.json` 或工作区路径。

### 私钥正文过滤

`prepare()` 不读正文，所以私钥**文件名**在准备阶段按敏感规则拒绝，而私钥
**正文**只在用户确认、实际复制字节时扫描。复制阶段会识别以下 PEM/PGP 起始
标记：

```text
-----BEGIN PRIVATE KEY-----
-----BEGIN RSA PRIVATE KEY-----
-----BEGIN EC PRIVATE KEY-----
-----BEGIN OPENSSH PRIVATE KEY-----
-----BEGIN ENCRYPTED PRIVATE KEY-----
-----BEGIN PGP PRIVATE KEY BLOCK-----
```

扫描按复制块进行，并在块之间保留重叠长度，因此标记跨越复制块边界也能发现。
命中任一标记就取消整个交接，清理未发布的临时目录，不会发布任务。该检查复用
`read_policy` 的同一份标记规则，不是独立的完整凭据检测器：它只覆盖上述起始
标记，不保证识别其他私钥格式、编码/压缩后的内容、口令、token 或任意秘密。

## 六、人工操作流程

1. 你对佳说：“分析订单状态流转”。
2. 佳组织目标，提交候选文件/目录范围。
3. 服务端校验路径、数量、大小和敏感规则。
4. 网页弹出确认卡片，写明目标、范围、只读限制和隐私提示。
5. 你拒绝：立即终止，不生成交接包，不读取正文。
6. 你同意：服务端复制受限快照，写入 `task.json` / `manifest.json`。
7. 你把整个 `data/cc-handoff/<task_id>/` 交给 CC。
8. 要求 CC **只读**分析，只在 `workspace/` 内工作，不执行 shell、不修改文件。
9. CC 按协议把结果写到 `data/cc-handoff/<task_id>/result.json`。
10. 你回到佳这里说：“验证任务 `<task_id>` 的结果”。
11. 佳弹出确认卡片；确认后读取并严格验证，验证通过才展示结论。

不需要、也不应该把 CC 的输出当成已执行的事实。佳只把它当作文本数据。

## 七、已知限制

- **不是密码学签名。** `prepare()` 为遵守“拒绝时不读正文”，只固定
  大小/mtime/dev/ino；复制时才计算 SHA-256。若有人在同一时间粒度内
  原地改写并恢复 mtime，准备阶段无法证明内容完全一致。
- 交接包文件继承当前 Windows 用户的默认 ACL；本阶段没有额外设置只读
  ACL，也没有给 CC 建立进程/文件系统沙箱。CC 侧的“只读”目前依赖人工
  交接约束；接入 CLI 自动化前必须在进程、cwd 和权限层面另行隔离。
- 任务包没有服务端私钥签名。同一个 Windows 用户如果同时篡改
  `task.json`、`manifest.json` 和 `workspace/`，完整性检查无法区分。
  这一阶段防的是 CC 的误写/越权引用和普通篡改，不是本机恶意用户。
- 二进制过滤以扩展名为主（准备阶段不读正文）；没有扩展名的二进制文件
  可能进入工作区。CC 读取时应按文本协议处理，失败应报告，不得执行。
- 私钥正文过滤只识别上面列出的 PEM/PGP 起始标记，不是通用秘密扫描器；
  其他私钥格式、编码/压缩内容、口令、token 和云凭据不会被识别。它扫描的是
  确认后实际复制的字节，而不是 `prepare()` 阶段的全部候选正文。
- 清理临时目录本身失败时，原始异常仍会抛出，但临时目录可能残留；清理错误
  会写入日志，需要人工检查/清理。已成功发布的目录不会因为后续异常被删除。
- `deadline` 现在会在 `prepare_result()` 和 `validate_result()` 两个服务入口
  检查，但**不会**自动删除目录、杀进程或停止已经开始的读取；本阶段没有
  后台 runner。它使用本机系统时钟：系统时钟回拨会延后过期判断，向前调整
  会提前过期。`task.json` 可被本机用户手工修改，因此 deadline 只用于限制
  正常人工交接的时间窗口，**不能防止篡改，也不是密码学时间戳**。
- `read_json_capped()` 的顺序是 `lstat` → `os.open` → `fstat` → 重新
  `lstat`，并比较文件身份、大小和 mtime；可用平台还会加 `O_NOFOLLOW`。
  这缩小并检测“检查后替换”，但不是真正的原子打开：Windows 上仍存在检查
  与打开之间的窗口，硬链接、原地改写并恢复 mtime 等情况可能绕过。不能把
  它描述成“彻底消除 TOCTOU”。
- 单文件大小在三个位置执行：创建时 `resolve_file(max_bytes=...)` 在读取
  候选文件前拒绝；复制时再次解析并核对；验证时先按 manifest 声明值检查，
  再在遍历工作区实际读取时限制单文件和总量。manifest 声明值检查发生在
  读取 manifest 之后，不能阻止读取 manifest 本身。
- 没有自动重试、自动重新准备、自动启动 CC 或自动读取结果目录轮询。
- 结果摘要会展示给模型，但数据库只落任务号/状态/文件数等元数据，不落
  完整分析正文。
- 交接包目录需要你手工管理；系统不会自动清理旧任务。

## 八、测试

离线测试：

```bat
.venv\Scripts\python.exe scripts\test_cc_handoff.py
```

覆盖：

- 正常准备、正常验证、确认卡片文案；
- 用户拒绝准备/拒绝验证，确认没有执行、没有读正文、没有生成目录；
- 敏感文件、日志、缓存/依赖、二进制、链接/目录联接过滤；
- 文件数、单文件、总大小、结果大小上限；恰好达到单文件上限；
- 读取 manifest 后按更严格的 `max_file_bytes` 拒绝，以及工作区实际文件
  超过单文件上限时停止读取；
- deadline 在准备和验证入口被拒绝，边界时间按已过期处理，注入时间不依赖
  真实等待；
- 结果文件在检查后被替换成另一个普通文件或符号链接时拒绝读取；
- 路径越界、显式敏感路径、空白名单；
- 授权后源文件变化，失败且不发布交接包；
- 工作区被篡改、任务包与 manifest 不一致；
- 错误 schema、task_id、status、缺字段、重复 JSON 键；
- 绝对路径、`..`、manifest 之外的越权引用；
- `succeeded` 带非空 error 的伪造成功；
- 结果在确认后变化；
- 私钥正文（普通文件和跨复制块标记）在复制阶段被拒绝，不发布任务、不残留
  临时目录；
- 用户主目录本身、父目录和更高祖先不能作为读取根；主目录下的普通项目目录
  仍可接受；主目录无法解析时失败关闭；
- 复制中途出现意外异常时清理未发布临时目录，已有任务不受影响；清理本身失败
  不会覆盖原始异常，并留下错误日志；临时目录与已有同名目录碰撞时保留原目录
  和内容，不发布正式任务；
- `result.json` / `task.json` / `manifest.json` 中的超长整数映射为
  `cc_result_invalid`；
- 模型伪造 `task_id` / `handoff_dir`；
- 确认默认配置关闭、空白名单、真实鼠标输入仍关闭；
- 静态检查交接代码里没有进程/Shell 调用。

结果（2026-10-24）：`158/158 通过`。

相关回归：

| 脚本 | 结果 |
| --- | --- |
| `scripts/test_file_reading.py` | 77/77 |
| `scripts/test_policy.py` | 38/38 |
| `scripts/test_tools.py`（默认） | 18/18 |
| `scripts/test_tool_calling.py` | 5/5 |
| `scripts/test_gate_prepare.py` | 77/77 |
| `scripts/test_workflow_engine.py` | 87/87 |
| `scripts/test_workflow_confirm.py` | 24/24 |
| `scripts/test_app_security.py` | 30/30 |

`scripts/test_tool_calling.py` 会向当前模型提供商发请求，不属于离线测试；
本阶段新增代码本身没有调用模型。`scripts/test_workflow_api.py` 需要另开
服务进程，本次未运行。没有运行 `scripts/test_agent_loop.py`，避免真实
打开记事本等桌面副作用。

## 九、接入只读 CLI 自动化前必须补的检查

这一阶段**没有**实现 CLI runner。真要把交接自动化，至少先完成：

1. **独立凭据**：不要复用当前用户/全局 CC 的登录态。为自动化单独创建
   受限 token/profile；确认 `--restricted` 不会读取用户设置，也不会把
   凭据泄露给子进程。
2. **固定可执行文件**：使用绝对路径和已核对的版本/SHA-256；不依赖 PATH
   上的 `claude`（当前机器上的 PATH shim 已损坏）。
3. **最小工具集**：显式 `--tools` 允许清单，明确禁用 Edit/Write/Bash/
   Notebook/Web 等；`--permission-mode` 设为只读；`--permission-prompts none`
   只能在工具集已经最小化的前提下使用。
4. **工作目录隔离**：cwd 固定到当前 `<task_id>/workspace`，禁止访问父目录、
   原始仓库、用户主目录和网络共享。
5. **环境隔离**：清空/白名单环境变量，不传入 API key、代理、SSH/Git 凭据；
   禁用 MCP、插件、hooks、用户/项目 settings（`--strict-mcp-config`、
   `--no-session-persistence` 等）。
6. **进程约束**：超时、最大预算、输出字节上限、进程树强制结束；不自动
   重试；一次只允许一个 runner。
7. **结构化输出**：使用 `--output-format json` 和 `--json-schema` 约束结果；
   仍然要按本阶段的协议二次验证，不能因为 CC 声称完成就跳过。
8. **结果落盘**：只接受写入指定 `result.json`；校验文件不是链接、大小受限，
   读取前后核对指纹。
9. **审计**：记录 task_id、开始/结束时间、退出码、资源用量和验证结论；
   不记录源文件正文、token 或完整 CC 输出。
10. **人工兜底**：每次 CLI 调用前仍要用户确认；用户拒绝后不启动子进程；
    不因为失败自动扩大权限或改用另一种执行方式。

在这些条件满足之前，只保留当前的人工交接方式。
