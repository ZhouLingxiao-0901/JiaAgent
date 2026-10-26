"""首次真实点击验收的就绪检查（离线、只读）。

这个脚本只做三件事：

  1. 静态检查 `web/input_acceptance.html`：确认它是无网络、无外部资源、
     只有一个按钮和一个计数的独立页面；
  2. 检查 `docs/first-click-acceptance.md` 是否包含必需的人工验收要点；
  3. 读取当前配置/环境变量，确认真实输入的两个启用条件都**没有**满足，
     且登记处里的点击工具没有真实后端。

它不会启动服务、不会访问网络、不会读写 data/ 下的状态文件（只会读取）、
不会调用任何 Windows 输入 API，也不会修改配置。它检查的是**当前这个
进程**读到的配置；正在运行的服务是否同状态，请以网页上的状态接口为准。

用法：
    .venv\\Scripts\\python.exe scripts\\check_first_click_readiness.py
"""

from __future__ import annotations

import html.parser
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api.security import is_loopback_host  # noqa: E402
from app.input import control  # noqa: E402
from app.input.windows_backend import ENV_REAL_MOUSE, real_mouse_enablement  # noqa: E402
from app.settings import WEB_DIR, get_secrets, get_tools_config  # noqa: E402

PAGE = WEB_DIR / "input_acceptance.html"
DOC = ROOT / "docs" / "first-click-acceptance.md"

RESULTS: list[tuple[str, bool, str]] = []
WARNINGS: list[str] = []


