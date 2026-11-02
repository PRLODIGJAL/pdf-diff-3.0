# -*- coding: utf-8 -*-
"""
pdfdiff_group.py —— 把「逐条差异」聚成「一段段差异」
==================================================================
`pdfdiff_core` 交出来的清单是**逐行**的：中栏新增一整节会变成 10 条相邻的
「新增」，界面上就是 10 行一条条往下滚 —— 用户看的是「文档改了哪几段」，
不是「哪些行不一样」（2026-09-29 原话：「我不想要他一句一句地这样展现，
而是想要他像最开始一样一段一段地展现，告诉你哪一大段增加了，哪一大段被删减了」）。

这里做两件事，都不碰 PDF：

  1. **聚段** —— 把位置紧邻的相邻条目并成一段（`group_paragraphs`）；
  2. **备料** —— 每段带上旧/新两侧的整段文本 + 前后文，供界面直接渲染。

⚠️ 本模块**只管分组**，不判断「改的是不是同一处」—— 那是 `core` 的职责。
分段的依据只有**版面位置**（页 / 纵坐标 / 横栏）与**差异语义**（一删一增 = 一次替换）。

规则表
------
| # | 判据 | 常量 | 取值依据 |
|---|---|---|---|
| 1 | 必须同页、同层级（text / graphic / table / page / meta） | — | 跨页的内容不该并成一段 |
| 1a | ↳ 例外：`text` 与 `graphic` 互为同一层级 | `SIBLING_SCOPES` | 转曲区就是「用曲线画的正文」，版面上与正文上下相接时本是一整段 |
| 2 | `text`：纵坐标差 ≤ `PARA_GAP_PT` | 20.0pt | 正文行距约 11~13pt，段间空行 ≥ 22pt；取 20 卡在中间 |
| 2a | ↳ 跨 `text`/`graphic` 时改用**区间间隙** ≤ `PARA_GAP_PT` | 20.0pt | 转曲区是一整块（高 44pt）、正文是一行（高 6pt），比「上边界差」差 60pt 像隔了一整段，比「间隙」只差 15.6pt —— 是紧挨着的 |
| 3 | `text`：左边界差 ≤ `PARA_X_PT` | 50.0pt | 同一栏内的行首缩进（`♦ ` 之类）约 10~20pt，并排两栏差 200pt+ |
| 4 | ⚠️ 坐标**必须同侧**才比（旧侧比旧侧、新侧比新侧） | — | 改版会整段上移：旧侧 y=375 与新侧 y=373 只差 2pt，却是页面两处 |
| 5 | 两条都缺同侧坐标（一删一增）→ 只在**一次替换**时并段 | — | diff 引擎把「改成别的」拆成删+增，它们本来就是一处 |
| 5a | ↳ 横向必须同栏（左边界差 ≤ `PARA_X_PT`） | 50.0pt | 排序只看纵向，不同位置的一删一增会被排到一起 |
| 5b | ↳ 纵向也必须挨着（y 差 ≤ `PARA_GAP_PT`） | 20.0pt | 同一栏上下两处「一删一增」同样会误并（相距 210pt 也并过） |
| 6 | `table`：同一张表且行号差 ≤ `PARA_ROW_GAP` | 8 | 表格一条 = 一个单元格，同行/相邻行才是同一处改动 |
| 7 | `page`（整页增删）、`meta`（文档属性） | — | 各自成段，不与其他任何东西合并 |
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------- 规则常量
PARA_GAP_PT = 20.0      # 规则 2：同栏内纵向间距 ≤ 它 → 同一段
PARA_X_PT = 50.0        # 规则 3：左边界差 ≤ 它 → 同一栏
PARA_ROW_GAP = 8        # 规则 6：表格行号差 ≤ 它 → 同一段

#: 规则 1a：互为「同一层级」的来源对。
#: 转曲区（`graphic`）不是另一种内容 —— 它就是**用矢量曲线画出来的正文**，
#: 只是没有文字层（见 `pdfdiff_core.GRAPHIC_CTX`）。同一栏里上下相接的
#: 「转曲区 + 正文」在文档里本来就是**一整段**，硬拆成两段正是用户在
#: 2026-09-30 指出的问题：「可以放在一起分析差异，因为 pdf 中挨得很近」。
SIBLING_SCOPES = frozenset({"text", "graphic"})
#: `SIBLING_SCOPES` 的排序权重：一段里正文总是主体，写「所在位置」时排前面。
SCOPE_WEIGHT = {"text": 0, "table": 1, "graphic": 2, "page": 3, "meta": 4}

#: 多来源段里**追加**来源用的短名（首个来源仍用原文案，见 `_ctx_of`）。
#: 为什么不全拼两条完整文案：`所在位置` 列只有 ~110px，两条拼起来 190px+，
#: 界面上会被**从中间截断**（实测「第1页 正文 + 转曲」），关键是那个词还是看不全。
SCOPE_SHORT = {
    "text": "正文", "table": "表格", "graphic": "转曲区",
    "page": "整页", "meta": "文档属性",
}

# 段内条目数 -> 规模说明（界面「规模」列）
SCALE_TPL = "{n} 行"

# 段类型词汇：单类型沿用 core 的 kind，混合类型归成下面几个
MIXED_KINDS = ("mixed",)          # 「一删一增」这种
PARA_LABEL = {
    "added": "新增", "removed": "删除",
    "moved": "位置移动",
    "page_added": "新增页", "page_removed": "删除页",
    "mixed": "增删",
}

_BY_PAGE_ONLY = ("meta", "page")   # 规则 7：各自成段


def para_label(kind: str) -> str:
    """段类型的中文名（界面直接用）。"""
    return PARA_LABEL.get(kind, kind)


# ---------------------------------------------------------------- 数值判定（遗留工具）
# ⚠️ 2026-10-13 删「变动」档后，这三个函数**不再产生任何用户可见的「数值变动」档**：
#    所有内容差异都拆成「删 + 增」两条。它们作为纯逻辑工具保留（`parse_number` 的
#    数字抓取、`numeric_delta` 的骨架比对），供自检与将来可能的独立数值视图复用，
#    但**不再参与归桶 / 上色 / 清单档位**。核心判定链已与它们解耦。
#
# **同一件事只能有一份判据**（本项目反复踩过的坑：同一个判断在两个模块里各写一份，
# 越改越不像）。这三个函数原本长在 `pdfdiff_core` 里，但 core 拖着
# pdfminer / pdfplumber，而标注链路（`pdf_annotate` → `diff_preview`）既不读 PDF、
# 也不该被它拖住 —— 按项目惯例把**纯逻辑**规则上移到本模块，core 反向 import 复用。
# 搬家时逻辑一字未改；core 自检里那几条数值用例就是这次搬家的验收凭据。
#
# 判据是「两侧骨架完全一致、只有数字不同」：
#   `温度 150°C` → `温度 180°C`     ✓ 数值不同
#   `温度 150°C` → `压力 150Bar`    ✗ 骨架变了
#   `WI-2001 rev01` → `WI-2001 rev02` ✗ 只抓第一个数字，两边都是 2001
#   `6.5mm` → `7.0mm`               ✓（用户当初举的例子）
#
# 数字本身不受约束，「压力10Bar」照样读出 10；符号（-/+）只有前面不是
# 字母/汉字/数字时才算正负号，免得 `WI-2001` 被读成 -2001
_NUM_RE = re.compile(
    r"(?:(?<![A-Za-z0-9\u4e00-\u9fff])[-+])?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?")


def parse_number(s: str) -> Optional[float]:
    """从文本里抓第一个数字；`φ4.0mm` -> 4.0，`50%` -> 50.0。抓不到返回 None。"""
    if not s:
        return None
    m = _NUM_RE.search(s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def strip_number(s: str) -> str:
    """去掉数字后的文本骨架，用于判断「只有数字变了」。"""
    if not s:
        return ""
    return _NUM_RE.sub("#", s)


def numeric_delta(old: str, new: str) -> Optional[Dict[str, Any]]:
    """两边都含数字且骨架高度一致 -> 返回增减信息（遗留工具，不再产「数值变动」档）。"""
    a, b = parse_number(old), parse_number(new)
    if a is None or b is None:
        return None
    if a == b:
        return None          # 数字没变，动的是别处，交给文本「修改」判定
    if strip_number(old) != strip_number(new):
        return None          # 骨架也变了，不是单纯改数值
    delta = b - a
    pct = None
    if a != 0:
        pct = delta / abs(a) * 100.0
    return {"old_num": a, "new_num": b, "delta": delta, "pct": pct}


# ---------------------------------------------------------------- 段
@dataclass
class Para:
    """一段差异：若干条相邻的同位置条目 + 两侧整段文本 + 前后文。"""
    items: List[Any] = field(default_factory=list)
    kind: str = "mixed"
    page: Optional[int] = None
    scope: str = "text"
    context: str = ""
    before: str = ""          # 段前紧邻的一行原文（页面上的上一行）
    after: str = ""           # 段后紧邻的一行原文
    old_text: str = ""        # 段内所有条目的旧侧内容（按行拼）
    new_text: str = ""        # 段内所有条目的新侧内容（按行拼）
    note: str = ""            # 首个非空备注
    sources: tuple = ()       # 段内出现过的来源层级（`text` / `graphic` / …，去重）

    # ---- 供界面读的几个派生量 ----
    @property
    def n(self) -> int:
        return len(self.items)

    @property
    def scale(self) -> str:
        """规模说明：只有 1 条时留空（一行就说「一句话」，不用再标几行）。"""
        return SCALE_TPL.format(n=self.n) if self.n > 1 else ""

    @property
    def title(self) -> str:
        return para_label(self.kind)

    @property
    def has_old(self) -> bool:
        return bool(self.old_text.strip())

    @property
    def has_new(self) -> bool:
        return bool(self.new_text.strip())

    def add(self, it) -> None:
        """并入下一条（保持顺序）。前后文只取**首条的上文 / 末条的下文**。"""
        self.items.append(it)
        if it.old.strip():
            self.old_text = (self.old_text + "\n" + it.old) if self.old_text else it.old
        if it.new.strip():
            self.new_text = (self.new_text + "\n" + it.new) if self.new_text else it.new
        self._refresh()

    def _refresh(self) -> None:
        self.kind = _kind_of(self.items)
        self.before = _first_ctx(self.items, "ctx_before")
        self.after = _last_ctx(self.items, "ctx_after")
        self.note = next((it.note for it in self.items if it.note), "")
        self.context = _ctx_of(self.items)
        self.sources = _sources_of(self.items)

    @classmethod
    def from_item(cls, it) -> "Para":
        p = cls(items=[it], page=it.page_key if it.page_key >= 0 else None,
                scope=it.scope, context=it.context)
        if it.old.strip():
            p.old_text = it.old
        if it.new.strip():
            p.new_text = it.new
        p._refresh()
        p.before = getattr(it, "ctx_before", "") or ""
        p.after = getattr(it, "ctx_after", "") or ""
        return p


def _first_ctx(items: Sequence[Any], attr: str) -> str:
    for it in items:
        v = (getattr(it, attr, "") or "").strip()
        if v:
            return v
    return ""


def _last_ctx(items: Sequence[Any], attr: str) -> str:
    for it in reversed(list(items)):
        v = (getattr(it, attr, "") or "").strip()
        if v:
            return v
    return ""


def _kind_of(items: Sequence[Any]) -> str:
    """段类型：全同 → 那个类型；纯增 / 纯删 → 增 / 删；其余 → 增删。

    ⚠️ 2026-10-13 删「变动」档：不再有「修改 / 数值变动」归并逻辑，
    位移（moved）仍单独成段，其余混合一律归「增删（mixed）」。
    """
    ks = {it.kind for it in items}
    if len(ks) == 1:
        return next(iter(ks))
    if ks <= {"added", "page_added"}:
        return "added"
    if ks <= {"removed", "page_removed"}:
        return "removed"
    return "mixed"


def _sources_of(items: Sequence[Any]) -> tuple:
    """段内出现过的来源层级（去重、保持首次出现顺序）。"""
    out: List[str] = []
    for it in items:
        s = getattr(it, "scope", "text") or "text"
        if s not in out:
            out.append(s)
    return tuple(out)


def _ctx_of(items: Sequence[Any]) -> str:
    """段的「所在位置」文案。

    · **单来源**（绝大多数段）→ 逐字节等于该条目自带的 `context`，行为不变；
    · **多来源**（正文与转曲区并成一段）→ 首个来源用**原文案**（它带页码，
      例「第1页 正文」），其余来源只追加**短名**，用 ` + ` 连 →
      「第1页 正文 + 转曲区」。

    为什么多来源不整条拼：`所在位置` 列只有 ~110px，两条完整文案 190px+，
    界面会**从中间截断**，被截掉的恰好是「转曲区」这个词。
    用户明确要过「哪部分走了 OCR 得看得见」（2026-09-30），
    所以既要在这一列留痕，又不能长到看不清 —— 短名 + 同行的「变动说明」列
    （写着「矢量曲线绘制，无文字层；OCR 识别，仅供参考」）一起把话说全。

    首个来源按 `SCOPE_WEIGHT` 定（正文在前），不随条目排序漂移。
    """
    seen: dict = {}
    for it in items:
        c = (getattr(it, "context", "") or "").strip()
        if c and c not in seen:
            seen[c] = SCOPE_WEIGHT.get(getattr(it, "scope", "text") or "text", 9)
    parts = sorted(seen, key=lambda c: (seen[c], c))
    if len(parts) <= 1:
        return parts[0] if parts else ""
    head = parts[0]
    tail: List[str] = []
    for it in items:
        name = SCOPE_SHORT.get(getattr(it, "scope", "text") or "text", "")
        if name and name not in tail and name not in head:
            tail.append(name)
    return " + ".join([head] + tail)


# ---------------------------------------------------------------- 排序与判同段
def order_key(it) -> tuple:
    """按「页 → 上到下 → 左到右」排。

    ⚠️ 这里**不改 core 的清单顺序**，只在分段时排一次：清单是给人逐条读的，
    段是给人按位置看的，两者要的顺序不一样。纵向用 `y_key`（新版优先）——
    全页统一一个基准，新侧旧侧混着排会把不同栏的行插到一起去。

    ⚠️ `graphic` 走**与 `text` 同一条流**（2026-09-30 改）。改前它被甩到页尾
    （tier 2），于是「转曲区 + 紧挨其下的正文」在排序上被正文全部隔开，
    永远轮不到判相邻。既然两者能并成一段（规则 1a），就必须按位置排在一起。
    """
    pk = it.page_key
    if it.scope in SIBLING_SCOPES:
        y = it.y_key
        yy = y if y is not None else -1e9
        return (pk, 0, -yy, it.x_key or 0.0, it.id)
    if it.scope == "table":
        return (pk, 1, it.row or 0, it.col or 0, it.id)
    return (pk, 2, 0, 0, it.id)


def _near(a: Optional[float], b: Optional[float], tol: float) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def _same_level(a: str, b: str) -> bool:
    """规则 1 / 1a：两个来源算不算「同一层级」。"""
    return a == b or {a, b} <= SIBLING_SCOPES


def _v_gap(a1, a2, b1, b2) -> Optional[float]:
    """同一坐标系里两条的**纵向间隙**（重叠记 0）；任一坐标缺失 → None。

    ⚠️ 只认「两个纵坐标」，不假定谁是上谁是下 —— 本项目里 `y/y2` 与 `ny/ny2`
    一直是一对纵坐标，各处消费点（`pdfdiff_core._py_geom` / `pdf_annotate._clip_for` /
    `_frag_segment`）都用 `max/min` 自己摆正，这里保持一致。
    """
    if None in (a1, a2, b1, b2):
        return None
    a_lo, a_hi = min(a1, a2), max(a1, a2)
    b_lo, b_hi = min(b1, b2), max(b1, b2)
    if a_lo > b_hi:
        return a_lo - b_hi
    if b_lo > a_hi:
        return b_lo - a_hi
    return 0.0


def _touch_cross_level(last, it) -> bool:
    """规则 2a：跨 `text`/`graphic` 时判「挨不挨着」，用**区间间隙**而不是上边界差。

    为什么非换不可（实测 GRA-4118 第 1 页中栏）
    ------------------------------------------
    转曲区条目是一个**整块矩形**（`ny/ny2` = 整块上下边，实测高 44.4pt），
    正文条目是**一行**（高约 6pt）。拿「块顶」比「行顶」差 60pt，
    看着像隔了一整段；拿「块底到行顶」比只差 15.6pt —— 其实是紧挨着的。
    只比上边界的口径在这里**结构性失效**，跟阈值调多大无关。

    两侧各试一次（规则 4：坐标必须同侧才比）—— **某一侧坐标不全就换另一侧**，
    而不是当场判否；哪一侧齐就用哪一侧下结论，与同来源那条分支的顺序一致。
    """
    for xa, xb, t1, b1, t2, b2 in (
            (last.x, it.x, last.y, last.y2, it.y, it.y2),
            (last.nx, it.nx, last.ny, last.ny2, it.ny, it.ny2)):
        if xa is None or xb is None or None in (t1, b1, t2, b2):
            continue
        if not _near(xa, xb, PARA_X_PT):       # 规则 3：必须同一栏
            return False
        g = _v_gap(t1, b1, t2, b2)
        return g is not None and g <= PARA_GAP_PT
    return False


def _pair_ok(last, it) -> bool:
    """两条**相邻排序**的条目能不能挨在一起（规则 2/3/4/5 的成对判据）。"""
    if last.scope != it.scope:
        return _touch_cross_level(last, it)          # 跨来源先走 2a
    # ---- text：坐标必须同侧才比（规则 4）----
    if last.y is not None and it.y is not None:
        return (_near(last.y, it.y, PARA_GAP_PT)
                and _near(last.x, it.x, PARA_X_PT))
    if last.ny is not None and it.ny is not None:
        return (_near(last.ny, it.ny, PARA_GAP_PT)
                and _near(last.nx, it.nx, PARA_X_PT))
    # 一条只有旧侧坐标、一条只有新侧：横向/纵向都不可比，
    # 只有「一删一增」这种 diff 语义上明确的一次替换才并段（规则 5）。
    # ⚠️ 5a 要求**横向同栏**：排序只看纵向，页面上/下不同位置的一删一增会被排到一起 ——
    # 实测「生产日期」（左栏表格 x=638）紧挨着排在「中栏新增段」（nx=441）后面，
    # 光看 kind 组合就并了。横向差 > `PARA_X_PT` 就说明不是同一栏。
    if {last.kind, it.kind} != {"removed", "added"}:
        return False
    # ⚠️ 5a 要求**横向同栏**：排序只看纵向，页面上/下不同位置的一删一增会被排到一起 ——
    # 实测「生产日期」（左栏表格 x=638）紧挨着排在「中栏新增段」（nx=441）后面，
    # 光看 kind 组合就并了。横向差 > `PARA_X_PT` 就说明不是同一栏。
    # 两侧都**没有坐标**（如 Word 直读链路，只有文档顺序、无版面坐标）时，
    # 无法用横向判同栏 —— 但顺序本身就是文档阅读顺序，视为同栏、放行。
    if last.x_key is not None or it.x_key is not None:
        if not _near(last.x_key, it.x_key, PARA_X_PT):
            return False
    # ⚠️ 5b 还要求**纵向也挨着**：只比横向仍会漏 —— 同一栏上下两处「一删一增」
    # 照样被并。实测第 1 页右栏的「生产日期」（旧侧 y=224）与页脚
    # （新侧 y=14）同为右栏、横向只差 15pt，于是**链式**把页脚整段卷进
    # 「生产日期」那一段（相距 210pt 的两处并成一段）。
    # `y_key` 各取「有哪侧用哪侧」—— 两版版式一致时新侧 y 与旧侧 y 可直接比，
    # 这跟 `order_key` 用 y_key 统一排序是同一个前提。
    yy = (last.y_key, it.y_key)
    if None in yy:                     # 真缺坐标就退回 5a 的口径，不误伤
        return True
    return _near(yy[0], yy[1], PARA_GAP_PT)


def _same_para(p: Para, it) -> bool:
    """规则表 1~7 的落地。

    ⚠️ 判「挨着」时**段内每一条都试**（从后往前），不是只试段尾：
    排序流里会插进**别的栏**的条目 —— GRA-4118 实测第 1 栏「一个再折叠工具」
    (x=59, y≈405) 恰好排在中栏两条 (x=441) 之间，链式分组只比段尾，
    它一来就同栏对不上 → 另起一段，把它后面的中栏正文也整段切断
    （用户 2026-09-30：转曲区段和 ♦ 步骤列表「应该在一起」）。
    判据本身一条没松：还是同页、同层级、同栏、纵向挨着。
    """
    if it.page_key != p.items[0].page_key:                    # 规则 1（同页）
        return False
    if not _same_level(it.scope, p.scope):                    # 规则 1 / 1a（同层级）
        return False
    if p.scope in _BY_PAGE_ONLY:                              # 规则 7
        return False
    if p.scope == "table":                                    # 规则 6
        last = p.items[-1]
        return (it.context == last.context
                and _near(float(last.row or 0), float(it.row or 0), PARA_ROW_GAP))
    for pi in reversed(p.items):
        if _pair_ok(pi, it):
            return True
    return False


def group_paragraphs(items: Sequence[Any]) -> List[Para]:
    """条目 → 段（保持输入内容不变，只做分组与排序）。

    ⚠️ 放不进当前段时**回试更早的段**：排序流里异栏条目插队（见
    `_same_para` 的注释）会把链切断，先来的段因此缺了后半截 ——
    条目其实挨着**前面**某段的某一条。判据不松，只是别让「段创建顺序」
    挡住真正的相邻关系。
    """
    paras: List[Para] = []
    for it in sorted(items, key=order_key):
        if paras and _same_para(paras[-1], it):
            paras[-1].add(it)
            continue
        for p in reversed(paras[:-1]):
            if _same_para(p, it):
                p.add(it)
                break
        else:
            paras.append(Para.from_item(it))
    return paras


def group_text(paras: Sequence[Para], empty: str = "（这一侧没有内容）") -> List[tuple]:
    """给界面用的渲染料：每段 → `(旧侧文本, 新侧文本)`（已带前后文）。"""
    out = []
    for p in paras:
        out.append((with_ctx(p.before, p.old_text, p.after, empty),
                    with_ctx(p.before, p.new_text, p.after, empty)))
    return out


def with_ctx(before: str, body: str, after: str, empty: str = "（这一侧没有内容）") -> str:
    """把「前文 / 本段 / 后文」拼成详情面板里那一栏的文本。

    ⚠️ 这是**纯文本**入口（导出、老调用点用）。界面请走 `render_lines()` ——
    它把「哪几行是上下文、哪几行是本段」摊成带角色的行，界面才能分区着色。
    """
    parts = []
    if before:
        parts.append(before)
    parts.append(body if body.strip() else empty)
    if after:
        parts.append(after)
    return "\n".join(parts)


# ---------------------------------------------------------------- 详情面板的渲染料
# 角色枚举：界面按它上色，不猜内容。
R_BEFORE = "before"      # 前文（页面同栏上方的行）
R_BODY = "body"          # 本段（真正发生变动的行）
R_AFTER = "after"        # 后文（页面同栏下方的行）
R_RULE = "rule"          # 分隔条（标明上下文从哪开始、有几行）
R_EMPTY = "empty"        # 这一侧没有内容

EMPTY_TEXT = "（这一侧没有内容）"

#: 角色 → 分隔条文案（只在 R_RULE 用，`{n}` = 行数）
RULE_TPL = {
    R_BEFORE: "── ↑ 前文 · 页面同栏上方 {n} 行 ──",
    R_AFTER: "── ↓ 后文 · 页面同栏下方 {n} 行 ──",
}

#: 界面样式表：角色 → (前台色, 是否斜体)。配色值与 `diff_app` 的 TEXT2/BORDER 同一族。
ROLE_STYLE = {
    R_BEFORE: ("#8a9099", False),
    R_AFTER: ("#8a9099", False),
    R_RULE: ("#b6bcc4", False),
    R_EMPTY: ("#b6bcc4", True),
}


def lines_of(s: str) -> List[str]:
    """把多行文本拆成**非空的、已去首尾空白**的行（上下文的行源）。"""
    return [x.strip() for x in (s or "").split("\n") if x.strip()]


def oneline(s: str) -> str:
    """多行压成一行（行间用 `⏎`），供明细行那种只有一行高的地方用。"""
    return BRIEF_LINESEP.join(lines_of(s))


def render_lines(p: "Para", side: str = "new") -> List[tuple]:
    """一段差异 → `[(角色, 文本)]`，供详情面板**逐行**渲染。

    为什么要摊成行：用户 2026-09-29 ——「上下文的选择可以扩大一点范围，多加一些内容，
    这样子能够让人眼更明确地看出来哪些地方发生了变动」。光把上下文拼成一坨塞进去，
    眼睛分不清「哪句是新增的、哪句本来就有」；标上角色后界面就能把上下文压灰、
    本段保持正常色并叠红绿 —— 改动一眼跳出来。

    顺序：`前文（+分隔条） → 本段 → 后文（+分隔条）`。
    """
    body = (p.new_text if side == "new" else p.old_text) or ""
    out: List[tuple] = []
    before = lines_of(p.before)
    if before:
        out.append((R_RULE, RULE_TPL[R_BEFORE].format(n=len(before))))
        out.extend((R_BEFORE, x) for x in before)
    body_lines = lines_of(body)
    if body_lines:
        out.extend((R_BODY, x) for x in body_lines)
    else:
        out.append((R_EMPTY, EMPTY_TEXT))
    after = lines_of(p.after)
    if after:
        out.append((R_RULE, RULE_TPL[R_AFTER].format(n=len(after))))
        out.extend((R_AFTER, x) for x in after)
    return out


def body_offset(p: "Para", side: str = "new") -> int:
    """本段第一行在 `render_lines()` 里的**字符偏移** —— 字符级高亮要加它。

    偏移按「每行文本 + 一个换行」累加，和界面逐行插入的结果严格一致；
    两处（详情面板 / 弹窗）用同一个函数，不会各算各的。
    """
    off = 0
    for role, s in render_lines(p, side):
        if role == R_BODY:
            return off
        off += len(s) + 1
    return off


def body_text(p: "Para", side: str = "new") -> str:
    """本段在详情面板里的**实际文本**（= `render_lines` 拆行后重拼）。

    ⚠️ 字符级比对必须基于它，不能直接拿 `p.old_text / p.new_text`：
    `render_lines` 会丢掉空行、去掉首尾空白，一旦原文有这类字符，
    直接拿原文本比对算出的下标就会和插入的文本**错开**，红绿标到错位置。
    """
    src = p.new_text if side == "new" else p.old_text
    return "\n".join(lines_of(src))


# ---------------------------------------------------------------- 明细行的单行摘要
BRIEF_CTX = 40        # 明细行里前文/后文各露多少字
BRIEF_BODY = 160      # 明细行里本段内容露多少字
BRIEF_SEP = " ‖ "     # 前文 / 本段 / 后文 之间的分隔
BRIEF_LINESEP = " ⏎ "  # 段内多行之间的分隔（明细行只能一行）


def brief(p: Para, side: str = "new", ctx_show: int = BRIEF_CTX,
          body_show: int = BRIEF_BODY) -> str:
    """明细行里显示的一行摘要：`…前文 ‖ 本段 ‖ 后文…`

    为什么要塞前后文：用户要的是「这句话在原文哪儿」（2026-09-29 原话：
    「把原始的那个地方的前后文给标记出来」）。明细行只有一行高，塞不下三行，
    所以前文取**尾部**（紧贴本段的那几个字最有信息量）、后文取**头部**，
    本段中段截断 —— 完整内容在「选中项详情」里，点一下就能看。

    上下文可能有好几行，先压成一行（行间 `⏎`）再截 —— 不压的话换行符会把
    明细行撑成两行，整张表就错位了。
    """
    body = oneline(p.new_text if side == "new" else p.old_text)
    if len(body) > body_show:
        body = body[:body_show] + "…"
    parts = []
    if p.before:
        parts.append("…" + oneline(p.before)[-ctx_show:])
    parts.append(body or "（这一侧没有内容）")
    if p.after:
        parts.append(oneline(p.after)[:ctx_show] + "…")
    return BRIEF_SEP.join(parts)


# ==================================================================
# 自检
# ==================================================================
def self_test() -> None:
    """分段规则的单元自检：改动前后各判据的行为必须稳定。"""
    class It:
        def __init__(self, kind="added", scope="text", page=0, y=None, x=None,
                     ny=None, nx=None, y2=None, ny2=None,
                     old="", new="", ctx="", row=None):
            self.kind, self.scope = kind, scope
            self.page_old = self.page_new = page
            self.y, self.x, self.ny, self.nx = y, x, ny, nx
            self.y2, self.ny2 = y2, ny2
            self.old, self.new, self.context, self.row = old, new, ctx, row
            self.col = None
            self.id = self._n
            self.ctx_before = self.ctx_after = ""
            self.note = ""
            It._n += 1

        @property
        def page_key(self):
            p = self.page_new if self.page_new is not None else self.page_old
            return -1 if p is None else p

        @property
        def y_key(self):
            return self.ny if self.ny is not None else self.y

        @property
        def x_key(self):
            return self.nx if self.nx is not None else self.x

    It._n = 0
    ok = fail = 0

    def chk(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name}{('　→ ' + extra) if extra else ''}")

    # ① 同栏、行距 7pt 的一串新增 → 一段
    a = [It(kind="added", ny=430 - 7 * i, nx=441, new=f"L{i}") for i in range(10)]
    ps = group_paragraphs(a)
    chk("相邻行聚成一段", len(ps) == 1 and ps[0].n == 10)
    chk("段类型=新增", ps[0].kind == "added")
    chk("段文本按行拼", ps[0].new_text.count("\n") == 9)
    chk("规模说明", ps[0].scale == "10 行")

    # ② 同一高度、横向差 279pt（并排两栏）→ 分开
    ps = group_paragraphs([It(kind="added", ny=498, nx=227, new="A"),
                           It(kind="added", ny=498, nx=506, new="B")])
    chk("并排两栏不并段", len(ps) == 2)

    # ③ 同栏但隔了 94pt（跨段）→ 分开
    ps = group_paragraphs([It(kind="added", ny=430, nx=441, new="A"),
                           It(kind="added", ny=336, nx=441, new="B")])
    chk("隔段不并（间距 > 阈值）", len(ps) == 2)

    # ④ 坐标跨侧不可比：moved 旧侧 y=375、新侧 ny=279，而 added 只有新侧 ny=373；
    #    旧侧/新侧坐标不能互相比较（规则 4），即便数值上挨着也不并。
    ps = group_paragraphs([It(kind="moved", y=375, x=429, ny=279, nx=434, old="A", new="A"),
                           It(kind="added", ny=373, nx=441, new="B")])
    chk("旧侧/新侧坐标不跨侧比", len(ps) == 2)

    # ⑤ 一删一增（两侧各缺一侧坐标）→ 并成「增删」
    ps = group_paragraphs([It(kind="removed", y=100, x=50, old="旧句"),
                           It(kind="added", ny=100, nx=50, new="新句")])
    chk("一次替换并成增删", len(ps) == 1 and ps[0].kind == "mixed")

    # ⑤b 一删一增但**横向不同栏** → 不并（2026-09-29 实测：排序只看纵向，
    #    左栏表格的「生产日期」会紧跟在中栏新增段后面，光看 kind 组合就误并）
    ps = group_paragraphs([It(kind="removed", y=224, x=638, old="生产日期"),
                           It(kind="added", ny=373, nx=441, new="新增行")])
    chk("不同栏的一删一增不并段", len(ps) == 2, str([(p.kind, p.n) for p in ps]))

    # ⑤c 同栏但**纵向相差 210pt** 的一删一增也不并（2026-09-30 实测：第 1 页右栏
    #    「生产日期」y=224 与页脚 y=14 横向只差 15pt，只比横向会被链式卷成一段）
    ps = group_paragraphs([It(kind="removed", y=224, x=638, old="生产日期"),
                           It(kind="added", ny=14, nx=624, new="页脚")])
    chk("同栏但相距很远的一删一增不并段", len(ps) == 2, str([(p.kind, p.n) for p in ps]))
    # ⑤d 同栏且纵向挨着 → 仍然要并（不能因为 ⑤c 把正常替换也拆了）
    ps = group_paragraphs([It(kind="removed", y=13.8, x=624.8, old="Rev06 页脚"),
                           It(kind="added", ny=14.1, nx=623.8, new="Rev07 页脚")])
    chk("同栏同位置的一删一增仍并段", len(ps) == 1 and ps[0].kind == "mixed")

    # ⑥ 跨页不并
    ps = group_paragraphs([It(kind="added", page=0, ny=100, nx=50, new="A"),
                           It(kind="added", page=1, ny=100, nx=50, new="B")])
    chk("跨页不并段", len(ps) == 2)

    # ⑦ 表格：同表相邻行并、隔太远拆
    ps = group_paragraphs([It(kind="removed", scope="table", page=0, row=3, ctx="表1 · 压力"),
                           It(kind="removed", scope="table", page=0, row=5, ctx="表1 · 压力"),
                           It(kind="removed", scope="table", page=0, row=40, ctx="表1 · 压力")])
    chk("表格相邻行并段", len(ps) == 2 and ps[0].n == 2)

    # ⑧ 整页增删 / 文档属性 各自成段
    ps = group_paragraphs([It(kind="page_added", scope="page", page=0, new="A"),
                           It(kind="page_added", scope="page", page=1, new="B")])
    chk("整页增删各自成段", len(ps) == 2)

    # ⑨ 前后文取首条上文 / 末条下文
    x1 = It(kind="added", ny=100, nx=10, new="第一行")
    x2 = It(kind="added", ny=93, nx=10, new="第二行")
    x3 = It(kind="added", ny=86, nx=10, new="第三行")
    x1.ctx_before, x3.ctx_after = "上面是标题", "下面是正文"
    ps = group_paragraphs([x1, x2, x3])
    chk("前后文取段首/段尾", ps[0].before == "上面是标题" and ps[0].after == "下面是正文")
    chk("段内容含全部行", "第一行" in ps[0].new_text and "第三行" in ps[0].new_text)

    # ⑩ 排序：页 → 上到下
    ps = group_paragraphs([It(kind="added", ny=100, nx=10, new="下"),
                           It(kind="added", ny=200, nx=10, new="上")])
    chk("按位置从上到下排", ps[0].new_text == "上" and ps[1].new_text == "下")

    # ⑪ 详情渲染料：上下文逐行带角色 + 本段偏移算得准
    y = It(kind="added", ny=100, nx=10, new="新增第一句\n新增第二句")
    y.ctx_before = "上文一\n上文二\n上文三"
    y.ctx_after = "下文一\n下文二"
    para = group_paragraphs([y])[0]
    rl = render_lines(para, "new")
    roles = [r for r, _s in rl]
    chk("渲染料分行正确",
        roles == [R_RULE] + [R_BEFORE] * 3 + [R_BODY] * 2 + [R_RULE] + [R_AFTER] * 2,
        str(roles))
    chk("分隔条写明行数", "3 行" in rl[0][1] and "2 行" in rl[6][1])
    exp = len(rl[0][1]) + 1 + sum(len(s) + 1 for _r, s in rl[1:4])
    chk("本段字符偏移精确", body_offset(para, "new") == exp,
        f"{body_offset(para, 'new')} vs {exp}")
    chk("旧侧为空 → 占位行", render_lines(para, "old")[4][0] == R_EMPTY)
    chk("明细摘要压成一行", "\n" not in brief(para, "new"))
    chk("明细摘要含上下文", "上文" in brief(para, "new") and "下文" in brief(para, "new"))

    # ⑫ 转曲区（graphic）与紧挨其下的正文（text）并成一段（2026-09-30 用户：
    #    「可以放在一起分析差异，因为 pdf 中挨得很近」）。
    #    坐标取自 GRA-4118 第 1 页中栏**实测**：转曲区是一整块（高 44.4pt），
    #    正文是一行（高 6pt）—— 两边「上边界」差 60pt，看着像隔了一整段。
    _g = It(kind="added", scope="graphic", ny=490.275, ny2=445.875, nx=444.6,
            new="转曲区整段", ctx="转曲区（PDF 无文字层）")
    _t = It(kind="added", ny=430.238, ny2=424.238, nx=441.35,
            new="正文第一行", ctx="第1页 正文")
    ps = group_paragraphs([_g, _t])
    chk("转曲区 + 紧挨其下的正文并成一段", len(ps) == 1 and ps[0].n == 2,
        str([(p.kind, p.n, p.scope) for p in ps]))
    chk("合并段记下来源集合", ps[0].sources == ("graphic", "text"), str(ps[0].sources))
    chk("合并后「所在位置」两个来源都写明、正文在前（短名，免得被列宽截断）",
        ps[0].context == "第1页 正文 + 转曲区", ps[0].context)
    _solo = group_paragraphs([It(kind="added", ny=100, nx=10, new="普通新增",
                                 ctx="第1页 正文")])[0]
    chk("单来源段的「所在位置」保持原文案、逐字节不变", _solo.context == "第1页 正文",
        _solo.context)

    # ⑬ 跨来源用「区间间隙」判相邻，不是「上边界差」
    chk("跨来源：上边界差 60pt 仍算挨着（真实间隙 15.6pt）",
        _touch_cross_level(_g, _t) is True)
    chk("同来源：仍按上边界差判（60pt > 20pt 就是两处）",
        _near(_g.ny, _t.ny, PARA_GAP_PT) is False)
    chk("纵向间隙：重叠记 0", _v_gap(100.0, 90.0, 95.0, 85.0) == 0.0)
    chk("纵向间隙：上下分开算真间隙", abs(_v_gap(100.0, 90.0, 80.0, 70.0) - 10.0) < 1e-9)
    chk("纵向间隙：缺坐标返回 None", _v_gap(100.0, None, 80.0, 70.0) is None)

    # ⑭ 护栏：不同栏 / 隔一整段都不许并
    _far = It(kind="added", scope="graphic", ny=490.275, ny2=445.875, nx=444.6, new="转曲区")
    _other = It(kind="added", ny=430.238, ny2=424.238, nx=633.4, new="右栏正文")
    chk("转曲区与**不同栏**的正文不并段", len(group_paragraphs([_far, _other])) == 2,
        str([(p.scope, p.n) for p in group_paragraphs([_far, _other])]))
    _gap = It(kind="added", ny=200.0, ny2=194.0, nx=441.35, new="隔了一整段的正文")
    chk("转曲区与**隔了一整段**的正文不并段", len(group_paragraphs([_far, _gap])) == 2)
    chk("转曲区不与表格条目算同层级", not _same_level("graphic", "table"))
    chk("转曲区不与整页条目算同层级", not _same_level("graphic", "page"))

    # ⑮ 排序：转曲区不再被甩到页尾，与正文按位置混排（否则永远轮不到判相邻）
    _mix = group_paragraphs([
        It(kind="added", ny=430.238, ny2=424.238, nx=441.35, new="下面一行正文"),
        It(kind="added", scope="graphic", ny=490.275, ny2=445.875, nx=444.6,
           new="上面转曲区"),
    ])
    chk("转曲区与正文按位置混排（不再甩到页尾）",
        len(_mix) == 1 and _mix[0].new_text.startswith("上面转曲区"), str(_mix[0].new_text))

    # ⑯ 异栏条目插队不切断链（2026-09-30 用户：转曲区段和 ♦ 步骤「应该在一起」）
    #    GRA-4118 实测：第 1 栏「一个再折叠工具」(x=59, y≈405) 排序恰好落在
    #    中栏两条 (x=441) 之间。链式只比段尾 → 它一来就断链，中栏正文整段被切两半。
    _m1 = It(kind="added", ny=408.4, ny2=414.6, nx=441.4, new="♦ 从再折叠工具未扩口端装入")
    _left = It(kind="added", scope="graphic", ny=399.7, ny2=405.1, nx=59.4, new="一个再折叠工具")
    _m2 = It(kind="added", ny=401.4, ny2=407.4, nx=441.4, new="小心将带环芯轴")
    _ps = group_paragraphs([_m1, _left, _m2])
    chk("异栏插队：中栏两条仍并成一段（不被切断）",
        any(p.n == 2 and all(i.nx == 441.4 for i in p.items) for p in _ps),
        str([(p.scope, p.n) for p in _ps]))
    chk("异栏插队：异栏那条自成一段", len(_ps) == 2, str(len(_ps)))
    _ps2 = group_paragraphs([
        It(kind="added", ny=430.0, ny2=436.0, nx=441.0, new="中栏第 1 行"),
        It(kind="added", scope="graphic", ny=405.0, ny2=399.0, nx=59.0, new="左栏"),
        It(kind="added", ny=423.0, ny2=429.0, nx=441.0, new="中栏第 2 行"),
        It(kind="added", ny=416.0, ny2=422.0, nx=441.0, new="中栏第 3 行"),
    ])
    _mid = [p for p in _ps2 if p.items[0].nx == 441.0]
    chk("异栏插队：中栏三行全在一段（回试更早的段）",
        len(_ps2) == 2 and len(_mid) == 1 and _mid[0].n == 3,
        str([(p.scope, p.n) for p in _ps2]))

    print(f"分段自检：{ok} 项通过，{fail} 项失败")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    self_test()
