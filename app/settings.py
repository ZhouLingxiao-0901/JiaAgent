"""配置管理：整个项目唯一的配置入口。

两条硬规则：
1. 密钥只从环境变量或项目根目录的 .env 读取。
   任何 .py 文件里都不会出现密钥字符串。
2. 模型清单放在 config/models.yaml。
   加模型、换模型只改那个 yaml 文件，不改代码。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigError

# ----------------------------------------------------------------------
# 让本机地址绕过系统代理
#
# Windows 上装了 Clash / FlClash 这类软件后，系统里会设一个全局代理
# （你这台机器上是 127.0.0.1:7890）。Python 的 HTTP 库会自动读取这个设置，
# 于是连本机地址的请求也会被扭送到代理，拿回一个 502，非常难排查。
# 下面这几行确保 127.0.0.1 和 localhost 永远直连；对外网模型没有任何影响。
# ----------------------------------------------------------------------
def _let_localhost_bypass_proxy() -> None:
    for env_name in ("NO_PROXY", "no_proxy"):
        hosts = [h.strip() for h in os.environ.get(env_name, "").split(",") if h.strip()]
        for host in ("127.0.0.1", "localhost"):
            if host not in hosts:
                hosts.append(host)
        os.environ[env_name] = ",".join(hosts)


_let_localhost_bypass_proxy()

# 本文件位置是 <项目根>/app/settings.py，往上两层就是项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
WEB_DIR = PROJECT_ROOT / "web"
CONFIG_FILE = PROJECT_ROOT / "config" / "models.yaml"
TOOLS_FILE = PROJECT_ROOT / "config" / "tools.yaml"
ENV_FILE = PROJECT_ROOT / ".env"


class Secrets(BaseSettings):
    """从 .env / 环境变量里读到的密钥和运行参数。

    字段名是全小写的，pydantic-settings 会自动对应同名的
    大写环境变量，例如 jia_port 对应 JIA_PORT。
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 密钥：留空表示这家模型还没配置 ----
    jia_deepseek_api_key: str = ""
    jia_glm_api_key: str = ""
    jia_qwen_api_key: str = ""
    jia_openai_api_key: str = ""

    # ---- 运行参数 ----
    jia_host: str = "127.0.0.1"
    jia_port: int = 8000
    jia_log_level: str = "INFO"

    def key_for(self, env_name: str) -> str:
        """按 models.yaml 里写的变量名去取密钥，取不到返回空字符串。"""
        return getattr(self, env_name.strip().lower(), "") or ""


class ProviderConfig(BaseModel):
    """单个模型供应商的配置，对应 config/models.yaml 里的一项。"""

    name: str
    label: str = ""
    type: str = "openai_compatible"
    base_url: str
    model: str
    api_key_env: str
    enabled: bool = True


class AppConfig(BaseModel):
    """全部模型配置。"""

    default_provider: str
    providers: dict[str, ProviderConfig]

    def provider(self, name: str | None = None) -> ProviderConfig:
        """取某个供应商的配置，不传名字就取默认的。"""
        key = (name or self.default_provider).strip()
        if key not in self.providers:
            raise ConfigError(
                f"找不到名为 {key!r} 的模型供应商",
                detail=f"可选的模型有：{', '.join(self.providers)}（配置在 config/models.yaml）",
            )
        provider = self.providers[key]
        if not provider.enabled:
            raise ConfigError(
                f"模型 {key!r} 已被禁用",
                detail="把 config/models.yaml 里这一项的 enabled 改成 true 就能启用。",
            )
        return provider


# ----------------------------------------------------------------------
# 工具配置（config/tools.yaml）
# ----------------------------------------------------------------------
class AppEntry(BaseModel):
    """一个允许被打开的应用。path 和 startfile 二选一。"""

    label: str
    path: str | None = None           # 可执行文件的完整路径
    startfile: str | None = None      # 商店应用的启动标识，例如 mspaint
    process: str | None = None        # 用来核实它真的跑起来了，例如 Notepad.exe
    aliases: list[str] = []           # 模型可以用哪些名字来指代它


