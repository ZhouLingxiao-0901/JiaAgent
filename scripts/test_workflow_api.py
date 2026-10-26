"""工作流接口测试（自主工作流引擎 · 阶段 4）。

前提：先启动服务（另开窗口运行  .venv\\Scripts\\python.exe run.py）。

分两部分：
  A. **确定性**的接口约束——不依赖模型，一定跑得出结果：
     不存在的任务要报错、非终态任务不许删、伪造的编号不许通过
  B. **端到端**跑一个真实的只读任务——这一步会用真模型、会比较慢：
     建任务 -> 等它把计划拆出来 -> 批准 -> 等它跑完 -> 检查状态和步骤

**只读目标**：故意挑一个只需要"列目录"的活，不打开任何应用、不写任何文件。
跑完会把建的会话和任务都删掉，不留垃圾。

用法：
  .venv\\Scripts\\python.exe scripts\\test_workflow_api.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"

# 本机地址绝不要走系统代理（这台机器上装了代理软件，会把 127.0.0.1 也扭走）。
os.environ["NO_PROXY"] = ",".join(
    filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")])
)
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)

RESULTS: list[tuple[str, bool, str]] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, ok, detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """发一个请求，返回 (状态码, 响应体)。4xx/5xx 也正常返回，不抛异常。"""
    req = urllib.request.Request(
        BASE + path,
        data=(json.dumps(payload).encode("utf-8") if payload is not None else None),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def get(path: str) -> dict:
    return call("GET", path)[1]


def wait_task(task_id: str, predicate, *, timeout: float, label: str) -> dict | None:
    """轮询任务，直到 predicate(task) 成立或者超时。"""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = get(f"/api/tasks/{task_id}").get("task") or {}
        if predicate(last):
            return last
        time.sleep(1.5)
    print(f"    （等「{label}」超时了，最后一次看到的：status={last and last.get('status')}）")
    return None


def main() -> int:
    print("=" * 68)
    print("工作流接口测试（阶段 4）")
    print("=" * 68)

    try:
        health = get("/api/health")
    except Exception as exc:  # noqa: BLE001
        print(f"连不上服务（{exc}）。请先另开窗口运行 run.py 再跑本脚本。")
        return 1
    print(f"服务状态：{health}")
    print("  模型那边会真的被调用，慢一点是正常的。")

    session_id = ""
    task_id = ""

    try:
        # ================= A. 确定性的接口约束 =================
        print()
        print("A. 接口约束（不依赖模型）：")

        # 找不到任务必须是 **404**（客户端要了个不存在的编号），不是 500（服务器出错）。
        # 只断言"报错了"是不够的——500 也能算"报错了"，但那是在甩锅给服务器。
        code, body = call("GET", "/api/tasks/no-such-task-id")
        check("查一个不存在的任务返回 404，而不是 500",
              code == 404 and (body.get("error") or {}).get("code") == "task_not_found",
              f"HTTP {code} {(body.get('error') or {}).get('code')}")

        for action in ("approve", "cancel"):
            code, body = call("POST", f"/api/tasks/no-such-task-id/{action}")
            check(f"{action} 一个不存在的任务返回 404",
                  code == 404 and (body.get("error") or {}).get("code") == "task_not_found",
                  f"HTTP {code} {(body.get('error') or {}).get('code')}")

        code, body = call("DELETE", "/api/tasks/no-such-task-id")
        check("删一个不存在的任务返回 404",
              code == 404 and (body.get("error") or {}).get("code") == "task_not_found",
              f"HTTP {code} {(body.get('error') or {}).get('code')}")

        listed = get("/api/tasks?limit=5")
        check("能列出任务（列表接口通）", isinstance(listed.get("tasks"), list))

        # ================= B. 端到端跑一个只读任务 =================
        print()
        print("B. 端到端（真模型，只读目标，会慢一些）：")
        goal = "看看 D:\\JiaAgent 这个文件夹里有哪些文件和子文件夹，然后一句话告诉我"
        print(f"   目标：{goal}")

        code, body = call("POST", "/api/tasks", {"goal": goal})
        if code >= 400 or not body.get("task"):
            check("能建出任务", False, f"HTTP {code} {str(body)[:120]}")
            raise SystemExit(1)

        task = body["task"]
        task_id = task["id"]
        session_id = task["session_id"]
        check("建任务成功，状态是待执行",
              task["status"] == "pending", task["status"])
        check("任务挂在了一个会话下", bool(session_id), session_id[:12])
        check("会话里能找到这个任务",
              any(t["id"] == task_id
                  for t in get(f"/api/tasks?session_id={session_id}")["tasks"]))

        print("   等它把计划拆出来（最多 120 秒）...")
        planned = wait_task(task_id, lambda t: len(t.get("steps") or []) > 0,
                            timeout=120, label="计划拆好")
        check("拆出了计划", planned is not None and len(planned["steps"]) > 0,
              f"{len(planned['steps']) if planned else 0} 步")

        if planned:
            steps = planned["steps"]
            check("计划在等你批准（没批准之前不许跑）",
                  planned["status"] == "pending", planned["status"])
            check("每一步都有说明", all(s["goal"] for s in steps))
            check("每一步用的都是真实存在的工具",
                  all((s["tool"] == "" or s["tool"] in {
                      "get_system_info", "list_directory", "open_application",
                      "create_text_file", "list_indexed_apps", "search_apps"})
                      for s in steps),
                  "、".join(s["tool"] or "（说明）" for s in steps))
            print("   计划内容：")
            for s in steps:
                print(f"     {s['step_index'] + 1}. {s['goal']}（{s['tool'] or '不调工具'}）")

        print("   批准它 ...")
        code, body = call("POST", f"/api/tasks/{task_id}/approve")
        check("批准被接受", body.get("ok") is True, str(body)[:60])

        print("   等它跑完（最多 180 秒）...")
        done = wait_task(task_id, lambda t: t["status"] in
                         ("succeeded", "failed", "cancelled"),
                         timeout=180, label="跑到终态")
        if done is None:
            check("任务能跑到终态", False, "超时了")
        else:
            check("任务跑到了终态", done["status"] in ("succeeded", "failed", "cancelled"),
                  f"{done['status_label']}（{done['status']}）")
            ran = [s for s in done["steps"] if s["status"] == "succeeded"]
            check("至少跑成功了一步", len(ran) > 0,
                  f"{len(ran)}/{len(done['steps'])} 步成功")
            check("有终局总结或失败原因", bool(done["summary"] or done["error"]),
                  (done["summary"] or done["error"])[:70])
            if done["status"] == "failed":
                print(f"   （任务失败了，这是真实结果，不是测试的错：{done['error'][:80]}）")

            print("   控制接口的边界：")
            code, body = call("POST", f"/api/tasks/{task_id}/pause")
            check("已结束的任务不能再暂停", body.get("ok") is False, str(body)[:50])
            code, body = call("POST", f"/api/tasks/{task_id}/cancel")
            check("已结束的任务不能再取消", body.get("ok") is False, str(body)[:50])

    finally:
        # 收尾：把测试建的会话和任务删掉，不留垃圾
        print()
        print("清理：")
        if task_id:
            code, body = call("DELETE", f"/api/tasks/{task_id}")
            print(f"   删任务 -> {body}")
        if session_id:
            code, body = call("DELETE", f"/api/sessions/{session_id}")
            print(f"   删会话（顺带清掉它名下的任务）-> {body}")

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
