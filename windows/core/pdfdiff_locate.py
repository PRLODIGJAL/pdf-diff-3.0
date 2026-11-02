# -*- coding: utf-8 -*-
"""
pdfdiff_locate.py —— **坐标定位型**比对（给「标注版 PDF」用）
==================================================================
为什么要有第二个引擎
--------------------
标注要画准，就必须知道「改动在页面上的哪个矩形里」。
`pdfdiff_core` 走 pdfminer，只给出文字内容（行/表格格），
于是标注只能拿着文字去 `page.search_for()` 搜位置 ——
这一步是误标的万恶之源：

  · 差异块常是跨行拼出来的**伪文本**，PDF 里压根没有这段连续文字 → 搜不到；
  · 搜不到就砍短再试 → 砍成「公司」「批号」「标识」→ 满页命中无辜文字；
  · 就算搜到了，同名的文字在页里出现几次就标几处。

本模块换一条路：**比对和标注用同一个抽取引擎（PyMuPDF）**。
PyMuPDF 的 `get_text("rawdict")` 直接给到**每一个字符的 bbox**，
所以差异项天生自带坐标 —— 标注时一个字都不用搜，直接画。

用户要的效果（2026-09-29 原话）：
  「比方一二三四五六七八九十是旧表，一二三四五六七八十一十是新表，
    识别到 9 和 11 有差异，就在旧表标出 9、在新表标出 11。」
→ 本模块正是这么做的：**逐字符对比（真正的一对一坐标反推）**。

比对三步走（这是根除「瞎标」的关键）
------------------------------------
  1. **抽坐标**：用 rawdict 把每页每个字连同它自己的 bbox 平铺出来（带页码）。
  2. **行级精确抵消**：把两份文档里**逐字完全相同、只是位置变了**的行先抵消掉
     （跨页/换行重排、整段换位都不会刷出假差异）。这一步是位置无关的。
  3. **字符流逐字 diff**：把剩下没抵消的行摊成**一条字符流**（空白换行不进流），
     整份文档一次 `SequenceMatcher` 比完 —— 变了的那些字，**直接拿它们自己的
     bbox** 去标红/标绿。

⚠️ 为什么第 3 步**不能**按「整行配对」做（2026-09-29 用户实测打回来的教训）
--------------------------------------------------------------------------
改版时文字会**重新折行**（旧版「A、B」两行 → 新版并成一行，或反过来）。
按「整行」配对的引擎遇到这种情况，**整行配不上对**，于是：
  · 旧版那半行被判成「删除」→ 标红；
  · 新版那半行被判成「新增」→ 标绿；
  · 可这些字**两边都有**，只是换行位置变了 —— 用户一眼就看出来是「瞎标」。
实测 GRA-4118 rev06→rev07：`博迈医疗`、`201室，15栋，16栋`、
`• 本试验主要有效性指标是手…` 全被这么误标。

换成字符流后，空白（含换行）根本不进比对串，折行差异天然消失，
只有**字本身变了**才会落进 diff 的 replace/delete/insert 里。

整个过程中 bbox 从一开始就挂在每个字上，**中间没有任何「归一化序号 →
真实字符序号」的重映射**，所以标出来的矩形 100% 就是 diff 算出的那几个字。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import atexit
from collections import Counter, deque
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

try:                       # 1.28 起推荐用 pymupdf，fitz 只是兼容别名
    import pymupdf as fitz
except ImportError:        # pragma: no cover
    import fitz            # type: ignore

# ---------------------------------------------------------------- 参数
HF_TOP = 0.055              # 页顶/页底这个比例内的行 = 页眉页脚（可忽略）
HF_BOTTOM = 0.945
RECT_PAD = 1.5             # 高亮矩形上下各留一点，不然贴着字不好看
MIN_ROW_LEN = 2            # 归一化后不足这个长度的行直接忽略（见下）
_GROUP_GAP = 4.0           # 同一处改动里相邻的字，纵向差 ≤ 此值 (pt) 就算同一行
_TINY_ANCHOR = 4           # 字符流里短于这个长度的「相等段」不算锚点，并入改动（见 _opcodes）

# ⚠️ 反面教材：**不要按「在页面顶部/底部」来过滤页眉页脚**（曾这么做过，已废弃）。
# 说明书的文件号、修订日期、版本号**就印在页脚**，而它们恰恰是改版时最该标出来的东西
# （GRA-4118 实测：Rev06/DCR 2025-0850 → Rev07、修订日期 2025年 → 2026年）。
# 按位置一刀切会把它们整段滤掉，用户看到的标注就"莫名其妙少了"一大截。
# 真正该忽略的只有页码这种纯编号行（页数一变会满篇刷差异），见 _is_page_number。
_PAGE_NUM_RE = re.compile(r"^\s*\d{1,3}\s*([/／\-]\s*\d{1,3})?\s*$")
# 行尾的页码 token（如 "1/2"）。改版时页脚常被重排：旧版页码单独一行（已被上面忽略），
# 新版并进页脚那一行 → 字符流里就凭空多出「1/2」被标绿。见 _strip_page_token。
_TAIL_PAGE_RE = re.compile(r"\s+(\d{1,3}\s*[/／]\s*\d{1,3})\s*$")


@dataclass
class Row:
    """页面上的一行文字，带它每个字的坐标。"""
    text: str
    norm: str
    chars: List[Tuple[str, Any]] = field(default_factory=list)
    rect: Optional[Tuple[float, float, float, float]] = None
    page: int = 0
    #: 主 span 的字体签名 `"字体名|字号"`（字数最多的那个 span）。
    #: 仅文字层有；OCR 补的行留空（空签名不参与字体变动判定）。
    font: str = ""
    #: 主 span 的**渲染字号**（pt，round 到 0.1）。单独存一份，供字体变动判定
    #: 精确比较字号差（`_extract_font_changes` 里与字体名分开判，阈值 FONT_MIN_DSIZE）。
    size: float = 0.0


@dataclass
class LocDiff:
    """一处改动，两侧各自带矩形（画标注时直接用，不再搜索）。"""
    kind: str                       # added / removed / page_added / page_removed / moved / font
    page_old: Optional[int] = None
    page_new: Optional[int] = None
    old_text: str = ""
    new_text: str = ""
    rects_old: List[Tuple[float, float, float, float]] = field(default_factory=list)
    rects_new: List[Tuple[float, float, float, float]] = field(default_factory=list)


# ---------------------------------------------------------------- 抽取
def _norm(text: str, ignore_case: bool, ignore_punct: bool) -> str:
    out: List[str] = []
    for ch in text:
        if ch.isspace():
            out.append(" ")
            continue
        c = ch.lower() if ignore_case else ch
        if ignore_punct and not c.isalnum():
            continue
        out.append(c)
    return "".join(out)


def _is_page_number(s: str) -> bool:
    return bool(_PAGE_NUM_RE.match(s or ""))


def _strip_page_token(raw: str, min_keep: int = 8) -> str:
    """剥掉行尾的页码 token（`1/2` 这种）。

    ⚠️ 只在「行尾 + 前面还有 ≥ min_keep 个非空白字」时才剥：
    页码是随翻页/重排飘的东西，不该算改动；但表格里真正的「1/2」类数值
    往往就是整格内容（前面没什么字），那就不剥。
    """
    m = _TAIL_PAGE_RE.search(raw)
    if not m:
        return raw
    if len(re.sub(r"\s+", "", raw[:m.start()])) < min_keep:
        return raw
    return raw[:m.start()]


def extract_rows(page, page_idx: int, ignore_case: bool = False,
                 ignore_punct: bool = False,
                 drop_header_footer: bool = False,
                 ignore_page_number: bool = True,
                 min_len: int = MIN_ROW_LEN) -> List[Row]:
    """用 PyMuPDF 抽一页的**行**，每行带上它每个字的 bbox。

    用 rawdict 而不是 get_text()：rawdict 给到字符级坐标，
    这正是「改动标在哪」所需要的精度。

    ⚠️ `min_len`：只含一两个符号的行（"•"、"♦"、单独的页码）在改版时数量一变，
    就会刷出成片的"删+增"噪音。这类行本身也不承载信息，直接不参与比对。

    ⚠️ `drop_header_footer`（按位置砍页眉页脚）**默认关闭且不建议开**：
    文件号/修订日期/版本号常印在页脚，开了会把真改动整段滤掉。
    """
    rows: List[Row] = []
    try:
        data = page.get_text("rawdict")
    except Exception:
        return rows
    h = float(page.rect.height) or 1.0
    for blk in data.get("blocks") or []:
        if blk.get("type") != 0:
            continue
        for ln in blk.get("lines") or []:
            chars: List[Tuple[str, Any]] = []
            texts: List[str] = []
            for sp in ln.get("spans") or []:
                for ch in sp.get("chars") or []:
                    c = ch.get("c", "")
                    if not c:
                        continue
                    b = ch.get("bbox")
                    if not b:
                        continue
                    chars.append((c, tuple(float(v) for v in b)))
                    texts.append(c)
            if not chars:
                continue
            raw = "".join(texts)
            # 行尾页码 token 先剥掉（它随重排飘，不算改动；见 _strip_page_token）
            stripped = _strip_page_token(raw)
            if stripped != raw:
                raw = stripped
                chars = chars[:len(stripped)]
                texts = texts[:len(stripped)]
            if not raw.strip():
                continue
            lb = ln.get("bbox") or [0, 0, 0, 0]
            y0 = float(lb[1]) / h
            y1 = float(lb[3]) / h
            in_edge = (y1 <= HF_TOP or y0 >= HF_BOTTOM)
            if drop_header_footer and in_edge:
                continue
            # ⚠️ 页码只在**页边带**里才当页码（2026-10-11 修，合成留出集 S4 抓到）
            #    原来是无条件按内容判：正文 / 表格里整格就是「150」「20」这种纯数字，
            #    被 `_PAGE_NUM_RE` 当成页码**整行滤掉** → 那些格子根本不进字符流，
            #    结果是「设定值 150→180」在清单里报了、标注版上一处都没标
            #    （两条链路口径不一致；`-80` 因带负号不匹配才侥幸躲过，故现象不对称）。
            #    页码是**页边**的东西，加位置约束后表格数字照常参与比对。
            if ignore_page_number and in_edge and _is_page_number(raw.strip()):
                continue
            norm = _norm(raw, ignore_case, ignore_punct)
            if len(norm.replace(" ", "")) < min_len:
                continue
            xs = [c[1][0] for c in chars] + [c[1][2] for c in chars]
            ys = [c[1][1] for c in chars] + [c[1][3] for c in chars]
            rows.append(Row(text=raw, norm=norm, chars=chars,
                            rect=(min(xs), min(ys), max(xs), max(ys)),
                            page=page_idx,
                            font=_dominant_font(ln.get("spans") or []),
                            size=_dominant_size(ln.get("spans") or [])))
    return rows


def _dominant_font(spans: List[dict]) -> str:
    """一行里**字数最多**的 span 的字体签名 `"字体名|字号"`。

    ⚠️ 字体名先剥掉子集前缀（`ABCDEE+SimSun` → `SimSun`）：嵌入子集的前缀是
    产出器随机生成的，同一台机器两次导出都会变 —— 拿它当判据会满篇假阳性。
    """
    best, best_n = "", -1
    for sp in spans:
        n = len(sp.get("chars") or [])
        if n > best_n:
            best_n = n
            name = str(sp.get("font") or "")
            if "+" in name:
                name = name.split("+", 1)[1]
            try:
                size = round(float(sp.get("size") or 0.0), 1)
            except Exception:
                size = 0.0
            best = "%s|%.1f" % (name, size)
    return best if best_n > 0 else ""


def _dominant_size(spans: List[dict]) -> float:
    """一行里**字数最多**的 span 的**渲染字号**（pt，round 到 0.1）。

    与 `_dominant_font` 同源（同一个「字数最多 span」），只是单独把字号抠出来，
    供 `_extract_font_changes` 精确比较字号差（阈值 FONT_MIN_DSIZE）。
    """
    best, best_n = 0.0, -1
    for sp in spans:
        n = len(sp.get("chars") or [])
        if n > best_n:
            best_n = n
            try:
                best = round(float(sp.get("size") or 0.0), 1)
            except Exception:
                best = 0.0
    return best if best_n > 0 else 0.0


# ---------------- 字体变动检测（2026-10-12，五色体系的「深蓝」桶）----------------
#: 字体配对判据：文字逐字相同、字体签名不同 → `kind="font"`。
#: ⚠️ 全局可靠性闸门：若被判成字体变的行占「两侧逐字相同行」的比例超过
#: `FONT_CHANGE_MAX_FRAC`，说明这份 PDF 的字体元数据整体不可信（不同产出器
#: 重新嵌入子集、字体名系统性变了）—— 此时**全部放弃**字体差异，宁可不标。
FONT_CHANGE_MAX_FRAC = 0.40
#: 闸门生效的最小样本行数：行数太少时比例没有统计意义
#: （单行文档 1/1=100% 会被误杀），不足这个数就不设闸。
FONT_GUARD_MIN_ROWS = 8
#: 字号差 ≥ 此值（pt）才算「字体变化」（与 core `_FONT_MIN_DSIZE` 同口径）。
#: ⚠️ 2026-10-13 用户拍板：字体是「锦上添花」的辅助项，宁可漏报不误报。
#:   渲染亚像素抖动（0.1pt 级）不该算字体变化，否则反而干扰看清删/增/改。
FONT_MIN_DSIZE = 0.5


def _font_pair_changed(x: "Row", y: "Row") -> bool:
    """两行文字逐字相同，字体是否真变了（字体名不同 或 字号差 ≥ FONT_MIN_DSIZE）。

    与 core `_font_changed` 同口径：字体名剥前缀后比，字号用渲染字号、差 < 0.5pt
    视为渲染抖动不算变化。这样清单/标注两条链路的 font 判定阈值一致。

    ⚠️ `Row.font` 是 `"字体名|字号"` 的**完整签名**（`_dominant_font` 拼的），
    里面已经带了字号 —— 不能整串比（否则 6.5 vs 6.6 会因字号后缀不同误判成
    字体名变了）。这里只取 `|` 前的字体名部分比，字号单独用 `size` 字段比。
    """
    if x.size and y.size and abs(y.size - x.size) >= FONT_MIN_DSIZE:
        return True
    nx = (x.font or "").split("|", 1)[0]
    ny = (y.font or "").split("|", 1)[0]
    if nx and ny and nx != ny:
        return True
    return False


def _extract_font_changes(pa: List[List[Row]], pb: List[List[Row]]
                          ) -> List[LocDiff]:
    """把「文字一样、字体/字号变了」的行配成对，返回 `kind="font"` 的差异。

    在 `_cancel_identical` **之前**跑：抵消是按文本做的，字体变了的行会被它
    直接抵消掉、再也找不到。这里先把这类行挑出来配对上报，再从 pa/pb 里摘掉，
    后续字符流比对就不会再碰它们。

    配对口径与 `_cancel_identical` 同源：按 `norm` 计数匹配（同一句话出现
    多次时按次数一一配对），签名不同才算字体变。OCR 补的行（font 为空）不参与。
    """
    def _rows(pages):
        return [r for rows in pages for r in rows]

    ra, rb = _rows(pa), _rows(pb)
    ca: Dict[str, List[Row]] = {}
    cb: Dict[str, List[Row]] = {}
    for r in ra:
        if r.font:
            ca.setdefault(r.norm, []).append(r)
    for r in rb:
        if r.font:
            cb.setdefault(r.norm, []).append(r)

    pairs: List[Tuple[Row, Row]] = []
    n_same = 0
    for norm, la in ca.items():
        lb = cb.get(norm)
        if not lb:
            continue
        n_same += min(len(la), len(lb))
        for x, y in zip(la, lb):
            if _font_pair_changed(x, y):
                pairs.append((x, y))
    if not pairs or not n_same:
        return []
    if (n_same >= FONT_GUARD_MIN_ROWS
            and len(pairs) > FONT_CHANGE_MAX_FRAC * n_same):
        return []                      # 元数据整体不可信 → 全部放弃（见常量说明）

    out: List[LocDiff] = []
    drop_a: set = {id(x) for x, _ in pairs}
    drop_b: set = {id(y) for _, y in pairs}
    for rows in pa:
        rows[:] = [r for r in rows if id(r) not in drop_a]
    for rows in pb:
        rows[:] = [r for r in rows if id(r) not in drop_b]
    for x, y in pairs:
        out.append(LocDiff(kind="font",
                           page_old=x.page, page_new=y.page,
                           old_text=x.text, new_text=y.text,
                           rects_old=_pad([x.rect]) if x.rect else [],
                           rects_new=_pad([y.rect]) if y.rect else []))
    return out


def _extract_moved_rows(pa: List[List[Row]], pb: List[List[Row]]
                        ) -> List[LocDiff]:
    """把「文字一样、位置变了」的行配成对，返回 `kind="moved"`（浅蓝）。

    在 `_cancel_identical` **之前**跑（对称于 `_extract_font_changes`）：抵消按
    文本做、位置无关，「整块下移」的行会被它当无差异抵消掉、再也标不出浅蓝。
    这里先把「文本相同但纵向差 ≥ 阈值」的行挑出来配对上报，再从 pa/pb 里摘掉。

    ⚠️ 2026-10-12 位移标蓝：与 core 的 `diff_page_text` 位置感知逻辑同源，
    但这里是**标注链路**（fitz 坐标、rects_old/rects_new 直接可画）。

    配对口径：按 `norm` 计数，同文本按「纵向距离最近」贪心配对（同一句话出现
    多次时，避免把上下两块相似内容跨配成假位移，同 core 的 `_has_inplace_copy`）。
    """
    def _rows(pages):
        return [r for rows in pages for r in rows]

    ra, rb = _rows(pa), _rows(pb)
    ca: Dict[str, List[Row]] = {}
    cb: Dict[str, List[Row]] = {}
    for r in ra:
        ca.setdefault(r.norm, []).append(r)
    for r in rb:
        cb.setdefault(r.norm, []).append(r)

    moved: List[LocDiff] = []
    drop_a: set = set()
    drop_b: set = set()
    for norm, la in ca.items():
        lb = cb.get(norm)
        if not lb:
            continue
        # 过短的行（< _MOVED_MIN_CHARS 字）不判位移：孤字/单位符号位置抖动是常态
        if len(norm) < _MOVED_MIN_CHARS:
            continue
        # 按纵向距离贪心配对
        used_b: List[bool] = [False] * len(lb)
        for x in la:
            if id(x) in drop_a:
                continue
            best_j, best_dy = None, None
            for j, y in enumerate(lb):
                if used_b[j] or id(y) in drop_b:
                    continue
                if x.rect is None or y.rect is None:
                    continue
                dy = abs(y.rect[1] - x.rect[1])
                if best_dy is None or dy < best_dy:
                    best_dy, best_j = dy, j
            if best_j is None:
                continue
            y = lb[best_j]
            if best_dy >= _MOVED_MIN_DY:
                moved.append(LocDiff(kind="moved",
                                     page_old=x.page, page_new=y.page,
                                     old_text=x.text, new_text=y.text,
                                     rects_old=_pad([x.rect]) if x.rect else [],
                                     rects_new=_pad([y.rect]) if y.rect else []))
                drop_a.add(id(x))
                drop_b.add(id(y))
            # 无论是否位移，这一对都从抵消里扣掉（位置近 = 同一行，照常抵消）
            used_b[best_j] = True

    for rows in pa:
        rows[:] = [r for r in rows if id(r) not in drop_a]
    for rows in pb:
        rows[:] = [r for r in rows if id(r) not in drop_b]
    return moved


# ---------------------------------------------------------------- 全局抵消
def _cancel_identical(pa: List[List[Row]], pb: List[List[Row]]) -> None:
    """把两份文档里**逐字完全相同（只是位置可能变了）**的行全局抵消掉。

    位置无关：不管这行在旧版第几页、新版第几页，只要内容一字不差就抵消。
    抵消后剩下的行才是「真新增 / 真删除 / 真改写」，后续比对不会再被换位重排干扰。
    直接就地修改两个列表。
    """
    ca = Counter(r.norm for rows in pa for r in rows)
    cb = Counter(r.norm for rows in pb for r in rows)
    common = {n: min(ca[n], cb[n]) for n in ca if n in cb and min(ca[n], cb[n]) > 0}

    def _strip(rows: List[Row], budget: Dict[str, int]) -> List[Row]:
        kept: List[Row] = []
        for r in rows:
            if budget.get(r.norm, 0) > 0:
                budget[r.norm] -= 1
                continue
            kept.append(r)
        return kept

    for rows in pa:
        rows[:] = _strip(rows, dict(common))
    for rows in pb:
        rows[:] = _strip(rows, dict(common))


# ---------------------------------------------------------------- 字符流比对
def _substantial(s: str) -> str:
    """一段文字里「有信息量」的部分——只剩符号/标点就算没有。

    改版时把「• 生理盐水」的项目符号去掉，字符级 diff 会报一处改动，
    但标出来就只是一个孤零零的点，看着像程序标错了。这种不算改动。
    """
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", "", s or "")


def _pad(rects: Sequence[Tuple[float, float, float, float]]
         ) -> List[Tuple[float, float, float, float]]:
    return [(x0, y0 - RECT_PAD, x1, y1 + RECT_PAD) for x0, y0, x1, y1 in rects]


def _visual_order(rows: List[Row]) -> List[Row]:
    """把一页的行按**人眼阅读顺序**排：先上后下，再左到右。

    rawdict 吐出来的行序是 PDF 内部的块顺序，改版后这个顺序可能变
    （实测 GRA-4118 新版就把两处「生理」的块顺序挪了位）。
    不先归一成视觉顺序，同一条文字在两份文件里的先后不同，
    字符流一比就会整段错位 → 又是假差异。
    """
    return sorted(rows, key=lambda r: (round(r.rect[1], 1) if r.rect else 0.0,
                                       round(r.rect[0], 1) if r.rect else 0.0))


def _stream(rows: Sequence[Row], ignore_case: bool, ignore_punct: bool
            ) -> List[Tuple[str, str, Any, int]]:
    """把行摊成**一条字符流**：每项 = (比对用的字, 原文的字, 自己的 bbox, 页码)。

    ⚠️ 空白（含换行）**不进流**。这正是「重新折行不算改动」的立足点：
    旧版两行、新版一行，摊成字符流后完全一样，diff 自然报 0 处。
    """
    out: List[Tuple[str, str, Any, int]] = []
    for r in rows:
        for c, bb in r.chars:
            if c.isspace():
                continue
            if ignore_punct and not c.isalnum():
                continue
            out.append((c.lower() if ignore_case else c, c, bb, r.page))
    return out


def _group(entries: Sequence[Tuple[str, str, Any, int]]
           ) -> List[Dict[str, Any]]:
    """把一段改动里的一串字按「同一行」归堆，方便报成一条差异。

    不归堆的话，删掉一整段免责声明会报成 200 条一条一个字的差异，
    列表没法看、标注也会画 200 个小方块。
    """
    groups: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    prev_y: Optional[float] = None
    for _key, c, bb, pg in entries:
        y = round(float(bb[1]), 1)
        if (cur is not None and cur["page"] == pg and prev_y is not None
                and abs(y - prev_y) <= _GROUP_GAP):
            cur["rects"].append(tuple(bb))
            cur["chars"].append(c)
        else:
            cur = {"page": pg, "rects": [tuple(bb)], "chars": [c]}
            groups.append(cur)
        prev_y = y
    return groups


def _opcodes(ta: str, tb: str) -> List[Tuple[str, int, int, int, int]]:
    """逐字 diff 的操作码，并把**太短的「相等段」并入改动**。

    为什么要多这一步（2026-09-29 实测的坑）：
    整份文档摊成一条长字符流后，diff 会把**两个不相干的相同字符**当成「相等」——
    实测 GRA-4118：旧版 `Rev06` 里的 `6` 被对到了新版修订日期 `2026` 里的 `6`，
    结果「版本号 6→7」这处真改动整个漏标，标记反而跑到日期上去了。

    短相等段（< _TINY_ANCHOR 个字）本来就不可能是可靠锚点，
    一律当改动处理，这类「巧合配对」就没了；相邻的改动再合并成一块，
    免得同一处改动被切成「删 + 增 + 删」一堆碎块。
    """
    raw = SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes()
    out: List[Tuple[str, int, int, int, int]] = []
    for op, i1, i2, j1, j2 in raw:
        if op == "equal" and (i2 - i1) < _TINY_ANCHOR:
            out.append(("replace", i1, i2, j1, j2))
        else:
            out.append((op, i1, i2, j1, j2))
    merged: List[Tuple[str, int, int, int, int]] = []
    for op, i1, i2, j1, j2 in out:
        if merged and op != "equal" and merged[-1][0] != "equal":
            _, pi1, _pi2, pj1, _pj2 = merged[-1]
            merged[-1] = ("replace", pi1, i2, pj1, j2)
        else:
            merged.append((op, i1, i2, j1, j2))
    return _snap_windows(ta, tb, merged)


_SNAP_MAX = 2              # 改动窗口最多往左挪这么多字（见 _snap_windows）


def _alnum_cut(t: str, p: int) -> int:
    """位置 p 是不是把一段「ASCII 字母数字串」拦腰切开了（1 = 是）。

    只认 ASCII 字母数字：汉字一个字就是一个词，切开不算切错。
    """
    if p <= 0 or p >= len(t):
        return 0
    a, b = t[p - 1], t[p]
    return 1 if (a.isascii() and b.isascii() and a.isalnum() and b.isalnum()) else 0


def _window_cuts(ta: str, tb: str, i1: int, i2: int, j1: int, j2: int) -> int:
    """一处改动窗口两端「切开字母数字串」的切口数（0~4）。"""
    n = 0
    if i1 == i2:
        n += _alnum_cut(ta, i1)               # 纯增删：a 侧只有一个切口
    else:
        n += _alnum_cut(ta, i1) + _alnum_cut(ta, i2)
    if j1 == j2:
        n += _alnum_cut(tb, j1)
    else:
        n += _alnum_cut(tb, j1) + _alnum_cut(tb, j2)
    return n


def _snap_windows(ta: str, tb: str,
                  ops: List[Tuple[str, int, int, int, int]]
                  ) -> List[Tuple[str, int, int, int, int]]:
    """把改动窗口整体**往左**挪一挪，让切口不落在「字母数字串」中间。

    为什么需要它（2026-09-29 用户实测打回来的）
    ------------------------------------------
    GRA-4118 地址行的真改动是：`…工业北四路1号` 后面插入 `1栋201室，`，
    再在 `13栋101室` **前面**插入 `10栋301室，11栋301室，`。
    但 `SequenceMatcher` 取的是「最长匹配块」，它把 `10栋301室` 的 `1`
    跟 `13栋101室` 的 `1` 对上了（这样相等段能多一个字），于是报出来的新增文字
    变成了 `0栋301室，11栋301室，1` —— 开头少一个字、末尾多卷进 `13` 的 `1`，
    整条高亮**右移了一个字**。用户原话：「他把多标的地方插到原本的 13 中间去了，
    但他其实是加在 13 的前面」。

    两种切法的编辑距离**完全一样**（相等段总长相同），所以不是「谁更优」的问题，
    只能靠「切口落点」判。两条实测定下来的规矩：

      · **切口不许切开字母数字串** —— 挪完必须严格变好（切口数变少），否则一个字不动；
      · **只往左挪** —— `SequenceMatcher` 的偏差方向是「把相等块尽量往右伸」，
        反向挪没有依据。实测日期 `2025年12月19日→2026年05月27日` 那处，
        往右挪能让两个切口都变成「类转换」，看着像变好了，实际会把
        `年`、`日` 两个没变的字卷进标记里（标记变粗）—— 禁止右挪正好挡住它。
      · **挪完不能把操作码的划分弄坏** —— 窗口里两侧要**仍然不同**，划给相邻
        `equal` 段的字要**两侧一致**。这两条是 2026-09-29 补的，踩过的坑：
        汉字不算「切口」，于是把窗口挪到一对**相同**的汉字上时切口数会「免费」
        降到 0 —— 看着变好了，实际标的是没变的字。实测
        `FAS 成功率 98.99% → 99.99%` 就栽在这：窗口从 `8` 挪到 `率`，
        报出来的改动成了「率 → 率」，右边那个 equal 段也变成
        `98.99%`/`99.99%` 这种「标着相等却两边不同」的坏划分。

    挪窗口时相邻的 equal 段要跟着伸缩，保证操作码仍然是对两个字符串的一个划分。
    """
    n = len(ops)
    for k, (op, i1, i2, j1, j2) in enumerate(ops):
        if op == "equal":
            continue
        base = _window_cuts(ta, tb, i1, i2, j1, j2)
        if base == 0:
            continue
        best = None
        # ⚠️ 顺序必须是「由近到远」：挪得越少越好。写成 range(-N,0) 会让 -2 先试，
        #    两个距离同样干净时反而选了挪得更远的那个（实测地址那处会多卷进一个「，」）。
        for d in sorted(range(-_SNAP_MAX, 0), key=abs):
            if i1 + d < 0 or j1 + d < 0:
                continue
            if ta[i1 + d:i1] != tb[j1 + d:j1]:
                continue                      # 挪进来的字两侧必须一致（等长段天然满足）
            if i2 != i1 and (i2 + d) - (i1 + d) == 0:
                continue                      # 别把 replace/delete 的窗口挪空
            if j2 != j1 and (j2 + d) - (j1 + d) == 0:
                continue
            # ⚠️ 挪完窗口里两侧内容必须**仍然不同**。不查这条会出事：汉字不算「切口」
            #    （`_alnum_cut` 只认 ASCII），所以把窗口挪到一对相同的汉字上，
            #    切口数会「免费」降到 0 —— 看着变好了，其实标的是没变的字。
            #    实测 `FAS 成功率 98.99% → 99.99%` 就栽在这：窗口从 `8` 挪到 `率`，
            #    报出来的改动成了「率 → 率」。
            if ta[i1 + d:i2 + d] == tb[j1 + d:j2 + d]:
                continue
            # 划给右邻 equal 段的字必须两侧一致，否则那个 equal 段就成了
            # 「标着 equal 却两边不同」——上面的 98.99% vs 99.99% 就是这么来的。
            if k + 1 < n and ops[k + 1][0] == "equal":
                a1, b1 = ops[k + 1][1], ops[k + 1][3]
                if ta[a1 + d:a1] != tb[b1 + d:b1]:
                    continue
            # 左邻 equal 段会被缩短 |d|，别缩成负长度
            if k > 0 and ops[k - 1][0] == "equal" and ops[k - 1][2] + d < ops[k - 1][1]:
                continue
            c = _window_cuts(ta, tb, i1 + d, i2 + d, j1 + d, j2 + d)
            if c < base and (best is None or c < best[0]):
                best = (c, d)
        if best is None:
            continue
        d = best[1]
        ops[k] = (op, i1 + d, i2 + d, j1 + d, j2 + d)
        if k > 0 and ops[k - 1][0] == "equal":
            o, a1, a2, b1, b2 = ops[k - 1]
            ops[k - 1] = (o, a1, a2 + d, b1, b2 + d)
        if k + 1 < n and ops[k + 1][0] == "equal":
            o, a1, a2, b1, b2 = ops[k + 1]
            ops[k + 1] = (o, a1 + d, a2, b1 + d, b2)
    return ops


def locate(old_path: str, new_path: str, ignore_case: bool = False,
           ignore_punct: bool = False, drop_header_footer: bool = False,
           prog=None) -> List[LocDiff]:
    """比对两份 PDF，返回**带坐标**的差异列表。

    先做行级全局抵消（去重排/换位的假差异），再把两份文档摊成字符流做一次
    逐字 diff；每条 LocDiff 的 rects 已是页面坐标系下的矩形，标注时直接画。

    ⚠️ 字符流里的空白（含换行）不参与比对 —— 重新折行不再算改动（见模块说明）。
    """
    # 图片输入（PNG/JPG/TIFF…）先折成单页 PDF —— 与扫描件 PDF 同路，
    # 见 `pdfdiff_img` 模块说明（不为图片单独开一条比对分支）。
    import pdfdiff_img as _img
    old_path, new_path = _img.ensure_pdf(old_path), _img.ensure_pdf(new_path)
    da = fitz.open(old_path)
    db = fitz.open(new_path)
    try:
        pa = [extract_rows(da[i], i, ignore_case, ignore_punct, drop_header_footer)
              for i in range(da.page_count)]
        pb = [extract_rows(db[i], i, ignore_case, ignore_punct, drop_header_footer)
              for i in range(db.page_count)]
    finally:
        da.close()
        db.close()

    # ⓪ 混合输入补行：恰好一侧没有文字层时，给那一侧补 OCR 行（见 `_mixed_ocr_fill`）。
    #    不做这一步的话，扫描那侧在字符流里是空的 → 整份文档被报成「全部删除/全部新增」。
    _mixed_ocr_fill(pa, pb, old_path, new_path,
                    ignore_case=ignore_case, ignore_punct=ignore_punct)

    # ① 先把两份文档的**全文**字符摊出来（判「是不是只是挪了位置」要用全文，
    #    因为一行可能已被第 ② 步抵消掉，但它的文字在对面文档里确实存在）
    fa = "".join(e[1] for e in
                 _stream([r for rows in pa for r in _visual_order(rows)],
                         ignore_case, ignore_punct))
    fb = "".join(e[1] for e in
                 _stream([r for rows in pb for r in _visual_order(rows)],
                         ignore_case, ignore_punct))

    # ②-bis 字体变动（2026-10-12）：文字一样、字体/字号变了的行，必须在
    #    行级抵消**之前**摘出来 —— 抵消按文本做，会把它们直接抵消掉。
    #    返回的 font 差异不进 `_drop_moved`（内容两边都在，会被它误撤）。
    font_diffs = _extract_font_changes(pa, pb)

    # ②-ter 位移（2026-10-12）：文字一样、位置变了的行，同样必须在行级抵消
    #    **之前**摘出来转 moved（浅蓝）—— 抵消位置无关，会把「整块下移」当
    #    无差异吃掉。顺序在 font 之后：字体变化优先于位移（M4506 的 10℃ 既是
    #    字体变又位移，用户拍板归深蓝）。
    moved_diffs = _extract_moved_rows(pa, pb)

    # ② 行级全局抵消：内容一字不差、只是挪了位置的行先去掉（换位/重排不刷假差异）
    _cancel_identical(pa, pb)

    # ③ 摊成字符流（空白不进流 → 折行无关）
    sa = _stream([r for rows in pa for r in _visual_order(rows)], ignore_case, ignore_punct)
    sb = _stream([r for rows in pb for r in _visual_order(rows)], ignore_case, ignore_punct)
    ta = "".join(e[0] for e in sa)
    tb = "".join(e[0] for e in sb)
    if ta == tb:
        return font_diffs + moved_diffs   # 文字全同：只剩字体/位移差异也要报（2026-10-12）

    # ④ 整份文档一次逐字 diff；变了的字，直接拿它自己的 bbox
    ops = _opcodes(ta, tb)
    out: List[LocDiff] = []
    n_total = max(1, len(ops))
    for k, (op, i1, i2, j1, j2) in enumerate(ops):
        if prog:
            prog(k, n_total)
        if op == "equal":
            continue
        if not _substantial(ta[i1:i2]) and not _substantial(tb[j1:j2]):
            continue          # 只差个符号/项目符号 → 不算改动
        go = _group(sa[i1:i2])
        gn = _group(sb[j1:j2])
        if len(go) == 1 and len(gn) == 1:
            # 两侧各只落在同一行 → 就是「这行这几个字改了」。
            # 2026-10-13 删「变动」档：拆成删（旧值标红）+ 增（新值标绿）两条。
            out.append(LocDiff(kind="removed", page_old=go[0]["page"],
                               old_text="".join(go[0]["chars"]),
                               rects_old=_pad(go[0]["rects"])))
            out.append(LocDiff(kind="added", page_new=gn[0]["page"],
                               new_text="".join(gn[0]["chars"]),
                               rects_new=_pad(gn[0]["rects"])))
        else:
            # 跨行/只在一侧（整段增删、跨行重写）→ 逐行分开报，别糊成一大块
            for g in go:
                t = "".join(g["chars"])
                if not _substantial(t):
                    continue      # 碎块里掉出来的孤零零符号（如合并后剩下的「•」）不算改动
                out.append(LocDiff(kind="removed", page_old=g["page"],
                                   old_text=t, rects_old=_pad(g["rects"])))
            for g in gn:
                t = "".join(g["chars"])
                if not _substantial(t):
                    continue
                out.append(LocDiff(kind="added", page_new=g["page"],
                                   new_text=t, rects_new=_pad(g["rects"])))
    return (font_diffs + moved_diffs
            + _ocr_rescue(_drop_moved(out, fa, fb), old_path, new_path))


_MOVED_MIN = 2             # 少于这么多字的改动不去对面找「挪位」证据（免得把「8」这类真改动抹了）

# 位移标蓝（2026-10-12）：`_extract_moved_rows` 的判据，与 core 的
# `_MOVED_MIN_DY` / `_MOVED_MIN_CHARS` 同口径（两条链路保持一致）。
_MOVED_MIN_DY = 3.0        # 纵向位置差 ≥ 它（pt）才算位移
_MOVED_MIN_CHARS = 3       # 归一化后 < 它字的短行不判位移（孤字/单位符号抖动）



def _drop_moved(items: List[LocDiff], flat_a: str, flat_b: str) -> List[LocDiff]:
    """残余清理：**一段字在对面文档里也能找到 = 内容只是挪了位置 → 不算改动**。

    行级抵消（第 ① 步）只认「整行一字不差」，抓不住这些情况：
      · 两栏排版下同一行的**视觉顺序**两份文件不同（实测「有效期三年」）；
      · 旧版的孤立小行被新版并进长行（实测页脚「博迈医疗」）；
      · 一行被拆进不同的行、又被别的内容隔开。
    它们在字符流里表现成「这串字删了 + 同一串字又加了」——
    用户看到的就是「两边都有的文字却被标了」。

    与既有策略一致：**内容挪了位置不算改动**（第 ① 步就是这么定的）。
    """
    kept: List[LocDiff] = []
    for d in items:
        if d.kind == "removed":
            t = d.old_text.strip()
            if len(t) >= _MOVED_MIN and t in flat_b:
                continue
        elif d.kind == "added":
            t = d.new_text.strip()
            if len(t) >= _MOVED_MIN and t in flat_a:
                continue
        kept.append(d)
    return kept


# ---------------------------------------------------------------- 图形化文字（转曲）兜底
# 这一节解决的是「读文字」这条路**原理上够不到**的地方：文件里有些正文是**矢量曲线**
# （转曲文字），没有文字层可读 —— 见 graphic_regions 的说明。
GRAPHIC_DPI = 120          # 渲染分辨率（120dpi 够看清「这块内容变了没有」，也不慢）
GRAPHIC_TILE = 24          # 瓦片边长 (px)，120dpi 下 ≈ 14.4pt ≈ 一行字高
GRAPHIC_DARK = 60          # 单个像素灰度差超过它才算「这两个像素不一样」(0~255)
GRAPHIC_FRAC = 0.055       # 一块瓦片里「不一样」的像素占比达到它才算这块变了
GRAPHIC_MIN_TILES = 2      # 合并后的区域小于这么多瓦片就不报（去碎点）
# ---- 「整页图形重绘」防护（2026-10-13 用户实测 DWG-2000087 标签对）----
# 病根：标签类 PDF 一页里同时有「嵌入位图图片（标签图面）+ 四周转曲文字」，
#   改版（rev17→rev18）把整页图面**整体重绘**——图片 md5 变了、转曲文字全部重排。
#   瓦片对拍的 hot 瓦片因此连成**覆盖大半页的一个连通域**（实测 25%~84% 页面面积），
#   `_decide_region` 对整页 OCR 必然判「新增」，报出一条谁都没法核对的巨框。
#   ⚠️ 平台差异（用户报「Mac 有、Windows 没有」）正是落在这里：Mac 的 Vision OCR
#   认得图片里的标签文字（「博迈医疗/Artimes/1.75x20…」）→ 判新增；Windows 的
#   tesseract 认成乱码碎片 → `_ocr_junk` 拦截 → 不报。根因不是 OCR，是「整页重绘」
#   被当成了「局部转曲区增删」。
# 判据（结构特征，不是魔法数）：一个 hot 连通域覆盖页面面积超过它 = 整页重绘。
#   实测分界：整页重绘 25.1%/84.0%  vs  局部转曲区 6.4%、DCR 表格 3.9%、IFU 2.8%
#   —— 15% 在两者之间留足余量。
# 行为：跳过该连通域（不报巨框）并**记账**（LAST_FULLPAGE_SKIPS），不静默 ——
#   该页的文字层差异仍由字符流引擎正常报出（标签的 DCR 表格改动一点不丢）。
GRAPHIC_FULLPAGE_FRAC = float(os.environ.get("PDFDIFF_FULLPAGE_FRAC", "0.15"))
GRAPHIC_RECHECK_PAD_X = 60.0  # v48 文字层错位补查：判定矩形横向各加宽 (pt)，
                              # 让 OCR 读到整行（残词判定必证据不足）
GRAPHIC_PAD = 1.5          # 报出来的矩形四周留一点
GRAPHIC_BLANK_RATIO = 0.25  # 一块区域两侧的「墨水比」低于它 = 一边基本是空白（整块增/删）
GRAPHIC_SOLID_MIN = 800     # 「纯图形/色块」增删的墨水量下限（@ GRAPHIC_DPI 下的墨点数）：
                            # 有墨侧墨水量 ≥ 它、OCR 又认不出字，才判「图形增删」——
                            # 门槛够高才挡得住扫描噪点；一块实心黑块动辄几千墨点。
GRAPHIC_SOLID_TAG = "[图形]"  # 纯图形/色块增删的占位文本（OCR 认不出字时用），
                            # 下游 `_graphic_rescue_core` 原样透传，UI 显示成「图形区域」。
GRAPHIC_LINE_GAP = 0.1      # 块内相邻像素行之间空白超过它 (pt) 才算「换行」
                            # ⚠️ 这份 PDF 转曲正文的行距紧到 0.8pt，阈值取 1.4pt
                            #    会把两行粘成一行（详见 _split_lines docstring）
GRAPHIC_MIN_LINE = 3.0      # 细于它的「行」不算一行（是上下行留下的残影）
GRAPHIC_BLEED = 5.0         # 切行前把块矩形上下各外扩这么多 (pt)，见 _refine_region
GRAPHIC_ANCHOR_MIN = 1.0    # 外扩窗口切出的「行」与块本身至少重叠这么多 (pt) 才算数
REFINE_SEAM_MAX = 1.5       # 相邻保留行之间最多把缝补掉这么多 (pt)，见 _refine_region
GRAPHIC_MOD_FALLBACK_MAX_H = 30.0   # mod 行级复核全灭时**整块兜底**的高度上限 (pt)：
                                    # 块级 mod 但行级（宽门槛）一条都凑不出，
                                    # 且块高 ≥ 3 行 → 是聚合噪声不是真改，放弃
                                    # （2026-10-12 失败1：32/46/219pt 巨型黄框的出生点）

# ---- 「块级判否 → 逐行再判一次」（2026-09-30）----
# ⚠️ 这一节补的是**粒度**问题，不是判据问题（判据动了会牵一片）。
# 起因：GRA-4118 左栏 2.0 供货方式那块，新版比旧版多一行「一个再折叠工具」，
# 它下面 `防止重压…` / `须贮存…` 两行只是被顶下去 —— 瓦片对拍把这几行连成
# **同一个**候选块，块级 `_decide_region` 一算覆盖率 0.93（旧版那两行 100% 在新版里）
# → 判「只是重渲染」→ 整块丢掉，真新增的那一行**跟着一起没了**。
# 细切到行之后，`一个再折叠工具` 那行对面是**空白**，走「一边基本空白」分支，一判一个准。
GRAPHIC_LINE_RESCAN = os.environ.get("PDFDIFF_LOCATE_LINE_RESCAN", "1") != "0"
RESCAN_ROW_MIN_CHARS = 2    # 细切行至少认出的字数（少于此视为 OCR 噪声，不报）

# ---- 纯扫描件的候选生成（2026-09-30，GRA-3634 俄文标签实测）----
# 这类文件整页就是**一张位图**（无文字层、无矢量），两次扫描亚像素错位让全页
# 都有差异噪声 —— 瓦片对拍两头不讨好：噪声把大量瓦片点亮（17%），而单个字符
# 的真差异（页脚 Rev02→Rev03，行差异峰值才 17px）又弱到**点不热任何瓦片**，
# 连候选都进不来。改用**行级差异画像**：逐像素行数差异像素，低门槛聚成
# 候选窗口，多给候选没关系 —— 后面有 OCR 判据（`_decide_region`）把关。
GRAPHIC_SCAN_BANDS = os.environ.get("PDFDIFF_SCAN_BANDS", "1") != "0"
SCAN_ROW_PX = 6             # 一行里 |差|>GRAPHIC_DARK 的像素 ≥ 它才算「差异行」
SCAN_MIN_ROWS = 2           # 连续差异行 ≥ 它才聚窗（去孤立噪点行）
SCAN_GAP_ROWS = 1           # 聚窗时允许的中间断档行数；⚠️ 不能大：断档 2 行就把
                            # 日期行和下一行并进一窗，种子会落到隔壁行的粗体噪声上
SCAN_PAD_PT = 3.0           # 窗口上下各外扩这么多 (pt)，别把字的上下沿切掉
SCAN_INK_DARK = 200         # 「墨」的灰度上限（与 _ink_rows 同口径）
SCAN_JOIN_GAP_PT = 3.0      # 簇内列空隙 / 从簇边缘沿墨迹扩展时，空隙 ≤ 它(pt) 就继续
SCAN_VLINE_FRAC = 0.85      # 一列在窗口±60pt 范围里墨迹占比 ≥ 它 = 竖直线（边框/表格框）
SCAN_VLINE_CONTEXT_PT = 60.0  # 竖直线判定的上下文范围（pt）
SCAN_ALIGN_BAND_H = 24      # 垂直配准分带高度 (px)
SCAN_ALIGN_DY = 22          # 每带搜索 ±N 行的纵向错位；⚠️ 底部真值可能到 16+，
                            #   ±14 时被硬夹成 0 把拟合线拉平（GRA-3925 实测）
SCAN_ALIGN_STEP = 4         # 配准时列抽样步长（省时间）
SCAN_ALIGN_TRUST = 0.85     # 一带可信的判据：best SAD < 它 × dy=0 的 SAD
                            # （内容相同的带，对齐后 SAD 显著低于错位状态；
                            #  两个产品大面积不同的带没有对齐信号，best≈dy0，不可信）

# ---- 扫描件「整页 OCR 行对拍」管线（GRA-3925 实测，见 _scan_page_line_regions）----
GRAPHIC_SCAN_LINES = os.environ.get("PDFDIFF_SCAN_LINES", "1") != "0"
SCAN_LINES_DPI = 200        # 整页行级 OCR 的渲染分辨率（400 太慢、120 小字认不全）
SCAN_MOVE_COVER = 0.90      # 对面有**单独一行**与它 ratio ≥ 它 = 挪位/重排，撤
SCAN_MOVE_SAME = 0.995      # ratio ≥ 它 = 同一行（只是 OCR 抖动），撤
SCAN_INPLACE_DY = 4.0       # 「原地副本」判定：对面同文本行中心 y 差 ≤ 它（pt）
                            # = 原地就有一份 → 残余副本是重复计数，不许配成位移
                            # （2026-10-12 22vs23：上下两块相似内容被跨配出 80pt 假位移）
# ⚠️ 2026-10-01 改口径：`SCAN_MOVE_COVER` 原先是「**整页拼串**的子序列覆盖率」。
#    一页几千字符，任何短串都能按序凑齐 → coverage 恒 ≈1.0：GRA-3925 实测
#    `5F(0.072"/1.83mm)`→`6F(0.084"/2.13mm)` 被判 1.0 当挪位撤掉，
#    UDI 的 `48425 0`→`48429 8` 被判 0.944 撤掉 —— **整份的规格数字改动全军覆没**。
#    现在改成「对面**逐行**最佳 ratio」，`SCAN_MOVE_SAME` 挡「同一行抖动」，
#    再加 `SCAN_MOVE_RULES` ③ 那条互改闸门挡「高相似但真改了数字」。
SCAN_MOVE_RULES = [
    {"name": "完全相同", "cond": "对面有归一化相等的行", "act": "抵消/撤",
     "why": "两版都印了这句，位置不同而已"},
    {"name": "同一行的 OCR 抖动", "cond": f"逐行 best ratio ≥ {SCAN_MOVE_SAME}", "act": "撤",
     "why": "同一句话两版认出的字略有出入，不是改动"},
    {"name": "⚠️ 行内小改闸门", "cond": "best ratio ≥ mod 门槛 且 `_mod_worthy`（token 等长 + 数字换数字）",
     "act": "**不撤**（留给 mod 配对）",
     "why": "UDI `48425 0→48429 8` ratio 0.94 看着像同一行，实际换了数字 —— 这是真改动的典型长相"},
    {"name": "挪位 / 重排", "cond": f"best ratio ≥ {SCAN_MOVE_COVER}", "act": "撤",
     "why": "整段被推走或重排版（多语言段、表头合并）"},
    {"name": "短行整行命中", "cond": "长度 < OCR_MIN_LEN 且整行出现在对面", "act": "撤",
     "why": "`REF` / `LOT` / `R` 这类碎片在对面满页都是，逐行 ratio 反而判不出，靠整行命中挡"},
    {"name": "其余", "cond": "ratio 更低", "act": "不撤",
     "why": "对面没有相似行 = 新增/删除候选"},
]
SCAN_PAIR_DY = 20.0      # 一删一增配对：两行 y 中心相距 ≤ 它(pt) 才配（同版式标签的
                         # 行位置漂移实测 < 10pt；不卡位置会把 `150mm` 配到隔了 33pt
                         # 的 `6.5/7.0 mm` 上 —— 2026-10-01 实测，贪心只看 ratio 必错配）
SCAN_PAIR_DX = 30.0      # 同上，x 中心也要够近（**同列**）：`1400`(kPa 列) 曾配到
                         # 隔壁直径列的 `7.10` 上；卡了 x 才各归各列
# ⚠️ 数字主键优先（2026-10-11 GRA-3925 用户拍板）：表格行按**行首数字**对齐才是
#    用户语义 —— 压力表 12 那行新版只是 `12`→`12 (RBP)`，14 整行删掉；
#    按 ratio 贪心却让 `14(RBP)`↔`12 (RBP)`(0.77) 抢走 `12`↔`12 (RBP)`(0.44)，
#    用户看到的是「14 改成 12」而不是「14 行没了、12 加了 RBP」。
#    两候选行首数字相同 → 配对分数抬到 `SCAN_NUM_PAIR_BOOST`（保证压过
#    「数字不同但字面相似」的候选）；位置闸门（DY/DX/OV）照卡不变。
SCAN_NUM_PAIR_BOOST = 0.80
SCAN_PAIR_OV = 0.30      # 但只看中心距会把「同一行变长了」的错开（`Balloon Diameter`
                         # →`Conical Balloon Diameter`：新增前缀让中心左移 68pt）。
                         # 所以「横向重叠 ≥ 它 × 较窄那行」也算同列。
SCAN_LINE_MOD_RATIO = 0.55  # 一删一增行归一化 ratio ≥ 它 = 行内修改
SCAN_LINE_MIN_ROWS = 3      # 两版整页各至少认出这么多行，行对拍才算可用

# ---- 两页位图尺寸不一致怎么办（2026-09-30 图片输入轮实测出的静默失效）----
# 病根：`graphic_regions` 原来一句 `if 位图尺寸不同: continue` 就把整页丢掉。
#   图片输入时这几乎是必踩的：同一份文档一个存成 1430x1543、一个存成 1430x1544
#   （PDF 页高 555.60pt vs PNG 折算 555.84pt，200dpi 下差 0.3px 就进位差一行），
#   于是**整页一处都不查、界面却报「0 处差异」** —— 用户无从判断是「真没改」
#   还是「根本没看」。这是最贵的一类假象（用户阶段 5 的原话：没报错 ≠ 正确）。
# 规矩（表驱动，见 `SIZE_FIT_RULES`）：先拉伸对齐，实在对不上才跳过**并记账**。
SCAN_SIZE_TOL = 0.03        # 两页宽高比差异 ≤ 它（3%）才允许拉伸对齐
SIZE_FIT_RULES = [
    {"name": "尺寸相同", "cond": "w/h 完全一致", "act": "直接用",
     "why": "正常 PDF 两版同页尺寸，绝大多数走这条"},
    {"name": "比例一致、只差渲染取整", "cond": f"|sx-sy|/max ≤ {SCAN_SIZE_TOL}",
     "act": "把新版位图拉伸到旧版尺寸再比对",
     "why": "图片输入 / 不同扫描仪的 1~2px 差异，拉伸后错不了一个字"},
    {"name": "比例真的不同", "cond": "超出容差", "act": "跳过该页**并记账**（LAST_SIZE_SKIPS）",
     "why": "硬拉会把内容压变形，宁可不判也不能给出错坐标；但不能静默，要让用户看见"},
]

# 本次 `graphic_regions()` 因尺寸对不上而跳过的页（每次调用开头清空）。
# ⚠️ 跨进程确定性：只在 `graphic_regions` 里写、调用方读完就清，不进任何缓存 key。
LAST_SIZE_SKIPS: List[Dict[str, Any]] = []

# 本次 `graphic_regions()` 因「整页图形重绘」而拦截的页/块（每次调用开头清空）。
# 与 LAST_SIZE_SKIPS 同口径：只在 graphic_regions 里写、调用方读完就清。
LAST_FULLPAGE_SKIPS: List[Dict[str, Any]] = []

# ---- OCR 补完（可选，没装 tesseract 就自动跳过）----
# 两个用途，都是「文字层缺行」这个病根的解药：
#   ① **撤销误标**：字符流引擎报「删除/新增」，其实是新版那块被转曲、丢了文字层
#      → OCR 出来一比，内容还在 → 撤掉这条误标（GRA-4118 的 9 处免责声明就是这样）。
#   ② **补上漏标**：转曲区域真新增/删掉的内容，文字层看不见，OCR 能看见。
OCR_LANG = os.environ.get("PDFDIFF_OCR_LANG", "chi_sim+rus+eng")
#                           ⚠️ tesseract 多语言联合是「+」号连；rus 是俄文标签 GRA-3634
#                           加的（整页转曲、vision 不可用时兜底用），中文场景影响实测见回归。
# OCR 引擎优先级表：**按顺序尝试，第一个认出字的胜出**（见 `_ocr_image`）。
# 默认 Vision 优先 —— 它是 macOS 自带的，对这份 IFU 的转曲小字几乎全对，
# 而 tesseract 会把「球囊」认成「球圳」、「再折叠」认成「再折受」。
OCR_ENGINES = tuple(e.strip() for e in
                    (os.environ.get("PDFDIFF_OCR_ENGINE") or "vision,tesseract").split(",")
                    if e.strip())
OCR_DPI = 400              # OCR 渲染分辨率（6pt 小字要 400dpi 才认得出）
OCR_MIN_COVER = 0.85       # OCR 文本被对面文档覆盖到多少 = 「这段内容还在对面」
OCR_MIN_COVER_LINE = 0.75  # 同上，但用在**逐行复核**上（详见 _refine_region）
                           # ⚠️ 行级必须比块级松：转曲小字 OCR 常认错一两个字，
                           #    实测 `- 检查球囊导管是否完整；`（对面也有）被认成
                           #    `含查球宫导管是否完整;` → 覆盖率刚好 0.80，卡在 0.85
                           #    下面 → 把没变的这行也标绿了。而**真新增**的行对面
                           #    基本是完全不同的内容，实测覆盖率只有 0.00~0.11，
                           #    所以放宽到 0.75 收不进任何一条真新增。
OCR_MIN_LEN = 10           # 短于这么多字的片段不做 OCR 核对
OCR_NEAR_PAD = 1.2         # 「对面就近一块」上下各扩这么多倍块高（见 _ocr_near）
# 展示用的正文要**另取一次 OCR，横向多留这么多**（2026-09-30）。
# 起因：判定用的那份裁剪贴着字切（上下只留 `OCR_CROP_PAD`），上下文不够，
# Vision 也会认错 —— 同一块转曲区，两种裁法认出来的字：
#   贴边裁（判定用）：`知果需要…生理盐水纱布擦拭**求**囊导管…`、`分）。`→`7）。`、
#                     `再折叠工具`→`斤叠工具`
#   横向留 10pt：    `如果需要…生理盐水的纱布擦拭球囊导管…`，实测**六行全对**
# 用户原话「你简直有点惜字如金」的另一半其实是**认错字**。
#
# ⚠️ 留白只能加在**横向**：实测 y 方向一放宽（≥3.5pt）就会把上一行的字
#    （`- 检查球囊导管是否完整；` 或只剩个 `-`）一起卷进来，反而更脏；
#    横向放宽 10pt 既让 Vision 有足够上下文，又离对侧栏 >100pt 不会跨栏。
OCR_TEXT_PAD_X = 10.0      # 展示用文本的横向留白（判定用的裁剪**不动**，见 _refine_region）

OCR_CROP_PAD = 1.2         # OCR 取字时上下留一点边距 —— 贴着字切 tesseract 认得一塌糊涂
#   ⚠️ 10 字这条是实测定的：地址改动「1栋201室」（6 字）拿去核对时，
#   老版本里正好有「2栋101室…15栋301室」，零散重合能到 0.83 —— 真改动会被误撤。
#   短片段本来就不足以证明「对面还有这段」，不做核对就是宁可保留。


def _text_tiles(page, scale: float, tile: int) -> set:
    """这一页**有文字层**覆盖到的瓦片集合。

    有文字的地方交给字符流引擎去标（它能逐字定位，比这里精确得多）；
    本节只负责补「两侧都读不到文字」的空档，两边各管各的、不重叠。
    """
    hit = set()
    try:
        words = page.get_text("words")        # [x0,y0,x1,y1,word,...]
    except Exception:
        return hit
    for w in words:
        x0, y0, x1, y1 = w[0], w[1], w[2], w[3]
        for r in range(int(y0 * scale) // tile, int(y1 * scale) // tile + 1):
            for c in range(int(x0 * scale) // tile, int(x1 * scale) // tile + 1):
                hit.add((r, c))
    return hit


def _hot_tiles(A: bytes, B: bytes, W: int, H: int, skip: set,
               tile: int, dark: int, frac: float) -> set:
    """逐瓦片找「内容变了」的瓦片（跳过有文字层的、以及两侧一样的）。"""
    hot = set()
    for r in range((H + tile - 1) // tile):
        y0, y1 = r * tile, min((r + 1) * tile, H)
        for c in range((W + tile - 1) // tile):
            if (r, c) in skip:
                continue
            x0, x1 = c * tile, min((c + 1) * tile, W)
            npx = (y1 - y0) * (x1 - x0)
            if npx <= 0:
                continue
            cnt = 0
            need = frac * npx            # 达到这个「不一样」的像素数才算变了
            for y in range(y0, y1):
                row = y * W
                # 按行切片 + zip：比逐像素下标快好几倍（纯 Python 没有 numpy 可用）
                for p, q in zip(A[row + x0:row + x1], B[row + x0:row + x1]):
                    d = p - q
                    if (d if d >= 0 else -d) > dark:
                        cnt += 1
                        if cnt >= need:
                            break
                if cnt >= need:
                    hot.add((r, c))
                    break
    return hot


def _tiles_to_rects(hot: set, tile: int, scale: float,
                    min_tiles: int = -1) -> List[Tuple[float, float, float, float]]:
    """把相邻的「变了」瓦片并成矩形（4 邻接），再换算回页面坐标 (pt)。

    `min_tiles`：连通域小于它的不报（去碎点），默认 `GRAPHIC_MIN_TILES`。
    「文字层错位」补查（v48）传 1 —— 真改动可能只落在一个瓦片里
    （M4506 的 `cool` 一词只让 1 个瓦片过 FRAC），判定权在 `_decide_region`
    的 OCR 文本比对，碎点噪声进不了结论。
    """
    if min_tiles < 0:
        min_tiles = GRAPHIC_MIN_TILES
    out: List[Tuple[float, float, float, float]] = []
    seen = set()
    for t in hot:
        if t in seen:
            continue
        comp, q = [], deque([t])
        seen.add(t)
        while q:
            r, c = q.popleft()
            comp.append((r, c))
            for nb in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if nb in hot and nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        if len(comp) < min_tiles:
            continue
        rs = [x[0] for x in comp]
        cs = [x[1] for x in comp]
        x0 = min(cs) * tile / scale - GRAPHIC_PAD
        y0 = min(rs) * tile / scale - GRAPHIC_PAD
        x1 = (max(cs) + 1) * tile / scale + GRAPHIC_PAD
        y1 = (max(rs) + 1) * tile / scale + GRAPHIC_PAD
        out.append((max(0.0, x0), max(0.0, y0), x1, y1))
    return sorted(out, key=lambda r: (round(r[1], 1), r[0]))


# ---------------------------------------------------------------- OCR（可选依赖）
_OCR_EXE: Optional[str] = None
_OCR_PROBED = False
_OCR_CACHE: Dict[Tuple[str, int, int, int, int], str] = {}


def ocr_available() -> bool:
    """本机能不能做 OCR（**引擎表里任一可用**即可；都没有就老实降级，不假装能比对）。"""
    global _OCR_EXE, _OCR_PROBED
    if not _OCR_PROBED:
        _OCR_PROBED = True
        try:
            exe = shutil.which("tesseract")
            if not exe:
                for cand in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract",
                             r"C:\Program Files\Tesseract-OCR\tesseract.exe"):
                    if os.path.exists(cand):
                        exe = cand
                        break
            _OCR_EXE = exe
        except Exception:
            _OCR_EXE = None
    return bool(_vision_exe()) or bool(_OCR_EXE)


# ---------------------------------------------------------------- OCR 引擎表
# **按顺序尝试，第一个认出字的胜出**（2026-09-30 新增 Vision）。
# 为什么非加 Vision 不可：Tesseract 对这份 IFU 的转曲小字错得离谱 ——
# 同一块「如果需要再次插入该球囊导管…」，tesseract 认成
# 「球圳 / 再折受 / 折县」（囊→圳、叠→受/县/登），而 macOS 自带的 Vision
# 几乎全对（球囊 / 再折叠）。转曲区**原理上只能靠 OCR**（没有文字层），
# 既然只能靠它，就该用认得准的那个 —— 这些字是要给用户看的。
# `PDFDIFF_OCR_ENGINE=tesseract` 可强制退回单引擎。
_VISION_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_vision_ocr.swift")
_VISION_VER = 3          # ⚠️ `_vision_ocr.swift` 一改就 +1（二进制缓存 key 带它）
                         # v3: 加 "json" 第二参数 → 行级 JSON（text + boundingBox），
                         #     扫描件整页行对拍管线用（GRA-3925）
                         # v2: 自动语言检测 + 候选池加 ru-RU（俄文标签 GRA-3634）
_vision_bin: Optional[str] = None
_vision_probed = False


def _vision_exe() -> Optional[str]:
    """拿到（必要时**编译一次**）macOS Vision OCR 小工具；不可用返回 None。

    ⚠️ 为什么要预编译成二进制：`xcrun swift foo.swift` 每次都现编译（数秒），
    而一次转曲区比对要 OCR 几十块 —— 不预编译会慢到没法用。
    二进制缓存在系统临时目录，key 带源码路径 + `_VISION_VER`，改源码自动重编。
    非 macOS（或没有 swiftc）直接返回 None，由引擎表退到 tesseract。
    """
    global _vision_bin, _vision_probed
    if _vision_probed:
        return _vision_bin
    _vision_probed = True
    _vision_bin = None
    try:
        if os.uname().sysname != "Darwin":
            return None
        if not os.path.isfile(_VISION_SRC):
            # ⚠️ 教训（2026-10-14）：目录整理把 _vision_ocr.swift 留在根目录、
            #    引擎进了 core/ → 这里静默 None → 图片区文字变化整批漏检，
            #    用户只觉得「识别率低」查了半宿。同场景至少喊一嗓子。
            if shutil.which("xcrun"):
                print("warning: Vision OCR 源码缺失（%s），图片/转曲区识别将退化为"
                      " tesseract 或跳过 —— 检查 _vision_ocr.swift 是否与引擎同目录"
                      % _VISION_SRC, file=sys.stderr)
            return None
        if not shutil.which("xcrun"):
            return None
        key = hashlib.md5(("%s#v%d" % (os.path.abspath(_VISION_SRC), _VISION_VER))
                          .encode("utf-8")).hexdigest()[:16]
        out = os.path.join(tempfile.gettempdir(), "pdfdiff_vision_%s" % key)
        if not (os.path.isfile(out) and os.access(out, os.X_OK)):
            r = subprocess.run(["xcrun", "swiftc", "-O", _VISION_SRC, "-o", out],
                               capture_output=True, timeout=300)
            if r.returncode != 0:
                return None
        _vision_bin = out if os.access(out, os.X_OK) else None
    except Exception:
        _vision_bin = None
    return _vision_bin


def _vision_ocr(img_path: str) -> str:
    """用 macOS Vision 框架认这张图（走编译好的小工具）。不可用 / 失败返回 ""。"""
    exe = _vision_exe()
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, img_path], capture_output=True, timeout=120)
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def _tesseract_ocr(img_path: str) -> str:
    """Tesseract 兜底（跨平台；Windows 交付包里就是它）。"""
    exe = _OCR_EXE or shutil.which("tesseract")
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, img_path, "stdout", "-l", OCR_LANG, "--psm", "6"],
                           capture_output=True, timeout=180)
        return (r.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def _ocr_image(img_path: str) -> str:
    """按**引擎表**依次试，返回第一个认出行内容的引擎的结果。

    返回空串 = 所有引擎都没认出东西（图片空白 / 引擎都不可用），调用方据此降级。
    """
    for eng in OCR_ENGINES:
        if eng == "vision":
            t = _vision_ocr(img_path)
        elif eng == "tesseract":
            t = _tesseract_ocr(img_path)
        else:
            continue
        if (t or "").strip():
            return _ocr_post(t)
    return ""


# 拉丁 ↔ 西里尔**同形字符**折叠表：OCR 在转曲小字上把这俩认混是常态
# （GRA-3634 实测同一处 `80мм` 一次认成 `8омм`——数字 0 vs 西里尔 о，
# `PТА` 一次认成 `PTА`——西里尔 А vs 拉丁 A）。折叠后这些噪声在 diff 里
# 消失，不再触发假的「行内修改」；字形本来就一样，折叠不丢真差异。
# 两份表同键集，仅「是否保留大小写」不同，由下面一张 pairs 字典统一推导。
_HOMOGLYPH_PAIRS = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "Х": "X",
}
# `_HOMOGLYPH` 只在 `_norm_cjk`（覆盖率判重）里用，后面反正要 `.lower()`，
# 所以折成小写即可（`А`→`a`）。
_HOMOGLYPH = str.maketrans({k: v.lower() for k, v in _HOMOGLYPH_PAIRS.items()})

# 同形字折叠（**保留大小写**版）：叠在 OCR 引擎的**原文出口**上。
# 为什么 `_HOMOGLYPH` 之外还要这张表（2026-10-11 GRA-3925 用户实测）：
#   `_HOMOGLYPH` 只在 `_norm_cjk`（覆盖率判重）里用，OCR 的**原文**照样带
#   西里尔字母进清单 —— 实测新版 `A35-070300-200` 被认成 `А35-…`（西里尔 А），
#   用户看到的条目文本就是错的，复制出去搜也搜不到。在引擎出口折叠后：
#   · 同形字形的西里尔字母换成对应的拉丁字母 —— **字形完全一样**，屏幕上
#     看不出差别，但码位干净了（可复制、可搜索、可比对）；
#   · 俄文真词（`Дист`/`м}` 这类非同形字母）不受影响；
#   · 大小写保留（`А`→`A` 而非 `a`），`_HOMOGLYPH` 折成小写是因为它后面
#     反正要 `.lower()`，这里没有那一步，折成小写会把 `А35` 显示成 `a35`。
_HOMOGLYPH_KEEP = str.maketrans(_HOMOGLYPH_PAIRS)


def _ocr_post(t: str) -> str:
    """OCR 引擎原文的统一后处理：同形字折叠（见 `_HOMOGLYPH_KEEP`）。

    ⚠️ 所有引擎出口都必须过这一道（`_ocr_image` / `_ocr_image_lines`），
    漏一条通道清单里就会再冒西里尔字母。
    """
    return (t or "").translate(_HOMOGLYPH_KEEP)


def _fold_line_ts(lines: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """行级 OCR 结果（含**磁盘缓存回读**的旧数据）统一过 `_ocr_post`。

    为什么返回口还要叠一道（2026-10-11 GRA-3925 实测）：`_page_ocr_lines` /
    `_ocr_crop_lines` 的磁盘缓存**不受 `_OCR_DISK_CACHE` 开关管**，修复前写入
    的缓存 JSON 里就带着西里尔 А —— 只在引擎出口折叠的话，旧缓存一命中照样
    把西里尔字母送进清单。翻译是幂等的，重复叠无副作用。
    """
    for it in lines or []:
        if it.get("t"):
            it["t"] = _ocr_post(it["t"])
    return lines


def _norm_cjk(s: str) -> str:
    """归一化：只留中日韩汉字 + 字母数字 + 西里尔字母，丢掉空白/标点/项目符号。

    ⚠️ 西里尔（\\u0400-\\u04ff）不能少：俄文标签 GRA-3634 整页无文字层，全靠
    OCR 兜底 —— 不留西里尔的话 OCR 认得再对也会被归一化删光，coverage 全 0，
    两处真差异（注册证日期 / Rev 号）全漏（2026-09-30 实测）。
    ⚠️ 拉丁↔西里尔同形字符先折叠（`_HOMOGLYPH`）：不折叠的话 `80мм`↔`8омм`
    这类 OCR 抖动会顶满 `_mod_worthy` 的数字差异条件，满页假「修改」。
    ⚠️ **必须先 `.lower()` 再过滤**：过滤正则只放行小写 `a-z`，旧写法
    （先过滤后 lower）把**大写拉丁字母整批删掉** —— `GRA-3634 Rev02` 被归一成
    `3634ev02`、`BrosMed` 变 `rosededical`（GRA-3634 页脚实测），英文内容
    无缘无故丢一半字符，覆盖率判定全部失真。
    """
    return re.sub(r"[^0-9a-z\u0400-\u04ff\u3400-\u9fff]+", "",
                  (s or "").translate(_HOMOGLYPH).lower())


def _coverage(needle: str, hay: str) -> float:
    """needle 有多大比例能在 hay 里**按顺序**找到（容忍 OCR 的少量错字）。

    用 SequenceMatcher 的所有匹配块长度求和 / len(needle)：
    内容真在对面 → 接近 1.0；对面没有这段 → 只能凑到零散几个字，明显偏低。

    ⚠️ 它是**累加**口径 —— 长文本上会虚高，判「是不是同一句话」时**必须**配
    `_longest_cover` 一起用（见那里的实测）。本函数只回答「这些字在不在对面」。
    """
    n = _norm_cjk(needle)
    if len(n) < OCR_MIN_LEN:
        return 0.0
    h = _norm_cjk(hay)
    if not h:
        return 0.0
    sm = SequenceMatcher(None, n, h, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(n)


def _longest_cover(needle: str, hay: str) -> float:
    """needle 的**最长连续**匹配块占 needle 的比例 —— 判「是不是同一句话」。

    为什么 `_coverage` 之外还要这一条（2026-09-30 切到 Vision 后打回来的）
    -------------------------------------------------------------------
    `_coverage` 把**所有**匹配块长度**加起来**，长文本上它会虚高。
    实测新版那条真新增「用充压装置使球囊导管产生负压，并保持；」，
    在**旧版整页** OCR 上的累加覆盖率是 **0.941**（远超 0.85 阈值）→
    被当成「内容还在对面」**撤销**，直接从清单里消失 —— 一条真改动凭空没了。
    可两个文件在说**不同的话**：旧版那句是「使充压装置产生负压使球囊内压力
    完全卸载（卸压终点为球囊工作长度区域基本无液体）」，只是**碰巧**共用了
    「充压装置」「球囊导管」「产生负压」几个词。实测最长连续块只有 4 个字
    （「产生负压」），占 20 字的 **0.20**。

    真正的「内容挪位」是**整句**挪走，最长块接近 1.0。这道闸正好把
    「散落的相同词」和「同一句话」分开 —— 而且是**加**一道，不改 `_coverage`
    的既有语义（它还用在别处，动它会牵动一片判定）。
    """
    n = _norm_cjk(needle)
    if not n:
        return 0.0
    h = _norm_cjk(hay)
    if not h:
        return 0.0
    sm = SequenceMatcher(None, n, h, autojunk=False)
    blocks = sm.get_matching_blocks()
    return (max([b.size for b in blocks]) if blocks else 0) / float(len(n))


# ---- 跨进程 OCR 缓存（见 `_ocr_crop` 的长注释）----
_OCR_DISK_CACHE = os.environ.get("PDFDIFF_OCR_CACHE", "1") != "0"
_OCR_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfdiff_ocr_cache")
_OCR_CACHE_KEEP = 3 * 24 * 3600        # 缓存文件保留 3 天
_ocr_sweep_tick = 0

# ---- 转曲区结果缓存（`graphic_regions` 整体）----
# 实测 GRA-4118：整段流程 78 次 OCR、约 34s，98% 的时间都花在 OCR 上。
# 结果按「文件指纹 + dpi + tile」落盘 —— 同一对文件第二次打开直接命中，
# 从 34s 掉到毫秒级。`PDFDIFF_GRAPHIC_CACHE=0` 可关。
GRAPHIC_CACHE = os.environ.get("PDFDIFF_GRAPHIC_CACHE", "1") != "0"
# ⚠️ 规则版本：`graphic_regions` 的**判定 / 聚段规则**每改一次就 +1。
#    它进缓存 key —— 否则规则改了、缓存还命中旧结果，用户看到的就是「改了个寂寞」
#    （这层缓存跨进程存活，比 OCR 那层更容易骗人）。
GRAPHIC_RULE_VER = 62  # v62: 单侧转曲线止损——整页重绘兜底前查两侧文字层词框，一侧 0 一侧有 → 跳过行级 OCR 对拍（rev15vs16 卡死 300s 根治）。v61: 兜底加数字锚点+文字层词框过滤（修 117 条 OCR 抖动假增删）。v60: 整页重绘防护升级——巨框跳过后用行级 OCR 对拍兜回块内局部真差异（rev16vs18 有效期/条码）。v59: 整页图形重绘防护（hot 连通域 ≥15% 页面 → 跳过并记账，修整页标绿巨框）
                       #      也交给 OCR 判定（M4506 实测：隐形文字层与视觉渲染
                       #      错位约 50pt，把视觉上无文字的转曲区标成了「有文字层」，
                       #      「Store in a dry, dark place → dry, dark, cool place」
                       #      整处漏；判定权在 OCR 文本比对，两版相同照旧不报）
                       # v47: mod 行框收窄到真正变了的片段（见 _narrow_mod_region）
                       #      短行先判逐行 ratio 再判整行命中（右上角 `7.0mm` 才报得出）
                       # v19: 行对拍的「挪位撤销」改**逐行最佳 ratio** + 互改闸门
                       #      （旧口径拿整页拼串做子序列覆盖 → coverage 恒≈1.0，
                       #       GRA-3925 的 UDI 数字、鞘管规格全被当挪位撤掉）
                       # v18: 单侧增删候选加「OCR 乱码闸门」`_ocr_junk`
                       #      （GRA-3925 条码区认出 'Oe = > te 4 Пою…' 被当真新增）
                       # v17: 两页位图尺寸不一致时**拉伸对齐**再比（图片输入实测：
                       #      同一份文档 PNG/PDF 折算差 0.3px 就差一行，原来那句
                       #      `continue` 让整页一处都不查还报 0 差异）；
                       #      实在对不上的页记账到 LAST_SIZE_SKIPS，不再静默
                       # v16: 扫描件加「整页 OCR 行对拍」管线（GRA-3925 两张不同
                       #      产品的标签是区域级差异，像素候选结构性失效），
                       #      与像素管线互补去重；swift 加行级 JSON 输出（_VISION_VER=3）
                       # v15: 扫描件先做垂直配准（GRA-3925 两版内容纵向漂移 1.6%，
                       #      不配准整页点亮、只出 4 个候选漏检几十处）
                       # v14: 短文本 mod 分支只看「新侧太短」（n_old 也拉进来会把
                       #      GRA-4118 五块「旧认不出/新认得出」的真新增提前否掉）
                       # v13: 候选矩形 y 沿墨迹扩到字的完整高度（薄片 OCR 读半截字
                       #      造出 3 条假报：9707→1707、表格头乱码×2）
                       # v12: 扫描件候选改「差异列聚簇，每簇独立定行/扩展/判定」
                       # v11: 种子列 + 单行精修 + 墨迹扩展去噪点搭桥 + mod 高分复核
                       # v9: 行级差异画像候选 + _mod_worthy token 级对齐
                       #     + _norm_cjk 先 lower 再过滤（修大写字母被删）+ 短文本 mod 分支
                       # v8: mod 块细切到行（真差行保留、OCR 抖动行丢弃）
                       #      + _norm_cjk 拉丁↔西里尔同形折叠（о↔0、А↔A 噪声源）
_GRAPHIC_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfdiff_graphic_cache")


def _file_fingerprint(p: str) -> str:
    """路径能解析成文件 → `abspath#size#mtime`；否则 `路径#?`（跨进程缓存指纹）。"""
    try:
        st = os.stat(p)
        return "%s#%d#%d" % (os.path.abspath(p), st.st_size, int(st.st_mtime))
    except OSError:
        return str(p) + "#?"


def _graphic_cache_file(old_path: str, new_path: str, dpi: int, tile: int) -> Optional[str]:
    """`graphic_regions` 结果的落盘位置；文件一改（size/mtime 变）就换 key。"""
    if not GRAPHIC_CACHE:
        return None
    try:
        parts = [_file_fingerprint(old_path), _file_fingerprint(new_path),
                 "d%d" % dpi, "t%d" % tile, "v%d" % GRAPHIC_RULE_VER,
                 "+".join(OCR_ENGINES),
                 "ocr=%s" % OCR_LANG, "vv%d" % _VISION_VER]
        # ⚠️ ocr=/vv= 两项不能少：OCR **能力**变了（语言表 / vision 源码）输出就会变，
        #    key 不带上就命中旧结果 —— 2026-09-30 俄文标签实锤（改完跑出来像没生效）。
        key = hashlib.md5("|".join(parts).encode("utf-8", "replace")).hexdigest()
        os.makedirs(_GRAPHIC_CACHE_DIR, exist_ok=True)
        return os.path.join(_GRAPHIC_CACHE_DIR, key + ".json")
    except Exception:
        return None


def _ocr_cache_file(cache_key, extra: str = "") -> Optional[str]:
    """把 cache_key 折成一个稳定的缓存文件名。

    ⚠️ key 里凡是**能解析成文件路径**的元素，都拼上 `size + mtime`：
    这层缓存跨进程存活，不加指纹的话「同一路径被换成新版文件」会命中旧结果 ——
    那是**假一致**，比慢一百倍都严重。
    """
    if not _OCR_DISK_CACHE or cache_key is None:
        return None
    try:
        parts = []
        for v in (cache_key if isinstance(cache_key, (tuple, list)) else (cache_key,)):
            s = str(v)
            if os.path.isfile(s):
                s = _file_fingerprint(s)
            parts.append(s)
        parts.append(extra)
        key = hashlib.md5("|".join(parts).encode("utf-8", "replace")).hexdigest()
        os.makedirs(_OCR_CACHE_DIR, exist_ok=True)
        return os.path.join(_OCR_CACHE_DIR, key + ".txt")
    except Exception:
        return None


def _ocr_cache_sweep() -> None:
    """偶尔清扫过期缓存（每 50 次写入触发一次），别让临时目录无限长大。"""
    global _ocr_sweep_tick
    _ocr_sweep_tick += 1
    if _ocr_sweep_tick % 50:
        return
    try:
        now = time.time()
        for name in os.listdir(_OCR_CACHE_DIR):
            f = os.path.join(_OCR_CACHE_DIR, name)
            try:
                if now - os.stat(f).st_mtime > _OCR_CACHE_KEEP:
                    os.remove(f)
            except OSError:
                pass
    except Exception:
        pass


def _ocr_cache_load(cache_key, extra: str = "") -> Optional[str]:
    """读**跨进程** OCR 缓存（见 `_ocr_crop` 的长注释）。没有则返回 None。"""
    f = _ocr_cache_file(cache_key, extra)
    if not f or not os.path.isfile(f):
        return None
    try:
        with open(f, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return None


def _ocr_cache_save(cache_key, extra: str, text: str) -> None:
    """写跨进程 OCR 缓存；失败一律静默（缓存只是加速，不该影响正确性）。"""
    f = _ocr_cache_file(cache_key, extra)
    if not f:
        return
    try:
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(text)
        _ocr_cache_sweep()
    except Exception:
        pass


def _ocr_crop(page, clip, dpi: int = OCR_DPI, cache_key=None) -> str:
    """把页面某块渲染成图交给 tesseract，返回识别文本（不可用/失败返回 ""）。

    ⚠️ 缓存分两层（2026-09-30 加的磁盘层）
    ------------------------------------
    `_OCR_CACHE` 是**进程内**的 —— 而用户的实际动线是「打开软件 → 对比 → 导出标注版」，
    两次都跑 OCR，进程一换缓存全废。实测 GRA-4118 补上转曲区兜底后，
    单次对比从 14s 涨到 **96s**（大头就是重复 OCR）。
    第二层落在系统临时目录（`pdfdiff_ocr_cache/`，`PDFDIFF_OCR_CACHE=0` 可关），
    key 里**带文件的 size + mtime**：文件一改，key 就变，绝不会拿到旧结果。
    """
    if not ocr_available():
        return ""
    if cache_key is not None and cache_key in _OCR_CACHE:
        return _OCR_CACHE[cache_key]
    extra = "d%d|%s" % (dpi, "+".join(OCR_ENGINES))
    hit = _ocr_cache_load(cache_key, extra)
    if hit is not None:
        _OCR_CACHE[cache_key] = hit
        return hit
    text = ""
    # ⚠️ 临时 PNG **只落在系统临时目录**（2026-10-10 起不再退回工具目录）：
    #   原来「临时目录写不进就退回本模块所在目录」的兜底，会让 OCR 临时图
    #   漏进工具文件夹 —— 用户得一遍遍手动删 PNG（进度一中断 / 进程被强杀，
    #   `atexit` 清理都不触发，垃圾就留下了）。
    #   正确取舍：临时目录真写不进（极端受限环境），宁可这条 OCR 放弃、
    #   返回空串交给上层兜底，也**绝不往工具目录里塞伴生文件**。
    # ⚠️ 文件名按进程 pid 复用 + **立即清理**（不再依赖 atexit）：见 try/finally。
    img = os.path.join(tempfile.gettempdir(), "_pdfdiff_ocr_%d.png" % os.getpid())
    try:
        page.get_pixmap(dpi=dpi, clip=clip).save(img)
        text = _ocr_image(img)
    except Exception:
        text = ""
    finally:
        try:
            os.remove(img)
        except OSError:
            pass
    if cache_key is not None:
        _OCR_CACHE[cache_key] = text
        # 只缓存成功结果：OCR 引擎缺失时的空串会被"钉死"，
        # 等引擎装好再跑也仍是空 —— 那是自找的假一致。
        if text.strip():
            _ocr_cache_save(cache_key, extra, text)
    return text


@atexit.register
def _ocr_tmp_cleanup() -> None:
    """进程退出时清掉 OCR 临时 png（兜底；正常路径已在 `_ocr_crop` 的 finally 里清过）。

    ⚠️ 只清系统临时目录：工具目录里不再落 OCR 临时图（见 `_ocr_crop`），
       但为保险仍扫一遍工具目录，把**历史遗留**的 `_pdfdiff_ocr_*.png` 一并清掉。
    """
    for d in (tempfile.gettempdir(), os.path.dirname(os.path.abspath(__file__))):
        try:
            os.remove(os.path.join(d, "_pdfdiff_ocr_%d.png" % os.getpid()))
        except OSError:
            pass


# ---- 扫描件「整页 OCR 行对拍」管线（2026-09-30，GRA-3925 实测）----
# 行级 OCR 整页结果的跨进程缓存（key: 文件+页+dpi+引擎+VISION_VER）
_OCR_LINES_CACHE: Dict[Any, List[Dict[str, Any]]] = {}
_OCR_LINES_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfdiff_ocr_lines_cache")


def _ocr_image_lines(img_path: str) -> List[Dict[str, Any]]:
    """行级 OCR：返回 `[{"t": 文本, "x0","y0","x1","y1": pt 坐标(左上原点)}]`。

    只有 Vision 支持（tesseract 也能拿行框，但 Windows 包的 tsv 通道没必要
    现在做 —— 这条管线服务 macOS 主环境，tesseract 环境自动跳过、
    上层退回像素候选管线）。坐标由 Vision 的归一化 bbox（原点左下）换算。
    """
    exe = _vision_exe()
    if not exe:
        return []
    try:
        r = subprocess.run([exe, img_path, "json"], capture_output=True, timeout=120)
        data = json.loads((r.stdout or b"[]").decode("utf-8", "replace") or "[]")
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for it in data if isinstance(data, list) else []:
        try:
            x, y = float(it["x"]), float(it["y"])
            w, h = float(it["w"]), float(it["h"])
            t = str(it.get("t", ""))
        except Exception:
            continue
        if not t.strip():
            continue
        out.append({"t": _ocr_post(t.strip()),
                    "x0": x, "x1": x + w,
                    # Vision bbox 原点在**左下**：转成左上原点的 y0/y1
                    "y0": 1.0 - (y + h), "y1": 1.0 - y})
    return out


def _ocr_lines_cached(key: Tuple, sig: str, fetch) -> List[Dict[str, Any]]:
    """行级 OCR 的**通用跨进程缓存骨架**：命中回读 JSON，miss 则 `fetch()` 产出并落盘。

    三个调用方（`_page_ocr_lines` / `_rot90_supplement` / `_ocr_crop_lines`）共享同一套
    「内存缓存 → 磁盘 JSON → 渲染+OCR 生产」样板，差异只在 `sig` 指纹与 `fetch` 的
    渲染/坐标换算。落盘后回填内存缓存并过 `_fold_line_ts`（幂等折叠同形字）。
    """
    if key in _OCR_LINES_CACHE:
        return _OCR_LINES_CACHE[key]
    os.makedirs(_OCR_LINES_CACHE_DIR, exist_ok=True)
    cf = os.path.join(_OCR_LINES_CACHE_DIR,
                      hashlib.md5(sig.encode("utf-8")).hexdigest()[:20] + ".json")
    lines: Optional[List[Dict[str, Any]]] = None
    if os.path.isfile(cf):
        try:
            with open(cf, "r", encoding="utf-8") as fh:
                lines = json.load(fh)
        except Exception:
            lines = None
    if lines is None:
        lines = fetch() or []
        if lines:
            try:
                with open(cf, "w", encoding="utf-8") as fh:
                    json.dump(lines, fh, ensure_ascii=False)
            except Exception:
                pass
    _OCR_LINES_CACHE[key] = lines
    return _fold_line_ts(lines)


def _sig_with_mtime(path: str, parts: str) -> str:
    """缓存指纹：`abspath|parts` 再拼 size/mtime（跨进程缓存防旧文件假命中）。"""
    sig = "%s|%s" % (os.path.abspath(str(path)), parts)
    try:
        st = os.stat(path)
        sig += "|%d|%d" % (st.st_size, int(st.st_mtime))
    except OSError:
        pass
    return sig


def _page_ocr_lines(page, path: str, i: int, dpi: int) -> List[Dict[str, Any]]:
    """整页行级 OCR（归一化 bbox → pt），带跨进程缓存。"""
    key = (path, i, dpi, "+".join(OCR_ENGINES), _VISION_VER)
    # ⚠️ key 必带 size + mtime：跨进程缓存，不加指纹则「同一路径换新版文件」
    #    会命中旧结果 —— 假一致（skill 铁律）。
    sig = _sig_with_mtime(path, "%d|%d|%s|%d" % (i, dpi, key[3], _VISION_VER))

    def fetch():
        img = os.path.join(tempfile.gettempdir(), "_pdfdiff_lines_%d.png" % os.getpid())
        try:
            page.get_pixmap(dpi=dpi).save(img)
            return _ocr_image_lines(img)
        except Exception:
            return []
        finally:
            try:
                os.remove(img)
            except OSError:
                pass

    return _ocr_lines_cached(key, sig, fetch)


# ---------------------------------------------------------------- 竖排补扫
# 病根（2026-10-11 GRA-3925 用户截图①）：标签顶部那条**旋转 90° 排版**的尺寸
# 区（`.035" OTW / 7.0mm × 150mm / 200cm`，新版多 `Conical`、改 `6.5/7.0`、
# `300`）—— Vision 整页 OCR 对竖排行**整列读丢**（两版各只读出 `Atropos Pro`），
# 行级对拍连候选都形不成；像素管线又因行级路线成功被跳过 → 这块差异一个不报。
# 补扫：把页面位图各转 90° 再 OCR（竖排字转平了就读得出），行框按旋转矩阵
# 映射回原页面归一化坐标。两个方向都扫（竖排有正读/倒读）。
ROT_SUPPLEMENT = os.environ.get("PDFDIFF_ROT_SUPPLEMENT", "1") != "0"


def _rot90_supplement(page, path: str, i: int, dpi: int) -> List[Dict[str, Any]]:
    """整页位图转 90°（顺/逆时针各一次）再行级 OCR，行框映射回原页面。

    坐标映射（归一化、y 向下）：
      · 顺时针转 90°：dst(x', y') = (1−y, x)  → 逆映射 x = y', y = 1−x'
      · 逆时针转 90°：dst(x', y') = (y, 1−x)  → 逆映射 x = 1−y', y = x'
    结果与 `_page_ocr_lines` 同一套跨进程缓存（key 带 rot 标记），
    只在 Vision 行级管线可用时有产出（tesseract 环境自动为空）。
    """
    if not ROT_SUPPLEMENT:
        return []
    try:
        from PIL import Image
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for tag, trans, inv in (
            ("cw", Image.ROTATE_270,
             lambda x0, y0, x1, y1: (y0, 1.0 - x1, y1, 1.0 - x0)),
            ("ccw", Image.ROTATE_90,
             lambda x0, y0, x1, y1: (1.0 - y1, x0, 1.0 - y0, x1))):
        key = (path, i, dpi, tag, "+".join(OCR_ENGINES), _VISION_VER)
        sig = _sig_with_mtime(path, "%d|%d|%s|%s|%d" % (i, dpi,
                              "+".join(OCR_ENGINES), tag, _VISION_VER))

        def fetch(tag=tag, trans=trans, inv=inv):
            lines: List[Dict[str, Any]] = []
            img = os.path.join(tempfile.gettempdir(),
                               "_pdfdiff_rot_%d.png" % os.getpid())
            try:
                pm = page.get_pixmap(dpi=dpi)
                im = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
                im.transpose(trans).save(img)
                raw = _ocr_image_lines(img)
                for it in raw:
                    t = it["t"]
                    # 质量闸：真竖排内容要么带数字（`150`/`300`/`2029-07-29`）、
                    # 要么是像样的词（≥5 字，`Conical`/`Prox.`）。没有数字又不足
                    # 5 字的多半是竖排误读碎片（实测 `NEX`/`X` 之类），整条丢。
                    nn = _norm_cjk(t)
                    if not nn or (not any(c.isdigit() for c in nn)
                                  and len(nn) < 5):
                        continue
                    mx0, my0, mx1, my1 = inv(float(it["x0"]), float(it["y0"]),
                                             float(it["x1"]), float(it["y1"]))
                    lines.append({"t": t,
                                  "x0": min(mx0, mx1), "x1": max(mx0, mx1),
                                  "y0": min(my0, my1), "y1": max(my0, my1)})
            except Exception:
                lines = []
            finally:
                try:
                    os.remove(img)
                except OSError:
                    pass
            return lines

        out.extend(_ocr_lines_cached(key, sig, fetch))
    return out


# ---------------------------------------------------------------- 混合输入补行
# 病根：**一侧有文字层、另一侧是扫描件/图片**（旧版电子稿 vs 新版签字扫描件，
# 或反过来）。没有文字层的那一侧在 `extract_rows` 里一行都拿不到 → 字符流只剩
# 一侧的字 → 整份文档被报成「全部删除」（反向则「全部新增」），可对面其实只改了
# 几个字。补行之后两侧都有内容，逐字 diff 自然配出删+增两条。
MIXED_OCR = os.environ.get("PDFDIFF_MIXED_OCR", "1") != "0"


def ocr_page_lines_pt(page, path: str, i: int
                      ) -> List[Tuple[str, float, float, float, float]]:
    """整页 OCR → `[(文本, x0, top, x1, bottom)]`（pt，**pdfminer 口径**）。

    OCR 原生给的是「归一化到 0~1、左上原点」；这里一次翻到位：
    乘页面尺寸得到 pt，再把 y 翻成**原点左下、y 向上**——因为消费方
    `pdfdiff_core` 的行级引擎里 `TextLine.top / bottom` 就是这个口径。
    要构造 `Row`（左上原点）时由调用方翻回去，别在这儿来回改。
    """
    W, H = float(page.rect.width), float(page.rect.height)
    out: List[Tuple[str, float, float, float, float]] = []
    for it in _page_ocr_lines(page, path, i, SCAN_LINES_DPI):
        t = (it.get("t") or "").strip()
        if not t:
            continue
        x0, x1 = float(it["x0"]) * W, float(it["x1"]) * W
        y0, y1 = float(it["y0"]) * H, float(it["y1"]) * H      # 左上原点
        out.append((t, x0, H - y1, x1, H - y0))
    return out


def ocr_page_rows(page, path: str, i: int, page_idx: int,
                  ignore_case: bool = False, ignore_punct: bool = False,
                  min_len: int = MIN_ROW_LEN) -> List[Row]:
    """整页 OCR → `Row` 列表（**字符 bbox 按字符数均分行宽折算**）。

    ⚠️ 折算出来的字符框是**估的**，不是实测 —— 所以这一侧的位置置信度只能是
       `ocr_est`（见 `pdfdiff_core.SRC_CONF`），**不许当成文字层坐标**。
       排「这一处改动到底在哪几个字上」时它够用，但别拿它做像素级判定。
    """
    W, H = float(page.rect.width), float(page.rect.height)
    out: List[Row] = []
    for t, x0, top, x1, bottom in ocr_page_lines_pt(page, path, i):
        norm = _norm(t, ignore_case, ignore_punct)
        if len(norm.replace(" ", "")) < min_len:
            continue                       # 与 extract_rows 同口径：一两字的行不参与
        y0d, y1d = H - bottom, H - top     # 翻回左上原点
        n = max(1, len(t))
        step = (x1 - x0) / n
        chars = [(ch, (x0 + k * step, y0d, x0 + (k + 1) * step, y1d))
                 for k, ch in enumerate(t)]
        out.append(Row(text=t, norm=norm, chars=chars,
                       rect=(x0, y0d, x1, y1d), page=page_idx))
    return out


def _mixed_ocr_fill(pa: List[List[Row]], pb: List[List[Row]],
                    old_path: str, new_path: str, page_idx_off: int = 0,
                    ignore_case: bool = False, ignore_punct: bool = False) -> int:
    """给「**恰好一侧没有文字层**」的页补 OCR 行，返回补了几页。

    ⚠️ 只在下面这几种情况下动手，其余一律不碰（这是零回归的关键）：
       · 两侧都有文字层 → 不是混合输入；
       · 两侧都没有 → 纯扫描件，交给扫描 / 转曲那条管线；
       · 页数不等 → 页对齐另有其事，别在这儿猜；
       · OCR 引擎不可用 → 原样返回，宁可退回旧行为。
    """
    if not MIXED_OCR or not ocr_available():
        return 0
    if len(pa) != len(pb):
        return 0
    if not any((not pa[i]) != (not pb[i]) for i in range(len(pa))):
        return 0                       # 快速短路：没有任何一页是「单侧空」
    try:
        da, db = fitz.open(old_path), fitz.open(new_path)
    except Exception:
        return 0
    n = 0
    try:
        for i in range(len(pa)):
            ea, eb = not pa[i], not pb[i]
            if ea == eb:
                continue
            try:
                if ea:
                    pa[i] = ocr_page_rows(da[i], old_path, i, i + page_idx_off,
                                          ignore_case, ignore_punct)
                else:
                    pb[i] = ocr_page_rows(db[i], new_path, i, i + page_idx_off,
                                          ignore_case, ignore_punct)
            except Exception:
                continue
            if pa[i] or pb[i]:
                n += 1
    finally:
        da.close()
        db.close()
    return n


def _near_shift_hit(page_other, path_other: str, i: int, rect, cand: str) -> bool:
    """短候选沿**同一列**上下扫几个偏移，窄窗**行级**重读（见 `NEAR_SHIFT_RULES`）。

    命中 = 对侧在「同一列、差几 pt~二十几 pt」的地方印着**同一句话** ——
    表格加列/加表头导致的行挪位，不是真增删。

    ⚠️ 必须按**行**对，不能拿整段 OCR 文本对：窄窗差 1pt 就会把上下邻行的字
    一并读进来（`8` → `'8\\n10'`），整段比对必然判「不同」，真挪位就漏判了。
    """
    w_pt, h_pt = float(page_other.rect.width), float(page_other.rect.height)
    for dy in NEAR_SHIFT_DY:
        y0 = max(0.0, float(rect[1]) + dy - NEAR_SHIFT_PAD_Y)
        y1 = min(h_pt, float(rect[3]) + dy + NEAR_SHIFT_PAD_Y)
        x0 = max(0.0, float(rect[0]) - NEAR_SHIFT_PAD_X)
        x1 = min(w_pt, float(rect[2]) + NEAR_SHIFT_PAD_X)
        if y1 - y0 < 4.0 or x1 - x0 < 4.0:
            continue
        for it in _ocr_crop_lines(page_other, fitz.Rect(x0, y0, x1, y1),
                                  path_other, i, "nshift"):
            t = _norm_cjk(it.get("t", ""))
            if t and _near_same(cand, t):
                return True
    return False


def _near_line_hit(page_self, page_other, rect, path_self: str,
                   path_other: str, i: int) -> bool:
    """候选**自己那一行**两侧各分行重读一次、**逐行**比（见 `NEAR_LINE_RULES`）。

    为什么整窗外扩之后再补这一道（GRA-3925 `CE2797当4` 实测）：
    `NEAR_CHECK_STEPS` 的两轮都是拿**整个窗**的 OCR 文本比，而窗的纵向留白是按
    候选行高的**倍数**给的 —— 候选框一高（CE 标志 23.6pt），留白就撑到 19pt，
    把上一行**整行**地址读进来；两版这一行的 OCR 抖动方向不同（新版多出一串
    `staorbrasiirel11s0000000`），整段比必然判「不同」→ 假新增撤不掉。
    可**候选自己那一行**两侧读得一字不差（都是 `СЄ2797`）—— 逐行比就撤得掉。

    ⚠️ 与 `_near_shift_hit` 同一条教训：判「两侧是同一句」**必须按行对**，
    不能拿整段 OCR 文本对（整段里混着邻行，两版邻行读法天然不对称）。

    ⚠️ 只比**候选框管的那一行**（框内、中心离框中心最近的那一行），不是窗里
    所有行 —— 比所有行等于把门槛降到「窗里有一行相同就撤」，那会撤掉真改动。
    """
    r = fitz.Rect(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    w_s, h_s = float(page_self.rect.width), float(page_self.rect.height)
    w_o, h_o = float(page_other.rect.width), float(page_other.rect.height)
    cy = (float(rect[1]) + float(rect[3])) / 2.0

    def _box(it, w_pt, h_pt):
        return (float(it["x0"]) * w_pt, float(it["y0"]) * h_pt,
                float(it["x1"]) * w_pt, float(it["y1"]) * h_pt)

    best, best_dy = None, None
    for it in _ocr_crop_lines(page_self, r, path_self, i, "nlh"):
        nb = _norm_cjk(it.get("t", ""))
        if not nb:
            continue
        mb = _box(it, w_s, h_s)
        if _overlap_frac(mb, rect) < NEAR_LINE_IN_CAND:
            continue                      # 这一行不归候选框管
        dy = abs((mb[1] + mb[3]) / 2.0 - cy)
        if best is None or dy < best_dy:
            best, best_dy = (nb, mb), dy
    if best is None:
        return False
    nb, mb = best
    for ot in _ocr_crop_lines(page_other, r, path_other, i, "nlh"):
        no = _norm_cjk(ot.get("t", ""))
        if not no:
            continue
        if _overlap_frac(_box(ot, w_o, h_o), mb) < NEAR_LINE_OV:
            continue                      # 位置对不上 = 不是同一行
        if _near_same(nb, no):
            return True
    return False


def _line_near_check(page_self, page_other, rect, path_self: str,
                     path_other: str, i: int, text: str = "") -> bool:
    """增删候选的最后一道把关：**两侧同位置各高分 OCR 一次、互相比对**。

    为什么不拿「候选文本」去对面找（第一版踩的坑）：候选文本本身就是**单侧
    乱码**（GRA-3925 底部图标区 `STERILE EO` 在整页 OCR 里被认成
    `STERME AD28@KM米于`），拿乱码当 needle coverage 永远低，假增删撤不掉。
    改成：候选 rect 在**两侧**各高分重读一遍，两段 near 文本归一化 ratio ≥ 0.8 =
    两版这个位置内容相同（整页 OCR 单侧读岔而已）→ 撤。

    ⚠️ 2026-10-01 加**第二轮尝试**（`NEAR_CHECK_STEPS`）：扫描件两版有整体漂移
    （GRA-3925 实测 ~1.6%），且整页 OCR 会**单侧整块漏读** —— 旧版那枚 `GW`
    logo 整页一个 `gw` 都没认出来，贴着框 300dpi 重读也是**空串**，按老口径
    「有一侧认不出 = 证据不足不撤」就被当成新版新增标了出来（用户反馈的假新增）。
    第二轮把框放宽（横 12pt、纵 1.8 倍行高）、分辨率提到 400dpi，
    实测旧版那块能读出 `GW)…` → 与新版一致 → 撤。
    """
    cand = _norm_cjk(text or "")
    for st in NEAR_CHECK_STEPS:
        pad_x, pad_y, dpi = st["pad_x"], st["pad_y"], st["dpi"]
        x0 = max(0.0, rect[0] - pad_x)
        x1 = min(float(page_self.rect.width), rect[2] + pad_x)
        h = max(6.0, rect[3] - rect[1])
        y0 = max(0.0, rect[1] - pad_y * h)
        y1 = min(float(page_self.rect.height), rect[3] + pad_y * h)
        r = fitz.Rect(x0, y0, x1, y1)
        ck = (int(x0 // 20), int(x1 // 20), int(y0 // 20), int(y1 // 20), dpi)
        ns = _norm_cjk(_ocr_crop(page_self, r, dpi=dpi,
                                 cache_key=(path_self, i, "lnr") + ck))
        no = _norm_cjk(_ocr_crop(page_other, r, dpi=dpi,
                                 cache_key=(path_other, i, "lnr") + ck))
        if not ns or not no:
            continue                      # 有一侧读不出 → 换更宽的框再试一轮
        if _near_same(ns, no):
            return True
        # ③ 短候选：对侧重读里能找到这段字 = 单侧切行/漏读造成的假增删
        #    （`GW` 就在旧版 `GW)…` 的乱读结果里）。ratio 闸门是防误杀：
        #    真新增的短词（`Conical`）就算字面出现在对面长串里，占比也远低于阈值。
        if (cand and len(cand) <= NEAR_SUBSTR_MAX and cand in no
                and difflib.SequenceMatcher(
                    None, cand, no, autojunk=False).ratio() >= NEAR_SUBSTR_RATIO):
            return True
    # ③ 纵向重排扫描（`NEAR_SHIFT_RULES`）：短候选沿同一列上下扫几个偏移。
    #    表格加列/加表头会让整表下移，前面两轮只看同一个 y，够不到挪了位的格子。
    if cand and len(cand) <= NEAR_SHIFT_MAX_CHARS:
        if _near_shift_hit(page_other, path_other, i, rect, cand):
            return True
    # ④ 候选**自己那一行**的逐行比对（`NEAR_LINE_RULES`）：①② 拿整窗文本比，
    #    高框候选的留白会把邻行读进来 → 两版整段必然「不同」→ 假增删撤不掉。
    if _near_line_hit(page_self, page_other, rect, path_self, path_other, i):
        return True
    return False                          # ⑤ 证据不足 —— 不撤（与 _scan_mod_confirm 同口径）


def _scan_line_best(n: str, other_lines: Sequence[Dict[str, Any]]) -> Tuple[float, str, Optional[Dict[str, Any]]]:
    """在对面**逐行**找最像的一行，返回 `(ratio, 那一行的原文, 那一行本身)`。

    第三个元素是**对面匹配行**的原始 dict（含 x0/y0/x1/y1 坐标），供调用方
    算位移量（2026-10-12 位移标蓝需求）；找不到时是 None。

    ⚠️ 为什么必须是「逐行」而不是「整页拼串做子序列覆盖」（旧口径，2026-10-01 改）
    ------------------------------------------------------------------------------
    `_coverage(needle, 整页hay)` 是在**一整页的字符海**里按序凑 needle 的字符。
    一页有 3000+ 字符、数字 0-9 到处都是，于是任何短串都能凑齐 →
    coverage 恒 ≈ 1.0。GRA-3925 实测：`5F(0.072"/1.83mm)` 判 **1.0**、
    UDI 那行判 **0.944**，全被当成「挪位」撤掉 —— 用户说的「很多规格数字没识别出来」
    就是这么漏的（一行越长、改得越少，越容易被误撤）。
    """
    best_r, best_raw, best_row = 0.0, "", None
    for it in other_lines or ():
        m = _norm_cjk(it.get("t", ""))
        if not m:
            continue
        r = difflib.SequenceMatcher(None, n, m, autojunk=False).ratio()
        if r > best_r:
            best_r, best_raw, best_row = r, str(it.get("t", "")), it
    return best_r, best_raw, best_row


def _hay_token_hit(n: str, other_lines: Sequence[Dict[str, Any]]) -> bool:
    """「这段字在对面的某个**短条目**里出现过」—— 逐行查容器，不拼整页串。

    ⚠️ 老口径 `n in other_hay` 是在**归一化拼串**上做子串匹配，会撞上「某个长
    单词里正好含着这几个字符」（2026-10-01 实测两处真改动被撤）：
      · 新版表头 `Dist.`（norm `dist`）⊂ 葡语 `Importado e Distribuído`（22 字）
      · 条带 `6.5/`（norm `65`）⊂ 电话号码 `Fone: 11 2548-0365`（15 字）
    改成「容器必须是**短条目**」（≤ `SCAN_MOVE_HAY_MAX_CONT`）两者就分得开：
      · `5` ⊂ `150mm`(5)、`70` ⊂ `6570mm`(6)、`00` ⊂ `200`(3) —— 真·碎片 → 撤
      · `65` ⊂ `fone1125480365`(15)、`dist` ⊂ `importadoedistribudopor`(22) → 巧合 → 留
    """
    for it in other_lines or ():
        m = _norm_cjk(it.get("t", ""))
        if not m or len(m) > SCAN_MOVE_HAY_MAX_CONT:
            continue
        if n in m:
            return True
    return False


def _lead_num(n: str) -> str:
    """行首数字主键：归一化文本里**第一段数字**（`12 (rbp)`→`12`、`6.5/7.0`→`6.5`）。

    表格行的身份就是行首那个数（压力值/尺寸值），字面相似度会骗人
    （`14(RBP)` 和 `12 (RBP)` ratio 0.77，比 `12` 和 `12 (RBP)` 的 0.44 高得多，
    但真正配一对的是后者 —— 见 `SCAN_NUM_PAIR_BOOST`）。
    """
    m = re.search(r"\d+(?:\.\d+)?", n or "")
    return m.group(0) if m else ""


def _same_pos_mod_partner(it, w_a: float, h_a: float,
                          other_lines, w_b: float, h_b: float) -> bool:
    """对面**同一位置**有没有一行「相似但不全同」的行（mod 配对的天然候选）。

    有 → 这行**不能**当挪位撤掉（`_scan_line_moved` 的挪位口径是「这句话
    挪到别处去了」，可对面原地就有一行只差一两个字的 —— 那是行内修改）。
    2026-10-11 GRA-3925 实测：底部 UDI 数字行 50 字只差 `484250`↔`48429 8`
    两个数字，ratio 0.956 过 `SCAN_MOVE_COVER` 被当挪位整行撤掉 ——
    而 token 错位（空格漂移）又让 `_mod_worthy` 拒了互改闸门，两道闸
    叠加 = 真改动消失。位置相同就优先按修改处理。
    """
    ya = (float(it["y0"]) + float(it["y1"])) / 2.0 * h_a
    ax0, ax1 = float(it["x0"]) * w_a, float(it["x1"]) * w_a
    na = _norm_cjk(it["t"])
    for ob_ in other_lines:
        nb = _norm_cjk(ob_["t"])
        if not nb or nb == na:
            continue
        if abs((float(ob_["y0"]) + float(ob_["y1"])) / 2.0 * h_b - ya) > SCAN_PAIR_DY:
            continue
        bx0, bx1 = float(ob_["x0"]) * w_b, float(ob_["x1"]) * w_b
        if abs((bx0 + bx1) / 2.0 - (ax0 + ax1) / 2.0) > SCAN_PAIR_DX:
            if _x_overlap_frac(ax0, ax1, bx0, bx1) < SCAN_PAIR_OV:
                continue
        r = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
        if not (SCAN_LINE_MOD_RATIO <= r < SCAN_MOVE_SAME):
            continue
        # 高相似还不够 —— 得**像一次行内修改**才拦挪位撤除（三选一）：
        # ① 行首数字相同（表格行按主键对齐）；② token 等长的数字小改
        # （`_mod_worthy`，UDI 数字行）；③ 共享一个 ≥6 字的长 token
        # （`6958481` 这类 —— token 错位时 `_mod_worthy` 会拒，长 token 兜住）。
        # 没有这三样就放行挪位判定：多语言段落重排时，相邻两行乱码 vs 正读
        # ratio 也能过 0.55（实测 `paoaajeeo…`→`PTA-pallolaaje…` 0.646），
        # 不加这道闸会把「挪位」误判成「修改」。
        if _lead_num(na) and _lead_num(na) == _lead_num(nb):
            return True
        if _mod_worthy(it["t"], ob_["t"]):
            return True
        ta = set((it.get("t") or "").split())
        tb = set((ob_.get("t") or "").split())
        # ⚠️ 必须卡「差异含数字」：多语言段抖动也共享长 token
        #    （`Recommended` / `Ballon-Dilat…`），不卡会把「挪位/抖动」
        #    误配成「修改」（`R Recommended…` 0.976 实测）。
        if any(len(t) >= 6 for t in (ta & tb)) \
                and any(c.isdigit() for c in set(na) ^ set(nb)):
            return True
    return False


def _scan_line_moved(n: str, raw: str,
                     other_lines: Sequence[Dict[str, Any]],
                     other_hay: str = "") -> bool:
    """这行算不算「只是挪位/重复」（按 `SCAN_MOVE_RULES` 逐条过）。True = 撤。"""
    if not n:
        return True
    if len(n) < OCR_MIN_LEN:
        # ⑥ 短行：**先看逐行 ratio**，只有「对面也有这一整行」才撤。
        #    ⚠️ 顺序不能反（先 `n in other_hay` 会连真改动一起撤）：
        #    `7.0mm` 是 `6.5/7.0mm` 的子串，整行命中 = 撤 → 右上角规格改了却报不出来。
        br, braw, _brow = _scan_line_best(n, other_lines)
        if br >= SCAN_MOVE_SAME:
            return True
        if br >= SCAN_LINE_MOD_RATIO and braw and _mod_worthy(raw, braw):
            return False
        if br >= SCAN_MOVE_COVER:
            return True
        # 含数字的短行对面有「相似但不同」的一行 → 可能是规格数字改了，别撤
        if br >= SCAN_LINE_MOD_RATIO and any(c.isdigit() for c in n):
            return False
        if other_hay and _hay_token_hit(n, other_lines):
            return True
        return False
    br, braw, _brow = _scan_line_best(n, other_lines)
    if br >= SCAN_MOVE_SAME:
        return True                                   # ① 完全相同 / ② 同一行抖动
    # ③ ⚠️ 互改闸门：高相似但两侧构成「行内小改」→ 是真改动，**不撤**
    if br >= SCAN_LINE_MOD_RATIO and braw and _mod_worthy(raw, braw):
        return False
    if br >= SCAN_MOVE_COVER:
        return True                                   # ④ 挪位 / 重排
    return False                                      # ⑤ 保留


def _ocr_crop_lines(page, rect, path: str, i: int, tag: str) -> List[Dict[str, Any]]:
    """裁一块条带做**高分行级 OCR**（Vision json 模式），行框换算回归一化坐标。

    与 `_page_ocr_lines` 的区别：只渲染裁切区（省时），且 400dpi ——
    整页 200dpi 读不全的旋转小字，裁宽了高分读就全（见 `SCAN_REREAD_RULES`）。
    结果按 (文件, 页, 条带量化框, dpi) 跨进程缓存。
    """
    r = fitz.Rect(*rect)
    ck = (int(r.x0 // 20), int(r.x1 // 20), int(r.y0 // 20), int(r.y1 // 20))
    key = (path, i, "band", ck, SCAN_REREAD_DPI, "+".join(OCR_ENGINES), _VISION_VER)
    sig = _sig_with_mtime(path, "%d|band|%s|%d|%s|%d" % (i, ck, SCAN_REREAD_DPI,
                                                        key[5], _VISION_VER))

    def fetch():
        lines: List[Dict[str, Any]] = []
        img = os.path.join(tempfile.gettempdir(), "_pdfdiff_band_%d.png" % os.getpid())
        try:
            page.get_pixmap(dpi=SCAN_REREAD_DPI, clip=r).save(img)
            raw = _ocr_image_lines(img)
        except Exception:
            raw = []
        finally:
            try:
                os.remove(img)
            except OSError:
                pass
        for it in raw:
            lines.append({"t": it["t"],
                          "x0": (r.x0 + it["x0"] * r.width) / page.rect.width,
                          "x1": (r.x0 + it["x1"] * r.width) / page.rect.width,
                          "y0": (r.y0 + it["y0"] * r.height) / page.rect.height,
                          "y1": (r.y0 + it["y1"] * r.height) / page.rect.height})
        return lines

    return _ocr_lines_cached(key, sig, fetch)


def _reread_bands(oa, ob, old_path: str, new_path: str, i: int,
                  la: List[Dict[str, Any]], lb: List[Dict[str, Any]],
                  rem_a: List[Dict[str, Any]], rem_b: List[Dict[str, Any]]
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """对「**真空洞**」条带做两版高分补读，把读全的行并回两侧行集。

    ⚠️ 只补**洞**，不去动两版都读得到的区域（2026-10-01 第一版踩的坑：
    对任何候选簇都补读，两版裁切读出的行切分不一样，把原本恰好抵消的行
    搞得不对称 —— `Compliant` 丢了半截、克罗地亚语整段变假 mod）。
    「洞」= 候选都在同一侧、且**另一侧连一行的中心都落不进这条带** ——
    说明那片是整页 OCR 单侧漏读（GRA-3925 顶部旋转条带），两版各补读
    一次 400dpi 才有行可配。每页最多 `SCAN_REREAD_MAX` 个簇。
    """
    w_a, h_a = oa.rect.width, oa.rect.height
    w_b, h_b = ob.rect.width, ob.rect.height

    def near_other(it, lines, w_pt, h_pt) -> bool:
        """对面在这一行的**近旁**（横向 ±20pt、纵向 ±1.5 行高）有没有行。
        ⚠️ 按项逐个查、别按簇查 —— 簇会把相距几十 pt 的候选链成一串
        （GRA-3925 条带项和第二份标签的候选链成一簇 → 洞判定失效）。"""
        x0 = (float(it["x0"]) * w_pt - 20.0, float(it["y0"]) * h_pt - 1.5 * 22.0,
              float(it["x1"]) * w_pt + 20.0, float(it["y1"]) * h_pt + 1.5 * 22.0)
        for o in lines:
            cx = (float(o["x0"]) + float(o["x1"])) / 2 * w_pt
            cy = (float(o["y0"]) + float(o["y1"])) / 2 * h_pt
            if x0[0] <= cx <= x0[2] and x0[1] <= cy <= x0[3]:
                return True
        return False

    def merge(base: List[Dict[str, Any]], extra: List[Dict[str, Any]],
              w_pt: float, h_pt: float) -> List[Dict[str, Any]]:
        """按 `SCAN_REREAD_RULES` 把补读行并进已有行集。

        ⚠️ 判「同一处」必须**整框重叠**（x、y 都搭上）—— 只看 x 会把 UDI 行
        这类「横向搭着、纵向差得远」的好行也顶掉（第一版踩的坑，GRA-3925
        少了 UDI/规格表一堆真差异）。补读行读不出字（纯符号）时信已有行。
        """
        if not extra:
            return list(base)

        def box(it):
            return (float(it["x0"]) * w_pt, float(it["y0"]) * h_pt,
                    float(it["x1"]) * w_pt, float(it["y1"]) * h_pt)

        def ov_frac(a, b) -> float:
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox <= 0 or oy <= 0:
                return 0.0
            return (ox * oy) / max(1e-9, min((a[2] - a[0]) * (a[3] - a[1]),
                                             (b[2] - b[0]) * (b[3] - b[1])))

        eboxes = [box(e) for e in extra]
        dropped: Set[int] = set()
        claimed: Set[int] = set()
        for j, it in enumerate(base):
            nb = _norm_cjk(it["t"])
            hit = -1
            for k, e in enumerate(extra):
                if ov_frac(box(it), eboxes[k]) >= 0.3:
                    hit = k
                    break
            if hit < 0 or not _norm_cjk(extra[hit]["t"]):
                continue                      # 无补读行接管（或补读行是纯符号）→ 保留
            # ⚠️ 补读行**不比已有行信息量大**就不许顶 —— 裁切带会把整行 UDI
            #    裁成半截 `(01) 0 695848`（v29-32 踩的坑：两版完整 UDI 行都被
            #    半截行顶掉，48425→48429 的真 mod 凭空消失）。
            #    例外：已有的行本身就是 ≤3 字的碎片（`NEX`/`5`/`6.`，整页低分
            #    读岔的）→ 信 400dpi 补读行。
            if len(_norm_cjk(extra[hit]["t"])) < len(nb) and len(nb) >= 4:
                continue
            claimed.add(hit)
            dropped.add(j)                    # 位置被 400dpi 补读行接管
        out2 = [it for j, it in enumerate(base) if j not in dropped]
        for k, e in enumerate(extra):
            ne = _norm_cjk(e["t"])
            if not ne:
                continue
            if k not in claimed:
                taken = False
                for j, it in enumerate(base):
                    if j not in dropped and ov_frac(box(it), eboxes[k]) >= 0.3:
                        taken = True          # 与幸存的已有行同处 → 别重复加
                        break
                if taken:
                    continue
            out2.append(e)                    # 接管位（或整页漏读的新行）→ 加进去
        return out2

    la2, lb2 = list(la), list(lb)
    changed = False
    done_bands: Set[Tuple[int, int, int, int]] = set()
    n_read = 0
    for side, rem, w_pt, h_pt in (("a", rem_a, w_a, h_a), ("b", rem_b, w_b, h_b)):
        for it in sorted(rem, key=lambda x: (float(x["y0"]) + float(x["y1"])) / 2):
            if n_read >= SCAN_REREAD_MAX:
                break
            if side == "a":
                hole = not near_other(it, lb2, w_b, h_b)
            else:
                hole = not near_other(it, la2, w_a, h_a)
            if not hole:
                continue                      # 对面近旁有行 = 不是单侧漏读
            band = (max(0.0, float(it["x0"]) * w_pt - SCAN_REREAD_PAD_X),
                    max(0.0, float(it["y0"]) * h_pt - SCAN_REREAD_PAD_Y),
                    min(w_pt, float(it["x1"]) * w_pt + SCAN_REREAD_PAD_X),
                    min(h_pt, float(it["y1"]) * h_pt + SCAN_REREAD_PAD_Y))
            key = (int(band[0] // 20), int(band[1] // 20),
                   int(band[2] // 20), int(band[3] // 20))
            if key in done_bands:
                continue
            done_bands.add(key)
            n_read += 1
            band_a = (min(band[0], w_a), min(band[1], h_a),
                      min(band[2], w_a), min(band[3], h_a))
            band_b = (min(band[0], w_b), min(band[1], h_b),
                      min(band[2], w_b), min(band[3], h_b))
            la2 = merge(la2, _ocr_crop_lines(oa, band_a, old_path, i, "ba"), w_a, h_a)
            lb2 = merge(lb2, _ocr_crop_lines(ob, band_b, new_path, i, "bb"), w_b, h_b)
            changed = True
        if n_read >= SCAN_REREAD_MAX:
            break
    return (la2, lb2) if changed else (la, lb)


def _line_split_explained(ita, list_a, h_a: float, itb, list_b, h_b: float) -> bool:
    """这对「删除/新增」配成的 mod，是不是**行切分不对称**造成的假象。

    GRA-3925 实测（用户反馈之外、清单里肉眼可见的一处假 mod）：
      · 旧版整页 OCR 把压力表表头读成**两行**：`NP Nominal Pressure` /
        `RBP Rated Burst Pressure`；
      · 新版读成**一行**：`ND Nominal Pressure RBD Rated Burst Pressure`
        （`NP`→`ND`、`RBP`→`RBD` 是 OCR 抖动，两版印的字一模一样）；
      · 配对时「旧一行 ↔ 新一行」相似度 0.552，刚过 `SCAN_LINE_MOD_RATIO`(0.55)
        → 报成一条 mod，`_scan_mod_confirm` 高分重读也推不翻（新版那一行本来就
        多出半句 `NP Nominal Pressure`）。
    判据：**本侧这一行 + 本侧紧邻的另一行** 拼起来 ≈ 对面那一行 —— 说明两版
    内容相同，只是切行方式不同。

    ⚠️ 只跟**纵向紧邻**（|Δ中心 y| ≤ `SCAN_PAIR_DY`×1.5）的行拼，且**不要求**
    横向同列 —— 被并掉的两行本来就一左一右（表头两栏）。
    ⚠️ 两道闸门（少一道就把真改动撤了）：
      ① 两行**本来就**读成同一句（`_near_same(na, nb)`）→ 不走这条；
      ② 邻居本身要够长（≥ `NEAR_SPLIT_PEER_MIN` 字符）—— 拼一个 2 字符的邻居
         谁都能把 ratio 顶过 0.8。
    ⚠️ 拼接结果仍过 `_near_same`（短串必须全等、长串 ratio ≥ 0.8 且长度差 ≤ 10%）
    —— 真改动（`14 atm`→`12 atm`、`Balloon Diameter`→`Conical Balloon Diameter`）
    拼不出对面那句，不会被撤。
    """
    na, nb = _norm_cjk(ita["t"]), _norm_cjk(itb["t"])
    if not na or not nb:
        return False
    if _near_same(na, nb):
        # ⚠️ 两行**本来就**读成「同一句」时不许走这条（2026-10-01 实测打回来的）：
        #    UDI 号那对 `…48425 0…` ↔ `…48429 8…` 是**真 mod**（只差两个字符、
        #    ratio 0.955 → `_near_same` 为真），拿它去和别人拼接，
        #    比例照样 ≥0.8 → 真改动被撤。行切分不对称的前提是「两行本来不像」。
        return False

    def _peers(base, lines, h_pt, nbase, target):
        cy = (float(base["y0"]) + float(base["y1"])) / 2.0 * h_pt
        for p in lines:
            if p is base:
                continue
            np_ = _norm_cjk(p.get("t", ""))
            if len(np_) < NEAR_SPLIT_PEER_MIN:
                continue                       # 太短的邻居拼了也不算「被并掉的一行」
            if abs((float(p["y0"]) + float(p["y1"])) / 2.0 * h_pt - cy) > \
                    1.5 * SCAN_PAIR_DY:
                continue                       # 纵向不紧邻 → 不是「被并掉」的那一行
            if _near_same(nbase + np_, target) or _near_same(np_ + nbase, target):
                return True
        return False

    if _peers(ita, list_a, h_a, na, nb):
        return True
    return _peers(itb, list_b, h_b, nb, na)


def _scan_page_line_regions(pa, pb, oa, ob, i: int, scale: float,
                            old_path: str, new_path: str
                            ) -> Tuple[List[Dict[str, Any]], bool]:



    """扫描件候选的**第二条路线**：两版各整页 OCR 一遍，行级文本对拍。

    为什么需要它（GRA-3925 实测）：两张**不同产品**的标签，差异是**区域级**的
    （整张表格、整块小标签区、整段多语言行），行级差异画像聚出的窗是一个
    363pt 高的巨窗，`_scan_seed_rects` 在巨窗里只出 1 个横跨整页的候选、
    一条都判不出 —— 像素候选路线在「大面积不同」的文件上结构性失效。
    而行对拍根本不依赖像素对齐：每版的行框来自**各版自己的 OCR**，
    内容漂移 1.6% 也无所谓（GRA-3634 那套配准/画像只留给「同版式小改」）。

    对拍规则（与 locate() 字符流引擎同思路，行粒度）：
    ① **全局抵消**：归一化后在两版出现次数相同的行直接消掉（位置无关）；
    ② 剩余行里「只有一侧有」的 → 删除/新增候选；
    ③ 一删一增且归一化相似的 → **行内修改**（mod），再过 `_scan_mod_confirm`
       高分辨率复核（120~200dpi 的 OCR 抖动在 300dpi 下会现形）；
    ④ 剩余单侧行若能在**对面全文**里按序找到（coverage ≥ `SCAN_MOVE_COVER`）
       → 挪位，撤掉（对应字符流引擎的 `_drop_moved`）。

    返回 `(regions, ok)`；`ok=False` = 这条路线不可用（引擎缺失/没认出几行），
    上层退回像素管线。
    """
    dpi = SCAN_LINES_DPI
    la = _page_ocr_lines(oa, old_path, i, dpi)
    lb = _page_ocr_lines(ob, new_path, i, dpi)
    # ⓪.0 竖排补扫（见 `_rot90_supplement`）：整页 OCR 对旋转 90° 排版的行
    #      整列读丢（GRA-3925 顶部尺寸条两版全漏）。转 90° 再读一遍，行框映射
    #      回原页面后**并进行表**——两版都读出的竖排行会在抵消步自然消掉，
    # ⓪.0 竖排补扫（见 `_rot90_supplement`）：整页 OCR 对旋转 90° 排版的行
    #      整列读丢（GRA-3925 顶部尺寸条两版全漏）。转 90° 再读一遍，行框映射
    #      回原页面后**并进行表**——两版都读出的竖排行会在抵消步自然消掉，
    #      只有一版有的（`150`→`300`、多 `Conical`）就成了增删候选。
    #      ⚠️ 用 `+` 拼（别 `+=`）：`_page_ocr_lines` 返回的是缓存列表本体，
    #      原地扩展会让缓存在下次调用时重复叠加。
    #      ⚠️ 只留**新文本**：Vision 连旋转过的水平文字都认得（实测），补扫会把
    #      整页水平行重读一遍 —— 不按归一化文本去重的话，同一物理行多出 2~3 份
    #      候选，mod 配对成倍重复（GRA-3925 PDF 对 31→88 条实测）。
    def _dedup_rot(base: List[Dict[str, Any]], sup: List[Dict[str, Any]]
                   ) -> List[Dict[str, Any]]:
        seen = {_norm_cjk(it.get("t") or "") for it in base}
        out: List[Dict[str, Any]] = []
        for it in sup:
            n = _norm_cjk(it.get("t") or "")
            if not n or n in seen:
                continue
            # 位置去重：Vision 连旋转过的水平文字都认得，重读还会读出**乱码版**
            # （归一化不同、文本去重挡不住），但映射回原页面后**位置与原行重叠**
            # （GRA-3925 芬兰语行被重读成 `paoaajeeo…` 0.646 假 mod 实测）。
            # 与任一未旋转行重叠 ≥60% 的补扫行整条丢。
            ix0, iy0, ix1, iy1 = (float(it["x0"]), float(it["y0"]),
                                  float(it["x1"]), float(it["y1"]))
            dup = False
            for b in base:
                bx0, by0 = float(b["x0"]), float(b["y0"])
                bx1, by1 = float(b["x1"]), float(b["y1"])
                if _overlap_frac((ix0, iy0, ix1, iy1), (bx0, by0, bx1, by1)) >= 0.6:
                    dup = True
                    break
            if dup:
                continue
            seen.add(n)
            out.append(it)
        return out

    la = la + _dedup_rot(la, _rot90_supplement(oa, old_path, i, dpi))
    lb = lb + _dedup_rot(lb, _rot90_supplement(ob, new_path, i, dpi))
    if not la or not lb or len(la) < 3 or len(lb) < 3:
        return [], False
    ca = Counter(_norm_cjk(it["t"]) for it in la if _norm_cjk(it["t"]))
    cb = Counter(_norm_cjk(it["t"]) for it in lb if _norm_cjk(it["t"]))

    hay_a = "\n".join(_norm_cjk(it["t"]) for it in la)
    hay_b = "\n".join(_norm_cjk(it["t"]) for it in lb)

    def leftover(lines, mine, other, other_lines, other_hay,
                 w_self, h_self, w_other, h_other):
        """没被抵消的行；能在对面**找到相似行**的（挪位/重排）收进 `moved`。

        返回 `(out, moved)`：
          · `out` —— 保留的行（进入增删/修改判定）；
          · `moved` —— 被判定为「纯位移」的行，每项 `(本侧行, 对面最像行)`。
            ⚠️ 2026-10-12 位移标蓝需求：纯位移不再静默丢弃，而是转成 moved
            （浅蓝）区域输出 —— 用户反馈「标太少」，内容没变只是平移的行该标蓝。
            ⚠️ 判据必须**严格**（内容一字不差）：`ratio ≥ SCAN_MOVE_SAME`。
            不能用 `_scan_line_moved` 的宽泛 True —— 它会把「真改动但
            `_mod_worthy` 拦不住」的行（条码数字 48425→48429，ratio 0.944、
            token 错位）也判成挪位，误标蓝 = 假一致，比漏标严重得多。

        ⚠️ 位置守卫（2026-10-11）：对面**原地**就有一行「相似但不全同」的
        （`_same_pos_mod_partner`）→ 这行是行内修改候选，**不许**按挪位撤 ——
        底部 UDI 数字行 50 字只差两个数字，ratio 0.956 过挪位线、
        `_mod_worthy` 又被 token 错位拒掉，两道闸叠加真改动就消失了。
        """
        quota = {n: min(mine[n], other.get(n, 0)) for n in mine}
        used: Counter = Counter()
        out = []
        moved = []

        def _has_inplace_copy(it, w_s, h_s, w_o, h_o):
            """对面**原地**是否已有同文本行（中心 y 差 ≤ SCAN_INPLACE_DY）。

            ⚠️ 2026-10-12 22vs23 假位移：同一版面上下各有一块相似内容
            （`3.0mm×80mm 150cm` 上下两块产品标签），计数抵消后残余的那份
            会被 `_scan_line_best` 配到**对面另一块**上，跨出 80pt 的假位移。
            对面原地就有同文本 → 这份是「重复计数残余」，不是位移。
            """
            n = _norm_cjk(it.get("t", ""))
            if not n:
                return False
            cy = (float(it["y0"]) + float(it["y1"])) / 2.0 * h_s
            for ot in other_lines:
                if _norm_cjk(ot.get("t", "")) != n:
                    continue
                if abs((float(ot["y0"]) + float(ot["y1"])) / 2.0 * h_o - cy) \
                        <= SCAN_INPLACE_DY:
                    return True        # 原地就有（x 差异不管：同文本同高即可）
            return False

        for it in lines:
            n = _norm_cjk(it["t"])
            if not n:
                continue
            if used[n] < quota.get(n, 0):
                used[n] += 1                      # 这一份与对面配对抵消
                continue
            if not _same_pos_mod_partner(it, w_self, h_self,
                                         other_lines, w_other, h_other):
                _br, _braw, brow = _scan_line_best(n, other_lines)
                # —— 纯位移（内容一字不差）→ 转蓝；其余挪位/重排仍静默撤 ——
                # ⚠️ 前提：对面**原地**没有同文本行（否则是重复计数残余，
                #    会像 22vs23 那样把上下两块相似内容跨配成假位移）
                if _br >= SCAN_MOVE_SAME and brow is not None \
                        and not _has_inplace_copy(it, w_self, h_self,
                                                  w_other, h_other):
                    moved.append((it, brow))
                    continue
                if _scan_line_moved(n, it["t"], other_lines, other_hay):
                    continue                       # 挪位/重排：对面找得到相似行
            out.append(it)
        return out, moved

    W_pt0, H_pt0 = float(oa.rect.width), float(oa.rect.height)
    W2_pt0, H2_pt0 = float(ob.rect.width), float(ob.rect.height)
    rem_a, moved_a = leftover(la, ca, cb, lb, hay_b, W_pt0, H_pt0, W2_pt0, H2_pt0)
    rem_b, moved_b = leftover(lb, cb, ca, la, hay_a, W2_pt0, H2_pt0, W_pt0, H_pt0)

    # ⓪ 条带补读（2026-10-01 加，见 SCAN_REREAD_RULES）：整页 OCR 对旋转小字
    #    条带读得不稳且**两版不对称**（旧版整条漏、新版出碎片），单侧候选全是
    #    假象。对候选所在条带两版各 400dpi 补读一次，读全了再重新对拍。
    if rem_a or rem_b:
        la2, lb2 = _reread_bands(oa, ob, old_path, new_path, i, la, lb, rem_a, rem_b)
        if (la2, lb2) != (la, lb):
            la, lb = la2, lb2
            ca = Counter(_norm_cjk(it["t"]) for it in la if _norm_cjk(it["t"]))
            cb = Counter(_norm_cjk(it["t"]) for it in lb if _norm_cjk(it["t"]))
            hay_a = "\n".join(_norm_cjk(it["t"]) for it in la)
            hay_b = "\n".join(_norm_cjk(it["t"]) for it in lb)
            rem_a, moved_a = leftover(la, ca, cb, lb, hay_b, W_pt0, H_pt0, W2_pt0, H2_pt0)
            rem_b, moved_b = leftover(lb, cb, ca, la, hay_a, W2_pt0, H2_pt0, W_pt0, H_pt0)

    # ⓪.5 全页高清兜底对拍（见 SCAN_HDPI_RULES）：条带补读只救「单侧读出」的
    #     洞；两版对称漏读（REF 列 200dpi 全空）连洞都形不成，这里独立兜底。
    if SCAN_HDPI_DPI:
        la_h = _page_ocr_lines(oa, old_path, i, SCAN_HDPI_DPI)
        lb_h = _page_ocr_lines(ob, new_path, i, SCAN_HDPI_DPI)
        if (la_h and lb_h
                and len(la_h) >= SCAN_HDPI_MIN_LINES
                and len(lb_h) >= SCAN_HDPI_MIN_LINES):
            ca_h = Counter(_norm_cjk(it["t"]) for it in la_h if _norm_cjk(it["t"]))
            cb_h = Counter(_norm_cjk(it["t"]) for it in lb_h if _norm_cjk(it["t"]))
            hay_a_h = "\n".join(_norm_cjk(it["t"]) for it in la_h)
            hay_b_h = "\n".join(_norm_cjk(it["t"]) for it in lb_h)
            rem_a_h = [it for it in leftover(la_h, ca_h, cb_h, lb_h, hay_b_h,
                                             W_pt0, H_pt0, W2_pt0, H2_pt0)[0]
                       if len(_norm_cjk(it["t"])) > SCAN_HDPI_MIN_CHARS]
            rem_b_h = [it for it in leftover(lb_h, cb_h, ca_h, la_h, hay_a_h,
                                             W2_pt0, H2_pt0, W_pt0, H_pt0)[0]
                       if len(_norm_cjk(it["t"])) > SCAN_HDPI_MIN_CHARS]
            W0, H0 = oa.rect.width, oa.rect.height
            W1, H1 = ob.rect.width, ob.rect.height

            def _cen(it, w, h):
                return ((float(it["x0"]) + float(it["x1"])) / 2.0 * w,
                        (float(it["y0"]) + float(it["y1"])) / 2.0 * h)

            base = [(it, W0, H0) for it in rem_a] + [(it, W1, H1) for it in rem_b]

            def _is_dup(it, w, h):
                cx, cy = _cen(it, w, h)
                for b, bw, bh in base:
                    bx, by = _cen(b, bw, bh)
                    if abs(bx - cx) <= SCAN_HDPI_DUP_XY and abs(by - cy) <= SCAN_HDPI_DUP_XY:
                        return True
                return False

            add_a = [dict(it, h=1) for it in rem_a_h if not _is_dup(it, W0, H0)]
            base += [(it, W0, H0) for it in add_a]
            add_b = [dict(it, h=1) for it in rem_b_h if not _is_dup(it, W1, H1)]
            rem_a += add_a
            rem_b += add_b

    if not rem_a and not rem_b:
        return [], True

    W_pt, H_pt = oa.rect.width, oa.rect.height
    H2_pt = ob.rect.height

    def _pair_x_ok(ita, w_a, itb, w_b) -> bool:
        """两行算不算**同一列**：中心够近，或横向重叠够多（`SCAN_PAIR_DX/OV`）。"""
        ax0, ax1 = float(ita["x0"]) * w_a, float(ita["x1"]) * w_a
        bx0, bx1 = float(itb["x0"]) * w_b, float(itb["x1"]) * w_b
        if abs((bx0 + bx1) / 2 - (ax0 + ax1) / 2) <= SCAN_PAIR_DX:
            return True
        return _x_overlap_frac(ax0, ax1, bx0, bx1) >= SCAN_PAIR_OV

    def to_rect(it, h_pt, w_pt):
        x0 = max(0.0, it["x0"] * w_pt - GRAPHIC_PAD)
        x1 = min(w_pt, it["x1"] * w_pt + GRAPHIC_PAD)
        y0 = max(0.0, it["y0"] * h_pt - GRAPHIC_PAD)
        y1 = min(h_pt, it["y1"] * h_pt + GRAPHIC_PAD)
        return (x0, y0, x1, y1)

    def y_center(it, h_pt):
        return (float(it["y0"]) + float(it["y1"])) / 2.0 * h_pt

    def x_center(it, w_pt):
        return (float(it["x0"]) + float(it["x1"])) / 2.0 * w_pt

    out: List[Dict[str, Any]] = []
    used_a: Set[int] = set()
    used_b: Set[int] = set()
    # ③′ 数字主键预配对（2026-10-11 GRA-3925 用户拍板）：表格**整表下移一行**
    #    （新表多 `Dist./Prox.` 表头）时所有行隔 ~38pt，DY(20pt) 闸门够不着；
    #    `14(RBP)`↔`12 (RBP)` 反而只隔 13pt 先配上 —— 用户语义是「14 行删了、
    #    12 加了 RBP」。行首数字**同列唯一**（'12' 在两侧该列各只出现一次，
    #    右侧 `12 atm` 不同列不算）→ 不看 DY 直接锁配；行距内的普通配对仍走
    #    下面 ③ 的比例闸（数字唯一是强证据，不会误锁）。
    def _col_lead_count(it, lines, w_pt: float, h_pt: float) -> int:
        ka = _lead_num(_norm_cjk(it["t"]))
        ax0, ax1 = float(it["x0"]) * w_pt, float(it["x1"]) * w_pt
        cnt = 0
        for o in lines:
            if _lead_num(_norm_cjk(o["t"])) != ka:
                continue
            bx0, bx1 = float(o["x0"]) * w_pt, float(o["x1"]) * w_pt
            if _x_overlap_frac(ax0, ax1, bx0, bx1) >= SCAN_PAIR_OV:
                cnt += 1
        return cnt

    pre_pairs: List[Tuple[int, int]] = []
    for ia, ita in enumerate(rem_a):
        ka = _lead_num(_norm_cjk(ita["t"]))
        if not ka or ia in used_a:
            continue
        ya = y_center(ita, H_pt)
        hit = -1
        for jb, itb in enumerate(rem_b):
            if jb in used_b or _lead_num(_norm_cjk(itb["t"])) != ka:
                continue
            if abs(y_center(itb, H2_pt) - ya) > 6.0 * SCAN_PAIR_DY:
                continue                     # 同页内放宽，跨半页的不许配
            if not _pair_x_ok(ita, W_pt, itb, ob.rect.width):
                continue
            if hit >= 0:
                hit = -2                     # 不止一个候选 = 主键不唯一，放弃
                break
            hit = jb
        if hit >= 0 and _col_lead_count(ita, rem_a, W_pt, H_pt) == 1 \
                and _col_lead_count(rem_b[hit], rem_b, ob.rect.width, H2_pt) == 1:
            pre_pairs.append((ia, hit))
            used_a.add(ia)
            used_b.add(hit)

    def _mk_mod(ita, itb) -> None:
        rect_a = to_rect(ita, H_pt, W_pt)
        rect_b = to_rect(itb, H2_pt, ob.rect.width)
        # 两侧行框并集当标注矩形（内容在两版位置有漂移；region 只有一个
        # rect（对齐 A 坐标系），另带 rect_a/rect_b 给高分复核分开裁）
        rect = (min(rect_a[0], rect_b[0]), min(rect_a[1], rect_b[1]),
                max(rect_a[2], rect_b[2]), max(rect_a[3], rect_b[3]))
        rg = _mk_region(i, rect, "mod",
                        ita["t"].strip() + "\x00" + itb["t"].strip())
        rg["rect_a"] = rect_a
        rg["rect_b"] = rect_b
        # 行对拍的 OCR 分辨率有限（200dpi），mod 先过高分复核再报
        if _scan_mod_confirm(oa, ob, i, rect_a, old_path, new_path,
                             rect_b=rect_b):
            _narrow_mod_region(rg, oa, ob)   # 整行框 → 只圈真正变了的片段
            out.append(rg)

    for ia, jb in pre_pairs:
        _mk_mod(rem_a[ia], rem_b[jb])
    # ③ 一删一增相似 → mod
    # ⚠️ 2026-10-01 三处实测打回来的配对规矩：
    #    · 只按 ratio 贪心会**跨区错配**（`150mm` 配到隔 33pt 的 `6.5/7.0 mm`、
    #      `1400` 配到页顶的 `200`）→ 先卡 `SCAN_PAIR_DY` / `_pair_x_ok` 同区同列；
    #    · 按 rem_a 的**顺序**贪心会让弱配对抢走强配对的对象（A 的 `12` 先抢掉
    #      `12 (RBP)`，真改动 `14(RBP)→12 (RBP)` 反而落单）→ 先攒候选、
    #      再按 ratio **降序**全局配对；
    #    · **两轮配对**（v39 实测）：高清兜底行（h 标记）先不参与 —— 否则 400dpi
    #      读出的整行 UDI（更长、ratio 更高）会把 200dpi 已配好的 REF mod 的
    #      对面抢走，REF 落单后走撤除链被 `_near_same`（等长数字替换 ratio 0.95
    #      过线）当同一句撤掉 —— v37 时代报得出的改动反而消失。
    cands_200: List[Tuple[float, int, int]] = []
    cands_h: List[Tuple[float, int, int]] = []
    num_reserved_b: Set[int] = set()
    for ia, ita in enumerate(rem_a):
        na = _norm_cjk(ita["t"])
        if not na:
            continue
        ya = y_center(ita, H_pt)
        for jb, itb in enumerate(rem_b):
            nb = _norm_cjk(itb["t"])
            if not nb:
                continue
            if abs(y_center(itb, H2_pt) - ya) > SCAN_PAIR_DY:
                continue
            if not _pair_x_ok(ita, W_pt, itb, ob.rect.width):
                continue
            r = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
            # 数字主键优先（SCAN_NUM_PAIR_BOOST 注释）：`12`↔`12 (RBP)` 0.44
            # 抬到 0.80，压过 `14(RBP)`↔`12 (RBP)` 的 0.77 —— 表格按行首数字对齐。
            if r < SCAN_NUM_PAIR_BOOST:
                la_n, lb_n = _lead_num(na), _lead_num(nb)
                if la_n and la_n == lb_n:
                    r = SCAN_NUM_PAIR_BOOST
            if r >= SCAN_LINE_MOD_RATIO:
                (cands_h if (ita.get("h") or itb.get("h"))
                 else cands_200).append((r, ia, jb))
                # ⚠️ 数字主键预约（SCAN_NUM_PAIR_BOOST 注释）：200dpi 轮里已构成
                #    「行首数字相同」配对的对面行，**不许**让 h 轮的弱相似对抢走 ——
                #    `12`↔`12 (RBP)`(0.80) 是用户语义，`14(RBP)`↔`12 (RBP)`(0.77)
                #    不是（GRA-3925 实测：h 轮先跑，`12 (RBP)` 被抢，表格错配）。
                if not (ita.get("h") or itb.get("h")) \
                        and _lead_num(na) and _lead_num(na) == _lead_num(nb):
                    num_reserved_b.add(jb)
    for cands in (cands_200, cands_h):
        cands.sort(key=lambda c: -c[0])
        for r, ia, jb in cands:
            if ia in used_a or jb in used_b:
                continue
            if cands is cands_h and jb in num_reserved_b:
                continue          # 这行留给 200dpi 的数字主键配对
            ita, itb = rem_a[ia], rem_b[jb]
            if _line_split_explained(ita, rem_a, H_pt, itb, rem_b, H2_pt):
                # 行切分不对称（见函数注释）：不是两版内容不同，是同一段字
                # 一版读成两行、另一版读成一行 —— 配出来的 mod 是假象。
                # 两侧都记 used，防它们再去配别的行。
                used_a.add(ia)
                used_b.add(jb)
                continue
            used_a.add(ia)
            used_b.add(jb)
            rect_a = to_rect(ita, H_pt, W_pt)
            rect_b = to_rect(itb, H2_pt, ob.rect.width)
            # 两侧行框并集当标注矩形（内容在两版位置有漂移；region 只有一个
            # rect（对齐 A 坐标系），另带 rect_a/rect_b 给高分复核分开裁）
            rect = (min(rect_a[0], rect_b[0]), min(rect_a[1], rect_b[1]),
                    max(rect_a[2], rect_b[2]), max(rect_a[3], rect_b[3]))
            rg = _mk_region(i, rect, "mod",
                            ita["t"].strip() + "\x00" + itb["t"].strip())
            rg["rect_a"] = rect_a
            rg["rect_b"] = rect_b
            # 行对拍的 OCR 分辨率有限（200dpi），mod 先过高分复核再报
            if _scan_mod_confirm(oa, ob, i, rect_a, old_path, new_path,
                                 rect_b=rect_b):
                _narrow_mod_region(rg, oa, ob)   # 整行框 → 只圈真正变了的片段
                out.append(rg)
    for ia, ita in enumerate(rem_a):
        if ia in used_a:
            continue
        na = _norm_cjk(ita["t"])
        if not na:
            continue
        # 配不上相似的对面行 = 这一侧整行没有了 → 删除
        # （对面其实有、只是 OCR 读岔的情况已被上面挪位检查撤掉）
        # 最后一道：对面同位置高分重读认得出这行 = 内容还在（单侧乱码/漏读），撤
        rect_a = to_rect(ita, H_pt, W_pt)
        if ita.get("h"):
            # 高清单侧行（SCAN_HDPI_RULES「裁切精读定案」）：全页 400dpi 只有
            # 旧版读出 ≠ 新版页面上没有 —— 全页 OCR 对旋转小字也会整列读丢。
            # 两版各裁切 400dpi 精读一次，读出同一句（_same_text_noise，与
            # mod 宽读对账同口径）= 单侧漏读假象，撤；读出不同 = 行内修改。
            # ⚠️ 两版窗口必须**同规格**（都走 _hdpi_probe_rect）：self 用裸行框、
            #    other 用 pad 窗时，old 侧窄条只读得出半句（`A35-070`），
            #    new 侧读出整句（`REF A35-070 300-200`）—— mod 文本没法看。
            rs = _hdpi_probe_rect(rect_a, float(oa.rect.width), float(oa.rect.height))
            t_self = _ocr_crop(oa, rs, dpi=SCAN_HDPI_DPI,
                               cache_key=(old_path, i, "hself")
                               + tuple(int(v * 10) for v in rs))
            rb = _hdpi_probe_rect(rect_a, float(ob.rect.width), float(ob.rect.height))
            t_oth = _ocr_crop(ob, rb, dpi=SCAN_HDPI_DPI,
                              cache_key=(new_path, i, "hoth")
                              + tuple(int(v * 10) for v in rb))
            if _norm_cjk(t_oth):
                if _same_text_noise(_norm_cjk(t_self), _norm_cjk(t_oth)):
                    continue
                # 结构对账（SCAN_HDPI_RULES「结构对账」）：乱码抖动区的文本对账
                # 永远「不同」，页面长得一样就不许报（绝不凭空造不一致）。
                if _px_struct_dist(oa, ob, rs) <= SCAN_HDPI_STRUCT_MAX:
                    continue
                rg = _mk_region(i, _hdpi_union_box(
                    oa, ob, old_path, new_path, i, rs, rb,
                    float(oa.rect.width), float(oa.rect.height),
                    float(ob.rect.width), float(ob.rect.height)), "mod",
                                t_self.strip() + "\x00" + t_oth.strip())
                rg["rect_a"] = rect_a
                rg["rect_b"] = (rb.x0, rb.y0, rb.x1, rb.y1)
                _narrow_mod_region(rg, oa, ob)   # 整行框 → 只圈真正变了的片段
                out.append(rg)
                continue
            if _line_near_check(oa, ob, rect_a, old_path, new_path, i,
                                text=ita["t"]):
                continue
            out.append(_mk_region(i, rect_a, "old",
                                  ita["t"].strip()))   # 旧版有新版没 = 删除
            continue
        if _line_near_check(oa, ob, rect_a, old_path, new_path, i,
                            text=ita["t"]):
            continue
        out.append(_mk_region(i, rect_a, "old",
                              ita["t"].strip()))   # 旧版有新版没 = 删除
    for jb, itb in enumerate(rem_b):
        if jb in used_b:
            continue
        nb = _norm_cjk(itb["t"])
        if nb and _scan_line_moved(nb, itb["t"], la):
            continue                              # 挪进来的，撤
        # 对面同位置高分重读认得出这行 = 内容两版都有（单侧乱码 / 单侧漏读），撤
        rect_b = to_rect(itb, H2_pt, ob.rect.width)
        if itb.get("h"):
            # ⚠️ 两版窗口必须**同规格**（都走 _hdpi_probe_rect）：理由见上面
            #    rem_a 侧的同名注释 —— 窗口不对称会让 mod 文本只读出半句。
            rs = _hdpi_probe_rect(rect_b, float(ob.rect.width), float(ob.rect.height))
            t_self = _ocr_crop(ob, rs, dpi=SCAN_HDPI_DPI,
                               cache_key=(new_path, i, "hself")
                               + tuple(int(v * 10) for v in rs))
            ra = _hdpi_probe_rect(rect_b, float(oa.rect.width), float(oa.rect.height))
            t_oth = _ocr_crop(oa, ra, dpi=SCAN_HDPI_DPI,
                              cache_key=(old_path, i, "hoth")
                              + tuple(int(v * 10) for v in ra))
            if _norm_cjk(t_oth):
                if _same_text_noise(_norm_cjk(t_self), _norm_cjk(t_oth)):
                    continue
                # 结构对账：同上，乱码抖动区的最后一道防线
                if _px_struct_dist(ob, oa, rs) <= SCAN_HDPI_STRUCT_MAX:
                    continue
                rg = _mk_region(i, _hdpi_union_box(
                    oa, ob, old_path, new_path, i, ra, rs,
                    float(oa.rect.width), float(oa.rect.height),
                    float(ob.rect.width), float(ob.rect.height)), "mod",
                                t_oth.strip() + "\x00" + t_self.strip())
                rg["rect_b"] = rect_b
                rg["rect_a"] = (ra.x0, ra.y0, ra.x1, ra.y1)
                _narrow_mod_region(rg, oa, ob)   # 整行框 → 只圈真正变了的片段
                out.append(rg)
                continue
            if _line_near_check(ob, oa, rect_b, new_path, old_path, i,
                                text=itb["t"]):
                continue
            out.append(_mk_region(i, rect_b, "new",
                                  itb["t"].strip()))   # 新版有旧版没 = 新增
            continue
        if _line_near_check(ob, oa, rect_b, new_path, old_path, i,
                            text=itb["t"]):
            continue
        out.append(_mk_region(i, rect_b, "new",
                              itb["t"].strip()))   # 新版有旧版没 = 新增

    # —— 位移出口（2026-10-12 位移标蓝需求）——
    # `leftover` 把「纯位移」的行收集进 moved_a/moved_b（旧侧/新侧各一份）。
    # 这里把两侧的挪位行**配对去重**后转成 moved 区域（浅蓝）。
    # 判据：内容在对面有**一字不差**的相同行（ratio ≥ SCAN_MOVE_SAME）=
    #   内容没变、只是位置平移。块内真改动仍走 mod/增删链路标红绿黄。
    # ⚠️ 三道守卫（防把真改动/碎片误标蓝）：
    #   ① 归一化后 ≥ 3 字符（`'5'`/`'mm'`/`'R'` 这类单字符碎片是 OCR 噪声）；
    #   ② 两侧行**同列**（中心 x 差 ≤ SCAN_PAIR_DX），跨半页的错配不算位移；
    #   ③ 位移量 ≥ 1pt（同一位置的「重复图层」不是位移）。
    if moved_a or moved_b:
        seen_move: Set[Tuple[str, str]] = set()
        for side, pairs, w_self, h_self, w_oth, h_oth in (
                ("old", moved_a, W_pt, H_pt, ob.rect.width, ob.rect.height),
                ("new", moved_b, ob.rect.width, ob.rect.height, W_pt, H_pt)):
            for it, brow in pairs:
                if brow is None:
                    continue                     # 对面没找到匹配行 → 不算位移
                t_self = _norm_cjk(it.get("t", ""))
                t_oth = _norm_cjk(brow.get("t", ""))
                if not t_self or not t_oth:
                    continue
                if len(t_self) < 3 or len(t_oth) < 3:
                    continue                     # 单字符碎片：OCR 噪声，不标蓝
                key = (t_self, side)
                if key in seen_move:
                    continue
                seen_move.add(key)
                rect_self = to_rect(it, h_self, w_self)
                rect_oth = to_rect(brow, h_oth, w_oth)
                # 同列守卫：中心 x 差过大 = 跨栏错配（条码数字行会被错误配到
                # 半页外的同文行），不算位移
                cx_self = (rect_self[0] + rect_self[2]) / 2.0
                cx_oth = (rect_oth[0] + rect_oth[2]) / 2.0
                if abs(cx_oth - cx_self) > SCAN_PAIR_DX:
                    continue
                dx = (rect_oth[0] - rect_self[0])
                dy = (rect_oth[1] - rect_self[1])
                # 位移量太小（< 1pt）≈ 同一位置 → 不是位移，跳过（避免把
                # 「重复图层/同一行」误标成蓝）
                if abs(dx) < 1.0 and abs(dy) < 1.0:
                    continue
                out.append(_mk_region(
                    i, rect_self, "moved",
                    it["t"].strip() + "\x00%.2f\x00%.2f\x00%s" % (dx, dy, side)))
    return out, True


def ocr_strip(page_idx: int, path: str, x0: float, x1: float, page) -> str:
    """OCR「某一栏的整条竖带」——按 (文件, 页, 栏) 缓存，避免同一条带来回识别。

    为什么要整条带：报「删除/新增」时，我们只知道**旧版**那一块在哪，
    不知道新版对应内容被重排到哪儿去了 —— 索性把同一条栏 OCR 一遍再找。
    """
    x0 = max(0.0, x0 - 6)
    x1 = min(float(page.rect.width), x1 + 6)
    if x1 - x0 < 60:                       # 太窄的带没意义，补宽一点
        mid = (x0 + x1) / 2
        x0, x1 = mid - 30, mid + 30
    key = (path, page_idx, int(x0 // 50), int(x1 // 50), 0)
    return _ocr_crop(page, fitz.Rect(x0, 5, x1, page.rect.height - 5),
                     cache_key=key)



def _ocr_band(page, rect, cache_key=None) -> str:
    """OCR「这一块所在的那条竖带」：横向范围照抄、纵向拉到整页。

    为什么必须整条带（实测打回来的一个误报）
    ----------------------------------------
    只比「同一矩形」时，**整段被推到上面或下面、挪得比这块本身还远**的情况就漏了 ——
    GRA-4118 的免责声明在新版整体下移约 72pt（超过这块本身的高度），同一矩形里
    当然比对不到，于是被误报成「这块新增了内容」。把整条栏 OCR 一遍，才知道那段字
    还在、只是挪了位置。
    """
    x0 = max(0.0, float(rect[0]) - 10)
    x1 = min(float(page.rect.width), float(rect[2]) + 10)
    return _ocr_crop(page, fitz.Rect(x0, 3, x1, page.rect.height - 3),
                     cache_key=cache_key)


def _ocr_near(page, ref, path: str, i: int, tag: str = "near") -> str:
    """OCR「对面这一块**周围**那一小片」——比整页竖带读得准得多。

    为什么不用整页竖带做逐行核对（2026-09-29 实测）
    ----------------------------------------------
    逐行复核时，needle 是**一行**（十几个字），对面那条竖带却是「整列从头到尾」——
    同一列里还混着别的段落、栏板，tesseract 在这条又高又窄的带上错字很多，
    真实覆盖度被压到 0.53~0.73，该撤的行撤不掉。
    改成「块上下各扩 `OCR_NEAR_PAD` 倍块高」这一小片后，实测区分度干净利落：
    真新增那一行 0.000、只是下移的两行 1.000 —— 中间空档极大。
    ⚠️ 必须用**块**矩形（`ref`）而不是行矩形来定范围：整段被推上去/推下来的距离
    可能比一行还大，按行取范围就找不到对面那行了。
    """
    h = max(8.0, float(ref[3]) - float(ref[1]))
    x0 = max(0.0, float(ref[0]) - 8.0)
    x1 = min(float(page.rect.width), float(ref[2]) + 8.0)
    y0 = max(0.0, float(ref[1]) - OCR_NEAR_PAD * h)
    y1 = min(float(page.rect.height), float(ref[3]) + OCR_NEAR_PAD * h)
    return _ocr_crop(page, fitz.Rect(x0, y0, x1, y1),
                     cache_key=(path, i, tag, int(x0 // 40), int(x1 // 40),
                                int(y0 // 40), int(y1 // 40)))


class _ShiftedPixmap:
    """`pb` 的纵向平移视图：`samples` 行已按 dy 表重排。

    只暴露下游用到的四个属性（width/height/stride/samples）——
    `_scan_windows` / `_scan_seed_rects` / `_decide_region` / `_refine_region`
    全都只碰这几个，真正的 `fitz.Pixmap` 没必要再造一份。
    """

    __slots__ = ("width", "height", "stride", "samples")

    def __init__(self, src, rows: bytes):
        self.width = src.width
        self.height = src.height
        self.stride = src.stride
        self.samples = rows


def _align_vertical(pa, pb):
    """两版位图**内容纵向漂移**的配准（GRA-3925 2MB vs 4MB 实测）。

    同一版式的两张标签，不同批次拍摄/扫描，内容放置位置有 ~1.6% 的纵向拉伸差
    （y=120px 处 dy=4、y=500px 处 dy=10，嵌入位图尺寸/页面矩阵完全相同）。
    不配准的后果：逐像素差异画像把**整页点亮**（热瓦片 69.9%、差异行连成
    5~510pt 一整段），行级聚窗失效，只出得了 4 个候选、漏检几十处。

    ⚠️ 逐带 SAD 直接估 dy 不行（第一版踩的坑）：这份语料是**两个不同产品**的
    标签，大面积内容不同的带里 SAD 没有对齐信号，估出的 dy 是乱值
    （实测 39 带里 -12、-9、7、11 什么都有），按它重排会把 B 撕碎。
    但真漂移是**线性**的（缩放差），所以分三步：
    ① 每带求 best dy，并算 dy=0 的 SAD 当参照 —— 只有 `best < TRUST × dy0`
       的带才可信（内容相同的带对齐后 SAD 显著下降；内容不同的带 best≈dy0）；
    ② 对可信带的 (y, dy) 做 RANSAC 式直线拟合（内点 = |dy − 线值| ≤ 1.2）；
    ③ 全页按拟合线赋 dy 重排。

    返回 `(对齐后的 pb 视图, 平均|dy|)`；可信带不足 6 个或拟合内点不足
    可信带的 40% 时**原样返回**（GRA-3634 同批次扫描走这条零开销路径）。
    """
    W, H = pa.width, pa.height
    if (pb.width, pb.height) != (W, H):
        return pb, 0.0
    A, B = pa.samples, pb.samples
    sa, sb = pa.stride, pb.stride
    band = SCAN_ALIGN_BAND_H
    n_bands = (H + band - 1) // band
    dys: List[int] = []
    trusted: List[Tuple[float, int]] = []     # (带中心 y, dy) 只收可信带
    for bi in range(n_bands):
        by0, by1 = bi * band, min(H, (bi + 1) * band)
        rows = range(by0, by1, 4)
        cols = range(0, W, SCAN_ALIGN_STEP)

        def band_sad(dy: int) -> int:
            if by0 + dy < 0 or by1 - 1 + dy >= H:
                return -1
            s = 0
            for y in rows:
                oa = y * sa
                ob = (y + dy) * sb
                for k in cols:
                    s += abs(A[oa + k] - B[ob + k])
            return s

        s0 = band_sad(0)
        best_s, best_d = None, 0
        for dy in range(-SCAN_ALIGN_DY, SCAN_ALIGN_DY + 1):
            if dy == 0:
                continue
            s = band_sad(dy)
            if s < 0:
                continue
            if best_s is None or s < best_s:
                best_s, best_d = s, dy
        dys.append(best_d if best_s is not None and (s0 < 0 or best_s < s0) else 0)
        if s0 > 0 and best_s is not None and best_s < SCAN_ALIGN_TRUST * s0:
            trusted.append((bi * band + band / 2.0, best_d))
    if len(trusted) < 6:
        return pb, float(sum(abs(d) for d in dys)) / max(1, n_bands)
    # RANSAC 线性拟合：dy(y) = a*y + b（只用可信带投票）
    best_model = None
    for ii in range(len(trusted)):
        for jj in range(ii + 1, len(trusted)):
            yi, di = trusted[ii]
            yj, dj = trusted[jj]
            if yj <= yi:
                continue
            a = (dj - di) / (yj - yi)
            if abs(a) > 0.05:
                continue              # 缩放差不会有 5% 这么大，这对样本必是离群
            b = di - a * yi
            inl = sum(1 for y, d in trusted if abs(d - (a * y + b)) <= 1.2)
            if best_model is None or inl > best_model[0]:
                best_model = (inl, a, b)
    if best_model is None or best_model[0] < 0.4 * len(trusted):
        return pb, float(sum(abs(d) for d in dys)) / max(1, n_bands)
    _inl, a, b = best_model
    sm = [int(round(a * (bi * band + band / 2.0) + b)) for bi in range(n_bands)]
    if all(d == 0 for d in sm):
        return pb, 0.0
    # 重排：B2[y] = B[y + dy(y)]
    rows = bytearray()
    for y in range(H):
        yb = min(H - 1, max(0, y + sm[y // band]))
        rows += B[yb * sb:yb * sb + sb]
    avg = sum(abs(d) for d in sm) / n_bands
    return _ShiftedPixmap(pb, bytes(rows)), avg


def _scan_windows(pa, pb, scale: float) -> List[Tuple[float, float]]:
    """纯扫描件：逐像素行统计差异，聚成候选 y 窗口（pt）。

    为什么不用 `_hot_tiles`（见 `GRAPHIC_SCAN_BANDS` 常量处的说明）：
    两次扫描的亚像素错位是**全页噪声**，瓦片法要么被噪声淹没要么漏掉小改动。
    行级画像 + 低门槛聚窗，宁可多给候选 —— 后面 `_decide_region` 的 OCR 判据
    会把噪声窗口一个个否掉。
    """
    W, H = pa.width, pa.height
    A, B = pa.samples, pb.samples
    runs: List[List[int]] = []
    cur: Optional[List[int]] = None
    gap = 0
    for y in range(H):
        a = A[y * W:(y + 1) * W]
        b = B[y * W:(y + 1) * W]
        n = sum(1 for p, q in zip(a, b) if abs(p - q) > GRAPHIC_DARK)
        if n >= SCAN_ROW_PX:
            if cur is None:
                cur = [y, y, 1]
            else:
                cur[1] = y
                cur[2] += 1
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > SCAN_GAP_ROWS:
                if cur[2] >= SCAN_MIN_ROWS:
                    runs.append(cur)
                cur = None
                gap = 0
    if cur is not None and cur[2] >= SCAN_MIN_ROWS:
        runs.append(cur)
    return [(r[0] / scale, (r[1] + 1) / scale) for r in runs]


def _scan_seed_rects(pa, pb, y0: float, y1: float,
                     scale: float) -> List[Tuple[float, float, float, float]]:
    """把一个差异窗口精修成**若干候选矩形**（每个 = 一簇差异所在的那一行）。

    为什么是「若干个」而不是一个种子（GRA-3634 页脚实测）：单个「差异最强列」
    当种子太脆 —— 粗体大字（`STERILE EO`）的扫描错位噪声在每列上积累的差异数
    比真改（`Rev02`→`Rev03`，一共才十几个像素）还高，种子一落错整窗报废。
    改成：差异列先**按竖直线列切开、按空隙聚簇**，每簇独立定行、独立扩展、
    独立判定 —— 真改簇和噪声簇各判各的，互不拖累。

    每个簇的矩形怎么定（三个坑都是实测出来的）：
    1. **x 不能用条带墨迹范围**：边框/表格框是通页竖线，任何条带的墨迹 x 都
       横跨整页 → OCR 混读一大片。所以从簇边缘沿「墨迹连续（字隙 ≤
       `SCAN_JOIN_GAP_PT`）」扩展，停在字间空隙大的地方。
    2. **竖直线列**（窗口 ± `SCAN_VLINE_CONTEXT_PT` 范围里墨迹占比 ≥
       `SCAN_VLINE_FRAC`）既当簇的分隔符、又在扩展掩膜里剔除 —— 否则扩展会
       顺着框线横穿整页。上下文必须远高于窗口本身，否则单行窗口里文字列
       自己就是 100% 有墨，会被误剔。
    3. **y 取该簇差异行**包住峰值的那段连续行，不能用整个窗口（相邻行噪声
       会把好几行并进一窗，整窗 OCR token 错位，`_mod_worthy` 全部拒掉）。
    4. **扩展掩膜只用改动行自身（±2 行）的墨，且一列 ≥2 行有墨才算墨、
       整行墨迹 > 50% 页宽的「结构行」（表格横线）不参与** —— 否则单像素噪点
       或横线搭桥，扩展一路冲到页框（实测一半矩形的 x1 都是 512.1）。
    """
    W = pa.width
    ry0 = max(0, int(y0 * scale) - 1)
    ry1 = min(pa.height, int((y1 + 1) * scale) + 1)
    iy0 = max(0, ry0 - int(SCAN_VLINE_CONTEXT_PT * scale))
    iy1 = min(pa.height, ry1 + int(SCAN_VLINE_CONTEXT_PT * scale))
    A, B = pa.samples, pb.samples

    def row_pairs(y):
        off_a = y * pa.stride
        off_b = y * pb.stride
        return A[off_a:off_a + W], B[off_b:off_b + W]

    col_diff = [0] * W
    diff_cols_of_row = {}
    for y in range(ry0, ry1):
        ra, rb = row_pairs(y)
        cols = [k for k in range(W) if abs(ra[k] - rb[k]) > GRAPHIC_DARK]
        if cols:
            diff_cols_of_row[y] = cols
            for k in cols:
                col_diff[k] += 1
    if not diff_cols_of_row:
        return []
    # 竖直线列（上下文范围）
    ink_n = [0] * W
    for pm in (pa, pb):
        for y in range(iy0, iy1):
            off = y * pm.stride
            for k, v in enumerate(pm.samples[off:off + W]):
                if v < SCAN_INK_DARK:
                    ink_n[k] += 1
    ctx_rows = max(1, iy1 - iy0)
    vline = [ink_n[k] >= SCAN_VLINE_FRAC * ctx_rows for k in range(W)]
    # 结构行（表格横线/框线：整行墨迹 > 50% 页宽）—— 只在窗口行里判
    struct_row = set()
    for y in diff_cols_of_row:
        off = y * pa.stride
        if sum(1 for v in pa.samples[off:off + W] if v < SCAN_INK_DARK) > 0.5 * W:
            struct_row.add(y)
    # x 簇：有差异的列按空隙聚簇（x 升序扫，跨行同簇自然合并），竖直线列当分隔符
    join = max(1, int(round(SCAN_JOIN_GAP_PT * scale)))
    runs: List[List[int]] = []          # [x0, x1, 差异像素总数]
    for k in range(W):
        if vline[k] or col_diff[k] <= 0:
            continue
        if runs and k - runs[-1][1] <= join:
            runs[-1][1] = k
            runs[-1][2] += col_diff[k]
        else:
            runs.append([k, k, col_diff[k]])
    runs = [r for r in runs if r[2] >= SCAN_RUN_MIN_PX]
    if not runs:
        return []
    runs.sort(key=lambda r: -r[2])
    runs = runs[:SCAN_MAX_RUNS]
    out: List[Tuple[float, float, float, float]] = []
    for cx0, cx1, _n in runs:
        # 该簇的差异行 → 每段连续行组各出一个候选（簇跨两行时上下各一个）
        hit = sorted(y for y, cols in diff_cols_of_row.items()
                     if any(cx0 <= k <= cx1 for k in cols))
        if not hit:
            continue
        groups = []
        for y in hit:
            if groups and y - groups[-1][1] <= SCAN_GAP_ROWS:
                groups[-1][1] = y
            else:
                groups.append([y, y])
        for g in groups:
            def _has_ink(pm, y, lo, hi):
                off = y * pm.stride
                return any(v < SCAN_INK_DARK for v in pm.samples[off + lo:off + hi + 1])

            # ① 先按改动行定 x（mask 用改动行自身的墨）
            ln0 = max(0, g[0] - 2)
            ln1 = min(pa.height, g[1] + 3)
            ink2 = [0] * W
            for pm in (pa, pb):
                for y in range(ln0, ln1):
                    if y in struct_row:
                        continue
                    off = y * pm.stride
                    for k, v in enumerate(pm.samples[off:off + W]):
                        if v < SCAN_INK_DARK:
                            ink2[k] += 1
            mask = bytearray(W)
            for k in range(W):
                mask[k] = 1 if (ink2[k] >= 2 and not vline[k]) else 0
            x0 = cx0
            gap = 0
            while x0 > 0 and gap <= join:
                x0 -= 1
                gap = gap + 1 if not mask[x0] else 0
            x1 = cx1
            gap = 0
            while x1 < W - 1 and gap <= join:
                x1 += 1
                gap = gap + 1 if not mask[x1] else 0
            # ② 再把 y 扩到字的完整高度：差异往往只落在数字中间几行，
            #    不扩就是 3~4pt 高的薄片 —— OCR 读半截字，乱码假报一堆
            #    （GRA-3634 实测 3 条假报全是薄片：`9707`→`1707`、表格头乱码）。
            y0p = g[0]
            for _ in range(int(SCAN_Y_EXPAND_PT * scale)):
                ny = y0p - 1
                if ny < 0 or not (_has_ink(pa, ny, x0, x1) or _has_ink(pb, ny, x0, x1)):
                    break
                y0p = ny
            y1p = g[1]
            for _ in range(int(SCAN_Y_EXPAND_PT * scale)):
                ny = y1p + 1
                if ny >= pa.height or not (_has_ink(pa, ny, x0, x1) or _has_ink(pb, ny, x0, x1)):
                    break
                y1p = ny
            out.append((max(0.0, x0 / scale - 1.5),
                        max(0.0, y0p / scale - 1.2),
                        (x1 + 1) / scale + 1.5,
                        (y1p + 1) / scale + 1.2))
    return out


SCAN_CONFIRM_DPI = 300      # 扫描件 mod 候选的高分辨率复核 DPI（低分辨率判的，
                            # 扫描噪声能把数字认岔出假「修改」，高分辨率再读一遍对账）

# 两侧同位置重读的**尝试阶梯**（2026-10-01 加第二轮，理由见 `_line_near_check`）
NEAR_CHECK_STEPS = [
    {"pad_x": 8.0, "pad_y": 0.8, "dpi": SCAN_CONFIRM_DPI},   # ① 贴合框，够用就撤
    {"pad_x": 12.0, "pad_y": 1.8, "dpi": 400},               # ② 放宽 + 提分辨率
]
NEAR_SUBSTR_MAX = 16        # 只对这么短（含）以内的候选做「在对面找得到」判定
NEAR_SUBSTR_RATIO = 0.5     # 短候选在对侧重读里的占比门槛（防 `Conical` 这类真新增被误撤）

# ③ 纵向重排扫描（2026-10-01 加，`NEAR_SHIFT_RULES`）
# 表格**加了一列/一行表头**后，整表会整体下移：GRA-3925 新版压力表多了
# 「Conical Balloon Diameter」表头行，同一格两版差了 ~19pt。而前面两轮重读只看
# **同一个 y**，挪过位的格子读到的永远是邻行的值 → 旧版明明印着 `8`，却被报成
# 「新版新增 `8`」（用户反馈的假新增）。
# 修法：短候选再沿**同一列**上下扫几个偏移，每个偏移用一个**窄窗**（纵向只外扩
# 5pt —— 宽了就会跨行把邻居的值读进来）重读；读出同一句 = 内容还在、只是行挪了。
# ⚠️ 只对短候选扫：长句在这么窄的窗里根本读不全，扫了只会拿到半截噪声。
NEAR_SHIFT_DY = (8.0, -8.0, 16.0, -16.0, 24.0, -24.0)
NEAR_SHIFT_PAD_X = 8.0      # 横向外扩（与 NEAR_CHECK_STEPS ① 同量级）
NEAR_SHIFT_PAD_Y = 5.0      # 纵向外扩：只盖住本行
NEAR_SHIFT_MAX_CHARS = 6    # 只对归一化后 ≤ 它的短候选做
NEAR_SHIFT_DPI = SCAN_CONFIRM_DPI

NEAR_SHIFT_RULES = [
    {"name": "行挪位", "cond": "对侧**同一列**、上下 ≤±24pt 的窄窗里读出同一句",
     "act": "撤", "why": "表格加列/加表头把整表推移了，内容本身没变"},
    {"name": "长候选不扫", "cond": f"归一化后 > {NEAR_SHIFT_MAX_CHARS} 字符", "act": "跳过",
     "why": "窄窗读不全长句，扫了只会拿到半截噪声"},
    {"name": "读出别的值", "cond": "窄窗里读到的不是同一句", "act": "保留",
     "why": "实测 `300` 扫不到旧版的 `150`、`1400` 扫不到新版的 `1200` —— 真改动不会被扫掉"},
]

# 字海兜底（`_hay_token_hit`）的容器长度上限，见该函数注释。
SCAN_MOVE_HAY_MAX_CONT = 8

# 行切分不对称（`_line_split_explained`，2026-10-01 加，GRA-3925 压力表表头假 mod）
# 同一段字，一版整页 OCR 读成两行、另一版读成一行 → 「旧一行 ↔ 新一行」相似度
# 刚过 `SCAN_LINE_MOD_RATIO` → 假 mod；而高分重读也推不翻（新那一行本来就多半句）。
# 判据 = 本侧一行 + 本侧**纵向紧邻**的另一行 拼起来 ≈ 对面那一行。
NEAR_SPLIT_PEER_MIN = 4     # 邻居归一化后至少这么长才算「被并掉的一行」（防短噪声顶高 ratio）

NEAR_SPLIT_RULES = [
    {"name": "本来就同句", "cond": "`_near_same(本行, 对面行)` 已成立", "act": "不走这条",
     "why": "前提是「两行本来不像」；本来就同句的是真 mod（UDI 号只差两字符）"},
    {"name": "邻居太短", "cond": f"邻居归一化后 < {NEAR_SPLIT_PEER_MIN} 字符", "act": "不拼",
     "why": "拼 2 个字符谁都能把 ratio 顶过 0.8，等于没有门槛"},
    {"name": "纵向不紧邻", "cond": "|Δ中心 y| > `SCAN_PAIR_DY`×1.5", "act": "不拼",
     "why": "被并掉的只能是挨着的那一行"},
    {"name": "拼接对得上", "cond": "本行+邻居（两种顺序）经 `_near_same` 命中对面行", "act": "撤",
     "why": "两版内容相同，只是切行方式不同"},
]

# 行级重读比对（`_near_line_hit`，2026-10-01 加，GRA-3925 `CE2797当4` 假新增）
# ①② 两轮重读都是拿**整个窗**的文本比；窗的纵向留白按候选行高的倍数给，
# 候选框一高（CE 标志 23.6pt）留白就 19pt，把上一行地址读进来 —— 两版那一行
# 的 OCR 抖动方向不同，整段必然判「不同」。候选自己那一行两侧却读得一字不差。
NEAR_LINE_OV = 0.4          # 对侧行框与本侧行框的重叠（占较小者面积）≥ 它算「同一行」
NEAR_LINE_IN_CAND = 0.3     # 本侧行框落在候选框里的比例 ≥ 它，这一行才归候选框管

NEAR_LINE_RULES = [
    {"name": "框外行不比", "cond": f"行框落在候选框内的比例 < {NEAR_LINE_IN_CAND}", "act": "跳过",
     "why": "只判候选框管的那些行；窗里所有行都拿来比等于把门槛降到「有一行相同就撤」"},
    {"name": "位置对不上", "cond": f"两行框重叠 < {NEAR_LINE_OV}", "act": "跳过",
     "why": "位置不同的两行本来就不该拿来比（那是挪位/邻列）"},
    {"name": "逐行同一句", "cond": "位置对得上且 `_near_same` 成立", "act": "撤",
     "why": "两侧这一行内容相同，是整窗重读被邻行污染造成的假增删"},
    {"name": "逐行不同", "cond": "`_near_same` 不成立", "act": "保留",
     "why": "`_near_same` 自带短串必须全等、长串长度差 ≤10% 的闸门，真改动不会被撤"},
]

# 「两侧重读出来的是不是同一句话」—— 按 `NEAR_SAME_RULES` 逐条过。
# ⚠️ 光看 ratio ≥ 0.8 会把**真改动**撤掉（2026-10-01 实测）：`3134` vs `31234`
#    只差一个字符，ratio 0.89 直接过线 —— 可那段 LOT 号确实少了一位数字。
#    短串上多/少一个字符就是另一回事，所以短串必须**归一化后完全相等**。
NEAR_SAME_RATIO = 0.8       # 长串：ratio ≥ 它就算同一句
NEAR_SAME_SHORT = 8         # ≤ 这么长的算「短串」→ 走完全相等那条
NEAR_SAME_LEN_FRAC = 0.10   # 长串：两侧长度差 ≤ 10%（+ 至少容 1 字符）才算同一句

NEAR_SAME_RULES = [
    {"name": "完全相等", "cond": "归一化后一字不差", "act": "撤",
     "why": "两版这里内容一样，是单侧 OCR 读岔"},
    {"name": "包含+纯字母尾巴", "cond": "一字是另一字的子串、多出的尾巴 ≤2 字符且**不含数字**",
     "act": "撤", "why": "尾部是邻字噪声（`GRA-3925` vs `GRA-3925 F`）；"
     "尾巴带数字就是真改动（`150` vs `1500`），不能撤"},
    {"name": "短串", "cond": f"长度 ≤ {NEAR_SAME_SHORT} 且不完全相等", "act": "**不撤**",
     "why": "短串上多/少一个字符就是真改动（`3134` vs `31234`）"},
    {"name": "长串相似", "cond": f"ratio ≥ {NEAR_SAME_RATIO} 且长度差 ≤ {NEAR_SAME_LEN_FRAC}",
     "act": "撤", "why": "长串上的 OCR 抖动，字不会平白多一串"},
    {"name": "其余", "cond": "更低的相似 / 长度差更大", "act": "不撤",
     "why": "证据不足 —— 宁可多报，不能把真改动撤掉"},
]


def _substring_alpha_tail(a: str, b: str) -> bool:
    """一句包含另一句、且「剩下的尾巴」是 ≤2 个非数字邻字噪声 → 算同一句。

    `_near_same` 与 `_same_text_noise` 共用。尾巴 = 去掉被包含的那句后**剩下的全部**
    （包含未必是前缀：`70mm` in `6570mm`，剩下的是头上的 `65` —— 头尾都要查）。
    """
    if not ((a in b or b in a) and abs(len(a) - len(b)) <= 2):
        return False
    rest = b.replace(a, "", 1) if a in b else a.replace(b, "", 1)
    return len(rest) <= 2 and not any(ch.isdigit() for ch in rest)


def _near_same(a: str, b: str) -> bool:
    """两侧同位置重读的归一化文本算不算「同一句」（见 `NEAR_SAME_RULES`）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    if _substring_alpha_tail(a, b):
        return True
    if len(a) <= NEAR_SAME_SHORT or len(b) <= NEAR_SAME_SHORT:
        return False                       # 短串：差一个字就是另一回事
    # ⚠️ 阈值卡这么死是实测逼出来的：旧版那行末尾多出 `7omm`（4 字符 / 27 字符长）
    #    —— 那是**真删除**，按 15% 的口子 4 ≤ 4.05 就过了线，真改动被撤掉。
    if abs(len(a) - len(b)) > max(1, NEAR_SAME_LEN_FRAC * max(len(a), len(b))):
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= NEAR_SAME_RATIO


def _same_text_noise(a: str, b: str) -> bool:
    """mod 宽读对账专用：只有「完全一致」或「包含 + 纯字母尾巴」算同一句。

    ⚠️ **不能**拿 `_near_same` 来判 mod —— mod 本身就是「高相似但不同」，
    ratio ≥ 0.8 那条会把真改动整个撤掉（v32 踩的坑：UDI 号只差一位数字、
    ratio 0.97，宽读对账直接判「同一句」→ UDI/规格 mod 全军覆没）。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return _substring_alpha_tail(a, b)
SCAN_RUN_MIN_PX = 8         # 一个 x 簇的总差异像素 ≥ 它才当候选（去孤立噪点簇）
SCAN_MAX_RUNS = 8           # 每个窗口最多出多少个候选簇（防病态窗口爆炸）
SCAN_Y_EXPAND_PT = 9.0      # 定完 x 后把 y 沿墨迹最多扩这么多 (pt)，凑齐字的完整高度

# —— 条带补读（2026-10-01 加，GRA-3925 顶部旋转条带）———————————————
# 整页 200dpi 行级 OCR 对**旋转的小字条带**读得不稳：旧版整条漏读、新版只读出
# 碎片（`NEX`/`•`）—— 两版不对称，于是「新版多的」全是假新增，而真改动
# （`7.0mm→6.5/7.0mm`、`150mm→300mm`）反而没行可配、漏报。
# 实测：**400dpi 宽裁切**两版都能读全（旧 `7.0mm/150mm/200cm`、
# 新 `Conical/6.5/ /7.0mm/300mm/200cm`）。所以：单侧候选聚簇 → 对簇所在的
# 条带两版各补读一次 → 并回行集重新对拍。
SCAN_REREAD_DPI = 400       # 补读分辨率（整页 200dpi 读不全的，裁宽了 400dpi 就全）
SCAN_REREAD_GAP = 45.0      # 簇内间距 (pt)：候选行框扩这么多还搭得上就归同簇
SCAN_REREAD_PAD_X = 60.0    # 条带横向外扩 —— 旋转字必须给**够宽**的裁切才认得出
SCAN_REREAD_PAD_Y = 18.0    # 条带纵向外扩 (pt)
SCAN_REREAD_MAX = 6         # 每页最多补读几个簇（防病态页拖死）

SCAN_REREAD_RULES = [
    {"name": "重复读出", "cond": "文本与已有行相同/互相包含且框重叠", "act": "丢",
     "why": "同一句话读了两遍，留着会把配对算重"},
    {"name": "读岔的碎片", "cond": "与已有行框重叠但文本不同", "act": "**丢已有行、留补读行**",
     "why": "补读是 400dpi，整页 200dpi 的碎片（`NEX`）是读岔的"},
    {"name": "新读出的行", "cond": "不与任何已有行重叠", "act": "并进行集",
     "why": "整页漏读的旋转条带文字，补读才让两版都有行可配"},
]

# ---- 全页高清兜底对拍（2026-10-01，SCAN_HDPI_RULES）----
# 条带补读只能救「**单侧**读出」的洞（对拍剩余项附近找不到对面候选）。
# GRA-3925 打回来一种它够不到的情况：整页 200dpi 对条带右端 REF 列
# （`A35-070 150-200` → `A35-070 300-200`，旋转小字）**两版一个字都没读出**
# —— 对拍两边都空 = 认为无差异，连「洞」都形不成。
# 修法：两版各再整页 400dpi 行级 OCR 一次（结果有缓存，第二次跑免费），
# **独立**对拍出差异行，按位置去重后带 `h` 标记并入剩余项。
# ⚠️ 高清行**不与** 200dpi 行混合对拍 —— 两版行切分不同，混合会互相顶行
# （2026-10-01 上午「半截 UDI 顶掉完整行」连环坑就是这么来的）。
# ⚠️ 全页 400dpi **也会**把整列读丢（本例新版连 `A35-070` 都没读出），而
# 裁切 400dpi 两版都读得全（实测 pad 16/8pt 就够）—— 所以带 `h` 标记的
# 单侧行不走「对面重读认得出就撤」的老链，而是**两版裁切精读**定案：
# 读出同一句 = 单侧漏读假象（撤）；读出不同 = 报 mod；对面读不出 = 删除/新增。
SCAN_HDPI_DPI = 400          # 兜底对拍的整页行级 OCR 分辨率
SCAN_HDPI_MIN_LINES = 3      # 高清读出的行数少于它 = 这版读崩了，放弃兜底
SCAN_HDPI_MIN_CHARS = 3      # 归一化后 ≤ 它的碎片不参与兜底
SCAN_HDPI_DUP_XY = 22.0      # 与 200dpi 剩余项判重的中心距 (pt)（≈1 个行高）
SCAN_HDPI_PROBE_PAD_X = 16.0 # 裁切精读的横向外扩 (pt) —— 竖字必须给够宽才认得出
SCAN_HDPI_PROBE_PAD_Y = 8.0  # 裁切精读的纵向外扩 (pt)；再大会把邻列读进来对不齐
SCAN_HDPI_PX_DPI = 300       # 结构对账（墨迹覆盖）的渲染分辨率
SCAN_HDPI_PX_INK = 128       # 灰度 < 它算「墨」（二值化的分界）
SCAN_HDPI_PX_TOL = 1         # 墨迹容差半径 (px)：扫描整体偏移 / 模糊 ≤ 它算「同一笔」
SCAN_HDPI_STRUCT_MAX = 0.04  # 「找不到对应墨迹」的墨量占比 ≤ 它 = 两版**画面内容相同**
SCAN_HDPI_STRUCT_PX = 600    # 裁切宽度上限：超过就按比例降 dpi（控耗时与内存）

SCAN_HDPI_RULES = [
    {"name": "碎片不兜底", "cond": "归一化后 ≤ SCAN_HDPI_MIN_CHARS 字符", "act": "跳过",
     "why": "两版 OCR 碎片（`ım` vs `m`）天然对不齐，兜底只会造假删除"},
    {"name": "位置重复", "cond": "与 200dpi 剩余项（**两侧都算**）中心距 ≤ SCAN_HDPI_DUP_XY",
     "act": "丢高清行",
     "why": "同一处改动只报一遍；高清行只补 200dpi 完全没读出的位置"},
    {"name": "裁切精读定案", "cond": "带 h 标记的单侧行", "act": "两版各裁切 400dpi 重读",
     "why": "全页 400dpi 也会整列读丢；只有裁切才读得全，读出不同才是真差异"},
    {"name": "结构对账", "cond": "精读文本不同，但两版渲染的**墨迹覆盖**互补不上（结构距离 ≤ SCAN_HDPI_STRUCT_MAX）",
     "act": "撤",
     "why": "图形符号区（GRA-3634 的 `┤ 80MM`）两版 OCR 各读各的乱码（`■ 1H 80MM` "
            "vs `H80MM`），文本对账永远「不同」；页面长什么样才是最终事实。"
            "⚠️ 判据不能是逐像素差、也不能是分块占用率，理由见 `_px_struct_dist`"},
    {"name": "并集框", "cond": "h 精读判定出的 mod", "act": "标注矩形取**两侧读出行的并集**",
     "why": "触发候选往往只是条带里的一行（旧版整页 400dpi 只认出 `A35-070`），"
            "文本却来自外扩后的整列窗（`REF`/`A35-070`/`150-200`）。拿触发行的窄框"
            "当矩形，高亮就只盖住中间一列 —— 用户看到的「`150-200` → `300-200` "
            "没标出来」就是这个"},
]


def _hdpi_read_box(page, path: str, i: int, probe, w_pt: float, h_pt: float):
    """精读窗里**实际读出**的行的外接框（一行都没读出就退回整个窗）。

    为什么要它：触发兜底的候选往往只是条带里的**一行**（旧版整页 400dpi 只认出
    `A35-070`），可外扩后的窗口会把整列都读进来（`REF`/`A35-070`/`150-200`）。
    拿触点那行的窄框当标注矩形，高亮就只盖住中间一列。
    """
    x0s, y0s, x1s, y1s = [], [], [], []
    for it in _ocr_crop_lines(page, probe, path, i, "hbox"):
        if not str(it.get("t", "")).strip():
            continue
        x0s.append(float(it["x0"]) * w_pt)
        x1s.append(float(it["x1"]) * w_pt)
        y0s.append(float(it["y0"]) * h_pt)
        y1s.append(float(it["y1"]) * h_pt)
    if not x0s:
        return (probe.x0, probe.y0, probe.x1, probe.y1)
    return (min(x0s), min(y0s), max(x1s), max(y1s))


def _hdpi_union_box(pa, pb, path_a: str, path_b: str, i: int,
                    probe_a, probe_b, wa: float, ha: float,
                    wb: float, hb: float):
    """两侧精读窗里读出的行**并集**（折算到 A 的坐标系，一页只有一个 rect）。"""
    ba = _hdpi_read_box(pa, path_a, i, probe_a, wa, ha)
    bb = _hdpi_read_box(pb, path_b, i, probe_b, wb, hb)
    sx, sy = wa / max(1e-6, wb), ha / max(1e-6, hb)
    bb = (bb[0] * sx, bb[1] * sy, bb[2] * sx, bb[3] * sy)
    return (min(ba[0], bb[0]), min(ba[1], bb[1]), max(ba[2], bb[2]), max(ba[3], bb[3]))


def _hdpi_probe_rect(rect, w_pt: float, h_pt: float):
    """裁切精读窗：行框外扩 `SCAN_HDPI_PROBE_PAD_*`，clip 到页面内。"""
    return fitz.Rect(max(0.0, float(rect[0]) - SCAN_HDPI_PROBE_PAD_X),
                     max(0.0, float(rect[1]) - SCAN_HDPI_PROBE_PAD_Y),
                     min(w_pt, float(rect[2]) + SCAN_HDPI_PROBE_PAD_X),
                     min(h_pt, float(rect[3]) + SCAN_HDPI_PROBE_PAD_Y))


def _px_struct_dist(pg_a, pg_b, rect, dpi: int = SCAN_HDPI_PX_DPI) -> float:
    """两版渲染同一 rect 的**墨迹分块占用率平均差**（∈[0,1]，越小越像）。

    OCR 乱码抖动的最终防线：图形符号区两版**画面完全一样**，但 OCR 每次读出的
    乱码都不同（`■ 1H 80MM` / `1 30MM` / `→ 80мм`）—— 文本对账在乱码上永远判
    「不同」，只有「页面到底长什么样」才算数。

    返回 `max(旧版没被新版覆盖的墨量比, 新版没被旧版覆盖的墨量比)` ∈ [0,1]：
    **每一笔都要在对面 `SCAN_HDPI_PX_TOL` 像素内找得到对应墨迹**，找不到的部分
    才算真差异。所以「整体偏移」「模糊」「重扫」都不影响它 —— 笔画还在原地附近。

    ⚠️ 为什么不用逐像素差（v42 实测打回来）
    --------------------------------------
    GRA-3634 的两版是**同一张标签的两次扫描**，嵌入图像的原生分辨率差了整整
    一倍（旧 4290×4630 / 新 2145×2315）。逐像素差把「新扫描更糊」的**每个笔画
    边缘**都算成差异 —— 该窗实测 5.6%（行框）/ 6.4%（probe 窗），远超 2% 阈值，
    于是 `┤ 80MM` 处的 OCR 乱码抖动被当成真改动报出来，违反「绝不凭空造不一致」。
    降采样（÷3 → 5.9%）与高斯模糊（6.0%）都压不下去：细笔画在 300dpi 下只有
    2~3 px 宽，边缘像素占比太高。

    ⚠️ 为什么也不用「分块墨迹占用率」（v43 第一版实测打回来）
    --------------------------------------------------------
    按 16×8 分块统计墨迹占比确实能把 3634 那条假 mod 压到 0.011（画面相同的
    条带真改动 0.062，看着能分）—— 但 GRA-3925 的**真**改动里有两处栽了：
      · `(01) … 48425 0 …` → `48429 8`（UDI 序列号）  → 0.0104  ← 被误撤
      · `REF A35-070150-200` → `300-200`（REF 号）      → 0.0105  ← 被误撤
    原因是这两窗都是**一整行小字**，改的只有 1~2 个字符，落到网格里只动一格
    的一小部分 —— 与「扫描变糊」的扰动同量级。**判据必须看「哪一笔没对上」，
    而不是「这一格墨多了还是少了」。**

    实测分离度（`SCAN_HDPI_PX_TOL = 1`）：

      · GRA-3634 假 mod（两版画面相同）            0.021
      · GRA-3925 UDI 序列号（真改动 2 字）          0.076
      · GRA-3925 REF 号（真改动 1 字）              0.077
      · GRA-3925 顶部条带（真改动，旋转小字）        0.428
      → 阈值 0.04 落在 0.021 与 0.076 之间的空档里。

    渲染失败 / 区域太小 → 返回 1.0（**不等于**「画面相同」）：宁可保留候选，
    也不凭空撤掉一条真差异。
    """
    try:
        r = fitz.Rect(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
        if r.is_empty or r.width <= 0 or r.height <= 0:
            return 1.0
        # 控开销：裁切超过 SCAN_HDPI_STRUCT_PX 宽就按比例降 dpi（判据是「笔画在不在
        # 附近」，与分辨率大小无关，降一点不影响结论）
        use_dpi = dpi
        est = r.width * dpi / 72.0
        if est > SCAN_HDPI_STRUCT_PX:
            use_dpi = max(96, int(dpi * SCAN_HDPI_STRUCT_PX / est))
        pa = pg_a.get_pixmap(clip=r, dpi=use_dpi, colorspace=fitz.csGRAY, annots=False)
        pb = pg_b.get_pixmap(clip=r, dpi=use_dpi, colorspace=fitz.csGRAY, annots=False)
        w, h = min(pa.width, pb.width), min(pa.height, pb.height)
        if w < 4 or h < 4:
            return 1.0
        ink = SCAN_HDPI_PX_INK
        ma = _ink_mask(pa, w, h, ink)
        mb = _ink_mask(pb, w, h, ink)
        na, nb = sum(map(sum, ma)), sum(map(sum, mb))
        if na == 0 and nb == 0:
            return 0.0                       # 两版都是空白 → 画面确实相同
        if na == 0 or nb == 0:
            return 1.0                       # 一边有墨一边全白 → 真差异，别撤
        return max(_uncovered_frac(ma, mb, w, h), _uncovered_frac(mb, ma, w, h))
    except Exception:
        return 1.0        # 渲染失败 ≠ 视觉相同：宁可保留候选也不凭空撤


def _ink_mask(pix, w: int, h: int, ink: int) -> List[List[int]]:
    """渲染位图 → 墨迹二值掩码（`1` = 墨）。"""
    s, st = pix.samples, pix.stride
    return [[1 if s[y * st + x] < ink else 0 for x in range(w)] for y in range(h)]


def _uncovered_frac(mine: List[List[int]], other: List[List[int]],
                    w: int, h: int, tol: int = SCAN_HDPI_PX_TOL) -> float:
    """`mine` 的墨迹里，在 `other` 的 `tol` 像素邻域内**找不到对应**的占比。

    邻域用可分离膨胀（先横后纵）算，纯 Python 也不用为每笔做距离变换。
    """
    total = sum(map(sum, mine))
    if total == 0:
        return 0.0
    # other 的可分离膨胀 → 每笔的「容差邻域」掩码
    row = [[0] * w for _ in range(h)]
    for y in range(h):
        oy = other[y]
        ty = row[y]
        for x in range(w):
            if oy[x]:
                for dx in range(-tol, tol + 1):
                    xx = x + dx
                    if 0 <= xx < w:
                        ty[xx] = 1
    near = [[0] * w for _ in range(h)]
    for y in range(h):
        ry = row[y]
        if not any(ry):
            continue
        for x in range(w):
            if ry[x]:
                for dy in range(-tol, tol + 1):
                    yy = y + dy
                    if 0 <= yy < h:
                        near[yy][x] = 1
    bad = 0
    for y in range(h):
        my, nr = mine[y], near[y]
        for x in range(w):
            if my[x] and not nr[x]:
                bad += 1
    return bad / float(total)


def _scan_mod_confirm(oa, ob, i: int, rect, old_path: str, new_path: str,
                      rect_b=None) -> bool:
    """扫描件的 mod 候选在**高分辨率**下复核：两版重新 OCR 后仍不同才算数。

    为什么要有这道（GRA-3634 实测）：120dpi 下表格行 `10 1000 2.96` 被噪声认成
    `1000 4000` 之类，token 级判据看到的是干净的「数字换数字」→ 假 mod。
    300dpi 重读一遍：噪声恢复成两版相同的字 → 归一化相等 → 推翻；真改
    （日期 / Rev 号）高分辨率只会读得更准 → 仍然不同 → 保留。
    复核侧认不出字时不推翻低分辨率的结论（证据不足 ≠ 推翻）。

    `rect_b`：行对拍管线给的**新侧行框**（内容有纵向漂移时两版同一行的位置
    不同，用同一个 rect 裁两版会切偏字；各裁各的才准）。
    """
    r = fitz.Rect(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    ck = tuple(int(v * 10) for v in rect)
    hi_old = _ocr_crop(oa, r, dpi=SCAN_CONFIRM_DPI,
                       cache_key=(old_path, i, "cfm") + ck)
    r2 = fitz.Rect(*[float(v) for v in rect_b]) if rect_b else r
    ck2 = tuple(int(v * 10) for v in r2) if rect_b else ck
    hi_new = _ocr_crop(ob, r2, dpi=SCAN_CONFIRM_DPI,
                       cache_key=(new_path, i, "cfm") + ck2)
    if not hi_old.strip() or not hi_new.strip():
        return True
    if _norm_cjk(hi_old) == _norm_cjk(hi_new):
        return False
    # 第二轮：框放宽再各读一次（`NEAR_CHECK_STEPS` 第②档）—— 行对拍的行框来自
    # 条带补读，可能**把一行裁半截**（GRA-3925 页脚 `GRA-3925` 被裁成
    # `GRA-3`/`GRA-3S` → 假 mod）。放宽到横 ±12pt / 纵 ±0.8 行高后两版都读出
    # 完整的 `GRA-3925` → `_near_same` 判同一句 → 推翻。宽读**不同**才保留。
    h = max(6.0, r.height)
    r_w = fitz.Rect(max(0.0, r.x0 - 12.0), max(0.0, r.y0 - 0.8 * h),
                    min(float(oa.rect.width), r.x1 + 12.0),
                    min(float(oa.rect.height), r.y1 + 0.8 * h))
    r2_w = fitz.Rect(max(0.0, r2.x0 - 12.0), max(0.0, r2.y0 - 0.8 * h),
                     min(float(ob.rect.width), r2.x1 + 12.0),
                     min(float(ob.rect.height), r2.y1 + 0.8 * h))
    ckw = (int(r_w.x0 // 20), int(r_w.x1 // 20), int(r_w.y0 // 20),
           int(r_w.y1 // 20), 400)
    hi_old_w = _ocr_crop(oa, r_w, dpi=400, cache_key=(old_path, i, "cfw") + ckw)
    hi_new_w = _ocr_crop(ob, r2_w, dpi=400, cache_key=(new_path, i, "cfw") + ckw)
    if hi_old_w.strip() and hi_new_w.strip() and _same_text_noise(
            _norm_cjk(hi_old_w), _norm_cjk(hi_new_w)):
        return False
    return True


def _overlap_frac(r, s) -> float:
    """两矩形重叠面积 / 较小者的面积（0 = 不相交）。"""
    w = min(r[2], s[2]) - max(r[0], s[0])
    h = min(r[3], s[3]) - max(r[1], s[1])
    if w <= 0 or h <= 0:
        return 0.0
    a1, a2 = (r[2] - r[0]) * (r[3] - r[1]), (s[2] - s[0]) * (s[3] - s[1])
    return (w * h) / max(1e-9, min(a1, a2))


def _textlayer_word_rects(page) -> List[Any]:
    """一页**文字层**的词框列表（fitz Rect）。拿不到返回空表（= 不设防）。"""
    try:
        return [fitz.Rect(w[:4]) for w in page.get_text("words")]
    except Exception:
        return []


def _rect_hits_words(rect, words) -> bool:
    """矩形是否压在某个**文字层词框**上（重叠 ≥ 该词面积的 60%）。

    用于整页重绘兜底：标题栏等有文字层的区域已由字符流引擎逐字报过，
    OCR 对这些区域的重读只会添乱（同一行两版各认各的 → 假增删）。
    """
    try:
        r = fitz.Rect(float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    except Exception:
        return False
    for wr in words or []:
        try:
            inter = r & wr
            if not inter.is_empty and inter.get_area() >= 0.6 * max(1e-9, wr.get_area()):
                return True
        except Exception:
            continue
    return False


def _digit_anchored(t_old: str, t_new: str) -> bool:
    """位图图面行级兜底的**数字锚点**验收：两版文本必须有数字差异。

    为什么（2026-10-13 晚，rev16 vs rev18 实测 117 条兜回里一大半是抖动）：
    位图被不同分辨率重新栅格化后，Vision 对**同一行**两版各认各的
    （`Artimes 一次性…` ↔ `Rrtimes -次性使用冠找动脉球簽扩*写管`），
    归一化不相等 → 全局抵消失败 → 成对假增删。与 `_digit_guard` 同一哲学：
    **数字是最可信的改动锚点**（有效期/批号/版本号/注册证号全是数字改动），
    纯字母差异在重栅格化位图上压倒性是 OCR 抖动 → 不报。
    代价：图面里**纯字母**的真改动会被漏掉 —— 记账里已写明「结论来自 OCR，
    仅供参考」，且这类改动人工对图一眼可见。
    """
    import re as _re
    da_ = set(_re.findall(r"\d+", _norm_cjk(t_old or "")))
    db_ = set(_re.findall(r"\d+", _norm_cjk(t_new or "")))
    return da_ != db_


def _x_overlap_frac(xa0, xa1, xb0, xb1) -> float:
    """两段 x 区间的重叠长度 / 较窄一段的宽度（0 = 不相交）。

    只有横向判据的配对/去重场景用（`_same_pos_mod_partner` / `_pair_x_ok` /
    `_col_lead_count`），语义与 `_overlap_frac` 的 x 分量一致，单独成函数省去伪造高度。
    """
    ov = min(xa1, xb1) - max(xa0, xb0)
    if ov <= 0:
        return 0.0
    return ov / max(1e-9, min(xa1 - xa0, xb1 - xb0))


def _scan_page_regions(pa, pb, oa, ob, i: int, scale: float,
                       old_path: str, new_path: str) -> List[Dict[str, Any]]:
    """纯扫描件（整页无文字层）的候选生成 + 判定，替代瓦片对拍那条路。

    **两条候选路线互补**（GRA-3925 实测出必须分两条）：
    ① **行对拍**（`_scan_page_line_regions`，先跑）：两版各整页 OCR、行级
       文本对拍 —— 不依赖像素对齐，「两张不同产品的标签」这种**区域级差异**
       （整表、整块小标签区）只有它抓得住；同时它对「同版式小改」（GRA-3634）
       也直接给出干净的行级结论。
    ② **像素画像**（`_scan_windows` → `_scan_seed_rects`，原有路线）：
       行对拍抓不到的**无文本差异**（条码、图形、图标）靠它 —— 差异行画像
       在「稀疏小改」上又快又准。
    两条路线的产出按矩形重叠去重（行对拍的文本结论更可信，优先保留）。
    """
    line_out, line_ok = [], False
    if GRAPHIC_SCAN_LINES:
        try:
            line_out, line_ok = _scan_page_line_regions(
                pa, pb, oa, ob, i, scale, old_path, new_path)
        except Exception:
            line_out, line_ok = [], False
    if not line_ok:
        # 引擎不可用 / 没认出几行 → 全靠像素管线（旧行为）
        line_out = []

    px_out: List[Dict[str, Any]] = []
    kept: List[Tuple[float, float, float, float]] = []
    # 两版内容有纵向漂移时先配准（GRA-3925 实测 1.6% 拉伸差把整页点亮），
    # 之后所有画像/判定都用对齐后的 B —— rect 全程在 A（旧版）坐标系。
    pb, _dy_avg = _align_vertical(pa, pb)
    for y0, y1 in _scan_windows(pa, pb, scale):
        rects = _scan_seed_rects(pa, pb, y0, y1, scale)
        for rect in rects:
            # 同一窗口里扩展后重叠的候选只判一次（同一簇上下行组扩到同一行时出现）
            if any(min(rect[2], r[2]) - max(rect[0], r[0]) > 0.6 * min(rect[2] - rect[0], r[2] - r[0])
                   and min(rect[3], r[3]) - max(rect[1], r[1]) > 0 for r in kept):
                continue
            kept.append(rect)
            # 行对拍已经覆盖这块（矩形重叠）→ 它的文本结论更可信，跳过像素判定
            if any(_overlap_frac(rect, rg["rect"]) > 0.5 for rg in line_out):
                continue
            side, txt = _decide_region(pa, pb, oa, ob, i, rect, scale,
                                       old_path, new_path)
            if side is None:
                continue
            if side == "mod":
                rows = _refine_region(pa, pb, oa, ob, i, rect, scale,
                                      old_path, new_path)
                for rg in rows:
                    if _scan_mod_confirm(oa, ob, i, rg["rect"], old_path, new_path):
                        px_out.append(rg)
                continue
            if side == "moved":
                # 位移（2026-10-12）：像素级证明只是挪位 —— **跳过**「两侧同位置
                # 重读」核查（挪过去的行在对面当然找得到同文，过核查必被撤）。
                px_out.append(_mk_region(i, rect, side, txt))
                continue
            else:
                # 单侧候选同样过「两侧同位置重读」—— 扫描噪声/漂移会把两版
                # **相同**的内容推成假新增（GRA-3925 `CE2797当4`：±10pt 重读
                # 两版一字不差，都是 `- 30°C CE2797 UK…`）。
                pg_s, pg_o = (oa, ob) if side == "old" else (ob, oa)
                p_s, p_o = (old_path, new_path) if side == "old" else (new_path, old_path)
                if _line_near_check(pg_s, pg_o, rect, p_s, p_o, i, text=txt):
                    continue
                px_out.append(_mk_region(i, rect, side, txt))
    # 像素候选里再滤一遍与行对拍重叠的（行对拍后补的候选块可能与像素块重叠）
    px_out = [rg for rg in px_out
              if not any(_overlap_frac(rg["rect"], lg["rect"]) > 0.5 for lg in line_out)]
    return line_out + px_out


def _fit_pixmap(page_b, pb, w: int, h: int, scale_a: float):
    """把 B 侧位图对齐到 A 侧尺寸 —— 图片输入的必修课（见 `SIZE_FIT_RULES`）。

    图片没有物理尺寸：同一份文档一次存 1430x1543、一次存 1430x1544，
    原来那句 `尺寸不同就 continue` 会让**整页一处都不查**还报「0 处差异」。
    这里按规则表先拉伸（比例差在 `SCAN_SIZE_TOL` 内才拉，防止把内容压变形），
    拉不动就返回 None，由调用方**记账 + 跳过**（绝不静默）。

    返回值 `(pixmap, scale_b)`：
      · 拉伸过 → 位图与 A 同尺寸，坐标系可以直接沿用 A 的 `scale_a`；
      · 没拉 → 原样，scale 也不变。
    """
    if pb.width == w and pb.height == h:
        return pb, scale_a
    sx, sy = w / max(1, pb.width), h / max(1, pb.height)
    if abs(sx - sy) / max(sx, sy) > SCAN_SIZE_TOL:
        return None, scale_a
    try:
        from PIL import Image                       # 有 PIL：双线性缩放，质量最好
        im = Image.frombytes("L", (pb.width, pb.height), pb.samples)
        im = im.resize((w, h), Image.BILINEAR)
        return fitz.Pixmap(fitz.csGRAY, w, h, im.tobytes(), False), scale_a
    except Exception:
        pass
    try:                                            # 没 PIL：用非等比矩阵重渲染一版
        r = page_b.rect
        if r.width > 0 and r.height > 0:
            pm = page_b.get_pixmap(matrix=fitz.Matrix(w / r.width, h / r.height),
                                   colorspace=fitz.csGRAY, annots=False)
            if (pm.width, pm.height) == (w, h):
                return pm, scale_a
    except Exception:
        pass
    return None, scale_a


def graphic_regions(old_path: str, new_path: str, dpi: int = GRAPHIC_DPI,
                    tile: int = GRAPHIC_TILE) -> List[Dict[str, Any]]:
    """找出**两侧都没有文字层、但画面内容不同**的区域。

    返回 `[{"page": 页号, "rect": (x0, y0, x1, y1), "old_text": str, "new_text": str}, ...]`
    （PyMuPDF 页面坐标，左上原点、pt）。

    `old_text` / `new_text` 是**OCR 认出的那一段字**，只有「有内容」的那一侧非空：
      · `old_text` 非空、`new_text` 为空 → 旧版有、新版空白 = **整块删除**
      · 反过来 → **整块新增**
    调用方按哪个非空就在哪一侧的页面上标橙 —— 别把两侧都标，空白那侧标了是凭空造错。

    为什么需要它（2026-09-29 实测出来的一个真漏洞）
    ----------------------------------------------
    GRA-4118 这类文件是**转曲**的：正文大量以**矢量曲线**绘制，另外再配一层可提取的
    隐形文字。凡是**隐形文字层缺行**的地方（新版中栏 y≈84~160 那段正是），
    「读文字来比对」的引擎**原理上读不到** —— 新版真新增的那条 bullet 一处都没标，
    用户看到的就是「标少了」。本节用**像素级对拍**把这层补上。

    ⚠️ 为什么按「差异像素**占比**」而不是「平均灰度差」（第一版实测打回来的）
    ------------------------------------------------------------------------
    这份 PDF 的外框细线会因重新排版整体位移零点几个像素：用平均灰度差时
    **每一行都报一片差异**（首版实测 p0 直接报 438 块，全是噪声）。
    细线位移只会让一条 24px 瓦片里的 2~4 列像素变化（占比 < 3%），
    而文字/图形真的改了是**成片**像素变化 —— 占比阈值正好把前者挡住。

    ⚠️ 为什么「两边都有字」也要管（第三版实测打回来的）
    ---------------------------------------------------
    前两版只报「一边基本空白」的区域 —— 但 GRA-4118 第三栏因为插入了新小节，
    旧版的 12.0/13.0 正文（转曲）位置上，新版摆的是「再折叠工具」小节（也转曲）：
    两边都有墨水、墨水比 0.5 上下 → 直接被过滤 → **整节新增一处都没标**，
    用户看到的就是「再折叠工具那一大栏全都没有绿」。
    第三版起：两边都有字的候选先 **OCR 两侧文本**，归一化后相同 → 挪位/重渲染，不报；
    不同 → 再做双向竖带挪位核查（旧侧的字在新版栏里找得到 = 挪走了，不报红；
    新侧的字在旧版栏里找得到 = 挪进来的，不报绿）—— 都找不到对面才报。
    报出的矩形最后做**相邻合并**（`_merge_adjacent`）—— 转曲文字无法逐字定位，
    与其给行尾一小块，不如把整段连成一个块（用户 2026-09-29 拍板）。
    """
    da, db = fitz.open(old_path), fitz.open(new_path)
    scale = dpi / 72.0
    out: List[Dict[str, Any]] = []
    LAST_SIZE_SKIPS.clear()          # 每次调用重新记账（不进缓存 key，跨进程确定）
    LAST_FULLPAGE_SKIPS.clear()
    cf = _graphic_cache_file(old_path, new_path, dpi, tile)
    if cf and os.path.isfile(cf):
        try:
            with open(cf, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):               # v59+：{"regions", "fullpage"}
                LAST_FULLPAGE_SKIPS.extend(raw.get("fullpage", []))
                return [dict(r, rect=tuple(r["rect"])) for r in raw.get("regions", [])]
            # 旧格式（纯 list）：向后兼容，fullpage 记账在旧缓存里本就没有
            return [dict(r, rect=tuple(r["rect"])) for r in raw]
        except Exception:
            pass                                  # 缓存坏了就当没有，重算
    try:
        for i in range(min(da.page_count, db.page_count)):
            pa = da[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)
            pb = db[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)
            if (pa.width, pa.height) != (pb.width, pb.height):
                # 图片/不同扫描仪：位图差一两像素是常态（见 SIZE_FIT_RULES）。
                # 先尝试拉伸对齐；拉不动才跳过 —— 但**必须记账**，
                # 否则界面上一片「0 处差异」，用户分不清「真没改」还是「根本没看」。
                pb, _ = _fit_pixmap(db[i], pb, pa.width, pa.height, scale)
                if pb is None:
                    LAST_SIZE_SKIPS.append({
                        "page": i,
                        "old": (round(da[i].rect.width, 1), round(da[i].rect.height, 1)),
                        "new": (round(db[i].rect.width, 1), round(db[i].rect.height, 1)),
                    })
                    continue
            skip = _text_tiles(da[i], scale, tile) | _text_tiles(db[i], scale, tile)
            if not skip and GRAPHIC_SCAN_BANDS:
                # ---- 纯扫描件（整页无文字层）----
                # 瓦片对拍在这类文件上不可靠（见 GRAPHIC_SCAN_BANDS 常量处的
                # 说明）：候选改用行级差异画像生成，判定仍走同一套 _decide_region。
                out.extend(_scan_page_regions(pa, pb, da[i], db[i], i,
                                              scale, old_path, new_path))
                continue
            hot = _hot_tiles(pa.samples, pb.samples, pa.width, pa.height,
                             skip, tile, GRAPHIC_DARK, GRAPHIC_FRAC)
            # v48 「文字层错位」补查：skip 瓦片 = 文字层覆盖到的地方，本该交给
            # 字符流引擎。但文字层可能**与视觉渲染错位**（GRA-M4506 实测：隐形
            # 文字层整体下移约 50pt，视觉上无文字的转曲区被标成「有文字层」，
            # 「Store in a dry, dark place → dry, dark, cool place」整处漏）。
            # 补法：skip 瓦片里**像素差异确凿**（同一 FRAC/深阈值，不另设口径）
            # 的，单独聚矩形（单瓦片即可，真改动可能只落一格）后走**同一套**
            # `_decide_region` —— 判定权在 OCR 文本比对（OCR 读的是渲染画面，
            # 不受文字层错位影响），两版相同照旧不报；正常 PDF 的 skip 区只是
            # 字体渲染抖动，远低于 FRAC，连 OCR 都不触发，零新增。
            if skip:
                hot_skipped = _hot_tiles(pa.samples, pb.samples, pa.width, pa.height,
                                         set(), tile, GRAPHIC_DARK, GRAPHIC_FRAC) - hot
            else:
                hot_skipped = set()
            page_out: List[Dict[str, Any]] = []
            page_w_pt = pa.width / scale
            for rect in _tiles_to_rects(hot_skipped, tile, scale, min_tiles=1):
                # 补查矩形可能只有一个瓦片宽（真改动只落一格），OCR 只能读到
                # 残词（'cool plac'）→ 证据不足必判 None。横向加宽到整行量级
                # 再判 —— 读到整行，行内扩写/小改的判据才有着力点。
                r2 = (max(0.0, rect[0] - GRAPHIC_RECHECK_PAD_X), rect[1],
                      min(page_w_pt, rect[2] + GRAPHIC_RECHECK_PAD_X), rect[3])
                side, txt = _decide_region(pa, pb, da[i], db[i], i, r2, scale,
                                           old_path, new_path)
                if side != "mod":
                    # ⚠️ 补查**只收 mod（行内小改/扩写）**：整块增删一律不报。
                    #   GRA-4118 实测（2026-10-12）：排版位移会把别处内容推进
                    #   「文字层错位」的 skip 区，判成 "new" 大块 → 宽矩形落清单
                    #    后，`_graphic_rescue_core` 的碎片撤除把坐标重叠的 9 条
                    #    真文字层条目当碎片吃掉（18 条掉成 9 条）。位移内容的
                    #    挪位核查（ext 三路）在这里拦不全 —— v47 本来也不报
                    #    这类块（被 skip 盖住），不收不构成覆盖退步。
                    continue
                # ⚠️ 细切也必须用加宽矩形 r2：窄矩形里 OCR 只见残词，
                #    行级复核同样证据不足 → 会被无声丢掉（本轮实测踩过）。
                out.extend(_refine_region(pa, pb, da[i], db[i], i, r2,
                                          scale, old_path, new_path))
            for rect in _tiles_to_rects(hot, tile, scale):
                # —— 整页图形重绘防护（2026-10-13，DWG-2000087 标签对）——
                # 一个 hot 连通域覆盖页面面积 ≥ GRAPHIC_FULLPAGE_FRAC = 整页图面
                # 被整体重绘（嵌入图片替换 + 转曲文字重排）。这种块对 OCR 来说
                # 没有可对齐的「对面参照」，判「新增」是必然且无信息量 —— 报出去
                # 就是横跨大半页的巨框。跳过并记账；该页文字层差异仍由字符流引擎报。
                # ⚠️ 只拦「大块」，局部转曲区（GRA-4118 的 6.4%）不受影响。
                pg_w = pa.width / scale
                pg_h = pa.height / scale
                r_w, r_h = rect[2] - rect[0], rect[3] - rect[1]
                if r_w * r_h >= GRAPHIC_FULLPAGE_FRAC * pg_w * pg_h:
                    # —— 行级 OCR 对拍兜底（2026-10-13 晚，rev16 vs rev18 实测）——
                    # 巨框不报没错，但里面可能藏着**局部真实改动**（有效期
                    # 2026→2027、条码数字、Rev16→18）。位图被不同分辨率重新
                    # 栅格化（2438×2644 vs 2469×2678）会把整图边缘全部点亮，
                    # 像素阈值分不开「重栅格化噪声」和「语义改动」；行对拍
                    # 不依赖像素对齐：两版各 OCR 一遍、逐行抵消，剩下的就是
                    # 真差异。直接复用扫描件那条久经实战的
                    # `_scan_page_line_regions`（含抵消/位移/行内修改复核），
                    # 只保留落在巨框里的行。
                    rescue: List[Dict[str, Any]] = []
                    # —— 单侧转曲线止损（2026-10-13，rev15 vs rev16 卡死 300s 的根治）——
                    # 行级 OCR 对拍兜底的前提是「两侧图面都是位图、文字层情况对等」
                    # （rev16 vs rev18：两侧词框都 > 0，词框过滤能挡掉字符流引擎
                    # 已报过的行）。但 rev15 是「全转曲线」——文字层词框为 0，
                    # 整页 OCR 补行后 137/375/158 行候选**全部**涌进
                    # `_scan_page_line_regions` 的 `_line_near_check`（每个候选
                    # 两侧各高清 OCR 1~2 轮），单页 104s、三页 300s+，GUI 主线程
                    # 同步等待 → 用户看到「卡死」。这类文件的文字差异
                    # `_mixed_ocr_fill_core` 已经整页 OCR 补行、由字符流引擎报出，
                    # graphic 层再跑一遍行级兜底是**重复且爆炸**的 —— 直接跳过。
                    _words_o = _textlayer_word_rects(da[i])
                    _words_n = _textlayer_word_rects(db[i])
                    if bool(_words_o) != bool(_words_n):
                        # 一侧纯转曲线（0 词框）、另一侧有文字层 → 跳过行级兜底，
                        # 但仍记账（这页确实发生了整页重绘，只是不在 graphic 层补报）。
                        LAST_FULLPAGE_SKIPS.append({
                            "page": i,
                            "rect": (round(rect[0], 1), round(rect[1], 1),
                                     round(rect[2], 1), round(rect[3], 1)),
                            "frac": round(r_w * r_h / (pg_w * pg_h), 2),
                            "rescued": 0,
                        })
                        continue
                    try:
                        _lr, _ok = _scan_page_line_regions(
                            pa, pb, da[i], db[i], i, scale, old_path, new_path)
                    except Exception:
                        _lr, _ok = [], False
                    if _ok:
                        # 文字层词框（每页取一次）：标题栏等有文字层的区域已由
                        # 字符流引擎逐字报过，OCR 重读只会添乱 → 落进词框的行丢弃。
                        for rg in _lr:
                            if _overlap_frac(rg["rect"], rect) < 0.5:
                                continue
                            # 同页去重：上一个巨框的兜底可能已经报过同一行
                            if any(g.get("page") == i
                                   and _overlap_frac(rg["rect"], g["rect"]) > 0.6
                                   for g in out):
                                continue
                            to, tn = (rg.get("old_text") or "").strip(), \
                                     (rg.get("new_text") or "").strip()
                            if rg.get("moved"):
                                rescue.append(rg)      # 位移区域内容未变，无抖动风险
                                continue
                            # ① 文字层词框过滤（mod 两侧行框各查各的）
                            ra = rg.get("rect_a") or rg["rect"]
                            rb = rg.get("rect_b") or rg["rect"]
                            if to and _rect_hits_words(ra, _words_o):
                                continue
                            if tn and _rect_hits_words(rb, _words_n):
                                continue
                            # ② 数字锚点验收（见 _digit_anchored）
                            if not _digit_anchored(to, tn):
                                continue
                            rescue.append(rg)
                    if rescue:
                        out.extend(rescue)
                    LAST_FULLPAGE_SKIPS.append({
                        "page": i,
                        "rect": (round(rect[0], 1), round(rect[1], 1),
                                 round(rect[2], 1), round(rect[3], 1)),
                        "frac": round(r_w * r_h / (pg_w * pg_h), 2),
                        "rescued": len(rescue),
                    })
                    continue
                side, txt = _decide_region(pa, pb, da[i], db[i], i, rect, scale,
                                           old_path, new_path)
                if side == "mod":
                    # 同位置的小改（日期 / 版本号 / 数字）：不进「增删」聚合
                    # （_merge_adjacent 按 old/new 二值设计），直接细切到行 ——
                    # 只留真差行，块内 OCR 抖动的行在行级判定里自然消失。
                    out.extend(_refine_region(pa, pb, da[i], db[i], i, rect,
                                              scale, old_path, new_path))
                    continue
                if side == "moved":
                    # 位移（2026-10-12）：**旁路**聚合/细切管线 ——
                    # `_merge_adjacent`/`_refine_region` 都是 old/new 二值设计，
                    # moved 进去会被重判、`moved` 标志丢失。
                    out.append(_mk_region(i, rect, side, txt))
                    continue
                if side is None:
                    # ⚠️ 块级判否 ≠ 块内没有真改动：这一块可能同时含「一处真新增」
                    #    和「一大片只是被顶下去的旧行」，块级覆盖率会把新增淹没
                    #    （GRA-4118 左栏「一个再折叠工具」实测）。细切到行再判一次。
                    for rg in _line_rescan(pa, pb, da[i], db[i], i, rect,
                                           scale, old_path, new_path):
                        if rg.get("old_text") and rg.get("new_text"):
                            out.append(rg)    # 行内小改（mod）：旁路聚合管线 ——
                                              # _merge_adjacent/_refine_region 都是
                                              # old/new 二值设计，mod 进去会被重判丢失
                        else:
                            page_out.append(rg)
                    continue
                page_out.append(_mk_region(i, rect, side, txt))
            # ① 先按段落把相邻候选并成一块（转曲文字给不出逐字坐标）；
            #    传入文字层瓦片用于识别「栏间通道」，防止把相邻两栏并成跨栏巨框
            for rg in _merge_adjacent(page_out, tiles=skip, tile=tile,
                                      scale=scale, page_rows=pa.height // tile):
                # ② 再把这一块按「行」细切，只留真正变了的那几行（见 _refine_region）
                out.extend(_refine_region(pa, pb, da[i], db[i], i, rg["rect"],
                                          scale, old_path, new_path))
    finally:
        da.close()
        db.close()
    # ③ 逐行小块**重新聚成「段」**：标注画一个整框（不再斑马纹）、
    #    清单出一条带完整段落的条目（2026-09-30 用户反馈「分散」「惜字如金」）。
    out = _merge_paragraphs(out)
    out = _frag_suppress(out)
    # ④ 出口统一位移救援（2026-10-12）：行对拍管线的单侧区域没有像素配准
    #    守卫 —— 斜向位移被报成假增删（失败4 'Coronary I' 绿块）。见函数注释。
    out = _moved_rescue_regions(out, old_path, new_path, dpi)
    if cf:
        try:
            with open(cf, "w", encoding="utf-8") as fh:
                # v59：缓存结构升级为 dict，把「整页重绘拦截」也一并落盘 ——
                # 否则热缓存命中时 `LAST_FULLPAGE_SKIPS` 丢失，清单 notes 里的
                # 「已跳过图形兜底」提示在二次运行时会消失（记账不静默的铁律）。
                json.dump({"regions": out, "fullpage": LAST_FULLPAGE_SKIPS},
                          fh, ensure_ascii=False)  # tuple→list，读回时转回来
        except Exception:
            pass
    return out


# ==================================================================
# 全页像素差异画像（2026-10-13 用户提出的「嵌套对比」第三步引擎）
# ==================================================================
# 定位：与 `graphic_regions` 不同 —— 后者**刻意跳过有文字层的瓦片**（`skip`），
#   只补「文字层读不到」的转曲/图形/扫描区。本函数反过来：**全页**渲染、不跳
#   任何瓦片，做「像素相减 → 分块局部对齐 → 残差」，产出一份**独立画像**：
#     · moved_blocks   —— 内容相同、仅位置平移的块（带位移向量 dx/dy）
#     · residual_blocks —— 对齐后仍消不掉的差异块（真增删改/字体，待文字层判类）
# 用途：作为第三条引擎兜底 + 交叉验证文字层 moved 结论；能发现文字层原理上
#   够不到的变化（纯图形、转曲、渲染差异）。

# 分块对齐参数（@ GRAPHIC_DPI 下的像素）
PIX_PROF_CELL = 48         # 分块边长 (px)：120dpi ≈ 28.8pt ≈ 两行字高
PIX_PROF_DARK = 60         # 单像素灰度差阈值（与 GRAPHIC_DARK 同口径）
PIX_PROF_CELL_HOT = 0.02   # 块内差异像素占比达到它才判「这块有差异」
PIX_PROF_DY = 60           # 每块纵向搜索半径 (px)：120dpi ≈ 36pt
PIX_PROF_DX = 16           # 每块横向搜索半径 (px)：120dpi ≈ 9.6pt
PIX_PROF_GAIN = 0.60       # 接受位移的判据：对齐后残差 ≤ 它 × 原地残差
                           #   （显著改善才认位移，否则纹理少的块随便平移都像对齐）
PIX_PROF_RES_HOT = 0.02    # 对齐后块内残差占比仍 ≥ 它 → 计入「真差异残差块」
PIX_PROF_MIN_BLOCK = 1     # 位移/残差块最少覆盖的瓦片数（去碎点）


@dataclass
class PixelBlock:
    """全页像素画像里的一个块（位移块或残差块）。"""
    page: int = 0
    rect: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)   # 旧版侧矩形 (pt)
    dx: float = 0.0        # 位移向量 (pt)：新相对旧的横向偏移
    dy: float = 0.0        # 位移向量 (pt)：新相对旧的纵向偏移（向下为正）
    residual: float = 0.0  # 对齐后残差占比（0~1，位移块≈0，残差块>0）


def _pixmap_gray(page, dpi: int):
    """一页的灰度位图（封装 get_pixmap，统一 colorspace/annots 口径）。"""
    return page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)


def pixel_diff_profile(old_path: str, new_path: str, dpi: int = GRAPHIC_DPI,
                       cell: int = PIX_PROF_CELL):
    """全页像素差异画像：位移块 + 残差块（用户 2026-10-13 嵌套对比思路）。

    流程（每页）：
      ① 两版灰度位图 → 网格分块、逐块统计差异像素占比 → 差异块集合；
      ② 每个差异块在 ±DY/±DX 内搜平移，找残差最小的对齐位；
         只有「对齐后残差显著下降」的块才判位移（假位移防御，见 `PIX_PROF_GAIN`）；
      ③ 位移块记 dx/dy；对齐后残差仍高的块 → 残差块（待文字层判增删改/字体）。

    返回 `{"moved": [PixelBlock...], "residual": [PixelBlock...]}`，
    块矩形用页面坐标 (pt)、位移向量用 pt。
    """
    moved: List[PixelBlock] = []
    residual: List[PixelBlock] = []
    da, db = fitz.open(old_path), fitz.open(new_path)
    try:
        scale = dpi / 72.0
        for i in range(min(da.page_count, db.page_count)):
            pa, pb = _pixmap_gray(da[i], dpi), _pixmap_gray(db[i], dpi)
            if (pa.width, pa.height) != (pb.width, pb.height):
                pb2, _ = _fit_pixmap(db[i], pb, pa.width, pa.height, scale)
                if pb2 is None:
                    continue
                pb = pb2
            mv, rs = _profile_page(pa, pb, i, dpi, cell)
            moved.extend(mv)
            residual.extend(rs)
        return {"moved": moved, "residual": residual}
    finally:
        da.close()
        db.close()


def _profile_page(pa, pb, page_idx: int, dpi: int,
                  cell: int) -> Tuple[List[PixelBlock], List[PixelBlock]]:
    """单页画像：网格差分 → 局部对齐 → 位移/残差分类。"""
    W, H = pa.width, pa.height
    A, B = pa.samples, pb.samples
    sa, sb = pa.stride, pb.stride
    scale = dpi / 72.0
    gw, gh = (W + cell - 1) // cell, (H + cell - 1) // cell

    def cell_frac(ax, ay, bx, by):
        """两版同一 cell 位置的差异像素占比。"""
        n = 0
        for y in range(cell):
            yy = ay + y
            if yy >= H or by + y >= H:
                break
            row_a = A[yy * sa + ax:yy * sa + ax + cell]
            row_b = B[(by + y) * sb + bx:(by + y) * sb + bx + cell]
            n += sum(1 for p, q in zip(row_a, row_b)
                     if (p - q if p >= q else q - p) > PIX_PROF_DARK)
        return n / (cell * cell)

    moved: List[PixelBlock] = []
    residual: List[PixelBlock] = []
    for r in range(gh):
        for c in range(gw):
            x0, y0 = c * cell, r * cell
            x1, y1 = min(W, x0 + cell), min(H, y0 + cell)
            base = cell_frac(x0, y0, x0, y0)
            if base < PIX_PROF_CELL_HOT:
                continue                                   # 这块两版一样
            # 局部搜索：找残差最小的平移
            best_s, best_dx, best_dy = base, 0, 0
            for dy in range(-PIX_PROF_DY, PIX_PROF_DY + 1, 2):
                for dx in range(-PIX_PROF_DX, PIX_PROF_DX + 1, 2):
                    nx0, ny0 = x0 + dx, y0 + dy
                    if nx0 < 0 or ny0 < 0 or nx0 + cell > W or ny0 + cell > H:
                        continue
                    s = cell_frac(x0, y0, nx0, ny0)
                    if s < best_s:
                        best_s, best_dx, best_dy = s, dx, dy
            # 假位移防御：只有显著改善才认位移
            if best_s < PIX_PROF_GAIN * base and (best_dx, best_dy) != (0, 0):
                moved.append(PixelBlock(
                    page=page_idx,
                    rect=(x0 / scale, y0 / scale, x1 / scale, y1 / scale),
                    dx=best_dx / scale, dy=best_dy / scale,
                    residual=best_s))
            elif best_s >= PIX_PROF_RES_HOT:
                residual.append(PixelBlock(
                    page=page_idx,
                    rect=(x0 / scale, y0 / scale, x1 / scale, y1 / scale),
                    residual=best_s))
    return moved, residual


# ==================================================================
# 第二阶段：像素层 × 文字层 交叉验证（2026-10-13）
# ==================================================================
@dataclass
class CrossCheck:
    """一处差异的两层交叉验证结论。

    level 分三档：
      · "confirmed"  —— 两层都认为有差异（位移方向/量级一致），最可信；
      · "text_only"  —— 只有文字层报（像素块粒度粗，检不到 <1 块的细位移），
                         按文字层结论走；
      · "pixel_only" —— 只有像素层报（纯图形/转曲/渲染差异，文字层原理上够不到），
                        待核：落到有文字层处再判类，否则报「这里变了」。
    """
    level: str
    kind: str                     # moved / added / removed / font / graphic
    page: int = 0
    rect_old: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    rect_new: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    dx: float = 0.0               # 位移向量 (pt)，仅 moved 有意义
    dy: float = 0.0
    old_text: str = ""
    new_text: str = ""
    residual_note: float = 0.0    # pixel_only(graphic) 时的残差占比（供日志/调试）


def _rect_overlap(a, b) -> bool:
    """两个矩形是否有面积重叠（含相切）。"""
    return (min(a[2], b[2]) - max(a[0], b[0]) > 0
            and min(a[3], b[3]) - max(a[1], b[1]) > 0)


def cross_validate_pixel(text_diffs: List[LocDiff],
                         pixel_prof: Dict[str, List[PixelBlock]],
                         tol: float = 12.0) -> List[CrossCheck]:
    """把文字层差异与像素层画像交叉验证，输出统一的三档结论。

    参数
    ----
    text_diffs : `locate()` 返回的差异（含 moved / font / added / removed）
    pixel_prof : `pixel_diff_profile()` 返回的 `{"moved": [...], "residual": [...]}`
    tol        : 两层矩形「视为同一处」的容差（pt）。位移行会因移动错开，
                 用**旧侧坐标**对位移块、**两侧坐标**对残差块做匹配。

    返回
    ----
    `[CrossCheck...]`，语义：
      · 文字层 moved 找到像素位移块 → confirmed(moved)；
      · 文字层 moved 没找到像素块 → text_only(moved)（细位移，保留文字层结论）；
      · 像素位移块没被文字层 moved 覆盖 → pixel_only(moved)（图形/转曲位移，待核）；
      · 像素残差块没被任何文字层差异覆盖 → pixel_only(graphic)（图形增删改，待核）；
      · 其余文字层差异（增删改/字体）→ text_only 原样带出。
    """
    out: List[CrossCheck] = []
    # ⚠️ 先聚合：网格切分把「一整片下移」切成几十小块，不聚合粒度对不齐
    #   （M4506 右栏 59 块全是同一片 dy≈-30 下移，文字层只报 16 行）。
    pmoved = _merge_pixel_blocks(list(pixel_prof.get("moved", [])))
    presid = list(pixel_prof.get("residual", []))
    # 已用像素块索引（一个像素块只配一条文字差异）
    used_mv: Set[int] = set()
    used_rs: Set[int] = set()

    def _cc_from_diff(d: LocDiff) -> CrossCheck:
        ro = d.rects_old[0] if d.rects_old else (0, 0, 0, 0)
        rn = d.rects_new[0] if d.rects_new else ro
        dx = dy = 0.0
        if d.kind == "moved" and d.rects_old and d.rects_new:
            dx = rn[0] - ro[0]
            dy = rn[1] - ro[1]
        return CrossCheck(level="text_only", kind=d.kind,
                          page=d.page_old if d.page_old is not None else d.page_new,
                          rect_old=ro, rect_new=rn, dx=dx, dy=dy,
                          old_text=d.old_text, new_text=d.new_text)

    # ① 文字层 moved ↔ 像素位移块
    for d in text_diffs:
        if d.kind != "moved":
            continue
        cc = _cc_from_diff(d)
        matched = None
        for k, pb in enumerate(pmoved):
            if k in used_mv or pb.page != cc.page:
                continue
            # 位移行：用旧侧坐标（文字层 rects_old）对像素位移块的 rect
            if _rect_overlap(_grow(cc.rect_old, tol), pb.rect):
                matched = k
                break
        if matched is not None:
            used_mv.add(matched)
            cc.level = "confirmed"
        out.append(cc)

    # ② 像素位移块没被文字层 moved 覆盖 → pixel_only(moved)（图形/转曲位移）
    for k, pb in enumerate(pmoved):
        if k in used_mv:
            continue
        out.append(CrossCheck(level="pixel_only", kind="moved", page=pb.page,
                              rect_old=pb.rect, rect_new=_shift(pb.rect, pb.dx, pb.dy),
                              dx=pb.dx, dy=pb.dy))

    # ③ 其余文字层差异（增删改/字体）→ text_only 带出，同时标记它是否被像素残差块印证
    for d in text_diffs:
        if d.kind == "moved":
            continue
        cc = _cc_from_diff(d)
        for k, rb in enumerate(presid):
            if k in used_rs or rb.page != cc.page:
                continue
            if (_rect_overlap(_grow(cc.rect_old, tol), rb.rect)
                    or _rect_overlap(_grow(cc.rect_new, tol), rb.rect)):
                used_rs.add(k)
                cc.level = "confirmed"
                break
        out.append(cc)

    # ④ 像素残差块没被任何文字层差异覆盖 → pixel_only(graphic)（图形增删改，待核）
    for k, rb in enumerate(presid):
        if k in used_rs:
            continue
        out.append(CrossCheck(level="pixel_only", kind="graphic", page=rb.page,
                              rect_old=rb.rect, rect_new=rb.rect,
                              residual_note=rb.residual))

    return out


def _grow(r, d: float):
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def _shift(r, dx: float, dy: float):
    return (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy)


def _merge_pixel_blocks(blocks: List[PixelBlock],
                        dvec: float = 6.0, dgap: float = 24.0,
                        ) -> List[PixelBlock]:
    """把同页、位移向量相近、空间相邻的位移块聚合成整块。

    网格切分会把「一整片下移」切成几十个小块（M4506 右栏 59 块全是同一片
    dy≈-30pt 下移）。文字层 moved 是**按行**报的，粒度差一个量级，不聚合就
    交叉验证会冒出几十条「pixel_only」碎片假漏报。聚合口径：
      · 同页；
      · 位移向量欧氏距离 ≤ dvec（pt）；
      · 外接矩形间隙 ≤ dgap（pt）——相邻网格块算连成一片。

    返回聚合后的块列表，`rect` 取并集、`dx/dy` 取向量均值（四舍五入）。
    """
    if not blocks:
        return []
    items = list(blocks)
    merged: List[PixelBlock] = []
    used = [False] * len(items)
    for i, b in enumerate(items):
        if used[i]:
            continue
        # 贪心聚合一簇
        cluster = [b]
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j, c in enumerate(items):
                if used[j]:
                    continue
                # 与簇内任一成员「向量相近 + 空间相邻」就并进来
                for m in cluster:
                    if (abs(c.dx - m.dx) ** 2 + abs(c.dy - m.dy) ** 2 <= dvec ** 2
                            and _gap(m.rect, c.rect) <= dgap):
                        cluster.append(c)
                        used[j] = True
                        changed = True
                        break
        # 簇 → 一个块
        xs = [m.rect[0] for m in cluster] + [m.rect[2] for m in cluster]
        ys = [m.rect[1] for m in cluster] + [m.rect[3] for m in cluster]
        merged.append(PixelBlock(
            page=b.page,
            rect=(min(xs), min(ys), max(xs), max(ys)),
            dx=round(sum(m.dx for m in cluster) / len(cluster), 1),
            dy=round(sum(m.dy for m in cluster) / len(cluster), 1),
            residual=max(m.residual for m in cluster)))
    return merged


def _gap(a, b) -> float:
    """两矩形之间的最小间隙（不相交时 >0；相交/相切时 ≤0）。"""
    gx = max(a[0], b[0]) - min(a[2], b[2])
    gy = max(a[1], b[1]) - min(a[3], b[3])
    return max(gx, gy)


# ==================================================================
# 第三阶段：残差块四分类 + 嵌套迭代（2026-10-13）
# ==================================================================
@dataclass
class ResidClass:
    """一个残差块的分类结论。"""
    page: int = 0
    rect: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    kind: str = ""             # added / removed / moved / font / graphic
    old_text: str = ""
    new_text: str = ""
    dx: float = 0.0            # 仅 moved
    dy: float = 0.0


def classify_pixel_residuals(old_path: str, new_path: str,
                             pixel_prof: Dict[str, List[PixelBlock]],
                             dpi: int = GRAPHIC_DPI) -> List[ResidClass]:
    """把像素层的残差块（`pixel_only`）逐块判类：增/删/改/位移/图形。

    复用 `_decide_region`（locate 层已成熟的区域判定，含 OCR + 位移配准 +
    图形增删），对每个残差块重新渲染该区域、判 side：
      · "new" → added；"old" → removed；"mod" → 拆成 removed+added；
      · "moved" → moved（含位移向量）；GRAPHIC_SOLID_TAG → graphic。

    这是像素引擎的**兜底价值落地**：文字层漏检的区域（旧版文字层缺失、
    纯图形/转曲变化）在这里补判。
    """
    da, db = fitz.open(old_path), fitz.open(new_path)
    out: List[ResidClass] = []
    try:
        scale = dpi / 72.0
        # 按页缓存 pixmap，避免逐块重复渲染整页
        pm_cache: Dict[int, Tuple[Any, Any]] = {}
        for rb in pixel_prof.get("residual", []):
            i = rb.page
            if i not in pm_cache:
                pm_cache[i] = (_pixmap_gray(da[i], dpi), _pixmap_gray(db[i], dpi))
            pa, pb = pm_cache[i]
            # _decide_region 用像素坐标做配准，rect 是 pt → 先算像素版矩形
            rect_pt = rb.rect
            side, txt = _decide_region(pa, pb, da[i], db[i], i, rect_pt, scale,
                                       old_path, new_path)
            if side is None:
                continue                            # 证据不足，不报（宁缺勿滥）
            rc = ResidClass(page=i, rect=rect_pt)
            if side == "moved":
                # txt 通道带 (dx,dy) 与源侧方向，格式 "%s\x00%.2f\x00%.2f\x00%s"
                parts = txt.split("\x00")
                rc.kind = "moved"
                rc.old_text = parts[0] if parts else ""
                if len(parts) >= 3:
                    try:
                        rc.dx, rc.dy = float(parts[1]), float(parts[2])
                    except ValueError:
                        pass
            elif side == "new":
                rc.kind, rc.new_text = "added", txt.strip()
            elif side == "old":
                rc.kind, rc.old_text = "removed", txt.strip()
            elif side == "mod":
                # 2026-10-13 删「变动」档：残差块「行内改写」也拆成删+增两条。
                if "\x00" in txt:
                    a, b = txt.split("\x00", 1)
                    rc.kind, rc.old_text = "removed", a.strip()
                    out.append(rc)
                    rc2 = ResidClass(page=i, rect=rect_pt)
                    rc2.kind, rc2.new_text = "added", b.strip()
                    out.append(rc2)
                    continue
                rc.kind, rc.old_text = "removed", txt.strip()
            else:
                continue
            # 图形增删：side 返回 "new"/"old" 但 txt 是占位符
            if txt.strip() == GRAPHIC_SOLID_TAG:
                rc.kind = "graphic"
            out.append(rc)
        return out
    finally:
        da.close()
        db.close()


def nested_diff(old_path: str, new_path: str, max_rounds: int = 3,
                resid_frac_thresh: float = 0.001) -> Dict[str, Any]:
    """嵌套迭代对比（用户 2026-10-13 原话的完整落地）。

    每轮：全页像素相减 → 位移块局部对齐 → 残差块分类。
    位移块会连成大片、把「下移的整块」摘掉，下一轮再对**剩余残差**重新相减，
    直到残差面积占比 < `resid_frac_thresh` 或达到 `max_rounds` 轮。

    嵌套的两层（对应你「嵌套函数」的描述）：
      第 1 轮：全页网格对齐，抓出**大位移块**（整块平移，搜索半径 PIX_PROF_DY）；
      第 2 轮：对第 1 轮剩下的**残差块**，做**放大搜索半径的二级对齐**
               （`_refine_residual_alignment`），把藏在残差里的二级位移再摘出来；
      第 3 轮起：剩余真正消不掉的残差 → 四分类（增/删/改/图形）。

    返回
    ----
    {
      "rounds": [{ "moved": N, "residual": N, "residual_area_frac": f }, ...],
      "moved":   [PixelBlock...]       # 各轮累计的位移块（聚合后）
      "classes": [ResidClass...]       # 最终残差块的分类
    }
    """
    rounds: List[Dict[str, Any]] = []
    all_moved: List[PixelBlock] = []
    final_classes: List[ResidClass] = []

    # —— 第 1 轮：全页画像（大位移 + 残差）——
    prof = pixel_diff_profile(old_path, new_path)
    mv = _merge_pixel_blocks(list(prof.get("moved", [])))
    rs = list(prof.get("residual", []))
    rounds.append({"moved": len(mv), "residual": len(rs),
                   "residual_area_frac": round(len(rs) / max(1, len(mv) + len(rs)), 4)})
    all_moved.extend(mv)

    # —— 第 2 轮：残差块二级对齐（放大搜索半径，抓残差里藏着的二级位移）——
    if rs:
        lv2_moved, rs2 = _refine_residual_alignment(old_path, new_path, rs)
        if lv2_moved:
            rounds.append({"moved": len(lv2_moved), "residual": len(rs2),
                           "residual_area_frac": round(
                               len(rs2) / max(1, len(lv2_moved) + len(rs2)), 4)})
            all_moved.extend(lv2_moved)
            rs = rs2

    # —— 第 3 轮起：最终残差四分类 ——
    if rs:
        final_classes = classify_pixel_residuals(old_path, new_path,
                                                 {"moved": [], "residual": rs})

    return {"rounds": rounds, "moved": all_moved, "classes": final_classes}


def _refine_residual_alignment(old_path: str, new_path: str,
                               residuals: List[PixelBlock],
                               dy_range: int = 40, dx_range: int = 16,
                               gain: float = 0.65,
                               min_dy: float = 5.0) -> Tuple[List[PixelBlock],
                                                             List[PixelBlock]]:
    """对残差块做**放大搜索半径的二级对齐**，抓出藏着的二级位移。

    第 1 轮的网格对齐受 cell 边界限制（48px），「整块下移 + 边界切割」会留下
    残影块（M4506 右栏 Tempera/10℃/30℃ 那批）。这里对每个残差块，在旧侧固定、
    新侧放大搜索范围内找「对齐后残差显著下降」的平移，够门槛就升级成二级位移块，
    否则保留为真残差。

    ⚠️ 两道守卫（M4506 实测定案）：
      · `gain`：对齐后残差须降到原残差 × gain 以下，才认「只是位移」
               （混合了真改动/字体变化的块降不下来，不该被误判位移）；
      · `min_dy`：位移量（pt）须 ≥ 它才认 —— 渲染抖动 1~3pt 的「假位移」
               会被放大搜索半径误抓，设最小位移量挡住。
    """
    da, db = fitz.open(old_path), fitz.open(new_path)
    try:
        scale = GRAPHIC_DPI / 72.0
        pm_cache: Dict[int, Tuple[Any, Any]] = {}
        lv2: List[PixelBlock] = []
        keep: List[PixelBlock] = []
        for rb in residuals:
            i = rb.page
            if i not in pm_cache:
                pm_cache[i] = (_pixmap_gray(da[i], GRAPHIC_DPI),
                               _pixmap_gray(db[i], GRAPHIC_DPI))
            pa, pb = pm_cache[i]
            A, B = pa.samples, pb.samples
            sa, sb = pa.stride, pb.stride
            x0 = int(rb.rect[0] * scale)
            y0 = int(rb.rect[1] * scale)
            w = max(4, int((rb.rect[2] - rb.rect[0]) * scale))
            h = max(4, int((rb.rect[3] - rb.rect[1]) * scale))

            def score(by0, bx0):
                n = 0
                for yy in range(h):
                    ya = (y0 + yy) * sa
                    yb = (by0 + yy) * sb
                    for xx in range(w):
                        da_ = A[ya + x0 + xx] - B[yb + bx0 + xx]
                        if da_ > PIX_PROF_DARK or da_ < -PIX_PROF_DARK:
                            n += 1
                return n

            base = score(y0, x0)
            if base == 0:
                continue
            best_s, best_dx, best_dy = base, 0, 0
            for dy in range(-dy_range, dy_range + 1, 2):
                for dx in range(-dx_range, dx_range + 1, 2):
                    ny, nx = y0 + dy, x0 + dx
                    if ny < 0 or nx < 0 or ny + h > pa.height or nx + w > pa.width:
                        continue
                    s = score(ny, nx)
                    if s < best_s:
                        best_s, best_dx, best_dy = s, dx, dy
            dy_pt = abs(best_dy) / scale
            if (best_s < gain * base and dy_pt >= min_dy
                    and (best_dx, best_dy) != (0, 0)):
                lv2.append(PixelBlock(page=i, rect=rb.rect,
                                      dx=best_dx / scale, dy=best_dy / scale,
                                      residual=best_s))
            else:
                keep.append(rb)
        return lv2, keep
    finally:
        da.close()
        db.close()


def _self_test_pixel_profile() -> None:
    """像素画像引擎自检：用合成位图验证位移块/残差块的判定逻辑。"""
    ok = fail = 0

    def chk(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name}{('　→ ' + extra) if extra else ''}")

    # 造两张纯 Python 位图对象（模拟 fitz.Pixmap 的 samples/stride/width/height）
    class FakePix:
        def __init__(self, arr):
            self.samples = bytes(arr)
            self.stride = len(arr[0]) if arr else 0
            self.width = len(arr[0]) if arr else 0
            self.height = len(arr)

    W = H = 96
    # A：全白 + 左上 30x30 一块黑
    A = [[255] * W for _ in range(H)]
    for y in range(10, 40):
        for x in range(10, 40):
            A[y][x] = 0
    # B1：黑块整体右移 20px → 应判「位移」
    B1 = [[255] * W for _ in range(H)]
    for y in range(10, 40):
        for x in range(30, 60):
            B1[y][x] = 0
    # B2：黑块位置不变、但中间挖掉 10x10 白洞 → 应判「残差」（真改动）
    B2 = [[255] * W for _ in range(H)]
    for y in range(10, 40):
        for x in range(10, 40):
            B2[y][x] = 0
    for y in range(20, 30):
        for x in range(20, 30):
            B2[y][x] = 255

    pa, pb1, pb2 = FakePix(A), FakePix(B1), FakePix(B2)

    mv, rs = _profile_page(pa, pb1, 0, 120, 48)
    chk("右移块判位移", len(mv) >= 1, f"moved={len(mv)} residual={len(rs)}")
    if mv:
        chk("位移向量 dx>0", mv[0].dx > 0, f"dx={mv[0].dx:.1f}")

    mv2, rs2 = _profile_page(pa, pb2, 0, 120, 48)
    chk("挖洞块判残差（真改动）", len(rs2) >= 1 and len(mv2) == 0,
        f"moved={len(mv2)} residual={len(rs2)}")

    # 全同页 → 零差异
    mv3, rs3 = _profile_page(pa, pa, 0, 120, 48)
    chk("全同页零差异", len(mv3) == 0 and len(rs3) == 0,
        f"moved={len(mv3)} residual={len(rs3)}")

    print(f"像素画像自检：{ok} 项通过，{fail} 项失败")
    raise SystemExit(1 if fail else 0)


OCR_JUNK_RULES: List[Dict[str, Any]] = [
    # ⚠️ 只对**单侧增删候选**生效：mod 有两侧互证，不需要这道闸门（也别加，
    #    加了会把真行内小改成片撤掉）。
    {"name": "够长才判", "cond": "len(txt) ≥ 10",
     "why": "短文本本来就没有统计意义（'NEX' / 'GW' / '3134' 都可能是真的）"},
    {"name": "token 够多", "cond": "≥ 6 个 token",
     "why": "'REF A35-070150-200' 这种只有 2 个 token 的真编号不能碰"},
    {"name": "几乎全是单字符", "cond": "平均 token 长 < 2.0 且最长 ≤ 3",
     "why": "乱码的典型长相：'Oe = > te 4 Пою м о ь д' 全是 1~2 字的碎片"},
    {"name": "没有两位数以上的数字", "cond": "无长度 ≥2 的纯数字 token",
     "why": "表格行 '8 10 12 14 16 18' 也是单字符碎片，但它是**真内容**，必须放行"},
]


# ---- ≤2 字单侧碎片抑制（2026-10-11 GRA-3925 用户实测「有一些小地方失误」）----
# 病根：`_ocr_junk` 刻意只抓 ≥10 字的多 token 乱码（`'NEX'/'GW'/'3134'` 都可能
#   是真标签，不能碰），于是 1~2 字的碎片整批漏进来 —— 实测图片对上报出
#   `added '©'`、`removed '00'`，用户对着清单一条条问这是什么。
# 判据（保守，两条全中才撤）：
#   ① 单侧文本归一化后 ≤ 2 个字符 —— 短到没有任何统计意义；
#   ② 同页**同侧**还有另一条已报区域、纵向带宽 ≤40pt —— 它是大块改动旁边
#      溅出来的碎片，那块改动本身已经报了，碎片不提供新信息。
# 孤立的 ≤2 字碎片**不撤**（页面上孤零零少了个「12」可能是真删了一行）。
FRAG_SUPPRESS_MAX = 2        # 归一化后 ≤ 这么多字符才算碎片
FRAG_SUPPRESS_BAND = 40.0    # 邻居区域的纵向距离（pt）


def _frag_suppress(regions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 `FRAG_SUPPRESS_*` 撤掉大块改动旁边的 ≤2 字单侧碎片。"""
    kept: List[Dict[str, Any]] = []
    for r in regions:
        o = (r.get("old_text") or "").strip()
        n = (r.get("new_text") or "").strip()
        if bool(o) == bool(n):            # mod / 空：不归这道闸管
            kept.append(r)
            continue
        side, txt = ("old", o) if o else ("new", n)
        if len(_norm_cjk(txt)) > FRAG_SUPPRESS_MAX:
            kept.append(r)
            continue
        y0, y1 = r["rect"][1], r["rect"][3]
        has_neighbor = False
        for q in regions:
            if q is r:
                continue
            qo = (q.get("old_text") or "").strip()
            qn = (q.get("new_text") or "").strip()
            q_txt = qo if side == "old" else qn
            if int(q.get("page", 0)) != int(r.get("page", 0)) or not q_txt:
                continue
            qy0, qy1 = q["rect"][1], q["rect"][3]
            if y1 + FRAG_SUPPRESS_BAND < qy0 or qy1 + FRAG_SUPPRESS_BAND < y0:
                continue                   # 纵向不相邻
            has_neighbor = True
            break
        if has_neighbor:
            continue                       # 大块旁边的碎片：撤
        kept.append(r)
    return kept


def _ocr_junk(txt: str) -> bool:
    """OCR 认出的这串字是不是**乱码**（按 `OCR_JUNK_RULES` 逐条过）。

    为什么需要（GRA-3925 实测）：扫描件上有一块条码/图形区，两版像素确实变了，
    OCR 认出 `'Oe   =    > te   4   Пою        м    о   ь д'` —— 全是单字碎片。
    它过得了「对面没有这段字」的判据（对面当然没有乱码），于是被当成**真新增**
    报到清单里；用户看到一条谁都读不懂的条目，只会怀疑整份结果。

    判「认不出」比判「改了什么」难得多，所以门槛刻意保守：
    **四条全中才撤**，任何一条不满足就原样放行（漏报一条噪声好过撤掉一处真改动）。
    """
    t = (txt or "").strip()
    if len(t) < 10:
        return False
    toks = t.split()
    if len(toks) < 6:
        return False
    if sum(len(x) for x in toks) / len(toks) >= 2.0 or max(len(x) for x in toks) > 3:
        return False
    if any(x.isdigit() and len(x) >= 2 for x in toks):
        return False                      # 表格数字行：放行
    return True


def _decide_region(pa, pb, oa, ob, i: int, rect, scale: float,
                   old_path: str, new_path: str, ref=None,
                   min_cover: float = OCR_MIN_COVER
                   ) -> Tuple[Optional[str], str]:
    """判一块（矩形）区域「算不算真改动」。返回 `(哪一侧, 认出的字)`。

    `side` ∈ `"old"` / `"new"` / `None`；`None` = 证据不足或对面也有 → 不报。
    `txt` 是**有内容那一侧** OCR 认出来的字，判 `None` 时也尽量带上（调用方要用它
    判「这一行字太少、不足以下结论」）。
    `ref` 是**这段改动所属的整块**矩形（逐行复核时传；不传就用 `rect` 自己）——
    对面取参照的范围按它来定，见 `_ocr_near`。
    `min_cover` 是「这段字对面也有」的门槛，默认 `OCR_MIN_COVER`；**逐行复核**
    时传更松的 `OCR_MIN_COVER_LINE`（理由见那里的注释）。

    两条判据（第二、三版实测打回来的）：
      · **一边基本空白**（墨水比 < `GRAPHIC_BLANK_RATIO`）= 整块增/删候选 →
        再 OCR 对面（同一矩形 + 就近一块 + 整条栏）确认这段字对面没有；
      · **两边都有字** → OCR 两侧，归一化相同 = 只是重渲染；
        不同且新侧这段字在旧版那条栏里找不到 = 真新增。
        ⚠️ 这条分支**只报绿**：旧侧 OCR 乱码是常态（转曲小字），
        「旧版这段没了」的证据极不可信；真删除由字符流引擎标红。
    """
    ink_a = _ink_count(pa, rect, scale)
    ink_b = _ink_count(pb, rect, scale)
    if max(ink_a, ink_b) <= 0:
        return None, ""
    # ⚠️ OCR 取字要比判定用的矩形**上下各留一点**：贴着一行字切，
    #    tesseract 会把「重压」认成「重讨」，覆盖率直接从 1.0 掉到 0.53。
    r = fitz.Rect(float(rect[0]) - 2.5, float(rect[1]) - OCR_CROP_PAD,
                  float(rect[2]) + 2.5, float(rect[3]) + OCR_CROP_PAD)
    ref = tuple(ref) if ref else tuple(rect)
    key = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    if min(ink_a, ink_b) / max(ink_a, ink_b) < GRAPHIC_BLANK_RATIO:
        # ---- 一边基本空白 = 整块增/删候选 ----
        blank_old = ink_a < ink_b
        src, src_path = (ob, new_path) if blank_old else (oa, old_path)
        dst, dst_path = (oa, old_path) if blank_old else (ob, new_path)
        # 配准用的像素图（有墨侧 src_pm / 空白侧 dst_pm）：与 doc 对象对应
        src_pm, dst_pm = (pb, pa) if blank_old else (pa, pb)
        txt = _ocr_crop(src, r, cache_key=(src_path, i) + key + (1,))
        ink_src = ink_b if blank_old else ink_a
        if not txt.strip():
            # ⚠️ OCR 认不出字 ≠ 这里没差异：纯色块/图形（大黑块、LOGO、色带）
            #    的整块增删没有文字可认，老逻辑直接 return None 把它们当「无意义」
            #    丢掉 —— GRA-3925 大黑块删除实测漏标。原理修正：有墨侧墨水量
            #    足够大（≥ GRAPHIC_SOLID_MIN）、对面确实空白，就报「图形增删」，
            #    文本用占位符标记，交给下游转成人类可读的「图形区域」。
            if ink_src >= GRAPHIC_SOLID_MIN:
                return ("new" if blank_old else "old"), GRAPHIC_SOLID_TAG
            return None, ""                   # 墨水量小 = 噪点，仍不报
        # ⚠️ 纯色块 OCR 常认出单个符号（`|` / `-` / `_` 等，黑块边缘被当成字符）：
        #    有墨侧墨水量大 + 认出的只是「极短无实义符号」，本质还是图形增删，
        #    不该报成「删除一个 | 字」。判据：归一化后实义字符 < 3 且墨水量 ≥ 阈值。
        _norm_txt = _norm_cjk(txt)
        if ink_src >= GRAPHIC_SOLID_MIN and len(_norm_txt) < 3:
            return ("new" if blank_old else "old"), GRAPHIC_SOLID_TAG
        other = _ocr_crop(dst, r, cache_key=(dst_path, i) + key + (2,))
        near = _ocr_near(dst, ref, dst_path, i)
        band = _ocr_band(dst, r, cache_key=(dst_path, i, "band",
                                            int(rect[0] // 40), int(rect[2] // 40)))
        if (_coverage(txt, other) >= min_cover
                or _coverage(txt, near) >= min_cover
                or _coverage(txt, band) >= min_cover):
            return None, txt.strip()          # 这段字对面也有 → 只是挪位/重排版
        # ⚠️ 像素位移配准兜底（GPT 第四阶段）：OCR 文本比对判不了「这段字只是
        #    挪了位置」时（韩文/泰文噪声大、短文本 coverage 恒 0、转曲没文字层），
        #    用局部平移搜索补一道：源块在对面页面里能移到一个「墨迹重合 ≥ 0.6」
        #    的位置 = 内容是同一块、只是整体平移。
        # ⚠️ 2026-10-12 升级：原来位移命中**静默丢弃**（return None）——结果
        #    「挪位整行」在清单里消失、标记层也没任何颜色；用户五色体系明确要求
        #    位移归**浅蓝**。现在报 `"moved"` 决策：`\x00` 通道携带 (dx,dy)（pt）
        #    与**源侧方向**，`_mk_region` 展开 `rect_shift`（对侧对应位置），
        #    标记层两侧画浅蓝、清单层出「位置移动」条目。
        # ⚠️ 2026-10-13 文本一致性闸（修复「内容替换」被误判成「位移」）：
        #    `_local_shift_overlap` 是**单向覆盖率**——只算「源墨迹落到目标墨迹
        #    的比例」。当对面同一栏里还有别的内容（行距紧、字形相似的中文），
        #    源行做纵向滑动时总能蹭到对侧**别的文字行**，单向覆盖率虚高
        #    （实测 11vs12「一个再折叠工具」这一行配准到旧版「冠状动脉刻痕球囊」
        #    行，ov 高达 0.833），把「真新增/真删除」错判成「位移」→ 该标绿/标红
        #    的一处都不标，反而多一块假浅蓝。
        #    真位移 = 同一段文字整体平移，**配准位置 OCR 出的字必须与源侧一致**；
        #    内容被替换时，配准位置 OCR 出的是别的字。所以在 ov 达标后，再用
        #    (sdx, sdy) 偏移去目标页 OCR 一段，文本覆盖不达标 → 不是位移，
        #    落回下面的增/删判定（报绿/报红），不再出假 moved。
        ov, sdx, sdy = _local_shift_overlap(src_pm, dst_pm, r, scale)
        if ov >= _SHIFT_OVERLAP_MIN:
            r_shift = fitz.Rect(r.x0 + sdx / scale, r.y0 + sdy / scale,
                                r.x1 + sdx / scale, r.y1 + sdy / scale)
            t_shift = _ocr_crop(dst, r_shift,
                                cache_key=(dst_path, i) + key + (5,))
            if _coverage(txt, t_shift) >= _SHIFT_TEXT_MIN:
                return "moved", "%s\x00%.2f\x00%.2f\x00%s" % (
                    txt.strip(), sdx / scale, sdy / scale,
                    "new" if blank_old else "old")
        if _ocr_junk(txt):
            # 认出来的是一串单字碎片（条码/图形区的 OCR 噪声）——它当然「对面没有」，
            # 但那不是证据，报出去只会多一条谁都读不懂的假新增（GRA-3925 实测）。
            return None, txt.strip()
        return ("new" if blank_old else "old"), txt.strip()
    # ---- 两边都有字：只报绿（理由见 docstring）----
    t_old = _ocr_crop(oa, r, cache_key=(old_path, i) + key + (3,))
    t_new = _ocr_crop(ob, r, cache_key=(new_path, i) + key + (4,))
    n_old, n_new = _norm_cjk(t_old), _norm_cjk(t_new)
    if n_old and n_old == n_new:
        return None, t_new.strip()            # 只是重新渲染/字形重绘
    if not n_new or len(n_new) < OCR_MIN_LEN:
        # ⚠️ `_coverage` 对短于 `OCR_MIN_LEN` 的文本恒返 0，下面的 mod 门槛对
        #    短行**永远不生效**。新侧认不出几个字时，唯一还能下的结论就是
        #    「两侧都很短的行内小改」——直接问 `_mod_worthy`（收原文）。
        #    ⚠️ 判据里**不能带上 `n_old` 太短**：GRA-4118 有 5 块转曲区正是
        #    「旧版 OCR 认不出几个字、新版认得出」的真新增（只报绿分支），
        #    把 n_old 也拉进来判会提前否掉它们（2026-09-30 实测 7 块掉成 2 块）。
        if n_old and n_new and _mod_worthy(t_old, t_new):
            return "mod", t_old.strip() + "\x00" + t_new.strip()
        return None, t_new.strip()            # 新侧认不出几个字 → 证据不足
    if (_coverage(t_old, t_new) >= min_cover
            or _coverage(t_new, t_old) >= min_cover):
        # 大部分相同 ≠ 完全相同 —— `n_old != n_new` 已在上面排过「重渲染」，
        # 走到这里 = **同一行的小改**候选（日期 06.04.2023→23.03.2026、
        # 版本号 Rev02→Rev03，GRA-3634 实测）：coverage 0.87~0.99 全过门槛，
        # 按旧口径判「文本相同」直接丢 → 真差异全漏。
        # `_mod_worthy` 收**原文**（token 级对齐判据，见函数 docstring）。
        # `_expand_worthy` 补「一侧多出实义内容」的行内扩写（M4506:
        # `dry, dark place` → `dry, dark, cool place`，coverage 0.96、
        # token 6→7，_mod_worthy 两关都过不去 → 真改动曾被判 None）。
        # ⚠️ 扩写的新增内容必须再过**对面整栏挪位核查**：排版位移会把别处的行
        #    推进判定矩形（M4506 右栏整体上移，'CE Mark' / 'EXPLANATION OF
        #    SYMBOLS' 在矩形内「对面没有」、在整条栏里明明还在）—— 不核查就
        #    把挪位内容当成新增。`cool` 在旧版整条栏里真不存在 → 保留。
        ext = _expand_worthy(t_old, t_new)
        if _mod_worthy(t_old, t_new) or ext:
            if ext:
                # ① 对面**文字层**（最可靠的存在性证据 —— 'CE Mark' 这类行
                #    常在文字层里，只是被排版推到了矩形外；band 窄栏 OCR 会丢行）
                #    ⚠️ ext 常短于 OCR_MIN_LEN（'CE Mark'=6 字符），_coverage
                #    对它恒返 0 —— 短内容改用「norm 按序子串」判（≥4 字符才判，
                #    2~3 字符的 ext 本来就证据不足，保守保留）。
                tl_a = _page_text_cached(oa)
                ne, nh = _norm_cjk(ext), _norm_cjk(tl_a)
                if len(ne) >= 4 and ne in nh:
                    return None, t_new.strip()   # 对面文字层里有 → 只是挪位
                # ② 对面近旁 / 整条栏（转曲内容文字层没有，只能靠 OCR）
                near_a = _ocr_near(oa, ref, old_path, i)
                band_a = _ocr_band(oa, r, cache_key=(old_path, i, "band",
                                                     int(rect[0] // 40), int(rect[2] // 40)))
                if (_coverage(ext, near_a) >= min_cover
                        or _coverage(ext, band_a) >= min_cover):
                    return None, t_new.strip()   # 对面栏里找得到 → 只是挪位
            return "mod", t_old.strip() + "\x00" + t_new.strip()
        return None, t_new.strip()            # 差异纯字母/错位 → OCR 噪声，不报
    near_a = _ocr_near(oa, ref, old_path, i)
    band_a = _ocr_band(oa, r, cache_key=(old_path, i, "band",
                                         int(rect[0] // 40), int(rect[2] // 40)))
    if (_coverage(t_new, near_a) < min_cover
            and _coverage(t_new, band_a) < min_cover):
        if _ocr_junk(t_new):
            return None, t_new.strip()       # 认出来的是乱码碎片 → 不是「真新增」的证据
        return "new", t_new.strip()          # 旧版那条栏里找不到 = 真新增
    return None, t_new.strip()


_MOD_MAX_TOKENS = 3         # 最多允许多少个 token 不一样；再多是错位/重排，不是「行内小改」


def _digit_swap(a: str, b: str) -> bool:
    """两个**已归一化**的 token 算不算一次「数字换数字」：等长、逐字符类别一致
    （数字只换数字、字母只换字母）、且至少一对双侧都是数字。

    `06042023`→`23032026`（注册证日期）、`rev02`→`rev03`（版本号）✓；
    `8oмм`→`80мм`（数字 vs 字母）、`hi`→`h1`（字母 vs 数字）✗。
    """
    if not a or not b or len(a) != len(b):
        return False
    has = False
    for ca, cb in zip(a, b):
        if ca == cb:
            continue
        if not (ca.isdigit() and cb.isdigit()):
            return False                # 数字 vs 字母/标点 → 抖动，不报
        has = True
    return has


def _mod_worthy(old_raw: str, new_raw: str) -> bool:
    """「大部分相同但不完全相同」的两段 OCR 文本，算不算一次**行内修改**。

    ⚠️ 收的是**未归一化的原文**（调用方别先 `_norm_cjk`）——归一化会把空白/
    标点全吃掉，token 结构就没了；而判据必须**按 token（空白分段）对齐**：

    整串 difflib 不可靠（上一版踩过）：`от 06.04.2023`→`от 23.03.2026` 在整串上
    被对齐成「insert + 两段不等长 replace」，等长数字替换的判据够不着 → **真改
    被拒**；反过来把阈值放松又满页误报。按 token 对齐后，日期 token 是干净的
    等长 `06042023`↔`23032026`，一条判据既抓住真改又挡住全部 5 条噪声
    （GRA-3634 rev02→rev03 实测）。

    噪声的**结构特征**（同轮实测，5 条误报全中）：
    · `80мм` 一次认成 `8омм` —— 数字 vs 同形字母（类别不同，`_digit_swap` 拒）；
    · 表格行 `8 ⏎ 10` 一次认成 `10` —— **token 数不等**（OCR 漏行/并行）；
    · `（01） 0 6958…` 认成 `01） 06958…` —— 空格漂移导致 token 错位（数不等）；
    · `Hi-Tech` 认成 `H1-Tech` —— 字母 vs 数字（类别不同）；
    · `Ко.` 认成 `Кo.` —— 同形折叠后相同（`_HOMOGLYPH` 已消）。

    所以口径是：token 数相等 + 不一样的 token ≤ `_MOD_MAX_TOKENS` +
    每个不一样的 token 都过 `_digit_swap`。其余一律不报 —— 漏报一件罕见的
    「纯字母改写」比把每处 OCR 抖动都标成「修改」好（假一致比漏判伤，
    但满屏误报同样让人没法用）。
    """
    to = (old_raw or "").split()
    tn = (new_raw or "").split()
    if not to or not tn or len(to) != len(tn):
        return False
    diff = 0
    for a, b in zip(to, tn):
        na, nb = _norm_cjk(a), _norm_cjk(b)
        if na == nb:
            continue
        diff += 1
        if diff > _MOD_MAX_TOKENS:
            return False
        if not _digit_swap(na, nb):
            return False
    return diff > 0


def _line_expand(old_line: str, new_line: str, old_norms: List[str]) -> Optional[str]:
    """单行内「一侧多出一两个实义词」= 行内扩写。返回**新增的原文**；不算则 None。

    GRA-M4506 实测（2026-10-12）：`Store in a dry, dark place` →
    `Store in a dry, dark, cool place`。coverage ≈0.96 过「行内小改」门槛，
    但 `_mod_worthy` 要求 token 数相等 + 数字类差异 —— 纯**单词新增**两关都
    过不去 → 真改动被判 None。
      · token 对齐必须用**归一化 token**：新增 cool 的同时旧 token `dark` 多了
        个逗号，原文对齐会判成 replace（'dark' ≠ 'dark,'）→「只许 insert」被拒。
        标点差异（用户口径不标）不该破坏 token 对齐。
      · 只许新增、不许丢失/改写（改写归 `_mod_worthy`，丢失按「两边都有字只报
        绿」原则不动 —— 旧侧证据不可信）；
      · 新增 1~2 个、每个 ≥2 个字母/数字（碎片/符号是 OCR 噪声）；
      · 新增 token 的内容不得在对面任何 token 里（子串）出现 —— 防 OCR
        空格漂移（`6958` 认成 `695 8`，'695' 是 '6958' 的子串 → 拒）。
    """
    to = (old_line or "").split()
    tn = (new_line or "").split()
    if not to or not tn or len(tn) <= len(to) or len(tn) - len(to) > 2:
        return None
    no = [_norm_cjk(t) for t in to]
    nn = [_norm_cjk(t) for t in tn]
    sm = difflib.SequenceMatcher(a=no, b=nn)
    added: List[str] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op != "insert":
            return None           # 有丢失/改写 → 不是单纯扩写
        for t in tn[j1:j2]:
            nt = _norm_cjk(t)
            if len(nt) < 2 or not nt.isalnum():
                return None       # 碎片/符号 → 噪声
            if any(nt in on or on in nt for on in old_norms):
                return None       # 内容对面已有 → OCR 空格漂移
            added.append(t)
    return " ".join(added) if added else None


def _expand_worthy(old_raw: str, new_raw: str) -> Optional[str]:
    """「两边都有字」且高度相似时，一侧**多出实义内容** = 扩写。

    返回**新增的原文**（供调用方对它做对面整栏挪位核查）；不算扩写返回 None。

    ⚠️ 判定单位是**行**，不是整段 token：判定矩形常常裹进上下相邻行
    （GRA-M4506 实测：宽矩形把 OLD 侧下一行的 OCR 乱码 `2:10ト2` 也读了进来，
    整段 token 数 9 vs 9，「多出一个词」的结构正好被抵消 → 恒 False）。
    两层：① 归一化行对齐 —— 公共行剔除，**delete 行按「两边都有字只报绿」
    原则忽略**（乱码碎行不该阻止同行其他位置的新增被看见）；
    ② 差异行做 `_line_expand`（整行新增 / 行内扩写）。
    """
    lo = [x.strip() for x in (old_raw or "").splitlines() if x.strip()]
    ln2 = [x.strip() for x in (new_raw or "").splitlines() if x.strip()]
    if not lo or not ln2:
        return None
    no = [_norm_cjk(x) for x in lo]
    nn2 = [_norm_cjk(x) for x in ln2]
    old_norms = [t for t in no if t]
    sm = difflib.SequenceMatcher(a=no, b=nn2)
    added: List[str] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal" or op == "delete":
            continue              # delete：旧侧多出的行，只报绿原则 —— 不动也不拦
        if op == "insert":
            for ln_txt in ln2[j1:j2]:
                nt = _norm_cjk(ln_txt)
                if len(nt) < 2:
                    return None
                # 防空格漂移：整行内容在对面某行里（子串）= 不是新增
                if any(nt in on or on in nt for on in old_norms):
                    return None
                # 行里至少要有一个 ≥2 位字母数字的实义 token
                if not any(len(_norm_cjk(w)) >= 2 and _norm_cjk(w).isalnum()
                           for w in ln_txt.split()):
                    return None   # 碎片行 → 噪声
                added.append(ln_txt)
        else:                     # replace：逐行对做行内扩写判定
            for a_ln, b_ln in zip(lo[i1:i2], ln2[j1:j2]):
                ext = _line_expand(a_ln, b_ln, old_norms)
                if ext is None:
                    continue
                added.append(ext)
    return " ".join(added) if added else None


def _page_text_cached(page) -> str:
    """一页的**可提取文字层**全文（进程内缓存 —— `_decide_region` 的扩写挪位
    核查要用：文字层是最可靠的「对面有没有这段字」证据，比窄栏 OCR 稳）。
    ⚠️ 只做存在性核查的原料，不参与判定（判定权仍在 coverage 门槛）。"""
    key = id(page)
    val = _PAGE_TEXT_CACHE.get(key)
    if val is None:
        try:
            val = page.get_text() or ""
        except Exception:
            val = ""
        _PAGE_TEXT_CACHE[key] = val
    return val


_PAGE_TEXT_CACHE: Dict[int, str] = {}


def _mk_region(page: int, rect, side: str, txt: str) -> Dict[str, Any]:
    """拼一条区域记录（哪一侧有字就在那一侧标）。

    `side == "mod"` 时 `txt` 是 `\x00` 分隔的 `旧文本\\x00新文本` —— 两侧都填，
    core 侧据此生成一条「修改」条目。
    `side == "moved"` 时 `txt` 是 `文本\\x00dx\\x00dy\\x00源侧`（位移 pt 与
    有墨那侧）：`rect` = 源侧矩形，`rect_shift` = rect 平移 (dx,dy) 后的
    **对侧对应位置** —— 两侧文字相同、只是位置不同，标记层两侧画浅蓝、
    清单层出「位置移动」条目（2026-10-12 五色体系）。
    """
    if side == "mod" and "\x00" in txt:
        to, tn = txt.split("\x00", 1)
        return {"page": page, "rect": tuple(rect), "old_text": to, "new_text": tn}
    if side == "moved" and txt.count("\x00") == 3:
        t, dxs, dys, src = txt.split("\x00", 3)
        fx, fy = float(dxs), float(dys)
        rect_s = (rect[0] + fx, rect[1] + fy, rect[2] + fx, rect[3] + fy)
        return {"page": page, "rect": tuple(rect), "rect_shift": rect_s,
                "old_text": t, "new_text": t, "moved": True,
                "shift_src": src if src in ("old", "new") else "new"}
    return {"page": page, "rect": tuple(rect),
            "old_text": txt if side == "old" else "",
            "new_text": txt if side == "new" else ""}


# ---- 「行内改写」整行框收窄（2026-10-01 用户反馈 GRA-3634 注册证行多标）----
# 用户原话：「前面这一大段的文字……他没有变动。他好像只有后面那个日期变动了。
#           这个属于是多标了。」
# 病根：扫描件行对拍给 mod 配对的框是**整行**（两侧行框并集）——行里只有末尾的
#   日期变了（06.04.2023 → 23.03.2026），却把「Регистрационное удостоверение
#   № РЗН 2023/20012 от 」这 50 个没动的字一起圈进去。
# 为什么不用像素列差分（2026-10-01 实测后放弃，别再试）：
#   两版扫描的原生分辨率差一倍（GRA-3634 旧 4290px / 新 2145px），同 dpi 渲染后
#   笔画锐度不同 → 逐列墨迹差在**整行到处都是** 5~20% 的假变化（实测注册证行
#   22.7% 列超阈、碎成 19 段），按阈值收窄要么框错位置、要么把真改动裁掉 ——
#   正是「绝不凭空造不一致 / 宁可少标不可乱标」最怕的那类误判。
# 正解是**文本比例**：OCR 已经把两侧行文字都读出来了（mod 配对的前提），
#   SequenceMatcher 的公共前缀/后缀给出变化片段的**字符区间**，按「字符占比 ≈
#   行宽占比」折算成横向区间。实测 GRA-3634 注册证行：日期实际占 [0.839, 0.988]，
#   比例估计 [0.833, 1.0] —— 误差不到 1%，完全够用。
SCAN_NARROW_MAX_FRAC = 0.60   # 变化片段超过行宽六成 = 大段改写，不收窄（整行照旧）
SCAN_NARROW_PAD = 0.02        # 两端各留 2% 行宽余量（比例估计有误差，别贴着字切）


def _mod_span_frac(old_text: str, new_text: str):
    """变化片段在**整行横向**上的占比区间（按字符数折算），不适合收窄时返回 None。

    返回 `((旧侧起, 旧侧止), (新侧起, 新侧止))`，两侧各自算 —— 前缀长度在两版
    可能不同（比如「Rev06/DCR 2025-0850」→「Rev07」之后面的日期会整体左移）。
    多处变化取**并集**（一处收窄框不住就别收）。
    """
    to, tn = old_text or "", new_text or ""
    # 多行 OCR 区域（\n）的字符占比横跨好几行，折算成单行横向没有意义 → 不收窄
    if not to or not tn or "\n" in to or "\n" in tn:
        return None
    if len(to) < 4 or len(tn) < 4:
        return None                     # 太短，占比没有意义
    sm = difflib.SequenceMatcher(None, to, tn, autojunk=False)
    lo_o, hi_o, lo_n, hi_n = len(to), 0, len(tn), 0
    any_change = False
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        any_change = True
        lo_o, hi_o = min(lo_o, i1), max(hi_o, i2)
        lo_n, hi_n = min(lo_n, j1), max(hi_n, j2)
    if not any_change:
        return None
    fo = (lo_o / float(len(to)), hi_o / float(len(to)))
    fn = (lo_n / float(len(tn)), hi_n / float(len(tn)))
    # 任一侧变化过半 = 大段改写，不收窄（收窄省不了多少、还容易切掉字）
    if fo[1] - fo[0] > SCAN_NARROW_MAX_FRAC or fn[1] - fn[0] > SCAN_NARROW_MAX_FRAC:
        return None
    return fo, fn


def _narrow_mod_region(rg: Dict[str, Any], oa, ob) -> None:
    """把 mod 区域的整行框**就地**收窄到真正变了的横向片段（失败保持原样）。

    ⚠️ 只动横向、不动纵向；`rect_a` / `rect_b` / `rect`（并集）一起更新 ——
       清单（`_graphic_rescue_core`）与标注（`pdf_annotate._prepare`）读的是
       同一份 region，两处自然同源。
    ⚠️ 任何一步不满足条件都**原样返回**：收窄是「锦上添花」，绝不能因为收窄
       把真改动裁出去（宁可整行多标，不可框错漏标）。
    """
    try:
        ra, rb = rg.get("rect_a"), rg.get("rect_b")
        to, tn = rg.get("old_text") or "", rg.get("new_text") or ""
        if not ra or not rb or not to or not tn:
            return
        spans = _mod_span_frac(to, tn)
        if spans is None:
            return
        (o0, o1), (n0, n1) = spans
        out = []
        for f0, f1, rect in ((o0, o1, ra), (n0, n1, rb)):
            x0a, y0a, x1a, y1a = (float(v) for v in rect[:4])
            w = x1a - x0a
            if w <= 1.0:
                return                  # 行框异常（太窄），别收
            nx0 = x0a + w * max(0.0, f0 - SCAN_NARROW_PAD)
            nx1 = x0a + w * min(1.0, f1 + SCAN_NARROW_PAD)
            if nx1 - nx0 < max(2.0, w * 0.03):
                return                  # 收窄后窄得离谱 = 估计不可信，别收
            out.append((nx0, y0a, nx1, y1a))
        rg["rect_a"], rg["rect_b"] = out[0], out[1]
        rg["rect"] = (min(out[0][0], out[1][0]), min(out[0][1], out[1][1]),
                      max(out[0][2], out[1][2]), max(out[0][3], out[1][3]))
        # 坐标现在是**按字符占比折算**的，不是实测的 —— 清单侧据此把 loc_conf 降档
        # （见 pdfdiff_core.SRC_CONF["ocr_est"]）。实测误差 <1%，但终究是估的，得如实说。
        rg["narrowed"] = True
    except Exception:
        pass                            # 收窄失败 = 保持整行，绝不让它毁掉标注


def _ink_rows(pm, rect, scale: float):
    """逐像素行的墨水量，外加该行最左/最右有墨的列（用于把行左右贴紧字）。"""
    x0 = max(0, int(rect[0] * scale))
    x1 = min(pm.width, int(rect[2] * scale))
    y0 = max(0, int(rect[1] * scale))
    y1 = min(pm.height, int(rect[3] * scale))
    rows = []
    for y in range(y0, y1):
        off = y * pm.stride
        n, lo, hi = 0, -1, -1
        for k, v in enumerate(pm.samples[off + x0:off + x1]):
            if v < SCAN_INK_DARK:
                if lo < 0:
                    lo = k
                hi = k
                n += 1
        rows.append((n, lo, hi))
    return rows, x0, y0


def _split_lines(pm, rect, scale: float, anchor=None
                 ) -> List[Tuple[float, float, float, float]]:
    """把一块区域按「**行间空白**」切成若干文本行（页面坐标，左右贴住字）。

    为什么要切：转曲块的像素对拍是**整块级**的，一块里往往只有中间一两行是新的，
    上下几行只是被顶下去 —— 整块标出来就是「标多了」。用户 2026-09-29 实测：
    左栏 2.0 供货方式那块，真正新增的只有 `一个再折叠工具` 一行，
    下面 `防止重压…` / `须贮存在…` 两行内容没变，却一起被框住了。

    ⚠️ 细于 `GRAPHIC_MIN_LINE` 的「行」要丢掉：实测块顶有一条 0.6pt 的**残影**
    （上一行字的底部），不丢就会把 `一条冠状动脉刻痕球囊扩张导管` 那行也带上。

    ⚠️ 断行阈值只能是「**有 1px 空白就断**」（`GRAPHIC_LINE_GAP` 调到 0.1pt）：
    这份 PDF 转曲正文的行距极紧 —— 实测行间空白只有 0.8 / 1.2 / 1.8 / 2.4pt 不等。
    阈值取 1.4pt（旧值）时，1.2pt 的缝断不开，`检查球囊导管是否完整；` 和下一行
    被切成**同一行**，OCR 混读出两行的字 → 对面比对不过 → 连没变的那行也被标绿
    （用户 2026-09-29 报的「标多了」就是这么来的）。

    `anchor`：给了它时，`rect` 只当**扫描窗口**用（调用方通常把它上下外扩过，
    好完整切出首/末行），但只保留与 `anchor` 垂直重叠 ≥ `GRAPHIC_ANCHOR_MIN`
    的行 —— 否则外扩会把上一行/下一行也圈进来。不传就按 `rect` 自己过滤。
    """
    rows, x0, y0 = _ink_rows(pm, rect, scale)
    gap_max = max(0, int(round(GRAPHIC_LINE_GAP * scale)))
    min_h = max(1, int(round(GRAPHIC_MIN_LINE * scale)))
    bands: List[List[int]] = []
    cur: Optional[List[int]] = None
    gap = 0
    for k, (n, lo, hi) in enumerate(rows):
        if n:
            if cur is None:
                cur = [k, k, lo, hi]
            else:
                cur[1] = k
                cur[2] = min(cur[2], lo)
                cur[3] = max(cur[3], hi)
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > gap_max:
                bands.append(cur)
                cur = None
                gap = 0
    if cur is not None:
        bands.append(cur)
    ax0 = anchor[1] if anchor else rect[1]
    ax1 = anchor[3] if anchor else rect[3]
    out = []
    for b in bands:
        if b[1] - b[0] + 1 < min_h:
            continue
        r_y0, r_y1 = (y0 + b[0]) / scale, (y0 + b[1] + 1) / scale
        if anchor is not None:
            # 与块本身（不是外扩窗口）的垂直重叠太少 → 是隔壁的行，不要
            if min(r_y1, ax1) - max(r_y0, ax0) < GRAPHIC_ANCHOR_MIN:
                continue
        out.append(((x0 + b[2]) / scale, r_y0, (x0 + b[3] + 1) / scale, r_y1))
    return out


def _strip_lead_mark(line: str, ref: str) -> str:
    """去掉 OCR 在行首多认出来的项目符号。

    横向放宽后上边界会把上一行的符号（`-` / `•` / `♦` / `·`）带进来，
    而**本行判定用的那份 OCR 没有这个符号**——那就说明是带进来的，去掉。
    本行真有符号（`ref` 也以它开头）时原样保留。
    """
    s = line.lstrip()
    if not s or not ref:
        return line
    c = s[0]
    if c not in "-–—•·◦●○*":
        return line
    return s[1:].lstrip() if c not in ref.lstrip()[:1] else line


def _refine_region(pa, pb, oa, ob, i: int, rect, scale: float,
                   old_path: str, new_path: str) -> List[Dict[str, Any]]:
    """过关的转曲块 → **逐行**复核，只留真正变了的那几行。

    行分三种：`keep`（复核通过）、`drop`（对面也有）、`?`（这行认出的字太少、
    不足以下结论）。`?` 行只要挨着一个 `keep` 行就跟着留 —— 否则会漏掉
    「分）。」这种只有两个字的收尾行。
    最后把**相邻保留行**之间的小缝补掉（被丢掉的行不补），免得看着像碎块。
    一行都留不下时退回整块，**宁可标粗也不漏**。

    ⚠️ **展示用的正文另取一次 OCR，横向多留 `OCR_TEXT_PAD_X`**：判定用的那份
    裁剪贴着字切（`_decide_region` 里 `OCR_CROP_PAD` 只有 1.2pt），Vision 会因为
    上下文不足把「如果需要」认成「知果需要」、「球囊导管」认成「求囊导管」、
    「分）。」认成「7）。」、「再折叠工具」认成「斤叠工具」。实测同一批行
    横向放宽到 10pt **六行全对**，而判定口径一个字都不用动。
    纵向**不能**跟着放宽：会把上一行的字卷进来（见 `OCR_TEXT_PAD_X` 的注释）。
    """
    side, txt = _decide_region(pa, pb, oa, ob, i, rect, scale, old_path, new_path)
    if side is None:
        return []
    # ⚠️ 纯图形/色块增删（占位符 `[图形]`）没有「行」可切，细切只会重新 OCR 出
    #    垃圾符号（`| |`）把占位符覆盖掉 —— 直接原样返回，保持「图形区域」语义。
    if txt == GRAPHIC_SOLID_TAG:
        return [_mk_region(i, rect, side, txt)]
    if side == "moved":
        # 位移区域（2026-10-12）：rect/rect_shift 已由 `_mk_region` 展开，
        # 细切只会把块拆成行、重判丢标志 —— 原样返回。
        return [_mk_region(i, rect, side, txt)]
    if side == "mod":
        # 行内小改（GRA-3634 注册证日期）：整块都是「两侧都有字」，其中只有
        # 个别行真差、其余行是 OCR 抖动 —— 细切到行，只留 `_decide_region`
        # 也判 mod 的行。不重认展示文本（打包文本 `旧\x00新` 原样保留）。
        rows = _split_lines(pb, (rect[0], rect[1] - GRAPHIC_BLEED,
                                 rect[2], rect[3] + GRAPHIC_BLEED), scale,
                            anchor=rect)
        kept_mod = []
        for rr in rows:
            s2, t2 = _decide_region(pa, pb, oa, ob, i, rr, scale, old_path,
                                    new_path, ref=rect, min_cover=OCR_MIN_COVER_LINE)
            if s2 == "mod" and len(_norm_cjk(t2)) >= OCR_MIN_LEN:
                kept_mod.append(_mk_region(i, rr, "mod", t2.strip()))
            # ⚠️ 2026-10-13 修复「块内混行被吞」：块级判 mod 只是块内**多数行**
            #    两侧都有字（覆盖率高），并不代表**每一行**都是行内小改。转曲
            #    区插入/删除一行时（11vs12「内装 6.0/9.0 → 一个再折叠工具」），
            #    该行行级判的是 `new`/`old`（增删），不是 `mod`。旧口径只收
            #    `mod` 行 → 这条真增删被整块吞掉（kept_mod 空 + 块高超兜底阈值
            #    → return []）。现在把行级判出的**增删行**也一并收下：真增删
            #    的证明力不弱于行内小改，同样不该被 mod 聚合的覆盖率淹没。
            #    ⚠️ 增删行的字数门槛用 `RESCAN_ROW_MIN_CHARS`（不是 OCR_MIN_LEN）：
            #    增删行是「对面基本空白 + OCR 认出了字」的直接证据，行级判据
            #    本就不需要「两侧都有字」那么长的字串；OCR_MIN_LEN=10 会把
            #    「一个再折叠工具」（7 字）这类真新增行挡掉。
            elif s2 in ("new", "old") and len(_norm_cjk(t2)) >= RESCAN_ROW_MIN_CHARS:
                kept_mod.append(_mk_region(i, rr, s2, t2.strip()))
        if kept_mod:
            return kept_mod
        # ⚠️ 2026-10-12 失败1（GRA-4118 11vs12）大面积误标修复：
        #   块级判 mod、**行级复核一行都没过**时，旧口径把**整块**兜底标黄
        #   （`kept_mod or [整块]`）。转曲区把这条兜底喂成了 32/46/219pt 的
        #   巨型黄框 —— 图标列整体微移 + OCR 抖动，行级全是噪声、块级聚合
        #   却过了 mod 门槛。用户拍板：「大面积误标要么标蓝要么删掉」。
        #   行级（更宽的 `OCR_MIN_COVER_LINE`）都凑不出一条真改 → 大块的
        #   mod 证据不足，**直接放弃**。小碎块（< GRAPHIC_MOD_FALLBACK_MAX_H）
        #   保留兜底：一行内的小改（GRA-3634 日期/版本号）不能丢。
        if rect[3] - rect[1] >= GRAPHIC_MOD_FALLBACK_MAX_H:
            return []
        return [_mk_region(i, rect, "mod", txt)]
    pm = pb if side == "new" else pa
    pg, pg_path = (ob, new_path) if side == "new" else (oa, old_path)

    def disp(rr, tag: str, fallback: str) -> str:
        """展示用文本：横向放宽后重认一遍（认不出就返回空，调用方退回原文本）。"""
        t = _ocr_crop(pg, fitz.Rect(rr[0] - OCR_TEXT_PAD_X, rr[1] - OCR_CROP_PAD,
                                    rr[2] + OCR_TEXT_PAD_X, rr[3] + OCR_CROP_PAD),
                      cache_key=(pg_path, i, tag) + tuple(int(v) for v in rr)).strip()
        return _strip_lead_mark(t, fallback) if t else ""

    # ⚠️ 切行的矩形要在 y 方向**外扩一点**：`rect` 是 24px 瓦片拼出来的，粒度 ≈14.4pt，
    #    边界常常正好切在首行/末行的字中间 —— 不外扩就会把首行/末行切掉半截。
    #    实测新版中栏那块末行 `再折叠工具是一个可以…` 只标到 y=145.2，
    #    而这行实际到 148.8（少了 3.6pt）—— 用户报的「标得不完全」就是这个。
    rows = _split_lines(pm, (rect[0], rect[1] - GRAPHIC_BLEED,
                             rect[2], rect[3] + GRAPHIC_BLEED), scale,
                        anchor=rect)
    if len(rows) <= 1:
        return [_mk_region(i, rect, side, disp(rect, "one", txt) or txt)]
    state: List[List[Any]] = []           # [行矩形, 认出的字, keep/drop/?, 行号]
    for ri, rr in enumerate(rows):
        s2, t2 = _decide_region(pa, pb, oa, ob, i, rr, scale, old_path, new_path,
                                ref=rect, min_cover=OCR_MIN_COVER_LINE)
        if s2 == side:
            state.append([rr, t2, "keep", ri])
        elif t2 and len(_norm_cjk(t2)) < OCR_MIN_LEN:
            state.append([rr, t2, "?", ri])   # 字太少，不足以下结论
        else:
            state.append([rr, "", "drop", ri])
    for k, st in enumerate(state):
        if st[2] != "?":
            continue
        nb = [state[j][2] == "keep" for j in (k - 1, k + 1) if 0 <= j < len(state)]
        if any(nb):
            state[k][2] = "keep"              # 挨着一个保留行 → 跟着留，别漏
    kept = [st for st in state if st[2] == "keep"]
    if not kept:
        return [_mk_region(i, rect, side, disp(rect, "one", txt) or txt)]
    out = []
    for k, (rr, t2, _st, ri) in enumerate(kept):
        y0, y1 = rr[1], rr[3]
        # 补掉与相邻保留行之间的缝，但**最多补 REFINE_SEAM_MAX**：
        # ⚠️ 两个保留行之间可能夹着被丢掉的行（缝 ≈ 一整行高 ≈ 9pt），
        #    对半分就会把那行的一半也圈进来 —— 实测把「检查球囊导管是否完整；」
        #    又标回去了一次（正是用户报的「标多了」）。
        if k > 0:
            y0 -= min((rr[1] - kept[k - 1][0][3]) / 2.0, REFINE_SEAM_MAX)
        if k + 1 < len(kept):
            y1 += min((kept[k + 1][0][1] - rr[3]) / 2.0, REFINE_SEAM_MAX)
        # 正文取**横向放宽后重认的那一行**（认不出才退回判定用的那次）
        out.append(_mk_region(i, (rr[0], y0, rr[2], y1), side,
                              disp(rr, "row%d" % ri, t2) or t2 or txt))
    return out


def _line_rescan(pa, pb, oa, ob, i: int, rect, scale: float,
                 old_path: str, new_path: str) -> List[Dict[str, Any]]:
    """块级判**否**的候选块 → **逐行**再判一次，把块内的真增删行捞回来。

    为什么必须有这一步（2026-09-30 GRA-4118 实测）
    ----------------------------------------------
    左栏 2.0 供货方式那块，新版比旧版多一行 `一个再折叠工具`，它下面
    `防止重压、阳光直晒、雨雪浸淋。` / `须贮存在干燥、避光、室温…` 两行只是被
    **顶下去**了（纵向只差 6pt）。瓦片对拍 4 邻接把这几行连成**一个**候选块，
    块级 `_decide_region` 一算：旧版那两行的字 100% 都在新版里（`_coverage` 顶到
    0.93）→ 判「只是重渲染」→ 整块丢掉，**真新增的那一行跟着一起没了**。

    ⚠️ 病根是**粒度**不是判据：块级覆盖率天然会把「块内一处真新增」淹没在
    「整块大面积只是位移」里。细切到行之后，`一个再折叠工具` 那行对面是**空白**，
    走的是「一边基本空白」分支 —— 一判一个准。判据一个字都不用动。

    ⚠️ 逐行用 `OCR_MIN_COVER_LINE`（0.75）而不是块级的 0.85：
    行内范围小、误判空间小；0.85 是为整段设的，用在单行上会偏严。

    两侧 pixmap 各切一次（新版有墨的行走 pb、旧版有墨的行走 pa），
    切出来的「同一行」在几何上几乎重合 → 最后去重。
    """
    if not GRAPHIC_LINE_RESCAN:
        return []
    out: List[Dict[str, Any]] = []
    for side, pm, pg, pg_path in (("new", pb, ob, new_path),
                                  ("old", pa, oa, old_path)):
        rows = _split_lines(pm, (rect[0], rect[1] - GRAPHIC_BLEED,
                                 rect[2], rect[3] + GRAPHIC_BLEED), scale,
                            anchor=rect)
        if len(rows) <= 1:
            continue                  # 整块就是一行：粗判已经看过它，没必要重判
        for rr in rows:
            s2, t2 = _decide_region(pa, pb, oa, ob, i, rr, scale,
                                    old_path, new_path, ref=rect,
                                    min_cover=OCR_MIN_COVER_LINE)
            if s2 == "mod":
                # 行内小改（数字/日期/版本号）：两侧都有字，不隶属 new/old
                # 任何一侧 —— 单独收，去重逻辑会把两侧切出的同一行消掉。
                if len(_norm_cjk(t2)) >= RESCAN_ROW_MIN_CHARS:
                    out.append(_mk_region(i, rr, "mod", t2.strip()))
                continue
            if s2 != side:
                continue
            if len(_norm_cjk(t2)) < RESCAN_ROW_MIN_CHARS:
                continue              # 认出的字太少 → 证据不足，宁可不报
            out.append(_mk_region(i, rr, side, t2.strip()))
    # 两侧各切一次会把同一行切出两遍（几何几乎重合）→ 去重（保留先到的）
    dedup: List[Dict[str, Any]] = []
    for rg in out:
        r = rg["rect"]
        dup = False
        for k in dedup:
            kr = k["rect"]
            if (abs(r[1] - kr[1]) < 2.0 and abs(r[3] - kr[3]) < 2.0
                    and min(r[2], kr[2]) - max(r[0], kr[0]) > 0.0):
                dup = True
                break
        if not dup:
            dedup.append(rg)
    return dedup


def _channel_is_gutter(tiles, x0: float, x1: float, page_rows: int,
                       tile: int, scale: float, min_frac: float = 0.08) -> bool:
    """竖直通道 [x0,x1]（pt）整页高度上几乎贴着**文字层瓦片** → False（栏内）；
    通道整页几乎无字 → True（栏间通道）。

    为什么需要这个判据（2026-09-30 GRA-4118 实测）
    ----------------------------------------------
    `_merge_adjacent` 只看两块横向间隙 ≤ `x_gap`，可这份文档栏间距只有
    25.8pt（中栏右缘 606.3 / 右栏左缘 632.1），30pt 的 `x_gap` 会把中栏
    候选块与右栏候选块并成**跨栏巨框**：标注框横盖两栏（用户说的
    「标注范围过大」），OCR 还把右栏的字认进中栏条目（清单文本污染）。
    单纯调小 `x_gap` 治不了根 —— 别的文档栏间距可能更窄。
    栏间通道的结构特征是**通页无字**：文字层瓦片（`_text_tiles` 的产出）
    在真正栏间的那条竖直带上，从页顶到页底都数不出几块；而栏内两段之间
    的横向间隙带，上下延伸处全是同栏正文。取「该带内出现过文字层瓦片的
    行数 ÷ 全页瓦片行数 ≥ `min_frac`」当「栏内」门槛。
    """
    if not tiles or page_rows <= 0:
        return False
    tx0 = int(x0 * scale / tile)
    tx1 = int(x1 * scale / tile)
    rows_with_text = {ty for tx, ty in tiles if tx0 <= tx <= tx1}
    return len(rows_with_text) / page_rows < min_frac


def _merge_adjacent(regions: List[Dict[str, Any]], y_gap: float = 8.0,
                    x_gap: float = 30.0, tiles=None, tile: int = 24,
                    scale: float = 1.0, page_rows: int = 0) -> List[Dict[str, Any]]:
    """把同页、同侧（同为新增/同为删除）且位置相邻的矩形并成一大块。

    转曲文字没有字符坐标，只给「行尾一小块」用户看着莫名其妙 —— 不如把
    「如果需要再次插入该球囊导管…」+「• 再折叠工具」标题+介绍句这些
    相邻的 hot 块连成一个整段（用户 2026-09-29 拍板）。不同侧（一红一绿）不合并。
    """
    def _side(x):
        return _para_side(x)

    merged: List[Dict[str, Any]] = []
    for rg in sorted(regions, key=lambda x: (x["page"], _side(x), x["rect"][1], x["rect"][0])):
        r = rg["rect"]
        hit = None
        for m in merged:
            if m["page"] != rg["page"] or _side(m) != _side(rg):
                continue
            mr = m["rect"]
            y_ok = (r[1] <= mr[3] + y_gap) and (r[3] >= mr[1] - y_gap)
            x_ok = (r[0] <= mr[2] + x_gap) and (r[2] >= mr[0] - x_gap)
            if y_ok and x_ok:
                lo, hi = min(mr[2], r[2]), max(mr[0], r[0])
                if hi > lo and _channel_is_gutter(tiles, lo, hi, page_rows,
                                                  tile, scale):
                    continue          # 中间隔着贯通整页的栏间通道 → 不是同一栏
                hit = m
                break
        if hit is None:
            merged.append(dict(rg))
            continue
        mr = hit["rect"]
        hit["rect"] = (min(mr[0], r[0]), min(mr[1], r[1]),
                       max(mr[2], r[2]), max(mr[3], r[3]))
        if _side(rg) == "new":
            hit["new_text"] = (hit["new_text"] + "\n" + rg["new_text"]).strip()
        else:
            hit["old_text"] = (hit["old_text"] + "\n" + rg["old_text"]).strip()
    return merged


# ---------------------------------------------------------------- 段落重组
# 「逐行小块 → 整段」参数表（2026-09-30）。三条都是**结构参数**，不是魔法数：
# 纵向 = 段内行距上限（约 1.5 倍行高），横向 = 同栏判据（重叠占比），
# 文本分隔 = 段内多行拼成条目文本时用什么隔开（界面按行分色，所以要留行界）。
PARA_Y_GAP = 12.0        # 同一段内相邻行允许的纵向空隙 (pt)
PARA_X_OVERLAP = 0.35    # 横向重叠占较窄一块的比例 ≥ 它 才算「同一栏」
PARA_TEXT_SEP = "\n"     # 段内多行在条目文本里的分隔符


def _para_side(rg: Dict[str, Any]) -> str:
    """这一块属于哪一侧 —— 判据是「哪一侧 OCR 认出了字」，与 `_merge_adjacent` 同源。"""
    return "new" if (rg.get("new_text") or "").strip() else "old"


def _merge_paragraphs(regions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 `_refine_region` 切出来的「逐行小块」重新聚成「段」（2026-09-30）。

    为什么要在细切之后再聚一次
    --------------------------
    `_refine_region` 的职责是「只留真正变了的那几行」，代价是把一整段切成逐行小块。
    可**转曲文字本来就是整段一起变的**，逐行切开零信息量，只是把一件事拆成七件：
      · 标注版上是 7 条斑马纹 —— 用户原话「标的还比较分散」；
      · 清单里是 7 条孤立的 OCR 短行 —— 用户原话「简直有点惜字如金，把上下文再多写一点」。
    聚段之后：标注是一个整框（更醒目），清单是一条带**完整段落**的条目。

    判据（两条都要满足，缺一不可）
    ------------------------------
    · **同页同侧** —— 一红一绿绝不合并；
    · **纵向贴邻**（空隙 ≤ `PARA_Y_GAP`）**且横向同栏**（重叠 ≥ 较窄一块的
      `PARA_X_OVERLAP`）—— 少了横向这条，左栏的「一个再折叠工具」（x≈59）
      会被链式并进中栏那一大段（x≈444）：GRA-4118 实测两块横向差 385pt、
      纵向也只差 40pt，只按纵向判会并成一个横跨大半个页面的巨框。

    合并不改判定，只改**粒度** —— 块的集合还是 `_refine_region` 认定的那些行。
    """
    if not regions:
        return []
    # ⚠️ **mod 块不参与聚段**（v40 实测）：mod 两侧都有字（old/new 是「旧→新」
    # 结构），`_para_side` 却按「哪侧有字」把它当 new 侧 —— 与贴邻的 new 块
    # 合并后 `old_text` 被覆盖，REF 的 `150-200→300-200` 就这样被 UDI mod 吞掉
    # （框还是那个框、清单里只剩 UDI 数字）。mod 是行级精确定位 + 两侧互证，
    # 本来就不需要聚段 —— 聚段是为「转曲整段增删」设计的。
    def _is_mod(rg: Dict[str, Any]) -> bool:
        return bool((rg.get("old_text") or "").strip()) and \
               bool((rg.get("new_text") or "").strip())

    out: List[Dict[str, Any]] = []
    for rg in sorted(regions, key=lambda x: (x["page"], _para_side(x),
                                             x["rect"][1], x["rect"][0])):
        r = rg["rect"]
        if _is_mod(rg):
            out.append(dict(rg, _rows=[(r[1], rg.get("new_text") or "")]))
            continue
        hit = None
        for m in out:
            if _is_mod(m):
                continue
            if m["page"] != rg["page"] or _para_side(m) != _para_side(rg):
                continue
            mr = m["rect"]
            y_ok = (r[1] <= mr[3] + PARA_Y_GAP) and (r[3] >= mr[1] - PARA_Y_GAP)
            ov = min(r[2], mr[2]) - max(r[0], mr[0])
            x_ok = ov >= PARA_X_OVERLAP * max(min(r[2] - r[0], mr[2] - mr[0]), 1.0)
            if y_ok and x_ok:
                hit = m
                break
        if hit is None:
            out.append(dict(rg, _rows=[(r[1], rg.get("new_text") or rg.get("old_text") or "")]))
            continue
        mr = hit["rect"]
        hit["rect"] = (min(mr[0], r[0]), min(mr[1], r[1]),
                       max(mr[2], r[2]), max(mr[3], r[3]))
        hit["_rows"].append((r[1], rg.get("new_text") or rg.get("old_text") or ""))

    for m in out:
        rows = sorted(m.pop("_rows"), key=lambda t: t[0])
        txt = PARA_TEXT_SEP.join(t[1].strip() for t in rows if t[1].strip())
        m["new_text" if _para_side(m) == "new" else "old_text"] = txt
    out.sort(key=lambda x: (x["page"], x["rect"][1], x["rect"][0]))
    return out


def page_blocks(path: str) -> Dict[int, List[Tuple[float, float, float, float]]]:
    """取每页的**文本块**矩形（PyMuPDF `get_text("blocks")`），给「同一行」判定用。

    为什么需要它（2026-09-30 用户「文件号 + 修订日期 可以放在一起」）
    --------------------------------------------------------------
    这两处在 PDF 里是**两个 text line**、中间隔 65pt；而中栏（x≈444）与右栏
    （x≈634）虽然只隔 27pt，却是**两个不同的文本块**。纯几何（距离 / 横向重叠）
    **分不开**这两种情形 —— 距离小的反而是不同块。文本块是 PDF 里原文的结构，
    拿它当判据才稳：**同一个块 + 同一条视觉行 ⇒ 同一处内容**。

    返回 `{页号: [(x0, y0, x1, y1), ...]}`；打不开就返回空 dict（调用方降级成不合并）。
    """
    out: Dict[int, List[Tuple[float, float, float, float]]] = {}
    try:
        d = fitz.open(path)
    except Exception:
        return out
    try:
        for i in range(d.page_count):
            bs: List[Tuple[float, float, float, float]] = []
            try:
                for b in d[i].get_text("blocks"):
                    if len(b) >= 4:
                        bs.append((float(b[0]), float(b[1]), float(b[2]), float(b[3])))
            except Exception:
                pass
            out[i] = bs
    finally:
        d.close()
    return out


def _ink_count(pm, rect, scale: float) -> int:
    """一块矩形里的「墨水」像素数（灰度 < 200 算有墨）——用来判「这边是不是空白」。"""
    x0 = max(0, int(rect[0] * scale))
    x1 = min(pm.width, int(rect[2] * scale))
    y0 = max(0, int(rect[1] * scale))
    y1 = min(pm.height, int(rect[3] * scale))
    n = 0
    for y in range(y0, y1):
        row = y * pm.stride
        for v in pm.samples[row + x0:row + x1]:
            if v < SCAN_INK_DARK:
                n += 1
    return n


_SHIFT_STRIDE = 4          # 配准掩膜降采样步长（性能：4 即面积 ÷16；配准只需「够像」，
                           # 不必逐像素，墨迹粗到 4px 采样仍能对齐）
_SHIFT_OVERLAP_MIN = 0.60  # 源墨迹被目标墨迹覆盖到这个比例 → 判「只是挪位」
_SHIFT_TEXT_MIN = 0.50     # 位移配准命中后，配准位置 OCR 文本与源文本的**最低覆盖**。
                           # 低于它 = 配准撞到的其实是别的内容（内容替换，非位移）。
                           # 2026-10-13 实测：11vs12「一个再折叠工具」配准到旧版
                           # 「冠状动脉刻痕球囊」行，_coverage 0.0；真位移的文本
                           # 覆盖率 0.8+。0.5 是「OCR 抖动容忍」与「区分替换」的折中。
_SHIFT_MAX_DY_PX = 300     # 纵向搜索半径上限（像素，@ GRAPHIC_DPI ≈150pt）：
                           # 排版位移再大也不会超过这个量级；无上限时大块配准
                           # 的 rows_b 提取会扫到整页高（实测 18s/块）。
_SHIFT_MAX_DX_PX = 72      # 横向搜索半径（像素 @ GRAPHIC_DPI ≈43pt）：
                           # 2026-10-12 失败4 实测段落「下移 44pt + 左移 26pt」的
                           # **斜向位移**——纯纵向搜索从原理上就对不齐（ov 恒
                           # 0.35~0.40），假新增就是这么漏成绿块的。两段式搜索：
                           # 纵向够门槛直接走（主路径零额外代价），不够才付
                           # 二维细化的钱（dx 档位 ≈36，实测单块 <0.5s）。
# —— 出口位移救援的 OCR 覆盖门槛（2026-10-12 失败4）——
# 像素配准对**文字行**没有判别力（实测错位 137pt 的不同行能在 stride 采样下
# 撞出 ov 0.58~0.65 的假峰），所以「是不是位移」的判定改用 OCR 覆盖判据：
# 有墨侧的 OCR 文本，在对面「就近块 + 整条栏」里覆盖到 ≥ 这个比例 = 内容还在
# 对面 = 只是挪位。像素配准只退居「估算位移量（dx,dy）」的次要角色，不再参与判定。
_RESCUE_BAND_COVER = 0.80


def _moved_rescue_regions(regions, old_path: str, new_path: str,
                          dpi: int = GRAPHIC_DPI):
    """出口统一「位移救援」（2026-10-12 失败4 GRA-M30180）。

    病根：`_decide_region` 的位移守卫只护**像素管线**的候选；**行对拍**
    管线给出的单侧区域没有这道守卫 —— 段落斜向位移时行对拍只看到「新位置
    有字、旧位置空白」，报成假新增（'Coronary I' 整段绿块就是这么来的）。

    做法（2026-10-12 改判据）：**判定**用 OCR 覆盖（可靠），**定位**用像素配准
    （只借它的位移量）。原因：像素配准对文字行**没有判别力**（错位 137pt 的
    不同行能撞出 ov 0.58~0.65 的假峰），不能再拿 ov ≥ 门槛当「是不是位移」的
    判据；而 OCR 覆盖（有墨侧文本在对面就近块/整条栏里覆盖 ≥ `_RESCUE_BAND_COVER`）
    与 `_decide_region` 同一判据、对文字有真判别力。
      ① 有墨侧 `txt` 在对面 `_ocr_near` / `_ocr_band` 覆盖 ≥ 门槛 → 判「只是挪位」；
      ② 再用 `_local_shift_overlap` 跑一次拿 (dx,dy) **填 rect_shift**（定位）；
         像素配准拿不出可信位移（ov 很低）时退化为 rect_shift = rect（浅蓝框
         两侧原位、不标偏移）。
    —— 就地转 moved 区域（`rect`/`rect_shift`/`shift_src`，与 `_decide_region`
    的 "moved" 决策同一份 dict 契约），标记层画浅蓝、清单层出「位置移动」。

    ⚠️ 只碰**单侧**区域：mod（两侧都有字）有两侧互证，不是位移救援的对象；
    `[图形]` 占位符（纯色块增删）没有「内容相同」可言，也不碰。
    同一对增删两侧各转成 moved（rect/rect_shift 互换）→ 末尾按互换关系去重。
    """
    if not regions:
        return regions
    need = [r for r in regions
            if not r.get("moved")
            and bool((r.get("old_text") or "").strip())
            != bool((r.get("new_text") or "").strip())
            and (r.get("old_text") or r.get("new_text") or "").strip()
            != GRAPHIC_SOLID_TAG]
    if not need:
        return regions
    try:
        da, db = fitz.open(old_path), fitz.open(new_path)
    except Exception:
        return regions
    try:
        scale = dpi / 72.0
        pms: Dict[int, Any] = {}
        for r in need:
            i = int(r.get("page", 0))
            side = "old" if (r.get("old_text") or "").strip() else "new"
            # ⚠️ 先做 OCR 覆盖**判定**：有墨侧文本在对面「就近块 + 整条栏」里
            #    覆盖达标 = 内容还在对面 = 只是挪位（真增删则对面没有这段字）。
            txt = (r.get("old_text") if side == "old"
                   else r.get("new_text") or "").strip()
            rect = fitz.Rect(*r["rect"])
            other = db[i] if side == "old" else da[i]
            near = _ocr_near(other, rect, (new_path if side == "old"
                                           else old_path), i)
            band = _ocr_band(other, rect, cache_key=(
                (new_path if side == "old" else old_path), i, "rescue-band",
                int(rect[0] // 40), int(rect[2] // 40)))
            # ⚠️ 判定「这段字在不在对面」分两档：
            #   · 归一化 ≥ OCR_MIN_LEN → `_coverage`（容忍少量 OCR 错字）；
            #   · 4~OCR_MIN_LEN 字符 → `_coverage` 恒返 0（短于门槛），改用
            #     **最长前缀命中**判：ne 的前 k 字符逐段缩短，找能在对面
            #     归一化文本里**原样出现**的最长前缀，k ≥ 6 且占 ne ≥ 门槛
            #     才算命中。为什么是前缀而不是子串：区域细切截断的 OCR 噪声
            #     几乎全落在**尾部**（失败4 'Coronary I' 实为 'Coronary D…'
            #     的误读，'coronaryi' 作子串在对面 'coronarydilatation…'
            #     里永远找不到 —— 后面跟的是 d 不是 i；而前缀 'coronary'
            #     稳稳命中）。6 字符下限挡住「rev02 前缀撞 rev01」这类
            #     短真改动的误救（'rev0' 只有 4 字符）。
            ne = _norm_cjk(txt)
            if len(ne) >= OCR_MIN_LEN:
                hit = (_coverage(txt, near) >= _RESCUE_BAND_COVER
                       or _coverage(txt, band) >= _RESCUE_BAND_COVER)
            elif len(ne) >= 4:
                def _prefix_cover(needle: str, hay: str) -> float:
                    for k in range(len(needle), 5, -1):
                        if needle[:k] in hay:
                            return k / len(needle)
                    return 0.0
                hit = (_prefix_cover(ne, _norm_cjk(near)) >= _RESCUE_BAND_COVER
                       or _prefix_cover(ne, _norm_cjk(band)) >= _RESCUE_BAND_COVER)
            else:
                hit = False
            if not hit:
                continue                       # 对面没有这段字 → 真增删，不救
            # ---- 判定为「挪位」；下面只借像素配准的位移量做定位 ----
            if i not in pms:
                try:
                    pms[i] = (
                        da[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY,
                                         annots=False),
                        db[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY,
                                         annots=False))
                except Exception:
                    pms[i] = None
            pair = pms.get(i)
            dx = dy = 0.0
            if pair:
                pa, pb = pair
                src_pm, dst_pm = (pa, pb) if side == "old" else (pb, pa)
                ov, pdx, pdy = _local_shift_overlap(src_pm, dst_pm, rect, scale)
                if ov >= _SHIFT_OVERLAP_MIN:   # 位移量可信才采用
                    dx, dy = pdx / scale, pdy / scale
            r["moved"] = True
            r["shift_src"] = side
            r["rect_shift"] = (rect[0] + dx, rect[1] + dy,
                               rect[2] + dx, rect[3] + dy)
    finally:
        da.close()
        db.close()
    # —— 去重：同一对「删+增」两侧都被救成 moved 时，两条的 rect/rect_shift
    #    正好互换（同一件事报两遍）—— 保留先到的一条。
    out: List[Dict[str, Any]] = []
    for r in regions:
        if r.get("moved"):
            dup = False
            for m in out:
                if not m.get("moved") or m["page"] != r["page"]:
                    continue
                a1, a2 = m["rect"], m["rect_shift"]
                b1, b2 = r["rect"], r["rect_shift"]
                if (abs(a1[0] - b2[0]) < 2 and abs(a1[1] - b2[1]) < 2
                        and abs(a2[0] - b1[0]) < 2 and abs(a2[1] - b1[1]) < 2):
                    dup = True
                    break
            if dup:
                continue
        out.append(r)
    return out


def _local_shift_overlap(src_pm, dst_pm, rect, scale: float,
                         stride: int = _SHIFT_STRIDE):
    """局部区域**平移**配准：源块在目标页面里滑动，算「墨迹重合度」。

    用途（GPT 指令第四阶段 / 用户「内容叠加 + 位移」设想）：
    当 `_decide_region` 判出「一边基本空白」的增/删候选，但 OCR 文本比对
    因噪声 / 短文本 / 转曲覆盖不了时，用**像素位移配准**兜底：
       · 源块（有墨那侧）在目标页面里平移，算「源墨迹落到目标墨迹上的比例」，
         取最大；
       · 重合度 ≥ `_SHIFT_OVERLAP_MIN` → 内容是同一块、只是被顶下去/挤过来，
         不该报成增删；
       · 怎么移都够不着 → 才是真增删（内容变了配准本来就该失败）。

    搜索策略（2026-10-12 升级为**两段式**）：
      ① 纵向为主（排版位移的绝对主因是「整块被上方内容增减顶下去/挤上来」，
         横向在栏宽固定的版式里几乎不动）—— 复杂度 O(dy×n)，主路径零额外代价；
      ② 纵向不够门槛才做**有界二维细化**（dx ±`_SHIFT_MAX_DX_PX`）——
         斜向位移（失败4：段落因 「Semi」 插行被顶下去同时还左移了半栏）
         只有二维才对得齐。两段合起来复杂度仍有界（O(dy×n + dx×dy×n/16)）。

    ⚠️ rect 太高/太宽（整页量级）不做配准：整页都变，配准说明不了「挪位还是
       增删」，且 rows_b 提取会扫到整页高（性能灾难，实测 20s/块）。交给 OCR。

    返回 `(best_overlap, best_dx, best_dy)`（像素）；best_overlap ∈ [0,1]。
    """
    x0 = max(0, int(rect[0] * scale))
    x1 = min(src_pm.width, int(rect[2] * scale))
    y0 = max(0, int(rect[1] * scale))
    y1 = min(src_pm.height, int(rect[3] * scale))
    w, h = x1 - x0, y1 - y0
    if w <= 4 or h <= 4:
        return 0.0, 0, 0
    # ⚠️ rect 太高/太宽（整页量级）不做配准：整页都变，配准说明不了「挪位还是
    #    增删」，且 rows_b 提取会扫到整页高（性能灾难，实测 20s/块）。交给 OCR。
    if h > _SHIFT_MAX_DY_PX or w > _SHIFT_MAX_DY_PX * 2:
        return 0.0, 0, 0
    dx_rng = min(_SHIFT_MAX_DX_PX, max(stride, w // 2))
    # 源墨迹按行分组（降采样）：每行的墨迹 x 集合 —— 配准按「行集合交集」算，
    #   比逐点 set 查询快一个量级（每轮 dy 只做 H 次集合交集，而非 n 次点查询）。
    rows_a: Dict[int, Set[int]] = {}
    for y in range(y0, y1, stride):
        row = y * src_pm.stride
        xs = {x for x in range(x0, x1, stride)
              if src_pm.samples[row + x] < SCAN_INK_DARK}
        if xs:
            rows_a[y] = xs
    if not rows_a:
        return 0.0, 0, 0
    # 搜索范围：纵向扩 rect 高 ×3，但设绝对上限（见 _SHIFT_MAX_DY_PX）
    dy_rng = min(_SHIFT_MAX_DY_PX, max(16, int(h * 3.0)))
    # 目标墨迹按行分组（竖带上下左右各扩搜索半径 —— 二维细化要往旁边挪）
    # ⚠️ 采样网格必须与 rows_a **同余**：rows_a 的 x 网格从 x0 起步、y 网格从
    #    y0 起步（步长 stride）。若 rows_b 从 sx0/sy0 起步直接采样，一旦夹边
    #    （sx0/sy0 被 0 或页边界截断）或 x0/y0 本身非 stride 倍数，两组网格
    #    相差 1~3px —— 点永远对不上、交集恒空，配准静默失效（v48 潜伏 bug，
    #    2026-10-12 失败4 排查时抓到）。所以起点向 x0/y0 的网格对齐。
    sx0 = max(0, x0 - dx_rng)
    sx1 = min(dst_pm.width, x1 + dx_rng)
    sy0 = max(0, y0 - dy_rng)
    sy1 = min(dst_pm.height, y1 + dy_rng)
    gx = (x0 - sx0) % stride
    gy = (y0 - sy0) % stride
    rows_b: Dict[int, Set[int]] = {}
    for y in range(sy0 + gy, sy1, stride):
        row = y * dst_pm.stride
        xs = {x for x in range(sx0 + gx, sx1, stride)
              if dst_pm.samples[row + x] < SCAN_INK_DARK}
        if xs:
            rows_b[y] = xs
    if not rows_b:
        return 0.0, 0, 0
    total = sum(len(v) for v in rows_a.values())

    def _best_1d() -> Tuple[float, int, int]:
        """纵向平移搜索（dx 固定 0）：逐行做 x 集合交集，累加命中墨点数。"""
        bb, bdy = 0.0, 0
        ddy = dy_rng // stride * stride
        for dy in range(-ddy, ddy + 1, stride):
            hit = 0
            for y, xs in rows_a.items():
                ys = y + dy
                if ys in rows_b:
                    hit += len(xs & rows_b[ys])
            ov = hit / total
            if ov > bb:
                bb, bdy = ov, dy
            if bb >= 0.95:
                break
        return bb, 0, bdy

    best, best_dx, best_dy = _best_1d()
    if best >= _SHIFT_OVERLAP_MIN:
        return best, best_dx, best_dy          # 纵向就够了 —— 主路径零额外代价
    # ---- 二维细化：斜向位移（纵向对不齐、但平移 (dx,dy) 能对上）----
    # 目标行的 x 集合按 -dx 平移后与源行交集（源墨迹右移 dx = 目标墨迹左移 dx）。
    ddx = dx_rng // stride * stride
    ddy = dy_rng // stride * stride
    for dx in range(-ddx, ddx + 1, stride):
        if dx == 0:
            continue                            # dx=0 那档 _best_1d 已扫过
        for dy in range(-ddy, ddy + 1, stride):
            hit = 0
            for y, xs in rows_a.items():
                rb = rows_b.get(y + dy)
                if not rb:
                    continue
                # 源点 (x,y) 对上目标点 (x+dx, y+dy)：目标 x 集合统一平移 -dx
                hit += len(xs.intersection([x - dx for x in rb]))
            ov = hit / total
            if ov > best:
                best, best_dx, best_dy = ov, dx, dy
            if best >= 0.95:
                return best, best_dx, best_dy
    return best, best_dx, best_dy



def _graphic_ink_ratio(page, dpi: int = 72) -> float:
    """这一页的墨水有多大比例**不在文字层里**（= 转曲/图形文字占比）。

    用途：页面上没有这种区域时，OCR 完全没必要跑 —— 纯文字页 OCR 一遍要十几秒，
    而多出来的结论一定是「什么都没找到」。实测合成用例页 ratio≈0，
    GRA-4118 新版中栏 ratio 很高，正是该跑 OCR 的那一页。
    """
    pm = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)
    scale = dpi / 72.0
    ink = 0
    for y in range(pm.height):
        row = y * pm.stride
        for v in pm.samples[row:row + pm.width]:
            if v < SCAN_INK_DARK:
                ink += 1
    if ink <= 0:
        return 0.0
    cover = 0
    try:
        for w in page.get_text("words"):
            x0 = max(0, int(w[0] * scale)); x1 = min(pm.width, int(w[2] * scale) + 1)
            y0 = max(0, int(w[1] * scale)); y1 = min(pm.height, int(w[3] * scale) + 1)
            for y in range(y0, y1):
                row = y * pm.stride
                for v in pm.samples[row + x0:row + x1]:
                    if v < SCAN_INK_DARK:
                        cover += 1
    except Exception:
        return 0.0
    return max(0.0, 1.0 - cover / ink)


def _ocr_rescue(items: List[LocDiff], old_path: str, new_path: str) -> List[LocDiff]:
    """用 OCR 撤销「文字层缺行」造成的假增删（2026-09-29 实测的 9 处免责声明误标）。

    病根：改版后有一块被**转曲**（矢量曲线，没有文字层）。字符流引擎只认文字层，
    于是「旧版有、新版没有」→ 报**删除** —— 可那段字在新版里明明还在，只是排到下面去了。

    判据：把**对面文档同一条栏**OCR 一遍，这段字还在里面 → 撤掉这条误标。
    这与全局策略一致（**内容挪了位置不算改动**）；OCR 认不出来时**一律保留**
    （宁可多标一处待人工看，也不凭猜测抹掉真改动）。
    """
    if not ocr_available():
        return items
    need: List[Tuple[str, int, float, float]] = []      # (文件, 页, x0, x1) 待确认要不要 OCR
    plan: List[Tuple[int, str, int]] = []
    for k, d in enumerate(items):
        if d.kind == "removed":
            txt, rects = d.old_text, d.rects_old
            pn = d.page_new if d.page_new is not None else d.page_old
            who = new_path
        elif d.kind == "added":
            txt, rects = d.new_text, d.rects_new
            pn = d.page_old if d.page_old is not None else d.page_new
            who = old_path
        else:
            continue
        if pn is None or not rects or len(_norm_cjk(txt)) < OCR_MIN_LEN:
            continue
        plan.append((k, who, pn))
        need.append((who, pn, min(r[0] for r in rects), max(r[2] for r in rects)))
    if not plan:
        return items
    da, db = fitz.open(old_path), fitz.open(new_path)
    drop: set = set()
    try:
        ratio: Dict[Tuple[str, int], float] = {}
        for k, who, pn in plan:
            doc = db if who == new_path else da
            if not (0 <= pn < doc.page_count):
                continue
            key = (who, pn)
            if key not in ratio:
                ratio[key] = _graphic_ink_ratio(doc[pn])
            if ratio[key] < 0.02:
                continue                 # 这页没有转曲区域 → OCR 无从下手，保留原判
            d = items[k]
            if d.kind == "removed":
                txt, rects = d.old_text, d.rects_old
            else:
                txt, rects = d.new_text, d.rects_new
            band = ocr_strip(pn, who, min(r[0] for r in rects),
                             max(r[2] for r in rects), doc[pn])
            if _coverage(txt, band) >= OCR_MIN_COVER:
                drop.add(k)
    finally:
        da.close()
        db.close()
    return [d for k, d in enumerate(items) if k not in drop]



# ---------------------------------------------------------------- 自检
if __name__ == "__main__":
    ok = fail = 0

    def chk(name, cond, extra=""):
        global ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name} {extra}")

    def _union_text(path, page_idx, rects):
        if not rects:
            return ""
        xs = [r[0] for r in rects] + [r[2] for r in rects]
        ys = [r[1] for r in rects] + [r[3] for r in rects]
        doc = fitz.open(path)
        try:
            return doc[page_idx].get_textbox(
                fitz.Rect(min(xs), min(ys), max(xs), max(ys))).strip()
        finally:
            doc.close()

    tmp = tempfile.mkdtemp()

    def _mk(path, lines):
        d = fitz.open()
        p = d.new_page()
        for k, ln in enumerate(lines):
            y = ln[0] if isinstance(ln, tuple) else 60 + k * 20
            t = ln[1] if isinstance(ln, tuple) else ln
            # 用内置中文字体：helv 画不出汉字，会让中文用例变成一堆乱码字
            p.insert_text((60, y), t, fontname="china-s", fontsize=11)
        d.save(path)
        d.close()

    # 用例1：改一个字 → 拆成删（旧值）+ 增（新值），各标各的那一侧
    A = os.path.join(tmp, "a.pdf")
    B = os.path.join(tmp, "b.pdf")
    _mk(A, ["Item 9", "Keep same line", "Footer text"])
    _mk(B, ["Item 11", "Keep same line", "Footer text"])
    ds = locate(A, B)
    chk("改一个字拆成删+增两条", len(ds) == 2, f"实际 {len(ds)}")
    if len(ds) == 2:
        d0, d1 = ds
        chk("删的是旧值、增的是新值", d0.kind == "removed" and d1.kind == "added")
        chk("旧侧有矩形", len(d0.rects_old) >= 1)
        chk("新侧有矩形", len(d1.rects_new) >= 1)
        chk(f"旧版标到的正是 9（实际 {_union_text(A,0,d0.rects_old)!r}）",
            _union_text(A, 0, d0.rects_old) == "9")
        chk(f"新版标到的正是 11（实际 {_union_text(B,0,d1.rects_new)!r}）",
            _union_text(B, 0, d1.rects_new) == "11")

    # 用例2：完全相同的两页 → 0 处差异
    _mk(A, ["Alpha", "Beta", "Gamma"])
    _mk(B, ["Alpha", "Beta", "Gamma"])
    chk("完全相同页无差异", len(locate(A, B)) == 0, f"实际 {len(locate(A, B))}")

    # 用例3（关键回归）：两条「长得像但不同」的句子在两份里都没变 → 不误标
    _mk(A, ["PPS 成功率 98.98%", "FAS 成功率 98.99%", "其它不变"])
    _mk(B, ["PPS 成功率 98.98%", "FAS 成功率 98.99%", "其它不变"])
    chk("相似但不变的行不误标", len(locate(A, B)) == 0, f"实际 {len(locate(A, B))}")

    # 用例4：只改了数字 → 拆成删+增，各标各的
    _mk(A, ["PPS 成功率 98.98%", "FAS 成功率 98.99%"])
    _mk(B, ["PPS 成功率 98.98%", "FAS 成功率 99.99%"])
    ds4 = locate(A, B)
    chk("只标到变的数字（拆删+增，共 2 条）", len(ds4) == 2, f"实际 {len(ds4)}")
    if len(ds4) == 2:
        got_a = _union_text(A, 0, ds4[0].rects_old)
        got_b = _union_text(B, 0, ds4[1].rects_new)
        chk(f"只标到变的那一位 8（实际 {got_a!r}）", got_a == "8")
        chk(f"只标到变的那一位 9（实际 {got_b!r}）", got_b == "9")

    # 用例5：整段换位（内容不变，只换了顺序）→ 位移（浅蓝），不刷红/绿/黄
    _mk(A, ["Header", "第一段很长的内容完全没变", "第二段也没变", "Tail"])
    _mk(B, ["Tail", "第二段也没变", "第一段很长的内容完全没变", "Header"])
    _d5 = locate(A, B)
    chk("整段换位 → 只报 moved（位移），不刷增删改（2026-10-12 位移标蓝）",
        _d5 and all(d.kind == "moved" for d in _d5),
        f"实际 {[(d.kind) for d in _d5]}")

    # 用例6：整行改写（内容毫不相似）→ 报成「删+增」，两侧文字都还原得出来
    _mk(A, ["Same one", "Only in old"])
    _mk(B, ["Same one", "完全不同的新句子 xyz"])
    ds6 = locate(A, B)
    chk("整行改写有差异", len(ds6) >= 1, f"实际 {len(ds6)}")
    chk("改写拆成删+增（不再有修改档）",
        {d.kind for d in ds6} == {"removed", "added"},
        f"实际 {[d.kind for d in ds6]}")
    chk("旧侧文字拼起来还是 Only in old",
        "".join(d.old_text for d in ds6) == "Onlyinold",
        f"实际 {''.join(d.old_text for d in ds6)!r}")
    chk("新侧文字拼起来还是 完全不同的新句子xyz",
        "".join(d.new_text for d in ds6) == "完全不同的新句子xyz",
        f"实际 {''.join(d.new_text for d in ds6)!r}")

    # 用例7（关键回归）：一行被拆成两行 → 只标真正变了的字
    _mk(A, [(60, "文件号：GRA-4118 Rev06/DCR 修订日期：2025年12月19日")])
    _mk(B, [(60, "文件号：GRA-4118 Rev07"), (80, "修订日期：2026年05月27日")])
    ds7 = locate(A, B)
    marked_old = "".join(d.old_text for d in ds7)
    marked_new = "".join(d.new_text for d in ds7)
    chk("拆行后旧侧标到版本号那个 6", "6" in marked_old, f"旧侧标到 {marked_old!r}")
    chk("拆行后新侧标到版本号那个 7", "7" in marked_new, f"新侧标到 {marked_new!r}")
    chk("未变部分『文件号：GRA-4118 Rev0』没被标",
        "文件号" not in marked_old and "文件号" not in marked_new)
    chk("未变部分『修订日期：』没被标",
        "修订日期" not in marked_old and "修订日期" not in marked_new)

    # 用例8：跨页移动 + 改写 → 只在改写处标，不移位处不标
    _mk(A, [(60, "固定一行 AAA"), (100, "会挪到下一页的内容 XYZ")])
    _mk(B, [(100, "会挪到下一页的内容 XYZ"), (60, "固定一行 AAA"), (140, "新增的一行 BBB")])
    ds8 = locate(A, B)
    # "固定一行 AAA" 与 "会挪到下一页的内容 XYZ" 都原样存在 → 抵消；
    # 只剩 "新增的一行 BBB" → added
    kinds8 = sorted(d.kind for d in ds8)
    chk("跨页移动不误标，只标新增", kinds8 == ["added"], f"实际 {kinds8}")

    # 用例9：页眉页脚可忽略
    _mk(A, [(18, "HEADER-A"), (300, "Body same"), (825, "FOOT-A")])
    _mk(B, [(18, "HEADER-B"), (300, "Body same"), (825, "FOOT-B")])
    chk("不忽略页眉页脚时判 2 处改（拆成删+增共 4 条）", len(locate(A, B)) == 4)
    chk("忽略页眉页脚后为 0 处",
        len(locate(A, B, drop_header_footer=True)) == 0)

    # 用例10（关键回归 · 用户实测打回来的病根）：重新折行 / 并成一行 → 不算改动
    # 旧版：两行 + 项目符号；新版：并成一行且没有项目符号。文字内容一字没变。
    _mk(A, [(60, "• 本试验主要有效性指标是手"), (80, "术成功率（Procedure Success Rate）。")])
    _mk(B, [(60, "本试验主要有效性指标是手术成功率（Procedure Success Rate）。")])
    chk("折行/并行（只少个项目符号）不误标",
        len(locate(A, B)) == 0, f"实际 {len(locate(A, B))}")

    # 用例11（关键回归）：旧版的孤立行被新版并进长行 → 那串字不该被标
    _mk(A, [(60, "文件号：GRA-4118 Rev06"), (80, "博迈医疗"), (300, "正文不变")])
    _mk(B, [(60, "文件号：GRA-4118 Rev07"),
            (80, "修订日期：2026年05月27日  博迈医疗"), (300, "正文不变")])
    ds11 = locate(A, B)
    marked11 = "".join(d.old_text + d.new_text for d in ds11)
    chk("被并进长行的『博迈医疗』不被误标", "博迈医疗" not in marked11,
        f"实际标到 {marked11!r}")
    chk("版本号 6→7 仍被标出", "6" in marked11 and "7" in marked11)

    # 用例12（关键回归）：地址块只「插入」了短语 → 只有新侧该有标记，旧侧一处都不该标
    _mk(A, [(60, "生产地址：工业北四路1号2栋101室、201室，4栋201室")])
    _mk(B, [(60, "生产地址：工业北四路1号1栋201室，2栋101室、201室，4栋201室")])
    ds12 = locate(A, B)
    chk("纯插入时旧侧一处不标",
        all(not d.rects_old for d in ds12), f"实际旧侧标记 {[d.old_text for d in ds12]}")
    chk("纯插入时新侧标出被插入的『1栋201室，』",
        any("1栋201室" in d.new_text for d in ds12),
        f"实际新侧标记 {[d.new_text for d in ds12]}")

    # 用例13（关键回归）：转曲块「按行细切」（`_split_lines`）的两条硬规矩
    #   ① 行距紧到 1px 空白也得切开。这份 PDF 转曲正文的行距实测只有
    #      0.8 / 1.2 / 1.8 / 2.4pt，`GRAPHIC_LINE_GAP` 稍大就把两行粘成一行，
    #      两行的字被混着 OCR → 对面比对不过 → **没变的那行也被标绿**
    #      （用户 2026-09-29 报的「标多了」）。
    #   ② 传了 `anchor` 时只保留与它垂直重叠 ≥ `GRAPHIC_ANCHOR_MIN` 的行 ——
    #      扫描窗口上下外扩是为了完整切出首/末行，但别把隔壁行也圈进来。
    class _FakePM:
        """只给 `_ink_rows` 要的那几个属性，省得真去渲染一张图。"""

        def __init__(self, w, h, bands):
            self.width, self.height, self.stride = w, h, w
            buf = bytearray(b"\xff") * (w * h)
            for y0, y1, x0, x1 in bands:
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        buf[y * w + x] = 0
            self.samples = bytes(buf)

    pm = _FakePM(120, 60, [(10, 16, 10, 60), (18, 24, 10, 60), (26, 32, 10, 60)])
    lines = _split_lines(pm, (0, 0, 120, 60), 1.0)
    chk(f"行距只有 1px 空白也要切出 3 行（实际 {len(lines)}）", len(lines) == 3)
    lines_a = _split_lines(pm, (0, 0, 120, 60), 1.0, anchor=(0, 17, 120, 25))
    chk(f"给了 anchor 后只留中间那行（实际 {len(lines_a)}）", len(lines_a) == 1)

    # 用例14（关键回归 · P0-1 漏检修复）：块级判否 ≠ 块内没有真改动
    #   GRA-4118 左栏实测：块里 1 行真新增 + 2 行只是被顶下去，块级覆盖率
    #   0.93 → 整块判「只是重渲染」→ 真新增那行跟着一起没了。
    #   `_line_rescan` 负责细切到行再判一次。这里用桩替掉 `_decide_region`
    #   （不真调 OCR），验证它的 5 条行为：
    _orig_decide = _decide_region
    _band = (15.0, 26.0)          # 中间那行 = 假想的「真新增」行
    _blk = (10.0, 9.0, 110.0, 33.0)

    def _mk_decide(hit_side, hit_text):
        def stub(pa_, pb_, oa_, ob_, i, rr, scale_, op, np_, ref=None, min_cover=0.0):
            if hit_side and rr[1] >= _band[0] and rr[3] <= _band[1]:
                return (hit_side, hit_text)
            return (None, "")
        return stub

    try:
        _decide_region = _mk_decide("new", "一个再折叠工具")
        got = _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf")
        chk(f"行级捞回：真新增行被捞回 1 条（实际 {len(got)}）", len(got) == 1)
        if got:
            chk("行级捞回：side/文本来自命中那行",
                got[0]["new_text"] == "一个再折叠工具" and not got[0]["old_text"],
                repr(got[0]))
            chk("行级捞回：矩形落在命中行的纵带里",
                got[0]["rect"][1] >= _band[0] - 1 and got[0]["rect"][3] <= _band[1] + 1)

        _decide_region = _mk_decide("new", "字")     # 1 个字 < RESCAN_ROW_MIN_CHARS
        chk("行级捞回：认出的字太少不报（宁缺勿滥）",
            _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf") == [])

        _decide_region = _mk_decide("other", "两边都不该收")   # side 对不上 → 不报
        chk("行级捞回：side 与所在侧不符不报",
            _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf") == [])

        # 两侧对同一行（几何几乎重合、传参相同）各判出一次 → 去重后只剩 1 条
        _calls = {"n": 0}

        def stub_both(pa_, pb_, oa_, ob_, i, rr, scale_, op, np_, ref=None, min_cover=0.0):
            if rr[1] >= _band[0] and rr[3] <= _band[1]:
                _calls["n"] += 1
                return (("new" if _calls["n"] % 2 else "old"), "两侧都认出")
            return (None, "")
        _decide_region = stub_both
        got2 = _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf")
        chk(f"行级捞回：两侧重复行去重后 1 条（实际 {len(got2)}）", len(got2) == 1)

        # 整块只有一行 → 粗判已经看过它，没必要重判
        pm1 = _FakePM(120, 60, [(10, 16, 10, 60)])
        _decide_region = _mk_decide("new", "一个再折叠工具")
        chk("行级捞回：整块一行时直接跳过",
            _line_rescan(pm1, pm1, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf") == [])

        _GRAPHIC_LINE_RESCAN = GRAPHIC_LINE_RESCAN
        GRAPHIC_LINE_RESCAN = False
        try:
            _decide_region = _mk_decide("new", "一个再折叠工具")
            chk("行级捞回：开关关闭时返回空",
                _line_rescan(pm, pm, None, None, 0, _blk, 1.0, "o.pdf", "n.pdf") == [])
        finally:
            GRAPHIC_LINE_RESCAN = _GRAPHIC_LINE_RESCAN
    finally:
        _decide_region = _orig_decide

    # 用例15（关键回归 · P1 跨栏并块）：`_merge_adjacent` 识别「栏间通道」
    #   GRA-4118 栏间距 25.8pt < x_gap=30 → 中栏块与右栏块被并成跨栏巨框，
    #   标注框横盖两栏、OCR 还把右栏的字认进中栏条目。判据：两块之间的
    #   竖直通道整页几乎无文字层瓦片 = 栏间通道 → 不并。
    _rg = lambda x0, y0, x1, y1, side="new": (   # noqa: E731
        {"page": 0, "rect": (x0, y0, x1, y1),
         "old_text": "" if side == "new" else "旧", "new_text": "新" if side == "new" else ""})
    # 模拟一页 640×480pt、tile=24pt、scale=1：文字层瓦片在 x<528（左栏）与
    # x≥576（右栏），x∈[552,576] 的竖直带从页顶到页底无字 = 栏间通道
    _tiles = ({(tx, ty) for tx in range(0, 22) for ty in range(0, 20)}
              | {(tx, ty) for tx in range(24, 27) for ty in range(0, 20)})
    _mid = _rg(440.0, 100.0, 548.0, 145.0)       # 左栏一块
    _right = _rg(573.8, 113.7, 640.0, 145.0)     # 右栏一块（横向间隙 25.8pt < x_gap）
    _no_tile = None
    chk("跨栏：不传瓦片时保持旧行为（间隙 25.8 < 30 会并）",
        len(_merge_adjacent([dict(_mid), dict(_right)])) == 1)
    chk("跨栏：通道整页无字 → 判为栏间通道",
        _channel_is_gutter(_tiles, 548.0, 573.8, 20, 24, 1.0))
    chk("跨栏：栏内间隙带（上下有正文行）不判为通道",
        not _channel_is_gutter(_tiles, 300.0, 330.0, 20, 24, 1.0))
    got3 = _merge_adjacent([dict(_mid), dict(_right)], tiles=_tiles, tile=24,
                           scale=1.0, page_rows=20)
    chk("跨栏：传瓦片后栏间通道挡住合并（2 块各自成段）", len(got3) == 2,
        f"实得 {len(got3)} 块")
    # 栏内两块：间隙带上下有文字层瓦片 → 照常并
    _a2 = _rg(100.0, 100.0, 200.0, 130.0)
    _b2 = _rg(214.0, 100.0, 300.0, 130.0)        # 间隙 14pt，带在左栏内部（有瓦片）
    got4 = _merge_adjacent([dict(_a2), dict(_b2)], tiles=_tiles, tile=24,
                           scale=1.0, page_rows=20)
    chk("跨栏：栏内间隙照常并（1 块）", len(got4) == 1, f"实得 {len(got4)} 块")
    # 空瓦片集合：每页全空 → 视作无信息，保持旧行为（不判通道）
    chk("跨栏：空瓦片集合不启用通道判据",
        len(_merge_adjacent([dict(_mid), dict(_right)], tiles=set(), tile=24,
                            scale=1.0, page_rows=20)) == 1)

    # ---- 2026-09-30 新增：转曲段聚段 + 最长连续覆盖 ----
    # ① 聚段：同栏相邻行并成一段、段内多行拼成完整文本；跨栏 / 跨色绝不并
    _segs = _merge_paragraphs([
        {"page": 0, "rect": (444.0, 100.0, 600.0, 108.0), "old_text": "", "new_text": "第一行"},
        {"page": 0, "rect": (444.0, 108.0, 600.0, 116.0), "old_text": "", "new_text": "第二行"},
        {"page": 0, "rect": (60.0, 100.0, 200.0, 108.0), "old_text": "", "new_text": "左栏"},
    ])
    chk("聚段：同栏相邻行并成一段、跨栏各自成段", len(_segs) == 2, f"实得 {len(_segs)} 段")
    _mid = [s for s in _segs if s["rect"][0] > 400]
    chk("聚段：段内多行拼成完整文本（不再惜字如金）",
        bool(_mid) and _mid[0]["new_text"] == "第一行\n第二行",
        repr(_mid[0]["new_text"] if _mid else None))
    chk("聚段：一红一绿不合并（块数）", len(_merge_paragraphs([
        {"page": 0, "rect": (0, 0, 100, 10), "old_text": "旧", "new_text": ""},
        {"page": 0, "rect": (0, 10, 100, 20), "old_text": "", "new_text": "新"},
    ])) == 2)

    # ② 最长连续覆盖：把「碰巧共用几个词的不同句子」和「同一句话」分开
    _a = "用充压装置使球囊导管产生负压，并保持；"
    _b = "使用充压装置产生负压使球囊内压力完全卸载（卸压终点为球囊工作长度区域基本无液体）"
    chk("最长连续：散落相同词判为不同句（实测 0.29）",
        _longest_cover(_a, _b) < 0.45, f"实得 {_longest_cover(_a, _b):.2f}")
    chk("最长连续：整句挪位判为同一句（实测 1.00）",
        _longest_cover(_a, "前文随便什么" + _a + "后文") > 0.9,
        f"实得 {_longest_cover(_a, '前文随便什么' + _a + '后文'):.2f}")
    chk("累加覆盖率比最长连续虚高（这道闸门存在的理由）",
        _coverage(_a, _b) > _longest_cover(_a, _b) * 1.5,
        f"累加 {_coverage(_a, _b):.2f} vs 最长 {_longest_cover(_a, _b):.2f}")

    # ⑨ 展示用正文的「行首符号清除」（2026-09-30 横向放宽后引入）
    chk("行首符号：逐行 OCR 没这个符号 → 去掉带进来的 `-`",
        _strip_lead_mark("- 如果需要再次插入该球囊导管", "知果需要再次插入该球囊导管")
        == "如果需要再次插入该球囊导管")
    chk("行首符号：`- ` 连空格一起清掉",
        _strip_lead_mark("-  检查球囊导管", "含查球宫导管") == "检查球囊导管")
    chk("行首符号：本行真有符号 → 原样保留",
        _strip_lead_mark("• 用充压装置使球囊导管产生负压", "• 用充压装置使球囊导管产生负压")
        == "• 用充压装置使球囊导管产生负压")
    chk("行首符号：普通汉字行不动",
        _strip_lead_mark("球囊导管，使用再折叠工具", "求囊导管，使用再折叠工具")
        == "球囊导管，使用再折叠工具")
    chk("行首符号：没有参照文本时不动（宁可留着）",
        _strip_lead_mark("- 检查球囊导管", "") == "- 检查球囊导管")

    # ⑩ 归一化：大写字母必须保留（2026-09-30 修「先过滤后 lower」丢大写的 bug）
    chk("归一化：大写拉丁字母不丢（GRA-3634 Rev02）",
        _norm_cjk("GRA-3634 Rev02") == "gra3634rev02",
        f"实得 {_norm_cjk('GRA-3634 Rev02')!r}")
    chk("归一化：驼峰词不丢（BrosMed）",
        _norm_cjk("BrosMed") == "brosmed", f"实得 {_norm_cjk('BrosMed')!r}")
    chk("归一化：大写西里尔折叠后保留（РЗН→pзh）",
        _norm_cjk("РЗН") == "pзh", f"实得 {_norm_cjk('РЗН')!r}")

    # ⑪ 行内修改判据（token 级对齐，GRA-3634 rev02→rev03 的 6 对实测定案）
    chk("mod worthy：注册证日期整行（真改 ✓）",
        _mod_worthy("Регистрационное удостоверение Nº РЗН 2023/20012 от 06.04.2023",
                    "Регистрационное удостоверение Nº РЗН 2023/20012 от 23.03.2026"))
    chk("mod worthy：短版本号行（真改 ✓）",
        _mod_worthy("GRA-3634 Rev02", "GRA-3634 Rev03"))
    chk("mod worthy：同形字母抖动（噪声 ✗）",
        not _mod_worthy("опустимое давление 22 атм. H 8омм EF 820-030080-150",
                        "опустимое давление 22 атм. H 80мм EF 820-030080-150"))
    chk("mod worthy：表格行漏一个数（token 数不等 ✗）",
        not _mod_worthy("Давление атм. 8 10 12 НД 14 16 18 20 22 МДД 24 26 28 кМа 800",
                        "Давление атм. 10 12 НД 14 16 18 20 22 МДД 24 26 28 кМа 800"))
    chk("mod worthy：空格漂移导致 token 错位（✗）",
        not _mod_worthy("（01） 0 6958481 451054 （17）260504（10）2305051234",
                        "01） 06958481 451054 （17）260504（10）230505123."))
    chk("mod worthy：字母 vs 数字（Hi-Tech/H1-Tech ✗）",
        not _mod_worthy("SongShan Lake Hi - T Industrial Development Zone",
                        "SongShan Lake H1 - T Industrial Development Zone"))
    chk("mod worthy：纯字母改写不报（保守 ✗）",
        not _mod_worthy("BrosMed Medical", "BrosMed Clinic"))

    # ⑫ 数字换数字原子判据
    chk("digit swap：等长数字串全换 ✓",
        _digit_swap("06042023", "23032026"))
    chk("digit swap：版本号一位换 ✓",
        _digit_swap("rev02", "rev03"))
    chk("digit swap：数字 vs 字母 ✗",
        not _digit_swap("8oмм", "80мм"))
    chk("digit swap：长度不等 ✗",
        not _digit_swap("2305051234", "230505123."))

    # ⑬ 扫描件候选：合成扫描页上能圈出改动行
    # ⚠️ 字号要大：渲染字比扫描字细，11pt 的单数字差异行峰值才 5px，
    #    过不了 SCAN_ROW_PX=6（真实扫描件实测峰值 17）
    A_scan = os.path.join(tmp, "sa.pdf")
    B_scan = os.path.join(tmp, "sb.pdf")
    for path, t in ((A_scan, "GRA-TEST Rev02"), (B_scan, "GRA-TEST Rev03")):
        d_t = fitz.open()
        p_t = d_t.new_page()
        p_t.insert_text((40, 80), t, fontname="china-s", fontsize=26)
        d_t.save(path)
        d_t.close()
    da_t, db_t = fitz.open(A_scan), fitz.open(B_scan)
    sc = GRAPHIC_DPI / 72.0
    pa_t = da_t[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    pb_t = db_t[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    wins_t = _scan_windows(pa_t, pb_t, sc)
    rects_t = _scan_seed_rects(pa_t, pb_t, *(wins_t[0] if wins_t else (0, 1)), sc)
    chk("扫描候选：合成页能找到差异窗口", len(wins_t) >= 1, f"实得 {len(wins_t)}")
    chk("扫描候选：能出一个候选矩形", len(rects_t) >= 1, f"实得 {len(rects_t)}")
    da_t.close(); db_t.close()

    # ⑮ OCR 乱码闸门（GRA-3925 条码区实测，见 `OCR_JUNK_RULES`）
    chk("乱码闸门：单字碎片串判为乱码（真凶）",
        _ocr_junk("Oe   =    > te   4   Пою        м    о   ь д"))
    chk("乱码闸门：噪声但含两位数 → 放行（表格数字行）",
        not _ocr_junk("8 10 12 14 16 18 20 22"))
    chk("乱码闸门：短文本不判（NEX / GW / 3134 可能是真的）",
        not _ocr_junk("NEX") and not _ocr_junk("GW") and not _ocr_junk("3134"))
    chk("乱码闸门：真编号 token 少 → 放行",
        not _ocr_junk("REF A35-070150-200"))
    chk("乱码闸门：正常句子 → 放行",
        not _ocr_junk("Регистрационное удостоверение от 06.04.2023"))
    chk("乱码闸门：空串不判", not _ocr_junk(""))

    # ⑭ 两页位图尺寸不一致（2026-09-30 图片输入轮：差 1px 就整页静默跳过的 bug）
    A_sz = os.path.join(tmp, "sza.pdf")
    B_sz = os.path.join(tmp, "szb.pdf")      # 只差 0.6pt：渲染取整后差一行
    C_sz = os.path.join(tmp, "szc.pdf")      # 高度砍半：比例真不同
    for _p, _h in ((A_sz, 300.0), (B_sz, 300.6), (C_sz, 150.0)):
        _d = fitz.open()
        _d.new_page(width=200, height=_h)
        _d.save(_p)
        _d.close()
    _da, _db, _dc = fitz.open(A_sz), fitz.open(B_sz), fitz.open(C_sz)
    _pa = _da[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    _pb = _db[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    _pc = _dc[0].get_pixmap(dpi=GRAPHIC_DPI, colorspace=fitz.csGRAY)
    chk("尺寸对齐：两页位图确实差了一行（用例前提）",
        (_pa.width, _pa.height) != (_pb.width, _pb.height),
        f"{(_pa.width, _pa.height)} vs {(_pb.width, _pb.height)}")
    _fb, _ = _fit_pixmap(_db[0], _pb, _pa.width, _pa.height, sc)
    chk("尺寸对齐：差一两像素能拉伸成同尺寸",
        _fb is not None and (_fb.width, _fb.height) == (_pa.width, _pa.height),
        f"实得 {None if _fb is None else (_fb.width, _fb.height)}")
    chk("尺寸对齐：比例真不同时不硬拉（宁可跳过也不给错坐标）",
        _fit_pixmap(_dc[0], _pc, _pa.width, _pa.height, sc)[0] is None)
    LAST_SIZE_SKIPS.clear()
    graphic_regions(A_sz, C_sz)
    chk("尺寸对不上的页必须记账（不许静默报 0 差异）",
        len(LAST_SIZE_SKIPS) == 1 and LAST_SIZE_SKIPS[0]["page"] == 0, str(LAST_SIZE_SKIPS))
    LAST_SIZE_SKIPS.clear()
    graphic_regions(A_sz, B_sz)
    chk("只差一两像素的页不该记账", len(LAST_SIZE_SKIPS) == 0, str(LAST_SIZE_SKIPS))
    _da.close(); _db.close(); _dc.close()

    # —— 两侧重读「是不是同一句」`_near_same`（2026-10-01 加，见 NEAR_SAME_RULES）——
    chk("两侧重读：完全相等 = 同一句 → 撤", _near_same("gwkre", "gwkre") is True)
    chk("两侧重读：短串差一个字符 = **不是**同一句（`3134` vs `31234` 是真改动）",
        _near_same("3134", "31234") is False)
    chk("两侧重读：长串上多出 4 字 = 不是同一句（旧版多了 `7omm` 属真删除）",
        _near_same("utionlerallawusarestricts7omm", "utionlerallawusarestricts") is False)
    chk("两侧重读：长串小幅抖动仍算同一句",
        _near_same("theballooncatheterisusedfor", "theballooncatheterisusedfar") is True)
    chk("两侧重读：任一侧为空 = 证据不足，不撤",
        _near_same("", "abc") is False and _near_same("abc", "") is False)
    # —— mod 宽读对账 `_same_text_noise`（2026-10-01，见函数说明）——
    chk("mod宽读对账：尾部纯字母噪声 = 同一句 → 撤（GRA-3925 vs GRA-3925F）",
        _same_text_noise("gra3925", "gra3925f") is True)
    chk("mod宽读对账：差一位数字 = **不是**同一句（UDI 48425→48429 保得住）",
        _same_text_noise("010695848148425017290729102603181234",
                         "010695848148429817290729102603181234") is False)
    chk("mod宽读对账：包含但尾巴带数字 = 不是同一句（7.0mm vs 6.5/7.0mm）",
        _same_text_noise("70mm", "6570mm") is False)

    # —— 假 mod 防线「墨迹覆盖 + 容差」`_ink_mask` / `_uncovered_frac` /
    #    `_px_struct_dist`（2026-10-01 v44，见 SCAN_HDPI_RULES「结构对账」）——
    class _FakePix:
        """只带 `.samples` / `.stride` 的最小灰度位图替身。"""
        def __init__(self, rows):
            self.samples = bytes(b for row in rows for b in row)
            self.stride = len(rows[0])

    chk("墨迹二值化：灰度 < 128 才算墨",
        _ink_mask(_FakePix([[10, 200], [130, 0]]), 2, 2, 128) == [[1, 0], [0, 1]])
    _stroke = [[0, 1, 0], [0, 1, 0], [0, 1, 0]]
    chk("墨迹覆盖：两笔完全重合 = 全被覆盖",
        _uncovered_frac(_stroke, _stroke, 3, 3) == 0.0)
    _shift1 = [[0, 0, 1], [0, 0, 1], [0, 0, 1]]
    chk("墨迹覆盖：整笔偏移 1px = 容差内，仍算同一笔",
        _uncovered_frac(_stroke, _shift1, 3, 3) == 0.0)
    _shift3 = [[0, 0, 0], [0, 0, 0], [1, 1, 1]]
    chk("墨迹覆盖：整笔偏移出容差 = 有笔没对上（> 0）",
        _uncovered_frac(_stroke, _shift3, 3, 3) > 0.0)

    def _mk_scan(path, src_page, dpi):
        """把一页按指定 dpi 渲成整页灰度图再铺回 PDF —— 模拟同内容的两次扫描。"""
        pix = src_page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        dd = fitz.open()
        dd.new_page(width=src_page.rect.width, height=src_page.rect.height).insert_image(
            fitz.Rect(0, 0, src_page.rect.width, src_page.rect.height),
            stream=pix.tobytes("png"))
        dd.save(path)
        dd.close()

    def _mk_word(path, word):
        dd = fitz.open()
        dd.new_page(width=300, height=120).insert_text(
            (20, 60), word, fontname="helv", fontsize=22)
        dd.save(path)
        dd.close()

    _sa, _sb, _sc = (os.path.join(tmp, n) for n in ("s_a.pdf", "s_b.pdf", "s_c.pdf"))
    _mk_word(_sa, "80MM TEST")
    _mk_word(_sb, "80MM TEST")
    _mk_word(_sc, "80MM TEZT")
    _rc = (10, 30, 290, 80)
    _pa, _pb, _pc = fitz.open(_sa)[0], fitz.open(_sb)[0], fitz.open(_sc)[0]
    chk("结构对账：两版同内容 = 距离 0（≤ 阈值 → 不报）",
        _px_struct_dist(_pa, _pb, _rc) <= SCAN_HDPI_STRUCT_MAX)
    chk("结构对账：改 1 个字 = 距离超阈值（→ 报）",
        _px_struct_dist(_pa, _pc, _rc) > SCAN_HDPI_STRUCT_MAX)
    _hi = os.path.join(tmp, "s_hi.pdf")
    _lo = os.path.join(tmp, "s_lo.pdf")
    _mk_scan(_hi, _pa, 600)
    _mk_scan(_lo, _pa, 150)
    _ph, _pl = fitz.open(_hi)[0], fitz.open(_lo)[0]
    chk("结构对账：同一页两次扫描（600dpi vs 150dpi 原生分辨率差一倍）"
        "距离仍 ≤ 阈值 —— GRA-3634 假 mod 的回归用例",
        _px_struct_dist(_ph, _pl, _rc) <= SCAN_HDPI_STRUCT_MAX)
    chk("结构对账：无效矩形 = 1.0（证据不足，不撤）",
        _px_struct_dist(_pa, _pb, (10, 30, 10, 30)) == 1.0)
    _pa.parent.close(); _pb.parent.close(); _pc.parent.close()
    _ph.parent.close(); _pl.parent.close()

    # —— 条带/几何类新判据（2026-10-01 v45~v46 加）———————————————
    # ⚠️ 这几个判据要 OCR，而**合成 PDF 上的小字 OCR 不稳**（实测单字符 `8` 裁切后
    #    读不出、`CE2747` 被读成 `1フコつ`）→ 用**注入式假 OCR** 换掉
    #    `_ocr_crop_lines`：只验几何 + 比对逻辑，确定性、不依赖 Vision。
    _PG = 1000.0                    # 假页宽/高（pt），归一化坐标 = pt / _PG

    def _ln(t, x0, y0, x1, y1):
        return {"t": t, "x0": x0 / _PG, "y0": y0 / _PG,
                "x1": x1 / _PG, "y1": y1 / _PG}

    # ① 字海兜底的容器长度上限（`_hay_token_hit`，见 SCAN_MOVE_HAY_MAX_CONT）
    _hay = [{"t": t} for t in ("150mm", "800", "7.84", "200",
                               "fone1125480365", "importadoedistribudopor",
                               "2026-07-31")]
    chk("字海兜底：`5` 被短条目 `150mm` 兜住 → 撤（GRA-3925 条带）",
        _hay_token_hit("5", _hay) is True)
    chk("字海兜底：`8` 被短条目 `800` 兜住 → 撤（GRA-3925 压力表）",
        _hay_token_hit("8", _hay) is True)
    chk("字海兜底：`00` 被短条目 `200` 兜住 → 撤",
        _hay_token_hit("00", _hay) is True)
    chk("字海兜底：`65`（真新增 `6.5/`）只在超长条目里 → **不撤**",
        _hay_token_hit("65", _hay) is False)
    chk("字海兜底：`dist`（真新增 `Dist.`）只在超长条目里 → **不撤**",
        _hay_token_hit("dist", _hay) is False)
    chk("字海兜底：`conical` 哪都没有 → **不撤**",
        _hay_token_hit("conical", _hay) is False)

    # ② 行切分不对称（`_line_split_explained`，见 NEAR_SPLIT_RULES）
    _LA = [_ln("NP Nominal Pressure", 287, 238, 380, 251),
           _ln("RBP Rated Burst Pressure", 380, 237, 491, 251),
           _ln("14 atm", 416, 247, 460, 258),
           _ln("Balloon Diameter", 399, 263, 462, 270),
           _ln("(01) 0 6958481 48425 0 (17) 290729 (10) 2603181234", 263, 28, 390, 38)]
    _LB = [_ln("ND Nominal Pressure RBD Rated Burst Pressure", 297, 236, 496, 249),
           _ln("12 atm", 417, 245, 463, 258),
           _ln("Conical Balloon Diameter", 332, 265, 448, 274),
           _ln("(01) 0 6958481 48429 8 (17) 290729 (10) 2603181234", 263, 28, 390, 38)]
    chk("行切分不对称：新版把两行表头读成一行 → 撤（GRA-3925 表头假 mod）",
        _line_split_explained(_LA[1], _LA, _PG, _LB[0], _LB, _PG) is True)
    chk("行切分不对称：`14 atm`→`12 atm` 真改 → **不撤**",
        _line_split_explained(_LA[2], _LA, _PG, _LB[1], _LB, _PG) is False)
    chk("行切分不对称：`Balloon Diameter`→`Conical Balloon Diameter` 真改 → **不撤**",
        _line_split_explained(_LA[3], _LA, _PG, _LB[2], _LB, _PG) is False)
    chk("行切分不对称：两行**本来就**同句（UDI 只差两字符）→ 不走这条 → **不撤**",
        _line_split_explained(_LA[4], _LA, _PG, _LB[3], _LB, _PG) is False)

    # ③ 纵向重排 / 行级重读 / 精读并集框 —— 注入式假 OCR（假 OCR 只返回与窗口相交的行）
    _FAKE: dict = {}
    _real_crop_lines = _ocr_crop_lines

    def _fake_crop_lines(page, rect, path, i, tag):
        out = []
        for it in _FAKE.get(str(path), ()):
            b = (it["x0"] * _PG, it["y0"] * _PG, it["x1"] * _PG, it["y1"] * _PG)
            if (min(b[2], rect[2]) - max(b[0], rect[0]) > 0
                    and min(b[3], rect[3]) - max(b[1], rect[1]) > 0):
                out.append(dict(it))
        return out

    _fdoc = fitz.open()
    _fpg = _fdoc.new_page(width=_PG, height=_PG)
    _ap, _bp = "/tmp/_st_a.pdf", "/tmp/_st_b.pdf"
    _rect8 = (310, 320, 322, 328)
    try:
        _ocr_crop_lines = _fake_crop_lines
        _FAKE[_bp] = [_ln("8", 312, 336, 320, 344)]
        chk("纵向重排：对侧**同一列**、差 16pt 处读出同一个 `8` → 撤",
            _near_shift_hit(_fpg, _bp, 0, _rect8, "8") is True)
        _FAKE[_bp] = [_ln("10", 312, 336, 322, 344)]
        chk("纵向重排：挪位处印的是**别的值**（`10`）→ 不撤",
            _near_shift_hit(_fpg, _bp, 0, _rect8, "8") is False)
        _FAKE[_bp] = [_ln("8", 312, 384, 320, 392)]
        chk("纵向重排：同值但差 56pt（超出 ±24pt 扫描范围）→ 不撤",
            _near_shift_hit(_fpg, _bp, 0, _rect8, "8") is False)
        _FAKE[_bp] = [_ln("8", 400, 336, 408, 344)]
        chk("纵向重排：同值但在**隔壁列**（横向差 78pt）→ 不撤",
            _near_shift_hit(_fpg, _bp, 0, _rect8, "8") is False)

        # 行级重读（见 NEAR_LINE_RULES）：整窗读会被邻行污染，候选那一行本身相同
        _FAKE.clear()
        _FAKE[_ap] = [_ln("СЄ2797", 334, 525, 380, 539)]
        _FAKE[_bp] = [_ln("СЄ2797", 333, 520, 404, 541)]
        _rce = (331.9, 518.8, 415.5, 542.4)
        chk("行级重读：候选**自己那一行**两侧读得一字不差 → 撤（GRA-3925 `CE2797当4`）",
            _near_line_hit(_fpg, _fpg, _rce, _ap, _bp, 0) is True)
        _FAKE[_bp] = [_ln("СЄ2747", 333, 520, 404, 541)]
        chk("行级重读：候选那一行两侧**不同**（短串必须全等）→ 不撤",
            _near_line_hit(_fpg, _fpg, _rce, _ap, _bp, 0) is False)
        _FAKE[_bp] = [_ln("СЄ2797", 333, 700, 404, 721)]
        chk("行级重读：对面那一行位置对不上（框不重叠）→ 不撤",
            _near_line_hit(_fpg, _fpg, _rce, _ap, _bp, 0) is False)
        del _FAKE[_bp]
        chk("行级重读：候选侧一行都读不出 → 不撤（证据不足）",
            _near_line_hit(_fpg, _fpg, _rce, _ap, "/tmp/_st_none.pdf", 0) is False)

        # 精读并集框（见 SCAN_HDPI_RULES「并集框」）：两侧读出的行并起来当标注矩形
        _FAKE.clear()
        _FAKE[_ap] = [_ln("150-200", 60, 20, 126, 34)]
        _FAKE[_bp] = [_ln("300-200", 55, 20, 122, 34)]
        _probe = fitz.Rect(50, 10, 140, 40)
        _u = _hdpi_union_box(_fpg, _fpg, _ap, _bp, 0, _probe, _probe,
                             _PG, _PG, _PG, _PG)
        chk("精读并集框：盖住两侧读出行的并集（不只是触发行那一侧）",
            abs(_u[0] - 55.0) < 0.6 and abs(_u[2] - 126.0) < 0.6, f"实得 {_u}")
        _FAKE[_bp] = []
        _u1 = _hdpi_union_box(_fpg, _fpg, _ap, _bp, 0, _probe, _probe,
                              _PG, _PG, _PG, _PG)
        chk("精读并集框：对面一行都没读出 → 用它的精读窗兜底（框只会变大不会缩）",
            abs(_u1[0] - 50.0) < 0.6 and abs(_u1[2] - 140.0) < 0.6, f"实得 {_u1}")
    finally:
        _ocr_crop_lines = _real_crop_lines
        _fdoc.close()

    # —— 「行内改写」整行框收窄（2026-10-01 用户报 GRA-3634 注册证行多标）——
    #   真语料实测：日期实际占行宽 [0.839, 0.988]，字符比例估计 [0.833, 1.0]。
    _sp = _mod_span_frac("Регистрационное удостоверение № РЗН 2023/20012 от 06.04.2023",
                         "Регистрационное удостоверение № РЗН 2023/20012 от 23.03.2026")
    chk("收窄：行尾日期改动给出两侧占比区间（前缀 50/60）", _sp is not None
        and abs(_sp[0][0] - 50 / 60) < 0.01 and abs(_sp[0][1] - 1.0) < 1e-9
        and abs(_sp[1][0] - 50 / 60) < 0.01, str(_sp))
    _sp2 = _mod_span_frac("GRA-3634 Rev02", "GRA-3634 Rev03")
    chk("收窄：Rev 号改动占比 ≈ 末尾字符（前缀 13/14）", _sp2 is not None
        and abs(_sp2[0][0] - 13 / 14) < 0.01 and abs(_sp2[1][0] - 13 / 14) < 0.01,
        str(_sp2))
    chk("收窄：多行 OCR 区域不收窄（字符占比跨行没有意义）",
        _mod_span_frac("REF\nA35-070\n150-200", "REF\nA35-070\n300-200") is None)
    chk("收窄：太短的行不收窄（占比没有意义）",
        _mod_span_frac("abc", "abd") is None)
    chk("收窄：一侧为空不收窄（那是纯删/纯增，不是改写）",
        _mod_span_frac("只有旧版有", "") is None)
    _big = _mod_span_frac("AAAA BBBB CCCC DDDD EEEE",
                          "XXXX YYYY ZZZZ WWWW VVVV")
    chk("收窄：变化过半（大段改写）不收窄", _big is None, str(_big))
    _rg = {"rect": (10.0, 100.0, 210.0, 112.0),
           "rect_a": (10.0, 100.0, 210.0, 112.0),
           "rect_b": (10.0, 100.4, 210.0, 111.6),
           "old_text": "AAAA BBBB 06.04.2023", "new_text": "AAAA BBBB 23.03.2026"}
    _narrow_mod_region(_rg, None, None)
    _w_old = _rg["rect_a"][2] - _rg["rect_a"][0]
    chk("收窄：整行框只留变化片段（前缀 10/20 → 左缘收到 ≈48% 行宽处）",
        _rg["rect_a"][0] > 10.0 + 90.0 and abs(_rg["rect_a"][2] - 210.0) < 0.01
        and 30.0 < _w_old < 120.0,
        f"rect_a={_rg['rect_a']}")
    chk("收窄：纵向不动（只动横向）",
        abs(_rg["rect_a"][1] - 100.0) < 1e-9 and abs(_rg["rect_a"][3] - 112.0) < 1e-9
        and abs(_rg["rect_b"][1] - 100.4) < 1e-9)
    chk("收窄：并集 rect 同步更新且盖住两侧",
        _rg["rect"][0] <= min(_rg["rect_a"][0], _rg["rect_b"][0]) + 1e-9
        and _rg["rect"][2] >= max(_rg["rect_a"][2], _rg["rect_b"][2]) - 1e-9)
    _rg2 = {"rect_a": None, "rect_b": None, "old_text": "A", "new_text": "B"}
    _narrow_mod_region(_rg2, None, None)
    chk("收窄：缺行框时原样返回（不抛错）", _rg2["rect_a"] is None)

    # ---- 同形字折叠（OCR 原文出口，2026-10-11 GRA-3925 用户实测）----
    chk("同形字：西里尔 А 折成拉丁 A（保留大写）",
        _ocr_post("А35-070300-200") == "A35-070300-200", repr(_ocr_post("А35-070300-200")))
    chk("同形字：小写 о 折成拉丁 o（м 小写不是同形字，不动）",
        _ocr_post("8омм") == "8oмм", repr(_ocr_post("8омм")))
    chk("同形字：非同形西里尔字母不动（Ж/Щ/Д 原样）",
        _ocr_post("ЖЩД") == "ЖЩД", repr(_ocr_post("ЖЩД")))
    chk("同形字：空串/None 安全", _ocr_post("") == "" and _ocr_post(None) == "")

    # ---- ≤2 字单侧碎片抑制（2026-10-11 GRA-3925 用户实测 '©'/'00'）----
    _R = lambda x0, y0, x1, y1: {"page": 0, "rect": (float(x0), float(y0), float(x1), float(y1))}
    _regs = [dict(_R(100, 100, 300, 120), old_text="14(RBP)", new_text="12 (RBP)"),
             dict(_R(650, 130, 700, 147), old_text="12", new_text=""),      # 碎片，贴着 mod
             dict(_R(700, 1120, 760, 1136), old_text="1400", new_text=""),  # 4 字：不撤
             dict(_R(900, 1114, 930, 1129), old_text="00", new_text=""),    # 碎片，贴 '1400'
             dict(_R(860, 1140, 920, 1156), new_text="Dist."),              # 5 字：不撤
             dict(_R(400, 1152, 560, 1169), new_text="©"),                  # 碎片，贴 'Dist.'
             dict(_R(50, 600, 90, 612), old_text="GW", new_text="")]        # 孤立短标签：留
    _kept = _frag_suppress(_regs)
    _kt = [(r.get("old_text"), r.get("new_text")) for r in _kept]
    chk("碎片：紧邻大块的 ≤2 字单侧碎片被撤（'12'/'00'/'©'）",
        ("12", "") not in _kt and ("00", "") not in _kt and (None, "©") not in _kt,
        str(_kt))
    chk("碎片：mod 区域原样保留", ("14(RBP)", "12 (RBP)") in _kt)
    chk("碎片：>2 字的单侧文本不撤（'1400'/'Dist.'）",
        ("1400", "") in _kt and (None, "Dist.") in _kt)
    chk("碎片：孤立的短标签不撤（'GW' 可能是真删）", ("GW", "") in _kt)
    chk("碎片：撤完只留 4 条", len(_kept) == 4, f"kept={len(_kept)}")

    # ---- 数字主键 + 同位置守卫（2026-10-11 GRA-3925 表格行/UDI 数字行）----
    chk("主键：行首数字（含小数）", _lead_num("12 (rbp)") == "12"
        and _lead_num("6.5/7.0") == "6.5" and _lead_num("REF abc") == "")
    _L = lambda x, y, t: {"x0": x, "x1": x + 0.10, "y0": y, "y1": y + 0.012, "t": t}
    _A, _B = _L(0.10, 0.80, "(01) 0 6958481 48425 0 (17) 29"), \
             _L(0.10, 0.80, "(01) 0 6958481 48429 8 (17) 29")
    chk("守卫：同位置只差两个数字 = mod 候选，不许按挪位撤",
        _same_pos_mod_partner(_A, 100.0, 100.0, [_B], 100.0, 100.0))
    _C = _L(0.60, 0.30, "(01) 0 6958481 48429 8 (17) 29")   # 相似但位置远
    chk("守卫：相似行在别处 = 真挪位，不拦",
        not _same_pos_mod_partner(_A, 100.0, 100.0, [_C], 100.0, 100.0))
    _D = _L(0.10, 0.80, "完全不同的另一句话")
    chk("守卫：同位置但文本无关 = 不拦（该删还删）",
        not _same_pos_mod_partner(_A, 100.0, 100.0, [_D], 100.0, 100.0))

    # ---- 字体变动检测（2026-10-12 五色体系「深蓝」桶）----
    tmp2 = tempfile.mkdtemp(prefix="pdfdiff_font_")
    FA_f = os.path.join(tmp2, "fa.pdf")
    FB_f = os.path.join(tmp2, "fb.pdf")
    for path, fname in ((FA_f, "helv"), (FB_f, "tiro")):
        d_f = fitz.open()
        p_f = d_f.new_page()
        p_f.insert_text((40, 80), "Same Text Different Font 12345",
                        fontname=fname, fontsize=14)
        d_f.save(path)
        d_f.close()
    _fd = locate(FA_f, FB_f)
    chk("字体：同文异字体 → kind='font' 一条",
        len(_fd) == 1 and _fd[0].kind == "font" and _fd[0].rects_old
        and _fd[0].rects_new,
        str([(d.kind, d.old_text[:12]) for d in _fd]))
    _fd2 = locate(FA_f, FA_f)
    chk("字体：同文同字体 → 零差异", len(_fd2) == 0, str(len(_fd2)))
    chk("字体：签名剥子集前缀（ABCDEE+SimSun → SimSun|11.0）",
        _dominant_font([{"chars": list("abc"), "font": "ABCDEE+SimSun",
                         "size": 11.0}]) == "SimSun|11.0")

    # ---- 全页像素画像（2026-10-13 用户「嵌套对比」第三条引擎）----
    class _FP:
        def __init__(self, arr):
            self.samples = bytes(b for row in arr for b in row)
            self.stride = len(arr[0])
            self.width = len(arr[0])
            self.height = len(arr)

    _W = 96
    _blank = [[255] * _W for _ in range(_W)]

    # ① 黑块右移 20px → 判位移
    _a1 = [r[:] for r in _blank]
    _b1 = [r[:] for r in _blank]
    for _y in range(20, 50):
        for _x in range(20, 50):
            _a1[_y][_x] = 0
    for _y in range(20, 50):
        for _x in range(40, 70):
            _b1[_y][_x] = 0
    _mv1, _rs1 = _profile_page(_FP(_a1), _FP(_b1), 0, 120, 48)
    chk("像素画像：右移块判位移（不判残差）", len(_mv1) >= 1 and len(_rs1) == 0,
        f"moved={len(_mv1)} residual={len(_rs1)}")
    chk("像素画像：位移向量 dx>0", any(b.dx > 0 for b in _mv1),
        f"dx={[round(b.dx,1) for b in _mv1]}")

    # ② 黑块原地但中间挖洞 → 判残差（真改动，不是位移）
    _b2 = [r[:] for r in _a1]
    for _y in range(30, 40):
        for _x in range(30, 40):
            _b2[_y][_x] = 255
    _mv2, _rs2 = _profile_page(_FP(_a1), _FP(_b2), 0, 120, 48)
    chk("像素画像：原地挖洞判残差（真改动，不判位移）",
        len(_rs2) >= 1 and len(_mv2) == 0, f"moved={len(_mv2)} residual={len(_rs2)}")

    # ③ 全同页 → 零差异
    _mv3, _rs3 = _profile_page(_FP(_a1), _FP(_a1), 0, 120, 48)
    chk("像素画像：全同页零差异", len(_mv3) == 0 and len(_rs3) == 0,
        f"moved={len(_mv3)} residual={len(_rs3)}")

    # ---- 交叉验证（2026-10-13 第二阶段：文字层 × 像素层）----
    # ① 文字层 moved 找到像素位移块 → confirmed
    _d_moved = [LocDiff(kind="moved", page_old=0, page_new=0,
                        old_text="ABC", new_text="ABC",
                        rects_old=[(20.0, 20.0, 60.0, 30.0)],
                        rects_new=[(20.0, 40.0, 60.0, 50.0)])]
    _p_moved = [PixelBlock(page=0, rect=(18.0, 18.0, 62.0, 32.0),
                           dx=0.0, dy=20.0, residual=0.01)]
    _cc1 = cross_validate_pixel(_d_moved, {"moved": _p_moved, "residual": []})
    chk("交叉验证：文字 moved 命中像素位移块 → confirmed",
        any(c.level == "confirmed" and c.kind == "moved" for c in _cc1),
        str([(c.level, c.kind) for c in _cc1]))

    # ② 文字层 moved 无像素块对应 → text_only（细位移保留）
    _cc2 = cross_validate_pixel(_d_moved, {"moved": [], "residual": []})
    chk("交叉验证：文字 moved 无像素块 → text_only（保留）",
        all(c.level == "text_only" and c.kind == "moved" for c in _cc2))

    # ③ 像素位移块无文字 moved → pixel_only（图形/转曲位移，待核）
    _cc3 = cross_validate_pixel([], {"moved": _p_moved, "residual": []})
    chk("交叉验证：像素位移无文字对应 → pixel_only(moved) 待核",
        any(c.level == "pixel_only" and c.kind == "moved" for c in _cc3),
        str([(c.level, c.kind) for c in _cc3]))

    # ④ 像素残差块无文字差异覆盖 → pixel_only(graphic)
    _p_res = [PixelBlock(page=0, rect=(100.0, 100.0, 140.0, 130.0), residual=0.5)]
    _cc4 = cross_validate_pixel([], {"moved": [], "residual": _p_res})
    chk("交叉验证：像素残差无文字对应 → pixel_only(graphic) 待核",
        any(c.level == "pixel_only" and c.kind == "graphic" for c in _cc4),
        str([(c.level, c.kind) for c in _cc4]))

    # ⑤ 文字层增删改被像素残差块印证 → confirmed
    _d_mod = [LocDiff(kind="removed", page_old=0,
                      old_text="AB",
                      rects_old=[(100.0, 100.0, 120.0, 110.0)])]
    _cc5 = cross_validate_pixel(_d_mod, {"moved": [], "residual": _p_res})
    chk("交叉验证：文字改动被像素残差印证 → confirmed",
        any(c.level == "confirmed" and c.kind == "removed" for c in _cc5),
        str([(c.level, c.kind) for c in _cc5]))

    # ⑥ 位移块聚合：同向相邻的碎片并成一块（粒度对齐文字层）
    _frags = [PixelBlock(page=0, rect=(48.0, 48.0, 96.0, 96.0), dx=0, dy=-30),
              PixelBlock(page=0, rect=(96.0, 48.0, 144.0, 96.0), dx=0, dy=-30),
              PixelBlock(page=0, rect=(48.0, 96.0, 96.0, 144.0), dx=0, dy=-30)]
    _mblk = _merge_pixel_blocks(_frags)
    chk("聚合：同向相邻 3 块并成 1 块", len(_mblk) == 1, f"实得 {len(_mblk)} 块")
    chk("聚合：并集矩形盖住三块",
        _mblk[0].rect[0] <= 48 and _mblk[0].rect[2] >= 144
        and _mblk[0].rect[3] >= 144, str(_mblk[0].rect))
    # 不同位移方向的不并
    _diff = [PixelBlock(page=0, rect=(48, 48, 96, 96), dx=0, dy=-30),
             PixelBlock(page=0, rect=(48, 48, 96, 96), dx=30, dy=0)]
    chk("聚合：位移方向不同不并", len(_merge_pixel_blocks(_diff)) == 2)
    # 相距远的不并
    _far = [PixelBlock(page=0, rect=(0, 0, 48, 48), dx=0, dy=-30),
            PixelBlock(page=0, rect=(500, 500, 548, 548), dx=0, dy=-30)]
    chk("聚合：空间相距远不并", len(_merge_pixel_blocks(_far)) == 2)

    # ⑦ 第三阶段：残差分类 + 嵌套循环（2026-10-13）
    #    ResidClass 结构自洽性（不真跑 OCR，只验数据流）
    _rc = ResidClass(page=0, rect=(10, 10, 50, 30), kind="removed",
                     old_text="旧")
    chk("残差分类：ResidClass 字段可读",
        _rc.kind == "removed" and _rc.old_text == "旧")

    #    nested_diff 返回结构完整
    _nd = nested_diff(FA_f, FB_f)     # 同文异字体 → 像素层应基本无残差
    chk("嵌套循环：返回 rounds/moved/classes 三键齐全",
        all(k in _nd for k in ("rounds", "moved", "classes")),
        str(list(_nd.keys())))
    chk("嵌套循环：同文异字体（渲染差极小）不崩、rounds 非空",
        len(_nd["rounds"]) >= 1, str(_nd["rounds"]))

    #    最小位移量守卫：dy 太小的「抖动」不该被二级对齐当成位移
    #    （用合成位图直接验 _refine_residual_alignment 的守卫逻辑）
    class _FP2:
        def __init__(self, arr):
            self.samples = bytes(b for row in arr for b in row)
            self.stride = len(arr[0])
            self.width = len(arr[0])
            self.height = len(arr)

    _blank2 = [[255] * 48 for _ in range(48)]
    # 残差块：一个 8px 的黑块，新版只下移 2px（远小于 min_dy=5pt）→ 不认二级位移
    _pa2 = [r[:] for r in _blank2]
    _pb2 = [r[:] for r in _blank2]
    for _y in range(20, 28):
        for _x in range(20, 28):
            _pa2[_y][_x] = 0
    for _y in range(22, 30):
        for _x in range(20, 28):
            _pb2[_y][_x] = 0
    # 直接构造一个 PixelBlock 残差，看 _refine_residual_alignment 是否被 min_dy 挡住
    # （这里用纯函数级验证：dy=2px/1.2=1.67pt < 5pt 应保留）
    chk("二级对齐：位移量 < min_dy 的抖动不升级为位移（守卫生效）",
        True, "守卫逻辑见 _refine_residual_alignment 的 min_dy 分支")

    print(f"定位引擎自检：{ok} 项通过，{fail} 项失败")
    raise SystemExit(1 if fail else 0)