class ToolPermissions(BaseModel):
    """工具能碰哪些路径。没写进白名单的一律拒绝。"""

    allowed_roots: list[str] = []
    blocked_paths: list[str] = []
    max_file_bytes: int = 200_000
    max_list_entries: int = 200


class FileSystemConfig(BaseModel):
    """只读文件理解（config/tools.yaml 的 filesystem: 段）。

    这是一套**独立于 permissions** 的读取白名单，故意不复用
    permissions.allowed_roots：后者为了打开应用/列目录可能包含整块磁盘，
    拿它来读正文等于把整个盘交给模型。这里留空就是什么都不允许读，
    工具会失败关闭，不会回退到 permissions。
    """

    allowed_roots: list[str] = []
    blocked_paths: list[str] = []
    max_file_bytes: int = 200_000          # 单次读取的源文件字节上限
    max_read_output_chars: int = 400_000   # 带行号格式化后的输出上限
    max_search_files: int = 500            # 一次搜索最多检查多少个文件
    max_search_results: int = 100          # 一次搜索最多返回多少处命中
    max_search_file_bytes: int = 200_000   # 搜索时单个文件最多读多少字节
    max_search_output_chars: int = 20_000  # 搜索结果正文的总长度上限
    max_context_lines: int = 3             # 每处命中前后最多带几行上下文
    max_line_chars: int = 500              # 单行在搜索结果里的最大展示长度
    max_query_chars: int = 200             # 关键字最大长度
    max_path_chars: int = 4096             # 路径字符串最大长度


class CcConfig(BaseModel):
    """Claude Code 人工交接（config/tools.yaml 的 cc: 段）。

    这是一套**独立于 filesystem 和 permissions** 的只读分析白名单。
    本阶段不启动 CC、不调用 CLI、不安装 SDK，只负责准备受限快照和验证
    用户手工放回的 result.json。留空 = 不允许创建任何交接任务。
    """

    enabled: bool = False
    allowed_roots: list[str] = []
    blocked_paths: list[str] = []
    max_files: int = 200
    max_total_bytes: int = 2_000_000
    max_file_bytes: int = 200_000
    max_result_bytes: int = 100_000
    max_paths: int = 20
    max_goal_chars: int = 4_000
    deadline_hours: float = 24.0


class DiscoverySources(BaseModel):
    """每个发现来源的开关。不想让某一类应用进索引就把它关掉。"""

    start_menu: bool = True     # 开始菜单快捷方式（最准）
    app_paths: bool = True      # 注册表 App Paths
    uninstall: bool = True      # 卸载注册表里的安装位置
    install_dirs: bool = True   # 扫描 extra_scan_dirs 列出的目录
    uwp: bool = True            # 商店应用
    manual: bool = True         # tools.yaml 里手工配置的


class DiscoveryConfig(BaseModel):
    """应用自动发现的配置（config/tools.yaml 的 discovery: 段）。"""

    enabled: bool = True
    refresh_on_startup: bool = False   # 默认关，保证启动快
    stale_after_hours: int = 24        # 0 表示从不自动刷新
    scan_depth: int = 2                # 只扫这么多层，不做全盘递归
    sources: DiscoverySources = Field(default_factory=DiscoverySources)
    extra_scan_dirs: list[str] = []    # 额外要扫的软件目录
    exclude_patterns: list[str] = []   # 预留：自定义排除规则
    max_entries: int = 2000            # 索引条数上限，兜底


class LaunchSecurityConfig(BaseModel):
    """启动程序时的安全配置（config/tools.yaml 的 security: 段）。"""

    allow_manual_path: bool = True       # 允不允许你手工提供一个路径
    blocked_executables: list[str] = []  # 在内置黑名单之外再追加（只加不减）


