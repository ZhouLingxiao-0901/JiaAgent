"""按名字找应用。

支持的写法：中文名、英文名、常用简称、拼写错误、全角半角混写。

**能力边界要说清楚**：纯标准库没有拼音表，所以「weixin -> 微信」
这类跨语种匹配做不到（那需要一个很大的拼音数据文件）。能做的靠别名表覆盖——
`config/tools.yaml` 里可以给应用写 aliases，手工配置的别名永远管用。

这个模块只做一件事：给一个名字，返回最像的几个候选。
不碰磁盘、不去重、不判断安全（安全是 guard 的事）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.apps.model import AppRecord, Candidate, Source, normalize

# 各种匹配方式的分数。差距是有意拉开的，好让"完全一样"稳稳压过"碰巧包含"。
_EXACT = 1.00
_PREFIX = 0.88
_CONTAINS = 0.78
_CONTAINED_BY = 0.72
_FUZZY_MIN = 0.70
_FUZZY_SCALE = 0.85
_MANUAL_BONUS = 0.05

# 唯一匹配的判定
_CONFIDENT = 0.90        # 到这个分数，直接认定就是它
_CLEAR_WIN = 0.10        # 比第二名高出这么多，也算它赢
_MIN_SCORE = 0.60        # 低于这个分数的不当作候选


@dataclass
class MatchResult:
    """匹配结果。status 只有四种：unique / ambiguous / none / empty。"""

    status: str
    query: str = ""
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def message(self) -> str:
        if self.status == "empty":
            return "你还没说应用的名字呢。"
        if self.status == "none":
            return (
                f"没找到和「{self.query}」对得上的应用。"
                "你可以换个说法，或者告诉我它的完整路径。"
            )
        best = self.best
        assert best is not None
        if self.status == "unique":
            return f"找到 {best.record.name}"
        names = "、".join(c.record.name for c in self.candidates[:5])
        return f"「{self.query}」对上了好几个应用：{names}。你要打开哪一个？"


def _compare(query: str, target: str, label: str) -> tuple[float, str] | None:
    """把查询词和某个名字比一下，返回（分数, 理由）。比不上就返回 None。"""
    if not target:
        return None
    if query == target:
        return _EXACT, f"{label}完全一样"
    if target.startswith(query):
        return _PREFIX, f"{label}以它开头"
    if query in target:
        return _CONTAINS, f"{label}里包含它"
    if target in query:
        return _CONTAINED_BY, f"它包含在{label}里"
    ratio = SequenceMatcher(None, query, target).ratio()
    if ratio >= _FUZZY_MIN:
        return ratio * _FUZZY_SCALE, f"{label}拼写接近（{ratio:.0%}）"
    return None


def score(query: str, record: AppRecord) -> Candidate | None:
    """给一条记录打分。比不上返回 None。"""
    if not record.enabled:
        return None
    q = normalize(query)
    if not q:
        return None

    best: tuple[float, str] | None = None
    for value, label in (
        (record.name_norm, "名字"),
        (record.identity_norm, "程序文件名"),
    ):
        hit = _compare(q, value, label)
        if hit and (best is None or hit[0] > best[0]):
            best = hit
    for alias in record.alias_norms:
        hit = _compare(q, alias, "别名")
        if hit and (best is None or hit[0] > best[0]):
            best = hit

    if best is None:
        return None

    value, reason = best
    if record.source is Source.MANUAL:
        # 手工配置的优先——那是你亲自指定过的。
        # 注意：这里**不能**再套 min(value, 1.0)。手工加分后可能超过 1.0，
        # 那正是它要的——让手工条目在同名（同为完全匹配）时压过其它来源。
        # 一旦封顶，手工加分就被抹平，和商店应用打成 1.0 平手，白加了。
        value += _MANUAL_BONUS
        reason += "，手工配置优先"
        return Candidate(record=record, score=value, reason=reason)
    return Candidate(record=record, score=min(value, 1.0), reason=reason)


def match(query: str, records: list[AppRecord], limit: int = 5) -> MatchResult:
    """在索引里找 query。

    返回三种情况：
      unique     —— 就一个，放心用
      ambiguous  —— 好几个都像，得让用户挑
      none       —— 一个都不像，如实说找不到
    """
    q = normalize(query)
    if not q:
        return MatchResult(status="empty", query=query)

    scored = [c for c in (score(query, r) for r in records) if c and c.score >= _MIN_SCORE]
    if not scored:
        return MatchResult(status="none", query=query)

    # 分数高的在前；同分时名字短的在前（"计算器"应当压过"计算器帮助"）
    scored.sort(key=lambda c: (-c.score, len(c.record.name), c.record.name))

    top = scored[0]
    if top.score >= _CONFIDENT:
        return MatchResult(status="unique", query=query, candidates=[top])
    if len(scored) == 1:
        return MatchResult(status="unique", query=query, candidates=[top])
    if top.score - scored[1].score >= _CLEAR_WIN:
        return MatchResult(status="unique", query=query, candidates=[top])
    return MatchResult(status="ambiguous", query=query, candidates=scored[:limit])


def search(query: str, records: list[AppRecord], limit: int = 20) -> list[Candidate]:
    """给模型用的搜索：返回一列候选，不做"唯不唯一"的判断。"""
    scored = [c for c in (score(query, r) for r in records) if c]
    scored.sort(key=lambda c: (-c.score, len(c.record.name), c.record.name))
    return scored[:limit]
