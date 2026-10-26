# JiaAgent（佳）

一个跑在自己 Windows 电脑上的个人 AI Agent：能聊天、能记住对话、能调用工具操作电脑，也能把一句话拆成任务在后台执行。所有高风险操作都必须经过本人确认。

> **公开仓库版**：已移除 `.env`、`keys.txt`、`data/`、`logs/` 和本机虚拟环境。首次运行请按「二、配置 API Key」创建自己的 `.env`。

## 现在有什么

- 本地网页聊天界面，SSE 流式输出与工具活动卡片
- 一套 OpenAI 兼容适配器接入 DeepSeek / 智谱 GLM / 通义千问 / OpenAI
- 12 个工具：应用发现与启动、文件读写与搜索、屏幕观察、单次点击等
- 统一的授权闸门（`app/agent/gate.py`），聊天与后台工作流共用同一份确认逻辑
- 自主工作流：拆解、过目、后台执行、暂停 / 恢复 / 取消
- SQLite 记忆与任务持久化
- 微信作为第二个入口（鉴权白名单 + DPAPI 凭证）

## 早期阶段一做了什么

- 一个本地网页聊天界面（打字机效果、会话列表、历史记录）
- 一个可扩展的模型适配器层，目前支持 DeepSeek / 智谱 GLM / 通义千问 / OpenAI
- SQLite 记忆：会话和消息都落盘，关掉程序再打开历史还在
- 统一的配置管理：密钥只放 `.env`，模型清单只放 `config/models.yaml`
- 日志（控制台 + 滚动文件）和统一的错误处理

阶段一**故意不做**的事：鼠标键盘自动化、远程控制、语音、iPhone 互通。
`app/tools/` 里已经留好了位置和"必须用户授权"的规矩，但一个真工具都没实现。

## 一、安装

需要的东西：Python 3.11 以上（你机器上是 3.13，够用）。

在项目根目录 `D:\JiaAgent` 下打开 VS Code 的终端（或者 PowerShell），执行：

```bat
:: 创建虚拟环境（如果还没建过）
python -m venv .venv

:: 安装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

只装了 6 个包，都是成熟的开源库，没有引入任何多余的东西。

## 二、配置 API Key

1. 直接打开项目里的 `.env` 文件（已经帮你建好了，密钥栏是空的）。

   如果你不小心把它删了，用这条命令从模板重新生成：

```bat
copy .env.example .env
```

2. 用记事本或 VS Code 打开 `.env`，在你要用的那一家后面填上 Key：

```
JIA_DEEPSEEK_API_KEY=sk-你的DeepSeek密钥
JIA_GLM_API_KEY=
JIA_QWEN_API_KEY=
JIA_OPENAI_API_KEY=
```

只填你要用的那一家就行，其余留空。DeepSeek 的 Key 在
<https://platform.deepseek.com/api_keys> 申请。

3. 保存后跑一下配置检查：

```bat
.venv\Scripts\python.exe scripts\check_config.py
```

看到 `[OK] 默认模型的 Key 已就绪` 就说明配置没问题了。

> **安全提醒**：`.env` 已经被 `.gitignore` 排除，`git status` 里看不到它，
> 也永远不会被提交。请不要把 Key 直接写进任何 `.py` 文件。

## 三、启动

```bat
.venv\Scripts\python.exe run.py
```

然后在浏览器打开 <http://127.0.0.1:8000>。

停止服务：在那个窗口按 `Ctrl+C`。

### 登录后自动启动（可选）

如果希望登录 Windows 后自动运行，见 [docs/autostart.md](docs/autostart.md)。
使用当前用户的任务计划程序，不要求管理员权限，也不会修改微信权限；
微信监听需要显式加 `-EnableWeChat` 或在 `.env` 里开启。

## 四、验证功能

按顺序做，每一步都能独立确认：

**1. 服务活着吗**
浏览器打开 <http://127.0.0.1:8000/api/health>，应该返回：
```json
{"status":"ok","name":"JiaAgent","version":"0.1.0"}
```

**2. 记忆模块（不需要联网、不花钱）**
```bat
.venv\Scripts\python.exe scripts\test_memory.py
```
它会在数据库里建一个测试会话、写 3 条消息、读回来、再删掉。
看到 `[OK] 记忆模块工作正常` 就对了。

**3. 模型调用（会联网，一次几厘钱）**
```bat
.venv\Scripts\python.exe scripts\test_llm.py
```
屏幕上应该逐字打出模型的回复，最后一行是 `[OK] 对话已写入数据库`。

**4. 网页聊天**
打开 <http://127.0.0.1:8000>，发一句"你好"。
应该看到回复逐字出现，左侧出现以你第一句话命名的会话。

**5. 上下文记忆**
在同一个会话里接着问"我刚才说了什么？"，模型应该答得上来。

**6. 持久化**
关掉服务窗口（`Ctrl+C`），重新 `run.py`，刷新网页。
刚才的对话应该还在左边的会话列表里。

## 五、换模型

编辑 `config/models.yaml`，改一行就行，不用动任何代码：

```yaml
default: qwen        # 把这里从 deepseek 改成 qwen
```

重启服务，网页右上角的模型下拉框里也能临时切换（针对单个会话）。

想加一家新的（比如 Moonshot、硅基流动），在 `providers:` 下面照抄一段：

```yaml
  moonshot:
    label: Moonshot
    type: openai_compatible
    base_url: https://api.moonshot.cn/v1
    model: moonshot-v1-8k
    api_key_env: JIA_MOONSHOT_API_KEY