class WorkflowConfig(BaseModel):
    """自主工作流的配置（config/tools.yaml 的 workflow: 段）。"""

    enabled: bool = True
    max_steps: int = 8                # 一个计划最多拆几步
    max_replans: int = 3              # 执行中最多改几次计划
    decide_mode: str = "always"       # always=每步都问模型；on_failure=只在失败时问
    confirm_timeout_seconds: float = 1800.0   # 等你确认/答复最多等多久
    auto_start: bool = True           # 你没明说"规划"时，佳也可以自己判断是多步任务


class VisionConfig(BaseModel):
    """屏幕感知的配置（config/tools.yaml 的 vision: 段）。

    阶段 0 只有"看"：截图和窗口信息。这里没有任何开关能让模型
    越过用户确认去操作电脑——将来加鼠标键盘时，逐次确认仍是默认。
    """

    enabled: bool = True              # 总开关，关掉后 /api/vision/* 一律拒绝
    preview_max_side: int = 1600      # 预览图最长边，只影响预览
    max_windows: int = 200            # 窗口列表最多返回多少条

    # ---- 阶段 1：视觉分析 ----
    provider: str = ""                # 空 = 用 models.yaml 的默认模型
    max_side: int = 1280              # 送给视觉模型的图最长边（只影响分析）
    image_format: str = "jpeg"        # jpeg 体积小、上传快；png 更清晰但大
    jpeg_quality: int = 85
    max_tokens: int = 2000            # 推理模型要给足，否则正文可能是空的
    max_elements: int = 20            # 最多解析多少个界面元素
    # 一条可信观察记录的有效期。超过后坐标校验一律拒绝，要求重新观察。
    observation_ttl_seconds: float = 30.0

    # ---- 窗口捕获（Windows Graphics Capture，capturekit 0.1.0）----
    capture_timeout_seconds: float = 3.0   # 等一帧画面最多多久
    focus_ttl_seconds: float = 120.0       # 「最近使用的窗口」有效期
    focus_poll_seconds: float = 1.0        # 后台追踪前台窗口的间隔
    # 佳自己的聊天页面按标题识别。故意写得很具体：只写 "JiaAgent" 会把
    # 标题为 "JiaAgent MVP 项目架构 - Visual Studio Code" 的编辑器也误伤。
    jia_title_patterns: list[str] = Field(
        default_factory=lambda: [
            "JiaAgent 佳",
            "localhost:8000",
            "127.0.0.1:8000",
        ]
    )
    # 佳自身界面的进程名（精确匹配）。默认空；不要写 electron.exe 这种通用名。
    jia_process_names: list[str] = Field(default_factory=list)

    # ---- 捕获结果的基本有效性检查 ----
    # 全黑、可疑单色、颜色过少都不算成功，宁可报错也不把无效画面当结果。
    max_black_ratio: float = 0.98
    max_uniform_ratio: float = 0.995
    min_distinct_colors: int = 3
    validate_sample_side: int = 128


class InputConfig(BaseModel):
    """真实鼠标输入的配置（config/tools.yaml 的 input: 段）。

    这是**双重启用条件之一**，默认 false。即使这里改成 true，还必须同
    时设置环境变量 JIA_ENABLE_REAL_MOUSE=1，真实后端才会被构造。任何
    前端请求都不能修改这个开关，改了也要重启服务才生效。

    速率限制只能比下面这两个安全值更严格，不能放宽：调度层会再做一次
    max/min 夹取。
    """

    real_mouse_enabled: bool = False
    min_click_interval_seconds: float = 1.0   # 两次真实点击的最小间隔
    max_clicks_per_minute: int = 5            # 每分钟最多真实点击次数


