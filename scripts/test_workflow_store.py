"""工作流数据层测试（自主工作流引擎 · 阶段 1）。

全部在**临时数据库**上跑，不碰 data/jiaagent.db，不联网，不启动任何程序。

守三件事：
  1. 可注入 db_path —— 这是把 DDL 内联进 workflow/store.py 的全部理由，
     测不了这一点就白内联了
  2. 状态机的合法性 —— 非法的转移必须被**拒绝**，不能悄悄写进去
  3. 再规划只动能改的步骤 —— 已经跑过的步骤绝不能被抹掉

用法：
  .venv\\Scripts\\python.exe scripts\\test_workflow_store.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.workflow import store  # noqa: E402
from app.workflow.model import (  # noqa: E402
    TERMINAL_TASK_STATUSES,
    StepStatus,
    TaskStatus,
    can_step_transition,
    can_transition,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def main() -> int:
    print("=" * 68)
    print("工作流数据层测试（阶段 1）")
    print("=" * 68)

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "workflow.db"

        # ---------------- 建表 ----------------
        print()
        print("建表与可注入的 db_path：")
        task = store.create_task("sess-1", "把 D 盘那几个文件夹看一眼，写个笔记",
                                 db_path=db)
        check("临时库里能建出任务", bool(task.id), task.id[:12])
        check("任务默认是待执行", task.status is TaskStatus.PENDING)
        check("标题自动从目标里取", bool(task.title), task.title)
        check("db_path 真的被用上了（临时库文件已生成）", db.exists())

        # 幂等：再调一次不该炸
        again = store.list_tasks(db_path=db)
        check("重复操作不会因为建表报错（DDL 幂等）", len(again) == 1)

        # ---------------- 任务增删改查 ----------------
        print()
        print("任务的增删改查：")
        got = store.get_task(task.id, db_path=db)
        check("能按编号读回来", got is not None and got.goal == task.goal)

        store.update_task(task.id, title="看一眼 D 盘", db_path=db)
        check("能改标题", store.get_task(task.id, db_path=db).title == "看一眼 D 盘")

        store.update_task(task.id, status=TaskStatus.RUNNING, db_path=db)
        check("能改成执行中", store.get_task(task.id, db_path=db).status is TaskStatus.RUNNING)

        store.update_task(task.id, paused=True, db_path=db)
        check("暂停是独立字段，不影响状态",
              store.get_task(task.id, db_path=db).paused
              and store.get_task(task.id, db_path=db).status is TaskStatus.RUNNING)

        store.update_task(task.id, paused=False, db_path=db)

        other = store.create_task("sess-2", "另一个任务", db_path=db)
        check("能按会话过滤",
              [t.id for t in store.list_tasks(session_id="sess-1", db_path=db)] == [task.id])
        check("能按状态过滤",
              len(store.list_tasks(status=TaskStatus.RUNNING, db_path=db)) == 1)

        try:
            store.update_task(task.id, goal="偷偷改目标", db_path=db)
            rejected = False
        except ValueError:
            rejected = True
        check("不允许改白名单以外的字段", rejected, "goal 不在可改列表里")

        # ---------------- 状态机 ----------------
        print()
        print("状态机：合法的放行，非法的拒绝：")
        check("待执行 -> 执行中", can_transition(TaskStatus.PENDING, TaskStatus.RUNNING))
        check("执行中 -> 等待确认", can_transition(TaskStatus.RUNNING, TaskStatus.WAITING_CONFIRMATION))
        check("等待确认 -> 执行中", can_transition(TaskStatus.WAITING_CONFIRMATION, TaskStatus.RUNNING))
        check("执行中 -> 成功", can_transition(TaskStatus.RUNNING, TaskStatus.SUCCEEDED))
        check("成功是终态，出不去",
              all(not can_transition(TaskStatus.SUCCEEDED, s) for s in TaskStatus))
        check("待执行不能直接跳到成功",
              not can_transition(TaskStatus.PENDING, TaskStatus.SUCCEEDED))
        check("失败后不能复活成执行中",
              not can_transition(TaskStatus.FAILED, TaskStatus.RUNNING))
        check("终态集合只有三个",
              TERMINAL_TASK_STATUSES == {TaskStatus.SUCCEEDED, TaskStatus.FAILED,
                                         TaskStatus.CANCELLED})

        # 数据库层也要真的挡住
        done = store.create_task("sess-1", "已经完成的任务", db_path=db)
        store.update_task(done.id, status=TaskStatus.RUNNING, db_path=db)
        store.update_task(done.id, status=TaskStatus.SUCCEEDED, db_path=db)
        ok = store.update_task(done.id, status=TaskStatus.RUNNING, db_path=db)
        check("已经成功的任务改不回执行中（被拒）", ok is False)
        check("状态确实没被改回去",
              store.get_task(done.id, db_path=db).status is TaskStatus.SUCCEEDED)

        check("步骤状态机：成功是终态",
              not can_step_transition(StepStatus.SUCCEEDED, StepStatus.RUNNING))
        check("步骤状态机：待执行 -> 已跳过（用户选择跳过）",
              can_step_transition(StepStatus.PENDING, StepStatus.SKIPPED))

        # ---------------- 步骤 ----------------
        print()
        print("步骤的读写：")
        ids = store.add_steps(task.id, [
            {"goal": "列出 D 盘顶层目录", "tool": "list_directory",
             "arguments": {"path": "D:\\"}, "expect": "看到文件夹列表"},
            {"goal": "挑一个文件夹看看里面", "tool": "list_directory",
             "arguments": {"path": "D:\\cc-AI"}},
            {"goal": "把结论写成笔记文件", "tool": "create_text_file",
             "arguments": {"path": "D:\\笔记.txt", "content": "…"}},
        ], db_path=db)
        check("批量加了三步", len(ids) == 3)

        steps = store.list_steps(task.id, db_path=db)
        check("步骤按顺序排", [s.step_index for s in steps] == [0, 1, 2])
        check("arguments 存进去又读出来还是 dict",
              steps[0].arguments == {"path": "D:\\"})
        check("每步默认待执行", all(s.status is StepStatus.PENDING for s in steps))

        nxt = store.next_step(task.id, db_path=db)
        check("下一个该跑的是第一步", nxt is not None and nxt.step_index == 0)

        store.mark_step_running(steps[0].id, db_path=db)
        fresh = store.list_steps(task.id, db_path=db)[0]
        check("开始跑一步：状态变执行中、尝试数 +1、记了开始时间",
              fresh.status is StepStatus.RUNNING and fresh.attempts == 1
              and bool(fresh.started_at))

        store.update_step(steps[0].id, status=StepStatus.SUCCEEDED,
                          result="看到 12 个文件夹", db_path=db)
        fresh = store.list_steps(task.id, db_path=db)[0]
        check("跑完一步：写了真实结果", fresh.result == "看到 12 个文件夹" and fresh.status is StepStatus.SUCCEEDED)

        nxt = store.next_step(task.id, db_path=db)
        check("下一步自动挪到第二步", nxt is not None and nxt.step_index == 1)

        long_text = "x" * 5000
        store.update_step(steps[0].id, result=long_text, db_path=db)
        trimmed = store.list_steps(task.id, db_path=db)[0].result
        check("过长的结果会被截断（免得撑爆面板和上下文）",
              len(trimmed) < 5000 and "已截断" in trimmed, f"{len(trimmed)} 字符")

        # ---------------- 再规划 ----------------
        print()
        print("再规划：只换还没跑的步骤，跑过的一律留着：")
        store.mark_step_running(steps[1].id, db_path=db)
        store.update_step(steps[1].id, status=StepStatus.SUCCEEDED, result="第二步也好了",
                          db_path=db)
        replaced = store.replace_pending_steps(
            task.id,
            [{"goal": "换个思路再试一次", "tool": "list_directory",
              "arguments": {"path": "D:\\other"}}],
            from_index=2, db_path=db,
        )
        check("替换返回新步骤的编号", len(replaced) == 1)
        now_steps = store.list_steps(task.id, db_path=db)
        check("前两步还在（跑过的不能被抹掉）",
              [s.step_index for s in now_steps][:2] == [0, 1])
        check("第二步的结果也还在",
              now_steps[1].result == "第二步也好了")
        check("被替换的第三步变成了新计划",
              any(s.goal == "换个思路再试一次" for s in now_steps))
        check("总数没膨胀（旧的待执行步被删掉了）", len(now_steps) == 3, f"{len(now_steps)} 步")

        # ---------------- 启动对账 ----------------
        print()
        print("启动对账：服务重启后，还在跑的任务不能自动续跑：")
        # 先把前面那个任务收尾。它一直是"执行中"，不收尾的话会被对账一起抓到，
        # 计数就不是干净的 2 了（对账本身没做错，是这里要的是精确值）。
        store.update_task(task.id, status=TaskStatus.SUCCEEDED, db_path=db)

        stuck = store.create_task("sess-3", "跑到一半服务挂了", db_path=db)
        store.update_task(stuck.id, status=TaskStatus.RUNNING, db_path=db)
        store.add_steps(stuck.id, [{"goal": "开个应用", "tool": "open_application"}],
                        db_path=db)

        waiting = store.create_task("sess-3", "正在等你确认", db_path=db)
        store.update_task(waiting.id, status=TaskStatus.RUNNING, db_path=db)
        store.update_task(waiting.id, status=TaskStatus.WAITING_CONFIRMATION, db_path=db)

        n = store.mark_interrupted_as_failed(db_path=db)
        check("两个残留任务都被对账到了", n == 2, f"{n} 个")
        check("执行中的被标成失败",
              store.get_task(stuck.id, db_path=db).status is TaskStatus.FAILED)
        check("失败原因写清楚了（用户看得到）",
              "重启" in store.get_task(stuck.id, db_path=db).error)
        check("等待确认的也被标成失败",
              store.get_task(waiting.id, db_path=db).status is TaskStatus.FAILED)
        check("它没跑完的步骤被标成已取消",
              all(s.status is StepStatus.CANCELLED
                  for s in store.list_steps(stuck.id, db_path=db)))

        # 已经终态的不该被动到
        check("已经成功的任务不受影响",
              store.get_task(done.id, db_path=db).status is TaskStatus.SUCCEEDED)

        # ---------------- 删除 ----------------
        print()
        print("删除：")
        before = len(store.list_tasks(db_path=db))
        removed = store.delete_tasks_for_session("sess-2", db_path=db)
        check("按会话删除能删掉", removed == 1)
        check("总数少了一个", len(store.list_tasks(db_path=db)) == before - 1)
        check("删了的任务读不到了", store.get_task(other.id, db_path=db) is None)
        check("它的步骤也一起没了", store.list_steps(other.id, db_path=db) == [])

        check("按编号删除", store.delete_task(done.id, db_path=db) is True)
        check("删不存在的返回 False", store.delete_task("不存在", db_path=db) is False)

    # ---------------- 主库没被动过 ----------------
    print()
    print("隔离性检查：")
    from app.memory.db import DB_PATH

    print(f"  主库位置：{DB_PATH}")
    check("临时测试没有污染主库（主库里不该有测试会话）",
          all(t.session_id not in ("sess-1", "sess-2", "sess-3")
              for t in store.list_tasks(session_id="sess-1")))

    print()
    print("=" * 68)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    if passed != total:
        print(f"结果：{passed}/{total} 通过，{total - passed} 项不通过")
        for note, ok, detail in RESULTS:
            if not ok:
                print(f"  [!!] {note}　{detail}")
        return 1
    print(f"结果：{passed}/{total} 全部通过 [OK]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