```

再到 `.env` 里加上对应的一行即可。

## 六、项目结构

```
D:\JiaAgent
├─ run.py                  启动入口
├─ requirements.txt        依赖清单
├─ .env                    密钥（不提交）
├─ .env.example            配置模板
├─ config\
│   └─ models.yaml         模型供应商清单
├─ app\
│   ├─ main.py             FastAPI 应用、路由挂载、错误处理
│   ├─ settings.py         配置读取（唯一的配置入口）
│   ├─ logging_setup.py    日志
│   ├─ errors.py           统一异常类型
│   ├─ api\chat.py         HTTP 接口
│   ├─ agent\core.py       智能体核心：编排记忆 + 模型
│   ├─ llm\                模型适配器
│   │   ├─ base.py                 统一接口
│   │   ├─ openai_compatible.py    一个类通吃 4 家模型
│   │   └─ registry.py             按名字取模型
│   ├─ memory\             记忆
│   │   ├─ schema.sql              建表语句
│   │   ├─ db.py                   连接与初始化
│   │   └─ store.py                会话与消息读写
│   └─ tools\base.py       工具接口（阶段一为空壳）
├─ web\                    网页界面（HTML + CSS + 原生 JS）
├─ scripts\                验证脚本
├─ data\jiaagent.db        SQLite 数据库
└─ logs\jiaagent.log       运行日志
```

分层规则：**网页 → 接口 → 智能体 → 适配器 / 记忆**。
每一层只跟下一层说话，所以换模型不影响界面，换界面不影响模型。

## 七、常见问题

**网页提示"还没有配置任何 API Key"**
`.env` 没建，或者 Key 填错了。跑 `scripts\check_config.py` 看具体是哪一家。

**报错 `missing_api_key`**
你选的模型没有配 Key。填 `.env`，或者把 `models.yaml` 里的 `default` 换成已配好的那家。

**报错 `API Key 无效或已过期`**
Key 复制时漏字符了，或者已经在官网上被删除。

**端口被占用**
改 `.env` 里的 `JIA_PORT=8001`。

**报错 `llm_error` / `HTTP 502`（尤其是接本机模型的时候）**
Windows 上装了 Clash / FlClash 这类软件后，系统里会有一个全局代理。
Python 的 HTTP 库会自动读它，于是连 `127.0.0.1` 的请求也被送去代理，
结果拿回一个 502，非常难查。本项目的 `app/settings.py` 已经处理了这件事，
让 `127.0.0.1` 和 `localhost` 永远直连，不需要你做任何配置。
如果你自己另写了脚本连本机模型（比如 Ollama），记得也设一下
`NO_PROXY=127.0.0.1,localhost`。

**中文显示成乱码**
在那个终端里先执行 `chcp 65001`，再运行脚本。

**想看详细日志**
`logs\jiaagent.log`，里面有每一次模型调用的记录。

## 八、下一步（阶段二预告）

- 长期记忆：自动从对话里提取事实，存进 `memories` 表（表已经建好了）
- Claude 适配器：新建 `app/llm/anthropic_adapter.py` 并注册，配合已有的接口
- 工具调用：真正实现"开应用、读界面"，全部走用户确认流程
- 流式输出的停顿优化、token 统计与费用估算