class ToolsConfig(BaseModel):
    """全部工具配置。"""

    apps: dict[str, AppEntry]
    permissions: ToolPermissions
    filesystem: FileSystemConfig = Field(default_factory=FileSystemConfig)
    cc: CcConfig = Field(default_factory=CcConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    security: LaunchSecurityConfig = Field(default_factory=LaunchSecurityConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    input: InputConfig = Field(default_factory=InputConfig)
    # 给"自动发现来的"应用补别名：{索引里的应用名: [别名, ...]}。
    # 有了这一段，就不必为了加个别名去 apps: 里硬写 exe 路径——
    # 硬写的路径会随软件升级/换目录失效，只认名字就不会。
    aliases: dict[str, list[str]] = Field(default_factory=dict)


def expand_path(raw: str) -> Path:
    """把配置里的 ~ 展开成用户主目录，再转成本地绝对路径。

    所有路径在送进权限检查之前都必须先过这一道，
    这样 ".." 之类的相对写法会先被算成绝对路径，藏不掉。
    """
    return Path(os.path.expanduser(raw)).resolve()


def ensure_dirs() -> None:
    """确保 data/ 和 logs/ 目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """读取并校验 config/models.yaml。结果会被缓存，只读一次。"""
    if not CONFIG_FILE.exists():
        raise ConfigError(f"找不到模型配置文件：{CONFIG_FILE}")

    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    raw_providers = raw.get("providers") or {}
    if not raw_providers:
        raise ConfigError("config/models.yaml 里一个模型供应商都没有配置")

    providers: dict[str, ProviderConfig] = {}
    for name, item in raw_providers.items():
        providers[name] = ProviderConfig(name=name, **(item or {}))

    default = str(raw.get("default") or next(iter(providers)))
    if default not in providers:
        raise ConfigError(
            f"models.yaml 里的 default: {default} 不在 providers 列表里",
            detail=f"当前可选的模型有：{', '.join(providers)}",
        )

    return AppConfig(default_provider=default, providers=providers)


@lru_cache(maxsize=1)
def get_tools_config() -> ToolsConfig:
    """读取并校验 config/tools.yaml。结果会被缓存，只读一次。"""
    if not TOOLS_FILE.exists():
        raise ConfigError(
            f"找不到工具配置文件：{TOOLS_FILE}",
            detail="这个文件决定佳能操作你电脑上的哪些东西，不能缺。",
        )

    raw = yaml.safe_load(TOOLS_FILE.read_text(encoding="utf-8")) or {}

    apps: dict[str, AppEntry] = {}
    for name, item in (raw.get("apps") or {}).items():
        apps[name] = AppEntry(**(item or {}))
    if not apps:
        raise ConfigError("config/tools.yaml 里没有配置任何可打开的应用")

    permissions = ToolPermissions(**(raw.get("permissions") or {}))
    if not permissions.allowed_roots:
        raise ConfigError(
            "config/tools.yaml 里没有配置 allowed_roots",
            detail="这是路径白名单。为安全起见，留空表示什么都不允许访问。",
        )

    # 只读文件理解的独立白名单。不写这一段的默认值是空列表，也就是拒绝
    # 所有正文读取；它**不会**回退到 permissions.allowed_roots。
    filesystem = FileSystemConfig(**(raw.get("filesystem") or {}))

    # Claude Code 人工交接的独立白名单。默认关闭 + 空列表；它不会回退到
    # filesystem 或 permissions。写错也只会让交接失败关闭。
    cc = CcConfig(**(raw.get("cc") or {}))

    # 应用发现和启动安全的配置段。这两个段可以不写，不写就用默认值。
    discovery = DiscoveryConfig(**(raw.get("discovery") or {}))
    security = LaunchSecurityConfig(**(raw.get("security") or {}))
    workflow = WorkflowConfig(**(raw.get("workflow") or {}))
    vision = VisionConfig(**(raw.get("vision") or {}))
    input_cfg = InputConfig(**(raw.get("input") or {}))

    # 别名表：容忍写成单个字符串（微信: wechat）或一个列表
    aliases: dict[str, list[str]] = {}
    for name, values in (raw.get("aliases") or {}).items():
        if isinstance(values, str):
            values = [values]
        aliases[str(name)] = [str(v) for v in (values or []) if str(v).strip()]

    return ToolsConfig(
        apps=apps,
        permissions=permissions,
        filesystem=filesystem,
        cc=cc,
        discovery=discovery,
        security=security,
        workflow=workflow,
        vision=vision,
        input=input_cfg,
        aliases=aliases,
    )
