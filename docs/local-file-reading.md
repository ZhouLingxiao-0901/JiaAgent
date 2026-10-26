# 只读文件理解

这一阶段给「佳」加了两个只读工具，让它能看明确的文本文件，而不是只能靠
模型猜：

| 工具 | 用途 |
| --- | --- |
| `read_text_file` | 读取一个允许目录内的文本文件 |
| `search_text_files` | 在一个允许目录内按关键字搜索文本内容 |

两个工具都**只读**。它们不执行文件内容，不写、不删、不移动、不重命名，
也不支持"顺便跑一下 PowerShell / CMD / Python"。没有任何配置可以把这些
能力打开——不是默认关闭，而是根本没实现。

代码位置：

- `app/tools/read_policy.py`：唯一的权限与内容安全入口
- `app/tools/read_tools.py`：两个工具的实现
- `app/tools/base.py`：`ToolResult.status`、`Resolution.blocked_code`
- `app/agent/gate.py`：阻止原因对应的界面状态
- `app/agent/core.py`：把状态和摘要发给前端
- `web/chat.js`：读取/搜索/拒绝/失败的状态展示
- `scripts/test_file_reading.py`：离线测试
- `config/tools.yaml`：`filesystem:` 配置段

## 一、模型怎么调用

### 读取文件

```json
{
  "path": "D:\\JiaAgent\\README.md",
  "include_line_numbers": true
}
```

返回内容包括：真实路径、大小、编码、行数、正文（默认每行带行号）。
`include_line_numbers` 可省略，默认 `true`。

### 搜索文件内容

```json
{
  "path": "D:\\JiaAgent\\docs",
  "query": "allowed_roots",
  "case_sensitive": false,
  "context_lines": 1
}
```

- `query` 是纯文本关键字，不是正则表达式。
- `case_sensitive` 默认 `false`。
- `context_lines` 默认 1，范围 0–3（受 `max_context_lines` 限制）。
- 返回：命中文件路径、行号、命中行，以及有限的前后文。

模型只能提交 `path`、`query` 这类普通参数。真实读取的路径由服务端在
`prepare()` 中解析，并通过 `Resolution.injected` 固定为
`resolved_path` / `resolved_dir`。模型如果自己提交 `resolved_path`、
`file_fingerprint` 之类的字段，参数校验会直接拒绝（`invalid_argument`）。

## 二、配置允许目录

只读文件理解**不沿用** `permissions.allowed_roots`。后者为了打开应用、
列目录，可能包含整块磁盘（当前配置里就有 `D:\`）；正文要发给模型的
读取不能跟着这么宽。

它只认 `config/tools.yaml` 里的这一段：

```yaml
filesystem:
  allowed_roots:
    - D:\JiaAgent
  blocked_paths:
    - D:\JiaAgent\.git
    - D:\JiaAgent\.venv
    - D:\JiaAgent\.env
    - ~\AppData
    - ~\.ssh
```

规则：

1. **留空 = 什么都不允许读。** 工具失败关闭，不会自动沿用
   `permissions.allowed_roots`，也不会"实在找不到白名单就放开一点"。
2. 根目录必须是绝对路径，或 `~` 开头的用户目录路径。相对路径会被拒绝。
3. 整块磁盘（`D:\`）、整个用户主目录、系统目录、敏感目录不能设成读取根；
   写了也会在启动时被丢掉，并记录一条警告。
4. `blocked_paths` 优先于允许目录。写错的 `blocked_paths` 规则无法证明
   挡住了什么，因此会让**所有**读取失败关闭（`read_policy_error`），
   直到配置被修正。
5. 改完要重启服务才生效。

默认只开放 `D:\JiaAgent`，没有开放桌面、文档、下载或整个用户目录。
如果要读笔记，请写确切的文件夹，例如：

```yaml
filesystem:
  allowed_roots:
    - D:\JiaAgent
    - ~\Documents\笔记