def check(note: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((note, bool(ok), detail))
    mark = "[OK]" if ok else "[!!]"
    print(f"  {mark} {note}" + (f"　{detail}" if detail else ""))


def warn(note: str) -> None:
    WARNINGS.append(note)
    print(f"  [--] {note}")


# ----------------------------------------------------------------------
# 页面静态检查
# ----------------------------------------------------------------------
_RESOURCE_ATTRS = ("src", "href", "action", "data", "poster", "srcset")
_FORBIDDEN_TAGS = {
    "img", "iframe", "frame", "object", "embed", "source", "track",
    "video", "audio", "canvas", "form", "a",
}
_FORBIDDEN_JS = (
    "fetch", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon",
    "localStorage", "sessionStorage", "indexedDB", "document.cookie",
    "window.open", "location.href", "location.assign", "location.replace",
    "import(", "eval(", "Function(", "innerHTML", "outerHTML",
    "document.write", "classList", "setAttribute", "history.",
)


class PageAudit(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.ids: set[str] = set()
        self.buttons: list[dict[str, str]] = []
        self.scripts: list[dict[str, str]] = []
        self.styles: list[dict[str, str]] = []
        self.metas: list[dict[str, str]] = []
        self.titles: list[str] = []
        self.headings: list[str] = []
        self.external_refs: list[str] = []
        self._in_script = False
        self._in_style = False
        self._in_title = False
        self._in_heading = False
        self.script_text: list[str] = []
        self.style_text: list[str] = []
        self.title_text = ""
        self.heading_text = ""
        self._heading_buf: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001
        tag = tag.casefold()
        values = {
            str(name).casefold(): str(value or "")
            for name, value in attrs
        }
        self.tags.append((tag, values))
        if "id" in values:
            self.ids.add(values["id"])
        if tag == "button":
            self.buttons.append(values)
        elif tag == "script":
            self.scripts.append(values)
            self._in_script = True
        elif tag == "style":
            self.styles.append(values)
            self._in_style = True
        elif tag == "meta":
            self.metas.append(values)
        elif tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2"}:
            self._in_heading = True
            self._heading_buf = []

        rel = values.get("rel", "").casefold()
        for attr in _RESOURCE_ATTRS:
            value = values.get(attr, "").strip()
            if not value:
                continue
            # 唯一允许的是内联 data: 图标，浏览器不会因此发网络请求。
            if (
                tag == "link"
                and attr == "href"
                and value.startswith("data:")
                and "icon" in rel
            ):
                continue
            self.external_refs.append(f"<{tag} {attr}={value[:80]!r}>")
        if tag == "meta" and values.get("http-equiv", "").casefold() == "refresh":
            self.external_refs.append("<meta http-equiv=refresh>")

    def handle_endtag(self, tag):  # noqa: ANN001
        tag = tag.casefold()
        if tag == "script":
            self._in_script = False
        elif tag == "style":
            self._in_style = False
        elif tag == "title":
            self._in_title = False
        elif tag in {"h1", "h2"}:
            self._in_heading = False
            self.heading_text = "".join(self._heading_buf).strip()
            self.headings.append(self.heading_text)

    def handle_data(self, data):  # noqa: ANN001
        if self._in_script:
            self.script_text.append(data)
        elif self._in_style:
            self.style_text.append(data)
        elif self._in_title:
            self.title_text += data
        elif self._in_heading:
            self._heading_buf.append(data)


def check_page() -> bool:
    print("\n[一] 测试页面")
    if not PAGE.exists():
        check("验收页面存在", False, str(PAGE))
        return False
    raw = PAGE.read_text(encoding="utf-8")
    audit = PageAudit()
    audit.feed(raw)

    check("验收页面存在且非空", bool(raw.strip()), f"{len(raw)} 字符")
    check("没有外部资源或网络地址", not audit.external_refs,
          ", ".join(audit.external_refs[:3]))
    check("没有需要浏览器的资源标签",
          not [tag for tag, _ in audit.tags if tag in _FORBIDDEN_TAGS])

    csp = [
        meta.get("content", "")
        for meta in audit.metas
        if meta.get("http-equiv", "").casefold() == "content-security-policy"
    ]
    check("设置了 CSP 禁止默认加载", bool(csp))
    check("CSP 禁止网络连接", any("connect-src 'none'" in item for item in csp),
          csp[0][:80] if csp else "")
    check("CSP 只允许内联脚本和样式",
          any(
              "default-src 'none'" in item
              and "script-src 'unsafe-inline'" in item
              and "style-src 'unsafe-inline'" in item
              for item in csp
          ))

    check("页面标题能区分验收窗口",
          "验收" in audit.title_text and "佳" in audit.title_text,
          audit.title_text.strip())
    check("页面有清晰的验收标题",
          any("验收" in item for item in audit.headings))
    check("只有一个测试按钮", len(audit.buttons) == 1,
          f"{len(audit.buttons)} 个")
    button = audit.buttons[0] if audit.buttons else {}
    check("按钮有稳定编号", button.get("id") == "acceptance-button", button.get("id", ""))
    check("按钮文字明确是测试按钮",
          "测试按钮" in raw and "单击" in raw)
    check("页面显示初始点击次数 0",
          bool(re.search(r'id="click-count"[^>]*>\s*0\s*<', raw)))
    check("没有表单", not [tag for tag, _ in audit.tags if tag == "form"])

    script = "".join(audit.script_text)
    style = "".join(audit.style_text)
    forbidden = [name for name in _FORBIDDEN_JS if name in script]
    check("内联脚本没有网络或持久化调用", not forbidden, ", ".join(forbidden))
    check("脚本没有导航或改写 HTML",
          not any(item in script for item in
                  ("location.", "window.open", "innerHTML", "outerHTML",
                   "document.write", "eval(")))
    check("脚本只使用 textContent 更新计数",
          "textContent" in script and "click-count" in script)
    check("只有一个 click 监听器",
          len(re.findall(r"addEventListener\s*\(\s*['\"]click", script)) == 1)
    check("没有内联 onclick", "onclick" not in raw.casefold())
    check("样式没有外部引用",
          "url(" not in style.casefold() and "@import" not in style.casefold())
    check("原始 HTML 没有 http(s) 地址",
          not re.search(r"https?://", raw, re.IGNORECASE))
    return True


# ----------------------------------------------------------------------
# 手册检查
# ----------------------------------------------------------------------
_DOC_TERMS = (
    ("可丢弃的 Windows 虚拟机", "建议优先使用可丢弃的 Windows 虚拟机"),
    ("待保存", "确保没有重要文件处于待保存状态"),
    ("测试页面已打开", "确认测试页面已打开"),
    ("两个既有条件", "真实输入需要两个既有条件"),
    ("JIA_ENABLE_REAL_MOUSE=1", "环境变量条件"),
    ("input.real_mouse_enabled: true", "配置条件"),
    ("不能代替", "不能用恢复急停代替启用条件"),
    ("每次点击仍需用户单独确认", "逐次授权要求"),
    ("只执行一次左键单击", "单次点击要求"),
    ("从 0 变为 1", "计数验收"),
    ("结果未知", "未知结果处理"),
    ("立即停止", "停止要求"),
    ("不允许重试", "禁止重试"),
    ("关闭真实输入", "验收后关闭"),
    ("急停", "急停状态检查"),
    ("未验证", "未验证项标记"),
)


def check_doc() -> None:
    print("\n[二] 验收手册")
    if not DOC.exists():
        check("验收手册存在", False, str(DOC))
        return
    text = DOC.read_text(encoding="utf-8")
    check("验收手册存在且非空", bool(text.strip()), f"{len(text)} 字符")
    missing = [note for phrase, note in _DOC_TERMS if phrase not in text]
    check("手册覆盖必需的人工验收要点", not missing, "；".join(missing))


# ----------------------------------------------------------------------
# 真实输入关闭状态
# ----------------------------------------------------------------------
def check_disabled() -> None:
    print("\n[三] 真实输入双重启用条件")
    config = get_tools_config().input
    decision = real_mouse_enablement()
    check("配置 input.real_mouse_enabled 为 false",
          config.real_mouse_enabled is False,
          str(config.real_mouse_enabled))
    check(f"环境变量 {ENV_REAL_MOUSE} 不是 1",
          decision.env_enabled is False)
    check("双重启用条件当前未满足", decision.allowed is False, decision.reason)

    # 只有两个条件都关闭时才导入登记处，确保不会构造真实后端。
    if decision.allowed:
        check("准备阶段不应继续；请先关闭真实输入",
              False, "当前进程读到的条件已被满足")
        return

    from app.tools.registry import registry

    tool = registry.get("click_element")
    backend = getattr(tool, "_backend", None)
    check("点击工具已登记", tool is not None)
    check("登记处没有构造真实输入后端", backend is None,
          type(backend).__name__ if backend is not None else "None")


# ----------------------------------------------------------------------
# 急停与页面地址
# ----------------------------------------------------------------------
def check_state() -> None:
    print("\n[四] 当前状态与访问地址")
    state = control.emergency.snapshot()
    if state.stopped:
        warn(
            "急停当前处于生效状态。验收开始前请在本机页面显式恢复；"
            "恢复不会启用真实输入。"
        )
        check("急停状态可读取", True,
              f"{state.reason or '（没有填写原因）'}（{state.source}）")
    else:
        check("急停当前未生效", True)

    rate = control.limiter.snapshot()
    check("速率限制是保守的硬上限",
          rate["min_interval_seconds"] >= 1.0
          and rate["max_per_minute"] <= 5,
          f"{rate['min_interval_seconds']:g} 秒 / {rate['max_per_minute']} 次每分钟")

    secrets = get_secrets()
    host = str(secrets.jia_host)
    if not is_loopback_host(host):
        display = "127.0.0.1"   # run.py 会拒绝这种绑定；这里只给出本机可用地址
    elif ":" in host and not host.startswith("["):
        display = f"[{host}]"
    else:
        display = host
    print(f"  [--] 准备好后打开：http://{display}:{secrets.jia_port}/input_acceptance.html")
    print("  [--] 本脚本不访问这个地址，也不会确认浏览器里实际打开了哪个页面。")


# ----------------------------------------------------------------------
def main() -> int:
    print("=" * 72)
    print("首次真实点击验收：就绪检查（离线、只读、不点击、不改配置）")
    print("=" * 72)

    page_ok = check_page()
    check_doc()
    check_disabled()
    check_state()

    failed = [item for item in RESULTS if not item[1]]
    print("\n" + "=" * 72)
    print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    for note, _, detail in failed:
        print(f"  [失败] {note}" + (f"　{detail}" if detail else ""))
    if WARNINGS:
        print(f"提示：{len(WARNINGS)} 条，请按上面的 [--] 处理。")
    if not failed and page_ok:
        print("检查通过：可以做人工验收准备；实际点击必须由你另行决定并逐次授权。")
    return 0 if not failed and page_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
