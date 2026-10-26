"""检查配置是否就绪。启动项目之前先跑这个，能省掉很多麻烦。

用法（在项目根目录下）：
    .venv\\Scripts\\python.exe scripts\\check_config.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能找到 app 包（本文件在 <项目根>/scripts/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.errors import JiaAgentError  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.settings import (  # noqa: E402
    CONFIG_FILE,
    DATA_DIR,
    ENV_FILE,
    LOG_DIR,
    PROJECT_ROOT,
    TOOLS_FILE,
    ensure_dirs,
    expand_path,
    get_config,
    get_secrets,
    get_tools_config,
)

OK = "[OK]"
BAD = "[!!]"


def main() -> int:
    ensure_dirs()
    setup_logging("WARNING")

    print("=" * 58)
    print("  JiaAgent 配置检查")
    print("=" * 58)
    print(f"项目根目录 : {PROJECT_ROOT}")
    print(f"模型配置   : {CONFIG_FILE}  {OK if CONFIG_FILE.exists() else BAD + ' 文件缺失'}")
    print(f".env 文件  : {ENV_FILE}  {OK if ENV_FILE.exists() else BAD + ' 请从 .env.example 复制一份'}")
    print(f"数据目录   : {DATA_DIR}")
    print(f"日志目录   : {LOG_DIR}")
    print()

    try:
        config = get_config()
    except JiaAgentError as exc:
        print(f"{BAD} 读取配置失败：{exc.message}")
        if exc.detail:
            print(f"     {exc.detail}")
        return 1

    secrets = get_secrets()

    print(f"默认模型：{config.default_provider}")
    print()
    print(f"{'':2}{'名称':<12}{'模型':<22}{'Key 状态'}")
    print("-" * 58)
    for name, provider in config.providers.items():
        mark = "*" if name == config.default_provider else " "
        key_state = "已配置" if secrets.key_for(provider.api_key_env) else "未配置"
        note = "" if provider.enabled else "（已禁用）"
        print(f"{mark:2}{name:<12}{provider.model:<22}{key_state}{note}")
    print()
    print("  （标 * 的是默认模型）")
    print()

    # ---------------- 工具配置 ----------------
    print()
    print("工具配置：")
    print(f"   配置文件：{TOOLS_FILE}  {'[OK]' if TOOLS_FILE.exists() else '[缺失]'}")
    try:
        tools = get_tools_config()
        labels = [a.label for a in tools.apps.values()]
        print(f"   可打开的应用（{len(labels)} 个）：{'、'.join(labels)}")
        print(f"   允许访问的目录（{len(tools.permissions.allowed_roots)} 个）：")
        for root in tools.permissions.allowed_roots:
            print(f"      {expand_path(root)}")
        print(f"   额外禁止的路径：{len(tools.permissions.blocked_paths)} 条")
        print(f"   单个文本文件上限：{tools.permissions.max_file_bytes:,} 字节")
    except JiaAgentError as exc:
        print(f"   [!!] {exc.message}")
        if exc.detail:
            print(f"        {exc.detail}")
    print()

    default_provider = config.provider()
    if secrets.key_for(default_provider.api_key_env):
        print(f"{OK} 默认模型的 Key 已就绪，可以启动项目了：")
        print("     .venv\\Scripts\\python.exe run.py")
        return 0

    print(f"{BAD} 还没有给默认模型 {config.default_provider} 配置 API Key。")
    print("     请打开项目根目录的 .env 文件，找到这一行并填入你的 Key：")
    print(f"         {default_provider.api_key_env}=你的Key")
    print("     填完保存，再重新运行一次本检查脚本。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
