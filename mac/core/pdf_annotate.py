# -*- coding: utf-8 -*-
"""
pdf_annotate.py —— 把差异**直接画回 PDF**（离线可用，不改原文只加注释层）
==================================================================
思路：从每条差异里取出「改动片段」→ 用 PyMuPDF 在目标页上搜索这段文字 →
拿到它真正的坐标 → 在该位置画**原生高亮注释**（不是截图/覆盖图，是 PDF 注释，
Acrobat / 预览 / Edge 都能看见，也能一键删掉）。

颜色（用户 2026-09-29 定的）：
  · 旧版 PDF —— 被删掉、被改掉的地方 → **红色高亮**
  · 新版 PDF —— 新增、改后的地方     → **绿色高亮**
  · 整页新增 / 整页删除 → 整页画一圈同色边框
  · **两侧都没有文字层、无法逐字定位、但画面确实变了**的区域 → **整块同色高亮**
    （转曲 PDF 的兜底；新增块标绿、删除块标红，颜色跟所属侧走。
    用户 2026-09-29 第二次拍板：**不要橙色**，「我只需要有绿色标记」——
    第一版的橙色让用户看不懂是什么意思，已废除）

⚠️ 两个必须记住的坑：
  1. 定位靠「搜文本」，所以**扫描件（图片型 PDF）没有文字层，搜不到** → 跳过并如实回报。
  2. 改动片段可能很碎（diff 出来的 replace 块常常只有一两个字），
     太短的片段搜下去会命中满页同字（比如片段="7"），所以有最小长度门槛，
     搜不到也不硬画 —— **宁可少标，不可乱标**。
"""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:                       # 1.28 起推荐用 pymupdf，fitz 只是兼容别名
    import pymupdf as fitz
except ImportError:        # pragma: no cover
    import fitz            # type: ignore

# ==================================================================
# 颜色：**按改动性质分色**（用户 2026-10-01 定；2026-10-13 删「变动」档）
# ==================================================================
#   红 = **纯删减**：**只在**旧版这边有、新版整段没了
#   绿 = **纯新增**：**只在**新版那边出现
#
# ⚠️ 2026-10-13 删「变动」档（**口径变更，别改回去**）：
#    不再有第三档「变动/改写黄」。任何「旧值→新值」的内容改写，判定层都
#    直接拆成「删（旧值标红）+ 增（新值标绿）」两条 —— 用户明确拍板：
#    「就单纯指明旧的被删掉了标红，新的被增加了标绿」。
#    因此这里只剩红/绿两色（外加位移浅蓝、字体深蓝两个辅助色）。
#
# 高亮注释用 0~1 的 RGB；mupdf 叠色是 multiply，黑字压在色块上仍然是黑的。
# ⚠️ 2026-09-30 用户反馈「标的还不是明，不是很明显」：早先用
#    `(1.0,0.62,0.62)` / `(0.60,0.93,0.66)`，在 6pt 小字（页脚的 Rev07、日期）上
#    淡得几乎看不见 —— 整体加深一档。
# ⚠️ 2026-10-01 用户第二次反馈「颜色有点稀、两个都是黄的」：一半是预览侧
#    `set_colors` 被静默跳过（退回默认黄，见 `paint_into` 的说明），另一半是
#    原来的 `(1.0,0.45,0.45)` / `(0.35,0.86,0.45)` 本身发白发黄（绿那个 G 高、
#    R 也不低，渲染出来是黄绿）。这里把饱和度拉满一档：红 `#FF4D4D`、
#    绿 `#29C257`，白底渲染实测 ≈ `(255,88,88)` / `(45,195,90)`，一眼分得开。
RED = (1.0, 0.30, 0.30)
GREEN = (0.16, 0.76, 0.34)
#: 黄：`#FFC71A`，白底渲染 ≈ `(255,200,40)`。比纯黄（255,255,0）暗一点，
#: 压在 6pt 小字上不会把字糊掉，又明显区别于红绿。
YELLOW = (1.0, 0.78, 0.10)

# —— 五色体系（用户 2026-10-12 定）——
#   红=删减 绿=新增 黄=变动（这三个默认开）；
#   位移=浅蓝、字体变动=深蓝（这两个默认关）。
# ⚠️ 浅蓝高亮在白底上要「看得见但不压字」：饱和度压低、亮度抬高，
#    实测 ≈ (150,200,255)；深蓝要和浅蓝一眼分开，走偏靛的深色。
BLUE_LIGHT = (0.59, 0.78, 1.0)     # 浅蓝：位移
BLUE_DARK = (0.25, 0.40, 0.85)     # 深蓝：字体变动

#: 颜色桶 —— **全项目只有这一组**。判定归桶只发生在 `_kind_of()` 一处，
#: 画图只查 `COLOR_OF`，别在画的地方再判断一次（那正是「两边颜色不一样」的老病根）。
KIND_DEL = "del"          # 纯删减：只在旧版有
KIND_ADD = "add"          # 纯新增：只在新版有
KIND_MOV = "mov"          # 位移：内容没变、位置变了（2026-10-12 新增）
KIND_FONT = "font"        # 字体变动：文字相同、字体/字号变了（仅文字层可判）
# 画图顺序（字典序会乱，显式钉住）：先删后增，**蓝色两类信息密度低，垫在底下画**。
# ⚠️ 2026-10-13 删「变动」档：不再有 KIND_MOD，任何内容差异都拆成「删（红）+ 增（绿）」。
KIND_ORDER = (KIND_DEL, KIND_ADD, KIND_MOV, KIND_FONT)
# 默认开启的桶（用户 2026-10-13 拍板：删/增/字体默认显示，仅位移默认关）。
KIND_DEFAULT_ON = (KIND_DEL, KIND_ADD, KIND_FONT)
COLOR_OF = {KIND_DEL: RED, KIND_ADD: GREEN,
            KIND_MOV: BLUE_LIGHT, KIND_FONT: BLUE_DARK}
# 颜色桶的中文名：界面图例、状态行统计都直接查这张表。
KIND_TEXT = {KIND_DEL: "删减", KIND_ADD: "新增",
             KIND_MOV: "位移", KIND_FONT: "字体"}


def _kind_of(it, side: str = "") -> str:
    """把一条差异归到颜色桶里 —— **唯一的归桶入口**。

    判据只有一句话：**「纯」就是只出现在一侧**。

    · `removed` / `page_removed` → 删减（红）：旧版有、新版没了
    · `added` / `page_added`     → 新增（绿）：只在新增的那一版里出现
    · `moved`                    → 位移（浅蓝）
    · `font`                     → 字体（深蓝）

    ⚠️ 2026-10-13 删「变动」档：不再有「修改/变动」桶，任何内容差异在判定层
       已经拆成「删 + 增」两条，这里不会再见到 `modified` 条目。
    """
    k = (getattr(it, "kind", "") or "").lower()
    if k in ("removed", "page_removed"):
        return KIND_DEL
    if k in ("added", "page_added"):
        return KIND_ADD
    if k == "moved":
        return KIND_MOV
    if k == "font":
        return KIND_FONT
    return KIND_DEL

MIN_FRAG = 2          # 去掉空白后至少 2 个字符才值得搜（1 个字会满页命中）
MAX_FRAG = 90         # 片段太长搜不到（PDF 里往往是分段的），切开来搜
_MIN_SUB = 4          # 切分后子片段的最小长度
PAD = 2               # 改动块向两侧扩几个字（见 _fragments 的说明）


def _stamp() -> str:
    """导出文件名用的时间戳（本地时间 `MMDD-HHMMSS`）。

    用户 2026-09-29 定的：标注版导出名必须带时间戳。
    此前导出名固定成 `…_【旧版·标红】.pdf`，重复导出互相覆盖、iCloud 还会累积
    一堆同名副本 —— 用户点开的是几小时前的旧产物，看到「每次导出来都长得一模一样」，
    其实代码早就改了。带上时刻，一眼就能看出这份是刚生成的还是过期的。
    """
    import time
    return time.strftime("%m%d-%H%M%S")


def _fragments(old: str, new: str) -> Tuple[List[str], List[str]]:
    """从一对旧/新文本里挑出「改动的片段」。

    返回 (旧侧片段列表, 新侧片段列表)：
      · delete  → 旧侧片段（新版没了）
      · insert  → 新侧片段（新版多出来的）
      · replace → 两侧都出（旧版原样 / 新版改成什么）

    ⚠️ 片段要**向两侧各扩 PAD 个字**再拿去搜。diff 出来的碎片常常只有一个字
    （150→180 只会切出「5」→「8」），单字既过不了最小长度门槛，搜下去还会满页命中。
    扩两个字之后就是完整的「150」/「180」，标出来也更像人话。
    """
    sm = SequenceMatcher(None, old or "", new or "", autojunk=False)
    fo: List[str] = []
    fn: List[str] = []
    o, n = old or "", new or ""
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        if op in ("delete", "replace"):
            fo.append(o[max(0, i1 - PAD): min(len(o), i2 + PAD)])
        if op in ("insert", "replace"):
            fn.append(n[max(0, j1 - PAD): min(len(n), j2 + PAD)])
    return fo, fn


