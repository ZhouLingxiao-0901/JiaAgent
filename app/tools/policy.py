"""路径权限检查：工具能不能碰某个路径，全由这里说了算。

设计上只有这一道关卡。任何涉及文件路径的工具，第一步都必须调用
check_path() 或 check_writable_file()，拿到批准的绝对路径后才能动手。
工具本体不允许自己解析路径、不允许自己判断权限。

三层判断，依次是：
  1. 先展开路径（~ 和 .. 都算清楚，藏不掉）
  2. 黑名单：命中就拒绝，优先级最高
  3. 白名单：不在允许目录里的拒绝

只要有一层不过，就抛 PathNotAllowed，工具会把这句话原样告诉模型。
"""

from __future__ import annotations

import logging

from app.errors import JiaAgentError
from app.settings import expand_path, get_tools_config

logger = logging.getLogger(__name__)


class PathNotAllowed(JiaAgentError):
    """路径不在允许范围内，或者踩到了禁止规则。"""

    code = "path_not_allowed"
    http_status = 403


def _norm(path: str) -> str:
    """把路径变成可以比较的形式：斜杠统一、去掉结尾分隔符、全部转小写。

    Windows 的路径不区分大小写，D:\\Temp 和 d:\\temp 是同一个地方。
    不统一的话白名单就是形同虚设。
    """
    text = str(path).replace("/", "\\")
    return text.rstrip("\\").lower()


def _is_inside(child: str, parent: str) -> bool:
    """child 是不是 parent 本身，或者是它下面的东西。

    这里不能用简单的字符串 startswith 判断：
    D:\\abc 和 D:\\abcdef 是两个不相干的地方，但前者是后者的前缀。
    所以要求 parent 后面必须紧跟一个反斜杠才算数。
    """
    if child == parent:
        return True
    return child.startswith(parent + "\\")


def check_path(raw: str, *, must_be_dir: bool = False, must_exist: bool = True) -> str:
    """校验一个路径。通过就返回展开后的绝对路径字符串，否则抛 PathNotAllowed。

    raw          原始路径（可能带 ~、可能是相对路径、可能带 ..）
    must_be_dir  是否要求必须是文件夹
    must_exist   是否要求必须已经存在（创建新文件时用 False）
    """
    if not raw or not str(raw).strip():
        raise PathNotAllowed("没有给出路径")

    text = str(raw).strip()
    try:
        target = expand_path(text)
    except (OSError, ValueError) as exc:
        raise PathNotAllowed(
            f"这个路径看不懂：{text}", detail=f"原始错误：{exc}"
        ) from exc

    permissions = get_tools_config().permissions
    target_norm = _norm(target)

    # ---- 第一层：黑名单 ----
    for blocked_raw in permissions.blocked_paths:
        blocked = _norm(expand_path(blocked_raw))
        if _is_inside(target_norm, blocked):
            logger.warning("路径被黑名单拦截：%s（命中规则 %s）", target, blocked_raw)
            raise PathNotAllowed(
                f"这个位置不允许访问：{target}",
                detail=f"它命中了 config/tools.yaml 里 blocked_paths 的规则「{blocked_raw}」。",
            )

    # ---- 第二层：白名单 ----
    allowed = [(root, _norm(expand_path(root))) for root in permissions.allowed_roots]
    if not any(_is_inside(target_norm, norm) for _, norm in allowed):
        readable = "、".join(str(expand_path(root)) for root, _ in allowed)
        logger.warning("路径被白名单拦截：%s", target)
        raise PathNotAllowed(
            f"这个位置不在允许访问的范围内：{target}",
            detail=f"目前只允许访问：{readable}。要增加请改 config/tools.yaml 的 allowed_roots。",
        )

    # ---- 第三层：存在性和类型 ----
    if must_exist and not target.exists():
        raise PathNotAllowed(f"这个路径不存在：{target}")
    if must_be_dir and target.exists() and not target.is_dir():
        raise PathNotAllowed(f"这不是一个文件夹：{target}")

    return str(target)


def check_writable_file(raw: str) -> str:
    """校验一个"准备写入的文件"路径。

    文件本身可以还不存在，但它所在的文件夹也要单独过一遍检查。
    这一步是为了堵住 D:\\允许的目录\\..\\..\\别的地方\\x.txt 这种绕法：
    即使最终文件路径看起来合规，父目录也会被单独验证一次。
    """
    target = check_path(raw, must_exist=False)

    from pathlib import Path

    path = Path(target)
    if path.exists() and path.is_dir():
        raise PathNotAllowed(
            f"这是一个文件夹，不能当文件写：{target}",
            detail="请换一个文件名，例如在这个文件夹下面新建一个文件。",
        )

    parent = path.parent
    if parent != path:
        check_path(str(parent), must_be_dir=True, must_exist=False)

    return target


def max_file_bytes() -> int:
    """单个文本文件最多允许多少字节。"""
    return get_tools_config().permissions.max_file_bytes


def max_list_entries() -> int:
    """列目录时最多返回多少条。"""
    return get_tools_config().permissions.max_list_entries
