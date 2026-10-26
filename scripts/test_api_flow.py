"""接口层端到端测试（阶段二第 6 步）。

前提：先启动服务（另开窗口运行  .venv\\Scripts\\python.exe run.py）。
本脚本会：
  1. 走 /api/chat/stream 发「打开记事本」，半路收到 need_confirm 后点「允许」，
     验证记事本真的被打开、tool_result 返回成功；
  2. 再发「打开计算器」，这次点「拒绝」，验证什么都没发生；
  3. 最后查 /sessions/{id}/messages?include_tool=1，确认工具记录已入库。

用法：
  .venv\\Scripts\\python.exe scripts\\test_api_flow.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:8000"

# 本机地址绝不要走系统代理。Windows 上装了 Clash 这类软件后，连 127.0.0.1
# 的请求也会被扭送到代理，拿回一个 502，非常难排查。app/settings.py 里有同样
# 的处理，但这个脚本不导入 app，所以自己设一遍（否则有代理就必然连不上）。
os.environ["NO_PROXY"] = ",".join(
    filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")])
)
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)


def post_json(path: str, payload: dict | None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=(json.dumps(payload).encode("utf-8") if payload is not None else None),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def stream_chat(message: str, decision: bool | None) -> dict:
    """发一条消息，边收 SSE 边处理。

    decision 为 True/False 时，一看到授权请求就立刻答复；None 表示这一轮
    不该出现授权。
    遇到「选择题」（kind=choose：工具对上了多个候选）时自动选第一个，
    并把过程打出来——这条链路要能真的走通，不能只是有个接口摆着。
    """
    req = urllib.request.Request(
        BASE + "/api/chat/stream",
        data=json.dumps({"message": message}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    session_id = None
    saw_confirm = False
    saw_choose = False
    chosen_name = ""
    tool_ok = None
    tool_output = ""
    final_seen = False

    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:].strip())
            t = event["type"]
            if t == "start":
                session_id = event["session_id"]
                print(f"  [start] 会话 {session_id}，模型 {event['model']}")
            elif t == "delta":
                print(event["text"], end="", flush=True)
            elif t == "need_confirm":
                action = event["action"]
                action_id = action["id"]
                if action.get("kind") == "choose":
                    saw_choose = True
                    options = action.get("options") or []
                    print(f"\n  [need_confirm:choose] {action.get('question', '')}")
                    for opt in options:
                        print(f"        - {opt['name']}（{opt['source']}）{opt['target']}")
                    if options:
                        chosen_name = options[0]["name"]
                        reply = post_json(
                            f"/api/pending/{action_id}/choose",
                            {"option_id": options[0]["id"]},
                        )
                        print(f"  [选择] 选了「{chosen_name}」 -> {reply}")
                else:
                    saw_confirm = True
                    print(f"\n  [need_confirm] {action['summary']}")
                    if decision is not None:
                        path = f"/api/pending/{action_id}/approve" if decision else \
                               f"/api/pending/{action_id}/reject"
                        reply = post_json(path, None)
                        print(f"  [答复] {'允许' if decision else '拒绝'} -> {reply}")
            elif t == "tool_start":
                print(f"\n  [tool_start] {event['summary']}")
            elif t == "tool_result":
                tool_ok = event["ok"]
                tool_output = event["output"]
                print(f"  [tool_result] ok={event['ok']}")
                print(f"      {event['output'][:300]}")
            elif t == "final":
                final_seen = True
            elif t == "done":
                print("\n  [done]")
            elif t == "error":
                print(f"\n  [error] {event.get('message', '')} {event.get('detail', '')}")
        print()

    return {
        "session_id": session_id,
        "saw_confirm": saw_confirm,
        "saw_choose": saw_choose,
        "chosen_name": chosen_name,
        "tool_ok": tool_ok,
        "tool_output": tool_output,
        "final_seen": final_seen,
    }


def main() -> int:
    # 先确认服务活着
    try:
        health = get_json("/api/health")
    except Exception as exc:  # noqa: BLE001
        print(f"连不上服务（{exc}）。请先另开窗口运行 run.py 再跑本脚本。")
        return 1
    print(f"服务状态：{health}\n")

    print("=" * 60)
    print("用例 1：打开记事本（允许）")
    r1 = stream_chat("帮我打开记事本", decision=True)
    ok1 = r1["saw_confirm"] and r1["tool_ok"] is True and "记事本" in (r1["tool_output"] or "")
    print(f"  -> {'通过' if ok1 else '不通过'}：收到授权={r1['saw_confirm']}，执行成功={r1['tool_ok']}")

    print("=" * 60)
    print("用例 2：打开计算器（拒绝）")
    r2 = stream_chat("帮我打开计算器", decision=False)
    ok2 = r2["saw_confirm"] and r2["tool_ok"] is False
    print(f"  -> {'通过' if ok2 else '不通过'}：收到授权={r2['saw_confirm']}，执行被拒={r2['tool_ok'] is False}")

    print("=" * 60)
    print("用例 3：历史里应包含工具记录")
    if not r1["session_id"]:
        print("  -> 跳过（用例 1 没有拿到会话 ID）")
    else:
        data = get_json(f"/api/sessions/{r1['session_id']}/messages?include_tool=1")
        roles = [m["role"] for m in data["messages"]]
        has_tool = "tool" in roles
        print(f"  消息角色序列：{roles}")
        print(f"  -> {'通过' if has_tool else '不通过'}：{'有' if has_tool else '没有'} tool 记录")

    print("=" * 60)
    print("用例 4：伪造的选择编号必须被拒（确定性，不依赖模型）")
    # 注意：编号必须用 ASCII。中文放进 URL 路径会让 urllib 在编码请求行时
    # 直接炸掉（UnicodeEncodeError），那是测试自己的问题，不是接口的。
    bad = post_json("/api/pending/no-such-pending-id/choose", {"option_id": "whatever"})
    ok4 = bad.get("ok") is False
    print(f"  -> {'通过' if ok4 else '不通过'}：{bad}")

    print("=" * 60)
    print("用例 5：名字对上多个候选时，先让用户选（尽力而为）")
    print("  说明：让模型碰巧说到一个会歧义的名字不是每次都能如愿，")
    print("  所以这一条只在真出现选择卡片时才判定，否则如实标为跳过。")
    r5 = stream_chat("打开 iCloud", decision=True)
    if r5["saw_choose"]:
        ok5 = r5["tool_ok"] is not None
        print(f"  -> {'通过' if ok5 else '不通过'}：出现选择卡片，选了「{r5['chosen_name']}」")
    else:
        ok5 = True
        print("  -> 本次模型没有产生歧义，跳过（不算失败）")

    print()
    print("=" * 60)
    all_ok = ok1 and ok2 and ok4 and ok5 and (has_tool if r1["session_id"] else True)
    print("结论：", "全部通过 ✅" if all_ok else "有失败项，请看上面")
    print("请确认：记事本窗口真的弹出来了；计算器没有弹出。")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