def _clean(frags: Sequence[str]) -> List[str]:
    """把片段洗成能拿去搜的形状：去空白、切短、丢太碎的。"""
    out: List[str] = []
    for f in frags:
        f = (f or "").strip()
        if not f:
            continue
        f = re.sub(r"\s+", " ", f)
        if len(f) <= MAX_FRAG:
            if len(f.strip()) >= MIN_FRAG:
                out.append(f)
            continue
        # 太长：按标点/空格切成小块再搜（PDF 里文本常是分段的，整段搜必失败）
        for piece in re.split(r"[,，、;；:：\s]+", f):
            piece = piece.strip()
            if len(piece) >= _MIN_SUB:
                out.append(piece[:MAX_FRAG])
    # 去重、保持顺序
    seen, uniq = set(), []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


MAX_HITS = 6        # 一个片段在同一处搜出这么多矩形 = 不是唯一标识，宁可不标


def _clip_for(it, kind: str, page) -> Optional[Any]:
    """把搜索裁剪到「改动块所在的那几行」内。

    两个必须处理的事：
      1. **坐标系不同**：pdfminer 原点在左下、PyMuPDF 原点在左上 → y 要用页高翻转。
      2. **不裁剪就会乱标**：整页搜「广东博迈医疗科技股份有限公司」这种名字，
         页面里出现几次就标几处，全是无辜的 —— 坐标框住那几行才准。
    """
    ytop = it.y if kind == "old" else it.ny
    ybot = it.y2 if kind == "old" else it.ny2
    if ytop is None and ybot is None:
        return None
    h = float(page.rect.height)
    a = h - (ytop if ytop is not None else ybot)
    b = h - (ybot if ybot is not None else ytop)
    if b < a:
        a, b = b, a
    return fitz.Rect(0, max(0.0, a - 3), float(page.rect.width), min(h, b + 3))


def _search_rects(page, frag: str, clip=None) -> List[Any]:
    """在页里（限定的区域内）搜一段文字，返回矩形列表。

    ⚠️ 两个护栏，都是被真实文件打出来的（2026-09-29 GRA-4118）：
      1. **绝不「搜不到就砍短再试」**。差异块常是跨行拼出来的伪文本，PDF 里
         压根没有这段连续文字；砍短之后就会变成「公司」「批号」「标识」这种
         满页都是的碎片，标到一堆无辜文字上 —— 宁可少标，不可乱标。
      2. 命中数 > MAX_HITS = 这段文字不足以定位改动在哪 → 放弃。
    """
    try:
        rects = page.search_for(frag, clip=clip) if clip else page.search_for(frag)
    except Exception:
        return []
    if not rects or len(rects) > MAX_HITS:
        return []
    return rects


# ⚠️ 2026-10-01 修：只判 `r.x0 <= prev.x1 + 2` 是**半个条件**，两头都得看。
#   少了「右端也要搭上」那一半，同一 y 带里**隔着 227pt 的两个框**也会被并起来
#   —— GRA-3925 的 `NEX`(159~190) 与 `3134`(417~435) 被并成一个 275pt 宽的框，
#   正好横压在一维码上：用户看到的是「一条莫名其妙的长横条」，完全对不上改动。
MERGE_RECT_RULES = [
    # 名称, 纵向条件, 横向条件（**两条都要成立**）, 处理
    ("同一行", "两个框 y0 相差 < 3pt", "无", "继续往下看横向"),
    ("横向真相邻/重叠", "同上", "r.x0 ≤ prev.x1 + 2 **且** r.x1 ≥ prev.x0 - 2", "并成一个"),
    ("横向隔开了", "同上", "任一条件不成立（中间有空隙）", "**不并**，各画各的"),
]


def _merge_rects(rects: Sequence[Any]) -> List[Any]:
    """相邻/重叠的矩形并成一个 —— 同一处被标两遍会叠色，越叠越深看着像另一种颜色。"""
    out: List[Any] = []
    for r in sorted(rects, key=lambda x: (round(x.y0, 1), x.x0)):
        if out and abs(out[-1].y0 - r.y0) < 3:
            prev = out[-1]
            # 横向必须真的搭上（见 MERGE_RECT_RULES）：只判左端会让「隔得很远、
            # 但起点在前面的框右边」这种情况被误并 —— 画出来就是一条长横条。
            if r.x0 <= prev.x1 + 2 and r.x1 >= prev.x0 - 2:
                out[-1] = type(r)(min(prev.x0, r.x0), min(prev.y0, r.y0),
                                  max(prev.x1, r.x1), max(prev.y1, r.y1))
                continue
        out.append(r)
    return out


def _rect_in(r, b, frac: float = 0.6) -> bool:
    """矩形是否**大体落**在某个块里（按重叠面积占比判 —— 块边界与字符框常差零点几磅）。"""
    inter = r & b
    if inter.is_empty:
        return False
    return inter.get_area() >= frac * max(r.get_area(), 1e-6)


# 「同一行合并」参数表（2026-09-30）。两条都是**结构参数**：
# 高度上限 = 「这是不是单行块」，y 容差 = 「算不算同一条视觉行」。
LINE_BLOCK_H = 2.5      # 参与合并的文本块高度上限（× 行高）：整段块不参与
LINE_Y_TOL = 0.6        # 「同一条视觉行」判据（× 行高）

# 「极小高亮也要看得见」参数表（2026-10-01 用户反馈「右下角有点缺失」）。
# 病根：页脚「文件号 + 修订日期」这类**单行小字**的改动矩形，高度就是字高
#   （实测 GRA-4118 页脚那行只有 9.6pt）。落到预览缩略图（默认 ~55dpi）里，
#   9.6pt ≈ 7px 的一条细线，肉眼几乎看不见 —— 用户以为是「右下角标漏了」。
#   真实改动其实**完整标上了**（矩形 y 573~583 都在页面内、渲染也没裁），
#   纯粹是**太矮看不出来**。所以给高亮设一个**最小视觉高度**：
#   矩形高不足 `MIN_HL_H` 时，向上下各扩一点、凑到至少这个高。
# ⚠️ 只对「单行级」的矮矩形生效（大段整块框的高度本就远超阈值，不受影响）；
#   扩高是**纯视觉**（让色条能看见），不改横向、不吞并别的行 —— 精度不损失。
MIN_HL_H = 14.0        # 最小高亮高度（pt）：页脚 6pt 小字也能在缩略图里看到一条色带


def _ensure_min_height(rects: Sequence[Any]) -> List[Any]:
    """把过矮的高亮矩形向上/下各扩一点，凑到 `MIN_HL_H` 高。

    只扩纵向、以矩形中心为轴上下对称扩 —— 横向不动，所以不会把旁边的字一起圈进来；
    也绝不向下吞到「下面那行」：扩的幅度以不超出上下相邻行距为限（用自身高度估）。
    """
    if not rects:
        return []
    out: List[Any] = []
    for r in rects:
        rr = r if isinstance(r, fitz.Rect) else fitz.Rect(*r)
        h = float(rr.height)
        if h < MIN_HL_H and h > 0:
            grow = (MIN_HL_H - h) / 2.0
            # 别扩过头：向上/下最多各扩出「自身高度」那么多（再高就压到别的行了）
            grow = min(grow, h)
            rr = fitz.Rect(rr.x0, rr.y0 - grow, rr.x1, rr.y1 + grow)
        out.append(rr)
    return out