```

不要为了方便写 `~\Documents`，更不要写 `D:\`。

## 三、路径与内容安全

所有判断都在 `app/tools/read_policy.py`，工具本身不拼路径、不判断权限。
原则是**无法可靠判断安全时，拒绝**。

### 路径

- 拒绝 UNC（`\\server\share`）、设备路径（`\\?\`、`\\.\`、`\??\`）、
  盘符相对路径（`D:foo`）、备用数据流（`file.txt:stream`）、空字符和超长路径。
- 用 `realpath` 解析后再做包含判断，`..` 不能绕出允许目录。
- 符号链接、目录联接、reparse point 会解析到真实路径；真实路径不在
  允许目录内就拒绝。搜索时则直接跳过所有链接/联接，避免越界和环。
- 系统关键目录（Windows、System32、Program Files、ProgramData 等）一律拒绝。
- 大小写按 Windows 规则比较（`normcase`）。

### 敏感文件

不只靠扩展名。以下任意一条命中就拒绝：

- 所在目录名：`.git`、`.ssh`、`.gnupg`、`.aws`、`.azure`、`.kube`、
  `.docker`、`.claude`、`AppData`、`secrets`、`credentials` 等。
- 文件名：`.env`（含 `.env.local` 等前缀）、`keys.txt`、`id_rsa`、
  `credentials*`、`secrets*`、`passwords*`、`token.json`、`auth.json` 等。
- 文件名中的完整词：`credential`、`password`、`passwd`、`secret`、
  `apikey`；以及 `access_token`、`refresh_token`、`private_key` 等片段。
- 扩展名：`.pem`、`.key`、`.pfx`、`.p12`、`.jks`、`.keystore`、
  `.kdbx`、`.gpg`、`.token`、`.secret`、`.db`、`.sqlite` 等。
- 正文包含 `-----BEGIN ... PRIVATE KEY-----` 之类的私钥标记。

原始写法也会检查：一个名叫 `.env` 的链接，即使指向普通的 `notes.txt`，
也不会因为"真实文件不敏感"就放行。

### 内容

- 二进制文件（NUL 字节或控制字符过多）拒绝。
- 只接受 UTF-8、UTF-8-SIG、带 BOM 的 UTF-16 / UTF-32。GBK、无 BOM 的
  UTF-16 等一律报"编码不支持"，**不猜编码**，避免返回乱码。
- 不执行任何文件内容。没有 shell、没有 `eval`、没有子进程。

### 大小与截断

所有上限都在 `config/tools.yaml` 的 `filesystem:` 段，代码里还有硬上限
（只能调小或在一定范围内调大）：

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `max_file_bytes` | 200,000 | 单次读取的源文件上限 |
| `max_read_output_chars` | 400,000 | 读取结果（含行号）的输出上限 |
| `max_search_files` | 500 | 一次搜索最多检查的文件数 |
| `max_search_results` | 100 | 一次搜索最多返回的命中数 |
| `max_search_file_bytes` | 200,000 | 搜索时单个文件的上限 |
| `max_search_output_chars` | 20,000 | 搜索结果正文总长度 |
| `max_context_lines` | 3 | 命中前后上下文行数上限 |
| `max_line_chars` | 500 | 单行展示上限（超出明确标注） |
| `max_query_chars` | 200 | 关键字长度上限 |
| `max_path_chars` | 4096 | 路径字符串长度上限 |

达到上限就停止，并在输出里明确写"已截断"及原因，不会悄悄截掉。搜索
默认跳过 `.git`、`.venv`、`node_modules`、`site-packages`、各类缓存和
构建目录，并统计跳过了多少。

## 四、授权与隐私

两个工具都 `requires_confirmation = True`，并沿用现有的
`gate.authorize_tool()`，没有独立调用路径。工作流引擎也走同一个闸门。

顺序是：

1. 模型请求工具。
2. `prepare()` 校验参数、解析真实路径、记录文件/目录指纹。
   **这一步只做 `stat`，不打开文件、不读正文。**
3. 弹确认卡片。卡片明确写出真实目标，并提示：

   > 读取文件：D:\JiaAgent\README.md（只读；文件内容可能发送给当前模型提供商）

   搜索卡片同样写出目录和关键字，并带同样的提示。
4. 用户拒绝：不读取任何内容，不把内容交给模型，返回 `rejected`。
5. 用户同意：执行时重新校验路径和指纹，发现文件/目录变化就停止
   （`file_changed`），不返回旧内容或混合内容。
6. 只有成功读取后才把正文交给模型。

隐私处理：

- 数据库只保存元数据摘要（路径、大小、行数、编码；搜索只保存命中统计），
  不保存完整正文。
- 普通日志不记录文件正文、密钥或凭据。错误信息只说明原因和路径。
- 搜索只返回有限上下文，不把整个文件塞进结果。
- 授权范围不会因为模型说"我需要看别的地方"而自动扩大。要扩大范围，
  只能由你改配置并重启。

## 五、界面状态

`ToolResult.status` 会通过 SSE 的 `tool_result` 事件传给前端：

| 状态 | 含义 |
| --- | --- |
| `read_ok` | 读取成功 |
| `read_truncated` | 读取成功，但输出因长度上限被截断 |
| `search_ok` | 搜索完成 |
| `search_truncated` | 搜索完成，但文件数/命中数/输出长度达到上限 |
| `path_not_allowed` | 路径不在允许目录，或含系统/敏感位置 |
| `no_allowed_roots` | 没有配置允许目录 |
| `read_policy_error` | 配置有误，读取被整体关闭 |
| `sensitive_file` | 凭据/密钥/敏感目录文件 |
| `file_not_found` | 文件或目录不存在 |
| `not_a_file` | 目标是目录等非普通文件 |
| `not_a_directory` | 搜索目标不是目录 |
| `binary_file` | 二进制文件 |
| `unsupported_encoding` | 编码不支持 |
| `file_too_large` | 超过大小上限 |
| `file_changed` | 授权后目标发生变化 |
| `read_failed` | 没有权限或其他读取错误 |
| `invalid_argument` | 参数不合法（包括模型伪造服务端字段） |
| `rejected` | 用户拒绝 |
| `blocked` | 被闸门阻止（其他原因） |

失败的读取不会被包装成"已获取内容"。前端会显示对应状态；即使闸门在
`tool_start` 之前就阻止了，也会补一张结果卡片说明原因。

## 六、已知限制

- 文件指纹基于大小、mtime、设备号和 inode，不是密码学哈希。它能挡住
  授权后的修改、替换、截断和追加；但如果有人在同一时间粒度内原地改写
  并恢复了 mtime，无法证明内容完全一致。这是有意的取舍：`prepare()`
  阶段不读正文，否则"用户拒绝后不读内容"就不成立。
- 某些文件系统（部分网络盘、虚拟盘）的 inode 恒为 0，身份比对会退化，
  仍会比对路径、大小和 mtime。
- 搜索是单线程目录遍历 + 逐文件读取，大目录可能接近文件数上限；达到上限
  会明确报告，不会无限扫描。
- 不支持正则表达式，不支持 PDF、Word、Excel 等二进制文档，不支持压缩包
  内搜索。
- 搜索不跟随符号链接和目录联接；需要搜索链接目标时，请直接给出目标的
  真实路径（仍须在允许目录内）。
- 不提供"读取整个目录"或"读取多个文件"的批量接口；一次只读一个文件。

## 七、测试与手工验证

离线测试：

```bat
.venv\Scripts\python.exe scripts\test_file_reading.py
```

覆盖：正常读取/搜索、行号与上下文、文件不存在、目录外路径、`..` 越界、
符号链接/目录联接越界、`.env`/凭据/私钥拒绝、二进制、过大文件、搜索
文件数/命中数/输出长度截断、用户拒绝、读取异常、模型伪造服务端参数、
授权后文件变化、工作流闸门。测试使用项目目录下的独立临时目录，不读
真实私人文件。

结果（2026-10-21）：`77/77 全部通过`。

相关回归：

| 脚本 | 结果 |
| --- | --- |
| `scripts/test_policy.py` | 28/28 |
| `scripts/test_tools.py`（默认，不执行真实操作） | 18/18 |
| `scripts/test_tool_calling.py` | 5/5 |
| `scripts/test_gate_prepare.py` | 77/77 |
| `scripts/test_workflow_engine.py` | 87/87 |
| `scripts/test_workflow_confirm.py` | 24/24 |
| `scripts/test_workflow_api.py` | 18/18 |
| `scripts/test_app_security.py` | 30/30 |

手工验证不需要开启真实鼠标或键盘输入：

1. 保持 `input.real_mouse_enabled: false`，正常启动服务。
2. 在网页里说"读一下 README.md"，确认卡片应显示真实路径和隐私提示；
   点拒绝，确认没有任何正文出现在回复里。
3. 再发起一次并同意，确认返回带行号的正文。
4. 尝试读 `C:\Windows\win.ini` 或项目外文件，确认被拒绝并显示
   `path_not_allowed`。
5. 搜索一个允许目录里的关键字，确认返回路径、行号、有限上下文和跳过统计。
