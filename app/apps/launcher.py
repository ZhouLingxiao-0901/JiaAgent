"""启动一个应用，并核实它到底起来了没有。

这是"真正动电脑"的地方，也是安全模型里**闸门二**所在：
即使某条目已经进了索引，启动前还要再查一遍危险程序黑名单——
索引和启动是两层，不能因为进了索引就放行。

⚠️ 这里**不调用 policy.check_path()**。原因写清楚，免得以后有人"顺手修好"：
  现在的 allowed_roots 里没有 C:\\Program Files，而且 C:\\Windows 在黑名单里，
  但发现的绝大多数应用恰恰装在这些地方。**启动程序和读写文件是两回事**：
    - 读写文件：必须在 allowed_roots 内（policy.check_path 管这件事）
    - 启动程序：信任边界是「索引 + 守卫 + 每次用户确认」
  如果哪天有人把 check_path 加进来"修 bug"，绝大多数应用就再也打不开了。

启动全程不走 shell：可执行文件直接给 subprocess.Popen（列表传参，参数、空格、
中文路径都不会被拆坏），快捷方式/商店应用/系统标识用 os.startfile 交给系统。
模型只报应用名、给不出路径，所以想借启动功能执行任意命令是行不通的。

返回 LaunchOutcome，三态分清楚：
  CONFIRMED   —— 有证据确认它真的起来了
  UNCONFIRMED —— 发出了请求，但拿不到证据，不能打包票
  FAILED      —— 确实失败了，或者被守卫拦下了
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

from app.apps import guard
from app.apps.model import AppRecord, LaunchKind, LaunchOutcome, LaunchStatus
from app.tools.win_process import find_process

logger = logging.getLogger(__name__)

# 启动后等多久，用来确认它没有立刻崩掉
_START_CHECK_SECONDS = 1.5


async def launch(record: AppRecord) -> LaunchOutcome:
    """启动一条索引记录并核实结果。绝不编造成功。"""
    kind = record.launch_kind
    target = record.target

    # ---- 闸门二：启动前再查一遍（纵深防御） ----
    allowed, reason = guard.check_target(target, kind)
    if not allowed:
        logger.warning("启动被守卫拦截：%s -> %s", record.name, reason)
        return LaunchOutcome(status=LaunchStatus.FAILED, message=reason)

    process = None
    if kind is LaunchKind.EXE:
        exe = target
        if not os.path.isfile(exe):
            return LaunchOutcome(
                status=LaunchStatus.FAILED,
                message=f"找不到这个程序：{exe}。它可能已经被卸载或移动了。",
            )
        try:
            # 不走 shell：路径用列表传，参数也是列表，空格/中文路径不会被拆坏。
            # 独立用户应用不通过 stdout/stderr 回传结果；显式接到 NUL，避免
            # 它们继承 Jia 启动日志的管道写端，导致启动包装层在 Jia 退出后
            # 仍等不到 EOF。参数、工作目录和窗口启动方式都不变。
            process = subprocess.Popen(
                [exe, *(record.args or [])],
                cwd=record.workdir or None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError as exc:
            return LaunchOutcome(
                status=LaunchStatus.FAILED,
                message=f"启动 {record.name} 失败：{type(exc).__name__}: {exc}",
            )
    elif kind in (LaunchKind.LNK, LaunchKind.AUMID, LaunchKind.STARTFILE):
        if not hasattr(os, "startfile"):
            return LaunchOutcome(
                status=LaunchStatus.FAILED,
                message="这个系统不支持用 startfile 启动应用。",
            )
        launch_target = (
            "shell:AppsFolder\\" + target if kind is LaunchKind.AUMID else target
        )
        try:
            os.startfile(launch_target)
        except OSError as exc:
            return LaunchOutcome(
                status=LaunchStatus.FAILED,
                message=f"启动 {record.name} 失败：{type(exc).__name__}: {exc}",
            )

    # ---- 启动之后必须核实。系统接受了请求，不等于窗口真的开了。 ----
    await asyncio.sleep(_START_CHECK_SECONDS)

    # 一、Popen 起的进程如果立刻以非零码退出，那是真失败
    if process is not None:
        code = process.poll()
        if code is not None and code != 0:
            return LaunchOutcome(
                status=LaunchStatus.FAILED,
                message=f"{record.name} 启动后立刻退出，返回码 {code}，没有正常运行。",
                pid=process.pid,
            )

    # 二、按进程名去进程列表里找。不能只看我们启动的那个进程还在不在：
    #     记事本、计算器这类程序启动后会把活转交给另一个进程，自己退出。
    hint = record.process_hint
    if hint:
        found = find_process(hint)
        if found:
            return LaunchOutcome(
                status=LaunchStatus.CONFIRMED,
                message=f"已启动 {record.name}，并在进程列表里确认 {hint} 正在运行（进程号 {found[0]}）。",
                pid=found[0],
            )
        # 找不到。可能进程名归不到自己，也可能真的没开起来。
        return LaunchOutcome(
            status=LaunchStatus.UNCONFIRMED,
            message=(
                f"已向系统发出启动 {record.name} 的请求，但在进程列表里没找到 {hint}，"
                "所以无法确认它是否真的打开了。请你看看窗口有没有出现。"
            ),
        )

    # 三、没有进程名可核对（例如商店应用常跑在系统宿主进程里）
    return LaunchOutcome(
        status=LaunchStatus.UNCONFIRMED,
        message=(
            f"已向系统发出启动 {record.name} 的请求，但拿不到进程证据，"
            "无法确认窗口是否出现。请你看看有没有打开。"
        ),
    )