def _merge_same_block(page, rects: Sequence[Any]) -> List[Any]:
    """把「同一文本块里、同一条视觉行」的矩形并成一个外接框（2026-09-30）。

    用户原话：「右下角那个文件号，还有那个修订日期。这些其实可以放在一起。」

    为什么用**文本块**判、而不是距离
    --------------------------------
    页脚那两处在 PDF 里是两个 text line、中间隔 **65pt**；而中栏（x≈444）与右栏
    （x≈634）**只隔 27pt** —— 比页脚内部还窄。纯按距离 / 横向重叠判，结果是
    「把两栏缝成一行、却把页脚分开」，正好反着。
    PyMuPDF 的 `get_text("blocks")` 给的是原文结构：GRA-4118 页脚那两段同属
    `block#111`，中栏是 `block#75`、右栏是 `block#44` —— 分得很干净。

    两条护栏（少一条都会错）
    ------------------------
    · **同一视觉行**（y 中心差 ≤ `LINE_Y_TOL` × 行高）—— 否则一个块里上下不同行的
      改动会被并成一个大框；
    · **块高 ≤ `LINE_BLOCK_H` × 行高** —— 整段块（多行正文）不参与，
      免得把一段里各处改动圈成一坨。

    顺带把同一个词里的**逐字小方块**（如地址新插的 `1栋201室` 被拆成 3 个矩形）
    并成一块，看着更像「一个改动」而不是一排碎点。
    """
    rs = [r if isinstance(r, fitz.Rect) else fitz.Rect(*r) for r in rects]
    if len(rs) < 2:
        return rs
    try:
        blocks = [fitz.Rect(b[:4]) for b in page.get_text("blocks")]
    except Exception:
        return rs
    hs = sorted(r.height for r in rs)
    lh = hs[len(hs) // 2] or 10.0
    out: List[Any] = []
    used = set()
    for i, r in enumerate(rs):
        if i in used:
            continue
        group = [i]
        cy = (r.y0 + r.y1) / 2.0
        for j in range(i + 1, len(rs)):
            if j in used:
                continue
            rj = rs[j]
            if abs(cy - (rj.y0 + rj.y1) / 2.0) > LINE_Y_TOL * lh:
                continue
            for b in blocks:
                if b.height > LINE_BLOCK_H * lh:
                    continue
                if _rect_in(r, b) and _rect_in(rj, b):
                    group.append(j)
                    used.add(j)
                    break
        if len(group) > 1:
            u = rs[group[0]]
            for k in group[1:]:
                u = u | rs[k]
            out.append(u)
        else:
            out.append(r)
    return out


# 「页边标识行整行标注」开关（2026-09-30）
# ------------------------------------------------------------------
# 用户原话：「文件号：GRA-4118 Rev07　修订日期：2026年05月27日    博迈医疗    1/2
# 这个的日期不要零零散散的改，日期不一样肯定是全标出来」。
#
# 病根：页脚是**字符级** diff，只框住真正变了的几个字 —— 新版页脚实测只标了
# `Rev07` 和 `2026年05月27日` 两小段，左边 `文件号：GRA-4118 Rev`、右边
# `博迈医疗    1/2` 都没标；旧版那边同样只标了中间一截（`06/DCR 2025-0850` +
# `2025年12月19日`）。一行被标得零零散散，用户以为是「标漏了」。
#
# 页眉/页脚这类**固定模板标识行**（文件号 / 修订日期 / 公司名 / 页码）是一体的：
# 任何一处变了都意味着「这一行标识整体作废」，整行标出才是对的。
#
# 做法：落在页边带（`pdfdiff_edges.in_edge_band`）里、且所在文本块的内容命中
# `pdfdiff_edges.EDGE_KEEP_RULES`（文件号 / 版本号 / 修订日期 / 带年份编号）的改动框，
# **横向**扩到该文本块的左右边界 —— y 不动，高度不变，只把「这一行」补全。
# ⚠️ 两个限制都是必要的：
#   · 必须命中 KEEP 规则 —— 页边带里也可能有普通正文（本例左栏最后两行就落在带内），
#     那些改动仍要逐字精确标，不能整行扩；
#   · 只扩横向 —— 整块纵向扩会把上下几行一起吞进来。
EDGE_LINE_FULL = os.environ.get("PDFDIFF_ANNOT_EDGE_FULL", "1") != "0"


def _expand_edge_line(page, rects: Sequence[Any]) -> List[Any]:
    """把页边「标识行」里的改动框横向补成整行（见上表的说明）。"""
    rs = [r if isinstance(r, fitz.Rect) else fitz.Rect(*r) for r in rects]
    if not rs:
        return rs
    try:
        import pdfdiff_edges as edges
    except Exception:
        return rs
    h = float(page.rect.height)
    try:
        blocks = [(fitz.Rect(b[:4]), b[4] or "") for b in page.get_text("blocks")]
    except Exception:
        return rs
    out: List[Any] = []
    for r in rs:
        if not edges.in_edge_band(r.y0, r.y1, h):
            out.append(r)
            continue
        band = None
        for b, txt in blocks:
            if b.y1 <= r.y0 or b.y0 >= r.y1:        # 纵向不相交
                continue
            if r.x0 < b.x0 - 1.0 or r.x1 > b.x1 + 1.0:
                continue                            # 这个框不在该块里
            if not edges.hit_keep(txt):
                continue                            # 不是标识行 → 保持逐字精确
            band = b if band is None else (band | b)
        if band is None:
            out.append(r)
        else:
            out.append(fitz.Rect(min(r.x0, band.x0), r.y0, max(r.x1, band.x1), r.y1))
    return out


def _paint(page, rects: Sequence[Any], color: Tuple[float, float, float]) -> int:
    """把矩形画成高亮注释，返回成功画上去的处数。

    ⚠️ `page` 必须是**调用方持有的存活对象**。写成 `doc[i].add_highlight_annot(r)`
       再设色，会因为临时 Page 当场被 GC 而抛 `annotation not bound to any page`
       —— 那异常又常被 `except: continue` 吞掉，结果注释带着**默认黄色**留在页面上。
       详见 `paint_into` 的说明。
    """
    n = 0
    _keep: List[Any] = []      # 注释对象在本次调用期间钉住，免得循环中途被回收
    for r in _merge_rects(rects):
        try:
            a = page.add_highlight_annot(r)
            a.set_colors(stroke=color)
            a.update()
            _keep.append(a)
            n += 1
        except Exception:
            continue
    return n


def _page_box(page, color: Tuple[float, float, float]) -> int:
    """整页新增/删除：在页面四周画一圈彩色边框。"""
    try:
        r = page.rect
        r = type(r)(r.x0 + 12, r.y0 + 12, r.x1 - 12, r.y1 - 12)
        a = page.add_rect_annot(r)
        a.set_colors(stroke=color)
        a.set_border(width=2.5)
        a.update()
        return 1
    except Exception:
        return 0


def _side_layers(side: Dict[str, Any],
                 color: Optional[Tuple[float, float, float]] = None):
    """把一侧的标记摊成「要画的东西」：`([(页号, [矩形], 颜色)], [(页号, 颜色)])`。

    **配色的唯一出口** —— 预览与落盘都走这里，所以「红=纯删减 / 绿=纯新增 /
    黄=变动」这条规矩只写在 `COLOR_OF` 一处，两边不可能画出两种颜色。

      · `side` 带 `by_kind`（新结构）→ 查 `COLOR_OF` 逐桶上色；
      · 没有 `by_kind`（老结构、或回归里手工造的样本）→ 整侧统一用 `color`。
    """
    hl: List[Tuple[int, List[Any], Any]] = []
    bx: List[Tuple[int, Any]] = []
    by_kind = side.get("by_kind") or {}
    if by_kind:
        # 已知桶按 KIND_ORDER（变动最后画、压在最上面），未知桶兜底排在后面
        order = [k for k in KIND_ORDER if k in by_kind]
        order += [k for k in by_kind if k not in KIND_ORDER]
        for kind in order:
            c = COLOR_OF.get(kind, YELLOW)
            for pi, rs in (by_kind.get(kind) or {}).items():
                hl.append((int(pi), rs, c))
        bk_boxes = side.get("boxes_by_kind") or {}
        for kind in order:
            c = COLOR_OF.get(kind, YELLOW)
            for pi in (bk_boxes.get(kind) or []):
                bx.append((int(pi), c))
    else:
        c = color or RED
        for pi, rs in (side.get("marks") or {}).items():
            hl.append((int(pi), rs, c))
        for pi in (side.get("boxes") or []):
            bx.append((int(pi), c))
    return hl, bx


def paint_into(doc, side: Dict[str, Any],
               color: Optional[Tuple[float, float, float]] = None) -> int:
    """把 `locate_marks()` 算好的**某一侧**标记画进一个已打开的文档（内存里）。

    预览（`diff_preview`）与落盘（`annotate_locate`）共用同一条画法，
    「屏幕上看到的」与「导出来会看到的」因此不可能长得不一样。
    `side` 就是 `locate_marks()["old"]` / `["new"]` 那一层 ——
    带 `by_kind` 时**按改动性质分三色**（红纯删减 / 绿纯新增 / 黄变动），
    不带时整侧用 `color`（老调用点仍按原样工作）。

    ⚠️ 2026-10-01 踩过的坑（用户报「两个颜色都是黄的」的**根因**）：
       PyMuPDF 的 `Annotation` 只在**它所属的 Page 对象活着**时有效。
       写成 `doc[pi].add_highlight_annot(r)` 时那个 Page 是临时对象，表达式一结束
       就被回收，紧接着的 `a.set_colors(...)` 抛
       `FzErrorArgument: annotation not bound to any page`；该异常被
       `except: continue` 吞掉，而注释**已经用默认黄色画上去了** → 两侧全黄。
       所以这里先把 Page 取到局部变量、并钉到本次调用结束。
       自检有一条「设定的颜色真的落到注释上」专门守这个。

    返回画上去的处数（高亮 + 整页框）。
    """
    hl, bx = _side_layers(side, color)
    n = 0
    keep: List[Any] = []
    for pi, rs, c in hl:
        if not (0 <= pi < doc.page_count):
            continue
        page = doc[pi]
        keep.append(page)       # ⚠️ 千万别写成 doc[pi].add_highlight_annot(...)
        n += _paint(page, rs, c)
    for pi, c in bx:
        if 0 <= pi < doc.page_count:
            page = doc[pi]
            keep.append(page)
            n += _page_box(page, c)
    return n


def _one_side(path: str, items: List[Any], color: Tuple[float, float, float],
              kind: str, out_path: str, prog=None) -> Dict[str, Any]:
    """给一侧（旧版或新版）生成标注版。kind='old' 取旧侧片段，'new' 取新侧片段。"""
    stats = {"marked": 0, "items": 0, "missed": 0}
    doc = fitz.open(path)
    try:
        buckets: Dict[int, List[Tuple[Any, str]]] = {}   # 页号 → [(差异项, 片段)]
        pages_box: List[int] = []
        for it in items:
            fo, fn = _fragments(it.old, it.new)
            frags = _clean(fo if kind == "old" else fn)
            page_idx = it.page_old if kind == "old" else it.page_new
            # 整页新增/删除：内容层面没有「一处改动」，改成整页画框
            if it.kind == ("page_removed" if kind == "old" else "page_added"):
                if page_idx is not None:
                    pages_box.append(page_idx)
                continue
            if not frags or page_idx is None:
                continue
            for f in frags:
                buckets.setdefault(page_idx, []).append((it, f))
            stats["items"] += 1

        for pi in pages_box:
            if 0 <= pi < doc.page_count:
                stats["marked"] += _page_box(doc[pi], color)

        for i, (pi, frags) in enumerate(buckets.items()):
            if prog:
                prog(i, len(buckets))
            if not (0 <= pi < doc.page_count):
                stats["missed"] += len(frags)
                continue
            page = doc[pi]
            hit = 0
            for it, f in frags:
                # 有坐标就只在「改动块那几行」里搜；没坐标（表格/行级差异）才整页搜
                rects = _search_rects(page, f, _clip_for(it, kind, page))
                if rects:
                    hit += _paint(page, rects, color)
                else:
                    stats["missed"] += 1
            stats["marked"] += hit
            _write_doc(doc, out_path)
    finally:
        doc.close()
    return stats


# ---------------------------------------------------------------- 段落聚合
# 用户 2026-09-29 反馈：「大段的文字你很容易出错……把相应的段落给它圈起来；
# 细节的文字你处理得很好（像右下角那种小角标），看着还不错。」
#
# 逐字矩形在**大段**上会碎成一堆小方块（GRA-4118 中栏那整段新增 = 9 条差异、
# 187 个矩形、每条只盖一行，行与行之间还留缝）—— 看着乱，也难核对。
# 但**不能一律整块**：版本号、日期这类细节逐字标才准。
# 判据：一段的高度达到「单行高」的 `BLOCK_LINE_RATIO` 倍（≈ 连续 3 行）才算大段。
BLOCK_LINE_RATIO = 2.6


def _segment_blocks(rects: Sequence[Any]) -> List[Dict[str, Any]]:
    """把一页一侧的差异矩形聚成「行 → 段」，并标出哪些段该整块圈。

    返回 `[{"rects": [...], "x0","y0","x1","y1", "nline": 行数, "big": 是否整块}, ...]`。

    ① **分行**：按 y 中心排序，中心相近（< 0.7 倍行高）**且水平相邻**的归同一视觉行；
    ② **合段**：相邻行**垂直贴邻**（行距 ≤ 0.6 倍行高）**且水平有交集** → 并成一段；
    ③ **判大段**：段含 ≥ 3 行、且段高 ≥ `BLOCK_LINE_RATIO` 倍行高 → `big=True`。

    ⚠️ 单行内**不合并**：一行里插了几个字（实测 GRA-4118 地址插 `1栋201室`）属于细节，
    合并会把整行没变的字一起圈进去，精度反而下降。
    """
    if not rects:
        return []
    rs = [r if isinstance(r, fitz.Rect) else fitz.Rect(*r) for r in rects]
    hs = sorted(r.height for r in rs)
    lh = hs[len(hs) // 2] or 10.0        # 行高取中位数，抗个别异常高的矩形

    # ① 分行（y 中心相近 **且** 水平相邻 —— 少了后面这条，两栏同高度的字会被并成一行）
    rs.sort(key=lambda r: ((r.y0 + r.y1) / 2.0, r.x0))
    lines: List[Dict[str, Any]] = []
    for r in rs:
        cy = (r.y0 + r.y1) / 2.0
        if lines:
            L = lines[-1]
            gap_x = min(r.x1, L["x1"]) - max(r.x0, L["x0"])
            if abs(cy - L["cy"]) < 0.7 * lh and gap_x > -0.5 * lh:
                L["rects"].append(r)
                L["cy"] = (L["cy"] * (len(L["rects"]) - 1) + cy) / float(len(L["rects"]))
                L["y0"] = min(L["y0"], r.y0); L["y1"] = max(L["y1"], r.y1)
                L["x0"] = min(L["x0"], r.x0); L["x1"] = max(L["x1"], r.x1)
                continue
        lines.append({"rects": [r], "cy": cy, "y0": r.y0, "y1": r.y1,
                      "x0": r.x0, "x1": r.x1})

    # ② 合段
    blocks: List[Dict[str, Any]] = []
    for L in lines:
        lb = fitz.Rect(L["x0"], L["y0"], L["x1"], L["y1"])
        if blocks:
            B = blocks[-1]
            vgap = L["y0"] - B["y1"]
            hov = min(L["x1"], B["x1"]) - max(L["x0"], B["x0"])
            wmin = min(L["x1"] - L["x0"], B["x1"] - B["x0"])
            if vgap <= 0.6 * lh and hov > 0.25 * max(wmin, 1.0):
                B["rects"].extend(L["rects"])
                B["lines"].append(lb)
                B["y0"] = min(B["y0"], L["y0"]); B["y1"] = max(B["y1"], L["y1"])
                B["x0"] = min(B["x0"], L["x0"]); B["x1"] = max(B["x1"], L["x1"])
                B["nline"] += 1
                continue
        blocks.append({"rects": list(L["rects"]), "lines": [lb],
                       "y0": L["y0"], "y1": L["y1"],
                       "x0": L["x0"], "x1": L["x1"], "nline": 1})

    # ③ 判大段
    for B in blocks:
        B["big"] = (B["nline"] >= 3
                    and (B["y1"] - B["y0"]) >= BLOCK_LINE_RATIO * lh)
    return blocks


# ---------------------------------------------------------------- 标记计算
# 用户 2026-10-01 的要求：「把导出来那一步删掉，变成直接在软件里显示这个效果，
# 利用原文件来显示、不改变也不复制原文件」。
#
# 于是把原来「算矩形 → 画注释 → 落盘」三合一的过程拆成两层：
#   · `_prepare()` + `_side_marks()`：**只算**「哪一页、哪几个矩形」，
#     一个字节都不写盘；原文件只被 `fitz.open()` 只读打开，渲完即关；
#   · `annotate_locate()`（落盘版）与 `diff_preview`（界面预览版）
#     各自消费**同一份**结果。
# ⚠️ 两边必须共用这一份计算 —— 否则「屏幕上看到的」和「导出来的」会慢慢走样，
#    这正是用户最不能接受的那类偏差（他说过「如果产出和我自己写的没区别就是失败」，
#    反过来「两个入口同一件事给出两种样子」更是事故）。


def _prepare(old_path: str, new_path: str, ignore_case: bool = False,
             ignore_punct: bool = False, drop_header_footer: bool = False,
             prog=None) -> Tuple[str, str, List[Any], Dict[str, Dict[int, List[Any]]],
                                 Dict[int, List[Any]], List[Any]]:
    """比对 + 取转曲区，返回 `(旧版PDF, 新版PDF, 差异, 单侧转曲矩形, 改写转曲矩形, 转曲区原表)`。

    「旧版PDF / 新版PDF」在图片输入时是折出来的单页 PDF（见 `pdfdiff_img`），
    调用方要渲染/打开的是这两个路径，**不是**用户给的原始路径。
    「单侧转曲矩形」= 删减红 / 新增绿；「改写转曲矩形」= 两侧都有的行内改写，
    两侧同涂黄（三色口径，2026-10-01）。
    """
    import pdfdiff_locate as loc
    import pdfdiff_noise as noise
    import pdfdiff_img as img

    old_pdf, new_pdf = img.ensure_pdf(old_path), img.ensure_pdf(new_path)
    diffs = loc.locate(old_pdf, new_pdf, ignore_case=ignore_case,
                       ignore_punct=ignore_punct,
                       drop_header_footer=drop_header_footer, prog=prog)
    # 舍去「只剩装饰符号」的差异 —— **与清单引擎共用同一张规则表**（pdfdiff_noise）：
    # 孤立的 `•` / `♦` 画到页面上就是一个莫名其妙的小点，用户 2026-09-29
    # 「那种一个一个的小点点，这种非文字类的、没有任何含义的标点符号，没必要标记出来」。
    diffs = [d for d in diffs if not noise.noise_only(d.old_text, d.new_text)]

    # —— 转曲区域（整块标；颜色按三色口径，2026-10-01 更新）——
    # 单列一个 try：OCR 引擎缺失 / 渲染异常都不该让整份标注失败。
    try:
        regions = loc.graphic_regions(old_pdf, new_pdf)
    except Exception:
        regions = []
    # · 两侧都有字（mod 配对，行内改写）→ **变动黄**，两侧同色 —— 与清单的
    #   modified 条目同口径（用户 2026-10-01 报 GRA-3634 注册证行「红红绿绿、
    #   其实只有日期变了」：红/绿只该给纯删/纯增，改写两侧都黄）。
    # · 单侧有字 → 跟所属侧走：删除红 / 新增绿。
    g: Dict[str, Dict[int, List[Any]]] = {"old": {}, "new": {}}
    g_mod: Dict[int, List[Any]] = {}
    g_mov: Dict[str, Dict[int, List[Any]]] = {"old": {}, "new": {}}
    single: Dict[str, Dict[int, List[Tuple[Any, str]]]] = {"old": {}, "new": {}}
    for r in regions:
        if r.get("moved"):
            # —— 位移区域（2026-10-12 五色体系）——
            # 引擎像素配准证明「两侧文字相同、只是位置不同」：源侧矩形 `rect`、
            # 对侧对应位置 `rect_shift`。两侧都画**浅蓝**，不进红/绿/黄任何桶。
            src = r.get("shift_src") or "new"
            a, b = ("new", "old") if src == "new" else ("old", "new")
            g_mov[a].setdefault(r["page"], []).append(fitz.Rect(*r["rect"]))
            g_mov[b].setdefault(r["page"], []).append(fitz.Rect(*r["rect_shift"]))
            continue
        ot, nt = r.get("old_text") or "", r.get("new_text") or ""
        if ot and nt:
            # ⚠️ 这里**只能用并集框 `rect`**，别改成按侧取 `rect_a` / `rect_b`
            #    （2026-10-01 试过，撤回）：行对拍管线给 `h` 行存的
            #    `rect_a` 是**自己那一侧的窄行框**（旧版 400dpi 只读出
            #    `A35-070`，框只有 10pt 宽 → 标不住 `150-200`），
            #    `rect_b` 却是**对侧探测窗**（42pt 宽 → 盖到左边日期列）。
            #    并集框虽然会把旧侧没变的邻格（压力表 `Pressure`）带上一点，
            #    但两侧都不会跑偏；宁可多带一点，也不能把用户要的那处标丢。
            g_mod.setdefault(r["page"], []).append(fitz.Rect(*r["rect"]))
            continue
        for side, txt in (("old", ot), ("new", nt)):
            if txt:
                # ⚠️ 同一个 Rect 对象同时进 `g`（红/绿）与 `single`（挪位配对池）
                #    —— 配对命中时按**对象同一性**从 g 里撤除，两处必须是同一个实例。
                _rc = fitz.Rect(*r["rect"])
                g[side].setdefault(r["page"], []).append(_rc)
                single[side].setdefault(r["page"], []).append((_rc, txt))

    # —— 挪位配对（2026-10-12，失败4 GRA-M30180）——
    # 清单层（`pdfdiff_core._merge_graphic_relocation`）会把「同页一删一增、
    # OCR 文本高度相似」的转曲区条目合并成 `moved`；标记层原来不做这一步，
    # 于是清单显示「位置移动」、预览却画出整行红+绿 —— 两个视图对不上。
    # 这里用**同一份判据**（`_coverage` 双向 ≥ `OCR_MIN_COVER`，短于
    # `OCR_MIN_LEN` 的不参与）在区域层面配对：配上的两侧都归「位移浅蓝」桶，
    # 并从红/绿桶里撤掉。只碰**有 OCR 文本**的区域；纯图形（无文本）不猜。
    try:
        import pdfdiff_locate as _loc
        _cover = getattr(_loc, "_coverage", None)
        if _cover is not None:
            _MIN_COVER = _loc.OCR_MIN_COVER
            _MIN_LEN = _loc.OCR_MIN_LEN
            for page, olist in single["old"].items():
                nlist = single["new"].get(page)
                if not nlist:
                    continue
                used: set = set()
                for orect, otxt in olist:
                    if len(_loc._norm_cjk(otxt)) < _MIN_LEN:
                        continue
                    for i, (nrect, ntxt) in enumerate(nlist):
                        if i in used or len(_loc._norm_cjk(ntxt)) < _MIN_LEN:
                            continue
                        if (_cover(otxt, ntxt) < _MIN_COVER
                                or _cover(ntxt, otxt) < _MIN_COVER):
                            continue
                        used.add(i)
                        g_mov["old"].setdefault(page, []).append(orect)
                        g_mov["new"].setdefault(page, []).append(nrect)
                        for bucket, rect in ((g["old"], orect),
                                             (g["new"], nrect)):
                            lst = bucket.get(page) or []
                            bucket[page] = [x for x in lst if x is not rect]
                        break
    except Exception:
        pass                      # 配不上就维持原样（红/绿），不该让标注失败
    return old_pdf, new_pdf, diffs, g, g_mod, regions, g_mov


def _refine_page(doc, pi: int, rects: Sequence[Any]) -> Tuple[List[Any], int, int]:
    """把一页上收来的原始矩形整成「该画的样子」，返回 `(矩形, 大段数, 丢掉数)`。

    三步老规矩，一步都不能省：
      ① `_expand_edge_line`：页边「文件号 + 修订日期」这类标识行先补成整行；
      ② `_segment_blocks`：聚成「段」，大段按**行**画（整块画会被高亮注释外扩撑歪）；
      ③ `_merge_rects`：同一行里的碎框并起来。

    ⚠️ `page` 只取一次（`doc[pi]` 连环调用既不划算，也正是 `paint_into` 里
       「注释失联」那个坑的同款写法）。
    """
    page = doc[pi]
    rs = _expand_edge_line(page, rects) if EDGE_LINE_FULL else rects
    keep: List[Any] = []
    blocks = missed = 0
    for blk in _segment_blocks(_merge_same_block(page, rs)):
        one = _merge_rects(blk["lines"] if blk["big"] else blk["rects"])
        if blk["big"]:
            blocks += 1
        elif not one:
            missed += 1
        # 2026-10-01：单行小字（页脚文件号/日期）的高亮条太矮，缩略图里看不见 →
        #   统一补到最小视觉高度。大段整块框本就够高，这里不碰它。
        if blk["big"]:
            keep.extend(one)
        else:
            keep.extend(_ensure_min_height(one))
    return keep, blocks, missed


def _side_marks(doc, diffs: List[Any], g_rects: Dict[int, List[Any]],
                side: str, g_mod: Optional[Dict[int, List[Any]]] = None,
                g_mov: Optional[Dict[str, Dict[int, List[Any]]]] = None
                ) -> Dict[str, Any]:
    """算出一侧的全部标记，**不画也不存**。

    返回:
      · `all`          ：`{页号: [矩形…]}`，**不分类型**合并后的全景（统计口径）；
      · `by_kind`      ：`{颜色桶: {页号: [矩形…]}}`，按「删减 / 新增」
                         分好桶，**每个桶各自聚合再合并** —— 跨颜色的框绝不能并，
                         并了就只能涂一个颜色，那正是要避免的；
      · `boxes`        ：整页新增/删除的页号（这些页画整页边框）；
      · `boxes_by_kind`：同上，按颜色桶分；
      · `stats`        ：`marked / items / missed / graphic / blocks`。

    `g_mod` 是**两侧都有字**的改写区域（行内改写）。2026-10-13 删「变动」档后，
    改写区域两侧各归各的：旧侧「删除红」、新侧「新增绿」—— 与清单拆成
    removed+added 两条同口径；不传时只处理单侧的 `g_rects`（老调用点兼容）。

    ⚠️ 统计只按 `all` 算一次。分桶是**配色**需要，不该让「共 N 处标记」跟着颜色走
       —— 同一处改动的框数不能因为换了颜色就变（这也保证了与老版本的零回归）。
    """
    st = {"marked": 0, "items": 0, "missed": 0, "graphic": 0, "blocks": 0}
    raw: Dict[str, Dict[int, List[Any]]] = {}
    boxes: Dict[str, List[int]] = {}
    for d in diffs:
        pi = d.page_old if side == "old" else d.page_new
        if pi is None or not (0 <= pi < doc.page_count):
            continue
        kind = _kind_of(d, side)
        # 整页新增/删除：内容层面没有「一处改动」，改成整页画框
        if d.kind == ("page_removed" if side == "old" else "page_added"):
            boxes.setdefault(kind, []).append(pi)
            st["items"] += 1
            continue
        rects = d.rects_old if side == "old" else d.rects_new
        if not rects:
            continue
        st["items"] += 1
        raw.setdefault(kind, {}).setdefault(pi, []).extend(
            fitz.Rect(*r) for r in rects)

    # ① 全景：所有类型合在一起（**老口径**，统计与「标了几处」都看它）
    flat: Dict[int, List[Any]] = {}
    for pages in raw.values():
        for pi, rs in pages.items():
            flat.setdefault(pi, []).extend(rs)
    all_marks: Dict[int, List[Any]] = {}
    for pi, rs in flat.items():
        keep, nb, nm = _refine_page(doc, pi, rs)
        st["blocks"] += nb
        st["missed"] += nm
        if keep:
            all_marks[pi] = keep

    # ② 分色桶：每桶独立走一遍聚合（同一处的红框与黄框不能并成一个）
    by_kind: Dict[str, Dict[int, List[Any]]] = {}
    for kind, pages in raw.items():
        out: Dict[int, List[Any]] = {}
        for pi, rs in pages.items():
            keep, _, _ = _refine_page(doc, pi, rs)
            if keep:
                out[pi] = keep
        if out:
            by_kind[kind] = out

    # ③ 转曲区（整块）：单侧的跟所属侧走（删除红 / 新增绿）；
    #    **两侧都有字**的改写区域（g_mod）两侧都归「变动黄」—— 与清单的
    #    modified 条目同口径（2026-10-01 用户报 GRA-3634 注册证行红绿错色）。
    gk = KIND_DEL if side == "old" else KIND_ADD
    for pi, rs in (g_rects or {}).items():
        if 0 <= pi < doc.page_count:
            one = _merge_rects(rs)
            if one:
                all_marks.setdefault(pi, []).extend(one)
                by_kind.setdefault(gk, {}).setdefault(pi, []).extend(one)
                st["graphic"] += len(one)
    for pi, rs in (g_mod or {}).items():
        # 2026-10-13 删「变动」档：两侧都有字的改写区域，旧侧归「删除红」、
        # 新侧归「新增绿」（与清单拆成 removed+added 两条同口径）。
        if 0 <= pi < doc.page_count:
            one = _merge_rects(rs)
            if one:
                all_marks.setdefault(pi, []).extend(one)
                by_kind.setdefault(gk, {}).setdefault(pi, []).extend(one)
                st["graphic"] += len(one)

    # ④ 挪位区（2026-10-12）：两侧都有、内容没变只是位置变了 → 「位移浅蓝」，
    #    两侧同色（与清单 `moved` 条目同口径）。
    for pi, rs in ((g_mov or {}).get(side) or {}).items():
        if 0 <= pi < doc.page_count:
            one = _merge_rects(rs)
            if one:
                all_marks.setdefault(pi, []).extend(one)
                by_kind.setdefault(KIND_MOV, {}).setdefault(pi, []).extend(one)
                st["graphic"] += len(one)

    all_marks = {pi: v for pi, v in all_marks.items() if v}
    all_boxes = [p for v in boxes.values() for p in v]
    st["marked"] = sum(len(v) for v in all_marks.values()) + len(all_boxes)
    return {"all": all_marks, "by_kind": by_kind, "boxes": all_boxes,
            "boxes_by_kind": boxes, "stats": st}


def filter_kinds(side: Optional[Dict[str, Any]],
                 enabled: Optional[Sequence[str]]) -> Dict[str, Any]:
    """按「开启的颜色桶」过滤一侧标记 —— **预览开关的唯一入口**。

    UI 的五颗勾选框（删减/新增/变动/位移/字体）切换后，把 `by_kind` /
    `boxes_by_kind` 里没开的桶滤掉再交给 `paint_into`，画法本身零改动。
    老结构（没有 `by_kind`）原样返回 —— 老调用点不受影响。
    """
    if not side or not side.get("by_kind") or enabled is None:
        return side or {}
    on = set(enabled)
    out = dict(side)
    fk = {k: v for k, v in side["by_kind"].items() if k in on}
    out["by_kind"] = fk
    fbk = {k: v for k, v in (side.get("boxes_by_kind") or {}).items()
           if k in on}
    out["boxes_by_kind"] = fbk
    # ⚠️ 2026-10-12 勾选 bug（184/185）：`by_kind` 被滤**空**时，`_side_layers`
    # 的 `if by_kind:` 判空会当成「老结构」回退成整侧单色全画（marks/boxes
    # 没被过滤）→ 五颗框全不勾反而**全显示**，勾上任意一颗才真正过滤。
    # 这里把 marks/boxes 一并按桶重建，保证「空选 = 什么都不画」。
    merged: Dict[int, list] = {}
    for _k, v in fk.items():
        for pi, rs in (v or {}).items():
            merged.setdefault(int(pi), []).extend(rs)
    out["marks"] = merged
    out["boxes"] = [pi for v in fbk.values() for pi in v]
    return out


def locate_marks(old_path: str, new_path: str, ignore_case: bool = False,
                 ignore_punct: bool = False, drop_header_footer: bool = False,
                 prog=None) -> Dict[str, Any]:
    """**只算标记、不落盘** —— 供界面内预览用（见 `diff_preview`）。

    ⚠️ 全程只读：原文件被 `fitz.open()` 打开算完就关，既不复制也不改写；
       界面上看到的所有东西都是渲染到内存里的像素。

    返回 `{"old": {...}, "new": {...}, "diffs": [...], "graphic": n}`，
    每侧含 `marks`（`{页号: [Rect…]}`）、`boxes`（整页框页号）、
    `stats`（与导出口径一致）、`pages`（该侧页数）、`path`（真正被渲染的文件）。
    """
    old_pdf, new_pdf, diffs, g, g_mod, regions, g_mov = _prepare(
        old_path, new_path, ignore_case, ignore_punct, drop_header_footer, prog)
    out: Dict[str, Any] = {}
    for side, path in (("old", old_pdf), ("new", new_pdf)):
        doc = fitz.open(path)
        try:
            sm = _side_marks(doc, diffs, g[side], side, g_mod, g_mov)
            out[side] = {"marks": sm["all"], "by_kind": sm["by_kind"],
                         "boxes": sm["boxes"],
                         "boxes_by_kind": sm["boxes_by_kind"],
                         "stats": sm["stats"],
                         "pages": doc.page_count, "path": path}
        finally:
            doc.close()
    out["diffs"] = diffs
    out["graphic"] = len(regions)
    return out


def annotate_locate(old_path: str, new_path: str, ignore_case: bool = False,
                    ignore_punct: bool = False, drop_header_footer: bool = False,
                    out_dir: Optional[str] = None, prog=None) -> Dict[str, Any]:
    """**坐标直画**版标注（落盘版，2026-09-29 起）。

    ⚠️ 2026-10-01 起界面不再调用它（用户要求「把导出来那一步删掉，改成软件内预览」），
       标记计算已与 `locate_marks` 合流到 `_prepare` + `_side_marks` ——
       保留这个函数是为了回归用例与「将来想再要导出」的可能，逻辑一字未改。

    与 `annotate()` 的根本区别：**不做任何文本搜索**。
    先用 `pdfdiff_locate` 用 PyMuPDF 抽字符级坐标、并直接算出改动落在
    哪几个字的矩形里，这里只负责把矩形画成高亮。

    这样就不存在「搜不到 / 搜到别处 / 砍短后标到无辜文字」这一类误标 ——
    旧表里那个 9 标红、新表里那个 11 标绿，画的就是 diff 算出来的那几个字。

    **转曲区域兜底（颜色跟所属侧走，用户 2026-09-29 拍板「不要橙色」）**：
    转曲 PDF（正文以矢量曲线绘制、另配一层隐形文字）里，凡是文字层缺行的地方，
    「读文字比对」原理上够不到 —— 真改动会整条漏掉。这里再调
    `loc.graphic_regions()` 做像素级对拍，把「两侧都没有文字层、但画面变了」
    的区域**整块**框出来：单侧的按所属侧上色（新增块 GREEN / 删除块 RED）；
    两侧都有字的**行内改写**两侧同涂 YELLOW（三色口径，2026-10-01）。

    **大段整块圈、细节逐字标（用户 2026-09-29 第二次反馈）**：
    整段增删会被 `locate()` 逐行拆开报，逐字画出来就是几十个小方块、行间还留缝
    （GRA-4118 中栏那整段 = 9 条差异 / 187 个矩形）。这里先按页把该侧的矩形
    收齐，再用 `_segment_blocks()` 聚成「段」：**段高约 ≥ 3 行 → 整段画一个外接
    矩形**；否则**保持逐字**（版本号 / 日期 / 一行里插几个字都属于细节，逐字最准）。
    ⚠️ 这一步**不需要 OCR** —— 有文字层时字符级坐标本来就比 OCR 准；OCR 只留给
    「根本没有文字层」的转曲区（上一段那条链路）。
    """
    out_dir = out_dir or (os.path.dirname(old_path) or os.path.expanduser("~"))
    b1 = os.path.splitext(os.path.basename(old_path))[0]
    b2 = os.path.splitext(os.path.basename(new_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    stamp = _stamp()
    o_path = os.path.join(out_dir, f"{b1}_【旧版·标红】_{stamp}.pdf")
    n_path = os.path.join(out_dir, f"{b2}_【新版·标绿】_{stamp}.pdf")

    old_pdf, new_pdf, diffs, g, g_mod, regions, g_mov = _prepare(
        old_path, new_path, ignore_case, ignore_punct, drop_header_footer, prog)

    so = {"marked": 0, "items": 0, "missed": 0, "graphic": 0, "blocks": 0}
    sn = {"marked": 0, "items": 0, "missed": 0, "graphic": 0, "blocks": 0}
    for src, dst, stats, color, side in (
            (old_pdf, o_path, so, RED, "old"),
            (new_pdf, n_path, sn, GREEN, "new")):
        doc = fitz.open(src)
        try:
            # 标记怎么算的、为什么是这几步 —— 全在 `_side_marks()`（与预览共用）。
            sm = _side_marks(doc, diffs, g[side], side, g_mod, g_mov)
            stats.update(sm["stats"])
            # 画也共用（含三色配色）—— 不在这里再写一遍循环，否则「预览有黄、
            # 导出没黄」这种偏差迟早出现。
            paint_into(doc, sm, color)
            _write_doc(doc, dst)
        finally:
            doc.close()
    return {"old_path": o_path, "new_path": n_path, "old": so, "new": sn,
            "diffs": diffs, "graphic": len(regions)}



def _write_doc(doc, dst: str) -> None:
    """保存文档。

    ⚠️ 不能用 `doc.save(dst)`：PyMuPDF 保存前会先 remove 目标文件，
    同名的上一版标注还在时（这是常态）就会报 `cannot remove file:
    Operation not permitted`，在受限制环境里必然失败。
    改成先导出字节、再以 'wb' 覆盖写 —— 覆盖不需要删除权限。
    """
    data = doc.tobytes(garbage=3, deflate=True)
    with open(dst, "wb") as f:
        f.write(data)


def annotate(old_path: str, new_path: str, items: List[Any],
             out_dir: str, prog=None) -> Dict[str, Any]:
    """**搜索式**标注（旧路径）。

    ⚠️ 定位靠 `page.search_for()`，会误标（详见模块头部说明），
    2026-09-29 起主路径已换成 `annotate_locate()`；这里保留仅作兜底/对照。
    """
    os.makedirs(out_dir, exist_ok=True)
    b1 = os.path.splitext(os.path.basename(old_path))[0]
    b2 = os.path.splitext(os.path.basename(new_path))[0]
    ts = _stamp()
    o_path = os.path.join(out_dir, f"{b1}_【旧版·标红】_{ts}.pdf")
    n_path = os.path.join(out_dir, f"{b2}_【新版·标绿】_{ts}.pdf")
    so = _one_side(old_path, items, RED, "old", o_path, prog)
    sn = _one_side(new_path, items, GREEN, "new", n_path, prog)
    return {"old_path": o_path, "new_path": n_path, "old": so, "new": sn}


# ------------------------------------------------------------------ 自检
if __name__ == "__main__":
    ok = fail = 0

    def chk(name, cond, extra=""):
        global ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name}" + (f"　{extra}" if extra else ""))

    fo, fn = _fragments("温度 150 度", "温度 180 度")
    chk("replace 两侧都出片段", len(fo) >= 1 and len(fn) >= 1)
    chk("旧侧片段含 150", any("150" in x for x in fo))
    chk("新侧片段含 180", any("180" in x for x in fn))

    fo2, fn2 = _fragments("AAAA", "AAAABBB")
    chk("insert 只在新侧", (not [x for x in fo2 if x.strip()]) and any("BBB" in x for x in fn2))

    chk("太碎的片段被丢掉", _clean(["7", "  ", "abc"]) == ["abc"])
    long_f = "，".join(["规格参数%d" % i for i in range(40)])
    cl = _clean([long_f])
    chk("超长片段被切开", len(cl) > 1 and all(len(x) <= MAX_FRAG for x in cl))

    chk("导出名时间戳格式 MMDD-HHMMSS", bool(re.fullmatch(r"\d{4}-\d{6}", _stamp())))

    # —— 配色（2026-10-13 删「变动」档：红=纯删减、绿=纯新增）——
    chk("红绿互不相同", RED != GREEN)
    chk("颜色桶都有颜色和中文明", set(COLOR_OF) == set(KIND_TEXT) == set(KIND_ORDER))

    class _D:                       # 造一条假差异（只要 kind / old_text / new_text）
        def __init__(self, kind, o="", n=""):
            self.kind, self.old_text, self.new_text = kind, o, n

    # ⚠️ 判据只有一个字：**「纯」= 只出现在一侧**。改写已在判定层拆成删+增两条。
    chk("纯删减 → 红", _kind_of(_D("removed"), "old") == KIND_DEL)
    chk("整页删除 → 红（不走「两侧都有」那条路）",
        _kind_of(_D("page_removed"), "old") == KIND_DEL)
    chk("纯新增 → 绿", _kind_of(_D("added"), "new") == KIND_ADD)
    chk("整页新增 → 绿", _kind_of(_D("page_added"), "new") == KIND_ADD)
    chk("挪位 → 浅蓝桶（2026-10-12 五色体系）",
        _kind_of(_D("moved", "同一句话", "同一句话"), "new") == KIND_MOV)
    chk("字体 → 深蓝桶", _kind_of(_D("font", "同一句话", "同一句话"), "new") == KIND_FONT)
    chk("四色桶互不相同、各有中文名",
        len({COLOR_OF[k] for k in KIND_ORDER}) == 4
        and set(COLOR_OF) == set(KIND_TEXT) == set(KIND_ORDER))
    chk("默认开启的是删减/新增/字体（位移默认关）",
        set(KIND_DEFAULT_ON) == {KIND_DEL, KIND_ADD, KIND_FONT})
    chk("filter_kinds：关掉的桶被滤掉、老结构原样返回",
        filter_kinds({"by_kind": {KIND_DEL: {0: []}, KIND_MOV: {0: []}},
                      "boxes_by_kind": {}},
                     KIND_DEFAULT_ON)["by_kind"].keys() == {KIND_DEL}
        and filter_kinds({"marks": {0: []}}, KIND_DEFAULT_ON) == {"marks": {0: []}})
    chk("filter_kinds：全不勾 → marks/boxes 同步滤空（修 184/185 勾选 bug："
        "空选不得回退成整侧全画）",
        filter_kinds({"by_kind": {KIND_DEL: {0: ["r"]}, KIND_ADD: {0: ["r"]}},
                      "boxes_by_kind": {KIND_DEL: [0]},
                      "marks": {0: ["r"]}, "boxes": [0]}, ())["marks"] == {}
        and filter_kinds({"by_kind": {KIND_DEL: {0: ["r"]}, KIND_ADD: {0: ["r"]}},
                          "boxes_by_kind": {KIND_DEL: [0]},
                          "marks": {0: ["r"]}, "boxes": [0]}, ())["boxes"] == []
        and filter_kinds({"by_kind": {KIND_DEL: {0: ["r"]}},
                          "marks": {0: ["r"]}}, (KIND_DEL,))["marks"] == {0: ["r"]})

    # 配色出口只有 `_side_layers` 一处：带 by_kind 时逐桶取色，不带时整侧单色
    _lay = _side_layers({"by_kind": {KIND_DEL: {0: [fitz.Rect(0, 0, 9, 9)]},
                                     KIND_ADD: {0: [fitz.Rect(0, 9, 9, 9)]}}})
    chk("分色时按桶给色（删减红、新增绿，顺序见 KIND_ORDER）",
        [c for _p, _r, c in _lay[0]] == [RED, GREEN], str([c for _p, _r, c in _lay[0]]))
    chk("不分色时整侧一个色（老调用点兼容）",
        all(c == GREEN for _p, _r, c in
            _side_layers({"marks": {0: [fitz.Rect(0, 0, 9, 9)]}}, GREEN)[0]))

    # —— 段落聚合（2026-09-29：大段整块圈、细节逐字标）——
    R = fitz.Rect
    same = _segment_blocks([R(0, 0, 40, 10), R(42, 0, 80, 10)])
    chk("同一行不合并（细节保持逐字）",
        len(same) == 1 and same[0]["nline"] == 1 and same[0]["big"] is False
        and len(same[0]["rects"]) == 2)
    pair = _segment_blocks([R(0, 0, 120, 10), R(0, 12, 120, 22)])
    chk("只有 2 行不整块（地址那种插入仍逐字）",
        len(pair) == 1 and pair[0]["nline"] == 2 and pair[0]["big"] is False)
    tall = _segment_blocks([R(0, y, 120, y + 10) for y in range(0, 40, 12)])
    chk("连续多行整段合成一块",
        len(tall) == 1 and tall[0]["nline"] >= 3 and tall[0]["big"] is True)
    two = _segment_blocks([R(0, 0, 40, 10), R(200, 0, 240, 10)])
    chk("两栏同高度的字不被并成一行", len(two) == 2)
    far = _segment_blocks([R(0, 0, 40, 10), R(0, 200, 40, 210)])
    chk("相距很远的两处不合并", len(far) == 2)

    # —— 页边「标识行」整行标注（2026-09-30：页脚日期不要零零散散地标）——
    class _PgStub:
        """极简页面桩：只要 rect.height 与 get_text('blocks')。"""
        class _Rect:
            height = 595.275
        rect = _Rect()

        def get_text(self, _what):
            return [
                (624.8, 574.6, 822.0, 581.1,
                 "文件号：GRA-4118 Rev07 \n 修订日期：2026年05月27日    博迈医疗    1/2\n"),
                (37.7, 564.3, 122.0, 578.9,
                 "无法进行抗凝治疗的患者\n造影剂有过敏反应或不能用药者\n"),
            ]

    _pg = _PgStub()
    _e = _expand_edge_line(_pg, [R(677.5, 573.1, 679.9, 582.6),
                                 R(748.9, 574.0, 770.8, 581.8)])
    chk("标识行整行标注：页脚两处碎标记各自补成一整行",
        len(_e) == 2 and all(abs(x.x0 - 624.8) < 0.1 and abs(x.x1 - 822.0) < 0.1 for x in _e),
        str([(round(x.x0, 1), round(x.x1, 1)) for x in _e]))
    chk("标识行整行标注：y 不动（只横向补全）",
        all(abs(x.y0 - (573.1 if i == 0 else 574.0)) < 0.01 for i, x in enumerate(_e)))
    _e2 = _expand_edge_line(_pg, [R(40.0, 566.0, 60.0, 578.0)])
    chk("标识行整行标注：页边带里的**普通正文行**不整行扩（仍逐字精确）",
        len(_e2) == 1 and abs(_e2[0].x0 - 40.0) < 0.1 and abs(_e2[0].x1 - 60.0) < 0.1,
        str((round(_e2[0].x0, 1), round(_e2[0].x1, 1))))
    _e3 = _expand_edge_line(_pg, [R(677.5, 300.0, 679.9, 309.0)])
    chk("标识行整行标注：不在页边带的框一律不动",
        len(_e3) == 1 and abs(_e3[0].x0 - 677.5) < 0.1 and abs(_e3[0].x1 - 679.9) < 0.1)

    # —— 合并：横向**真的**搭上才并（2026-10-01 用户反馈「一维码上一条长横条」）——
    _m1 = _merge_rects([R(159.5, 14.9, 190.1, 32.1), R(417.7, 12.2, 435.3, 22.5)])
    chk("合并：隔着 227pt 的两个框**不并**（旧口径会并成压在一维码上的长横条）",
        len(_m1) == 2, str([(round(x.x0, 1), round(x.x1, 1)) for x in _m1]))
    _m2 = _merge_rects([R(100.0, 10.0, 105.0, 20.0), R(105.5, 10.0, 110.0, 20.0)])
    chk("合并：同一行里真相邻的碎框照旧并成一个",
        len(_m2) == 1 and abs(_m2[0].x0 - 100.0) < 0.1 and abs(_m2[0].x1 - 110.0) < 0.1)
    _m3 = _merge_rects([R(100.0, 10.0, 150.0, 20.0), R(120.0, 12.0, 130.0, 18.0)])
    chk("合并：重叠的框并成一个（去重防叠色）", len(_m3) == 1)
    _m4 = _merge_rects([R(100.0, 10.0, 150.0, 20.0), R(300.0, 40.0, 350.0, 50.0)])
    chk("合并：不同行（y 差 30pt）不并", len(_m4) == 2)

    # —— 极小高亮也要看得见（2026-10-01 用户「右下角有点缺失」）——
    #   页脚「文件号 + 修订日期」单行小字高仅 9.6pt，缩略图里 ≈7px 看不见。
    #   只扩纵向、以中心为轴对称扩、且不超出自身高度（不吞并相邻行）。
    _h1 = _ensure_min_height([R(624.8, 573.4, 822.0, 583.0)])          # 高 9.6pt
    chk("极小高亮补到最小高度", len(_h1) == 1 and _h1[0].height >= MIN_HL_H - 0.01,
        str(round(_h1[0].height, 1)))
    chk("补高后横向不变（不圈旁边的字）",
        abs(_h1[0].x0 - 624.8) < 0.01 and abs(_h1[0].x1 - 822.0) < 0.01)
    chk("补高以中心为轴对称（上扩=下扩）",
        abs(_h1[0].y0 - 573.4 - (583.0 - _h1[0].y1)) < 0.01,
        str((round(_h1[0].y0, 2), round(_h1[0].y1, 2))))
    _h2 = _ensure_min_height([R(0, 0, 120, 50)])                       # 已经够高
    chk("本来就够高的大段整块框不被动",
        len(_h2) == 1 and abs(_h2[0].height - 50.0) < 0.01)
    _h3 = _ensure_min_height([R(10, 100, 60, 103)])                    # 极矮 3pt
    chk("极矮框也只在自身高度内扩（不压到上下行）",
        _h3[0].height >= 6.0 and _h3[0].y0 >= 100 - 3.0 - 0.01,
        str((round(_h3[0].y0, 1), round(_h3[0].y1, 1))))
    chk("空输入返回空", _ensure_min_height([]) == [])

    # —— 转曲区「行内改写」旧侧红 / 新侧绿（2026-10-13 删「变动」档）——
    #   清单里这种区域已拆成 removed（旧值标红）+ added（新值标绿），
    #   标注侧与之对齐：改写区旧侧归「删除红」、新侧归「新增绿」。
    _doc_m = fitz.open()
    try:
        _doc_m.new_page(width=400, height=300)
        _gm = {0: [fitz.Rect(100, 100, 300, 112)]}
        _sm_o = _side_marks(_doc_m, [], {}, "old", _gm)
        _sm_n = _side_marks(_doc_m, [], {}, "new", _gm)
        chk("转曲改写区旧侧归红、新侧归绿（不再同黄）",
            KIND_DEL in _sm_o["by_kind"] and KIND_ADD in _sm_n["by_kind"],
            str((set(_sm_o["by_kind"]), set(_sm_n["by_kind"]))))
        chk("转曲改写区计入 graphic 统计（两侧各一次）",
            _sm_o["stats"]["graphic"] == 1 and _sm_n["stats"]["graphic"] == 1,
            str((_sm_o["stats"]["graphic"], _sm_n["stats"]["graphic"])))
        # 单侧区域仍跟所属侧：删除红 / 新增绿（老口径不变）
        _sm_o2 = _side_marks(_doc_m, [], {0: [fitz.Rect(10, 10, 60, 22)]}, "old")
        _sm_n2 = _side_marks(_doc_m, [], {0: [fitz.Rect(10, 40, 60, 52)]}, "new")
        chk("转曲单侧区域仍按所属侧上色（删除红 / 新增绿）",
            KIND_DEL in _sm_o2["by_kind"] and KIND_ADD in _sm_n2["by_kind"])
    finally:
        _doc_m.close()

    print(f"标注模块自检：{ok} 项通过，{fail} 项失败")
    raise SystemExit(1 if fail else 0)
