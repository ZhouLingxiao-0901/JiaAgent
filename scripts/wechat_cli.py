"""Jia-WeChat 本机管理 CLI。

只做管理动作：二维码登录、配对/取消配对、查看状态。
**不会**自动登录，也不会启动监听。监听由 Jia 主服务在
`JIA_WECHAT_ENABLED=1` 时启动。

用法：
  .venv\\Scripts\\python.exe scripts\\wechat_cli.py login
  .venv\\Scripts\\python.exe scripts\\wechat_cli.py pair --user <ilink_user_id>
  .venv\\Scripts\\python.exe scripts\\wechat_cli.py list
  .venv\\Scripts\\python.exe scripts\\wechat_cli.py status

登录成功不等于授权：必须显式 pair，且只按可靠的 ilink_user_id 精确匹配。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.wechat.admin import logout_account, unpair_user  # noqa: E402
from app.wechat.api import ApiConfig, WeChatApi  # noqa: E402
from app.wechat.credentials import (  # noqa: E402
    AllowListStore,
    CredentialStore,
    DpapiProtector,
)
from app.wechat.errors import CredentialError, WeChatError  # noqa: E402
from app.wechat.login import login_with_qr  # noqa: E402
from app.wechat.redact import redact_id  # noqa: E402
from app.wechat.runtime import WeChatRuntimeConfig  # noqa: E402
from app.wechat.runtime_state import runtime_status_text  # noqa: E402
from app.wechat.store import WeChatStore  # noqa: E402


def _config() -> WeChatRuntimeConfig:
    return WeChatRuntimeConfig.from_env()


def _account_or_fail(config: WeChatRuntimeConfig, store: CredentialStore) -> str:
    if config.account_id:
        return config.account_id
    accounts = store.list_accounts()
    if len(accounts) == 1:
        return accounts[0]
    if not accounts:
        raise CredentialError("还没有登录过任何微信账号，请先运行 login")
    raise CredentialError("有多个微信账号，请加 --account 指定一个")


def cmd_login(args: argparse.Namespace) -> int:
    config = _config()
    protector = DpapiProtector()
    credentials_store = CredentialStore(config.state_dir, protector)
    api = WeChatApi(
        ApiConfig(
            base_url="https://ilinkai.weixin.qq.com",
            token="",
            channel_version=config.channel_version,
            client_version=config.client_version,
            bot_agent=config.bot_agent,
        )
    )
    credentials = asyncio.run(login_with_qr(
        api=api,
        credential_store=credentials_store,
        emit=print,
        ask_code=input,
    ))
    print()
    print(f"账号 ID：{credentials.account_id}")
    print("下一步（必须显式配对，登录本身不授权）：")
    if credentials.user_id:
        print(
            "  .venv\\Scripts\\python.exe scripts\\wechat_cli.py pair "
            f"--account {credentials.account_id} --user {credentials.user_id}"
        )
    else:
        print(
            "  服务端没有返回扫码用户 ID；请用该微信用户的 ilink_user_id 运行 pair。"
        )
    print("配对后，在 .env 里设置 JIA_WECHAT_ENABLED=1 再重启 Jia。")
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    config = _config()
    protector = DpapiProtector()
    credentials_store = CredentialStore(config.state_dir, protector)
    account = args.account or _account_or_fail(config, credentials_store)
    if credentials_store.load(account) is None:
        raise CredentialError(f"账号 {account} 还没有登录凭证")
    raw_scopes = str(args.scopes)
    parts = [s.strip() for s in raw_scopes.split(",")]
    if not raw_scopes.strip() or any(not part for part in parts):
        raise CredentialError("作用域格式不对，只能是 chat,task,approve 中的值")
    scopes = tuple(parts)
    entry = AllowListStore(config.state_dir).add(
        account_id=account,
        user_id=args.user,
        label=args.label or "",
        scopes=scopes,
    )
    print(f"已配对：{entry.account_id} <- {entry.user_id}")
    print(f"作用域：{', '.join(entry.scopes)}")
    print("重启 Jia（或等待微信入口重启）后生效。")
    return 0


def cmd_unpair(args: argparse.Namespace) -> int:
    config = _config()
    protector = DpapiProtector()
    credentials_store = CredentialStore(config.state_dir, protector)
    account = args.account or _account_or_fail(config, credentials_store)
    result = unpair_user(
        config.state_dir, account_id=account, user_id=args.user
    )
    print("已移除配对。" if result.removed else "没有找到这个配对。")
    print(f"已撤销该用户未使用的授权码：{result.revoked_codes} 个。")
    print("注意：取消配对不会取消正在运行的任务，也不会删除任务归属。")
    print("如需停止任务，请让用户在微信发送 /cancel，或在电脑端显式取消。")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    config = _config()
    protector = DpapiProtector()
    result = logout_account(
        config.state_dir, account_id=args.account, protector=protector
    )
    print(f"已删除账号 {result.account_id} 的本机 DPAPI 凭证。")
    print(f"已撤销该账号未使用的授权码：{result.revoked_codes} 个。")
    print("允许名单和任务归属没有删除；运行中的任务不会因此被取消。")
    print(
        "注意：以上只是本机凭证删除，不等于微信服务端 token 已失效或账号已解绑。"
    )
    print(
        "本工具没有调用未经确认的服务端解绑接口；如需服务端失效，"
        "请在微信客户端中按官方流程处理。"
    )
    return 0


def cmd_sender_hash(args: argparse.Namespace) -> int:
    """显式本地诊断：把用户 ID 换成本机日志里的同一个哈希短标识。"""
    print(redact_id(args.user))
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    config = _config()
    entries = AllowListStore(config.state_dir).list()
    if not entries:
        print("允许名单是空的（没有微信用户可以给 Jia 发消息）。")
        return 0
    for entry in entries:
        label = f"  {entry.label}" if entry.label else ""
        print(
            f"{entry.account_id}  {entry.user_id}  "
            f"[{', '.join(entry.scopes)}]{label}"
        )
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    config = _config()
    print(f"启用：{'是' if config.enabled else '否（JIA_WECHAT_ENABLED）'}")
    print(f"状态目录：{config.state_dir}")
    print(runtime_status_text(config.state_dir))
    try:
        protector = DpapiProtector()
        credentials_store = CredentialStore(config.state_dir, protector)
        accounts = credentials_store.list_accounts()
        print(f"已登录账号：{', '.join(accounts) if accounts else '（无）'}")
    except WeChatError as exc:
        print(f"凭证检查失败：{type(exc).__name__}")
        accounts = []
    entries = AllowListStore(config.state_dir).list()
    print(f"已配对用户：{len(entries)}")
    db_path = Path(config.state_dir) / "state.db"
    if db_path.exists():
        try:
            store = WeChatStore(db_path)
            store.init()
            for account in accounts:
                state = store.account_state(account)
                if state:
                    print(
                        f"账号 {account} 游标：{'有' if state.get('cursor') else '无'}"
                        f"；暂停到：{state.get('paused_until', 0)}"
                    )
        except Exception as exc:  # noqa: BLE001
            print(f"状态库读取失败：{type(exc).__name__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jia-WeChat 本机管理（不会自动登录或启动监听）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="显示二维码并登录微信 Bot 账号")

    pair = sub.add_parser("pair", help="把一个微信用户加入允许名单")
    pair.add_argument("--account", default="", help="Bot 账号 ID；只有一个账号时可省略")
    pair.add_argument("--user", required=True, help="可靠的 ilink_user_id")
    pair.add_argument("--label", default="", help="备注名（只用于本机显示）")
    pair.add_argument(
        "--scopes", default="chat",
        help="作用域，逗号分隔：chat,task,approve（默认仅 chat）",
    )

    unpair = sub.add_parser("unpair", help="从允许名单移除并撤销该用户未使用的授权码")
    unpair.add_argument("--account", default="")
    unpair.add_argument("--user", required=True)

    logout = sub.add_parser(
        "logout", help="删除本机 DPAPI 凭证（不等于服务端解绑）"
    )
    logout.add_argument("--account", required=True, help="要退出登录的账号 ID")

    sender_hash = sub.add_parser(
        "sender-hash", help="本地诊断：显示日志里使用的发送者哈希短标识"
    )
    sender_hash.add_argument("--user", required=True, help="要换算的微信用户 ID")

    sub.add_parser("list", help="列出允许名单")
    sub.add_parser("status", help="查看凭证/配对/暂停状态（不显示 token）")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING)
    args = build_parser().parse_args(argv)
    handlers = {
        "login": cmd_login,
        "pair": cmd_pair,
        "unpair": cmd_unpair,
        "logout": cmd_logout,
        "sender-hash": cmd_sender_hash,
        "list": cmd_list,
        "status": cmd_status,
    }
    try:
        return int(handlers[args.command](args))
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except WeChatError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI 不打印 traceback/敏感细节
        print(f"失败：{type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
