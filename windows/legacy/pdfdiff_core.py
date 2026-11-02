# -*- coding: utf-8 -*-
"""
pdfdiff_core.py —— PDF 双版本差异比对引擎
==================================================================
识别两份 PDF「改动了什么」，输出结构化差异清单。

技术底座：
  * 文本层：pdfminer.six 原生布局解析（extract_pages / LTTextLineHorizontal）
            保留每行的 bbox、字号、字体，用于定位 + 页眉页脚过滤
  * 表格层：pdfplumber（底层同为 pdfminer.six）提取表格结构，
            按「行键 + 列名」对齐，输出单元格级差异
  * 降级：表格识别不到时自动退回文本行级比对

章节索引：
  第 1 节  公共工具（文本规范化 / 数值解析 / 相似度）
  第 2 节  文本层提取（pdfminer）
  第 3 节  表格层提取（pdfplumber）
  第 4 节  页面对齐（页数不等时按内容指纹配对）
  第 5 节  行级比对（SequenceMatcher + 块内递归）
  第 6 节  表格级比对（行键匹配 + 单元格 diff）
  第 6.5 节 段级「前后文」（给界面按段落展示用）
  第 7 节  元数据比对
  第 7.5 节 纯装饰符号差异的舍去（规则表在 pdfdiff_noise）
  第 8 节  总入口 compare_pdfs()
  第 9 节  导出（Excel / HTML / JSON / CSV）
  第 10 节 自检 self_test()
"""

from __future__ import annotations

import bisect
import csv
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pdfdiff_noise import drop_noise       # 第 7.5 节：纯装饰符号差异的漏斗
import pdfdiff_edges as edges              # 页边行的去留规则（与标注链路共用）
# 数值判定（`parse_number` / `strip_number` / `numeric_delta`）住在 group 里：
# 2026-10-13 删「变动」档后不再产生「数值变动」档，这几个仅作遗留纯逻辑工具，
# 供自检复用。判据只有一份，别复制。
from pdfdiff_group import numeric_delta, parse_number

__version__ = "1.0.0"

# ==================================================================
# 第 1 节  公共工具
# ==================================================================

_WS_RE = re.compile(r"\s+")
# 全角/半角、各种破折号与引号统一
_TRANS_MAP = {
    "（": "(", "）": ")", "，": ",", "。": ".", "：": ":", "；": ";",
    "！": "!", "？": "?", "“": '"', "”": '"', "‘": "'", "’": "'",
    "－": "-", "—": "-", "–": "-", "〜": "~", "～": "~", "≥": ">=",
    "≤": "<=", "×": "x", "＋": "+", "／": "/", "％": "%",
}
_TRANS = str.maketrans(_TRANS_MAP)

_PUNCT_RE = re.compile(r"[\s\.,;:!?\"'`~@#$%^&*()\[\]{}<>/\\|\-_+=]+")


def normalize_text(s: str, opts: "Options") -> str:
    """按选项规范化一行文本，用于比对（不改动原文，原文单独保留）。"""
    if s is None:
        return ""
    t = s.translate(_TRANS)
    if opts.ignore_case:
        t = t.lower()
    if opts.ignore_whitespace:
        t = _WS_RE.sub("", t)
    else:
        t = _WS_RE.sub(" ", t).strip()
    if opts.ignore_punctuation:
        t = _PUNCT_RE.sub("", t)
    return t.strip()


# 数值判定（`parse_number` / `strip_number` / `numeric_delta`）已上移到
# `pdfdiff_group`（顶部 import 进来）—— 2026-10-13 删「变动」档后仅作遗留工具，
# 让纯逻辑规则住在一个不依赖 pdfminer 的模块里。


def ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def clip(s: Optional[str], n: int = 300) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


# ==================================================================
# ==================================================================
# 置信度（2026-10-10 加）
# ------------------------------------------------------------------
# 为什么要这一层：原先全项目**没有任何「把握度」概念**，引擎永远输出确定结论 ——
# 撞到没覆盖的边界时它不会说「我不确定」，而是照样给一个确定答案，错得理直气壮。
# 这正是「换一份新 PDF 就出错」的总根。现在把「这条有多可信」显式记下来，
# 界面据此把「已判定」和「请你核一眼」分开 ——
# 出错不再是「给了错答案」，而是「明确告诉你这里要人看一眼」。
#
# 来源标签：结论是**怎么**得出来的（决定 conf / loc_conf 这对默认值）
SRC_CONF = {
    "text":     (0.95, 0.95),   # PDF 文字层直接读出来精确比对 —— 最可靠
    "docx":     (0.98, 1.00),   # Word 直读（ZIP+XML，同样是精确文本）—— 最可靠
    "ocr_line": (0.70, 0.85),   # 扫描件 OCR 行对拍，两侧文本都认出了 —— 风险在认错字
    "ocr_est":  (0.70, 0.60),   # 同上，但坐标按字符占比折算（非实测）—— 位置是估的
    "graphic":  (0.40, 0.40),   # 只有像素区域，文本不可靠或根本没有 —— 可能裹进没变的邻格
}

# 三档：**取 conf 与 loc_conf 的较低者**定档 ——
# 存在性再确定，位置是估的也得降档（用户最反感的就是「框错地方」）。
CONF_HIGH_MIN = 0.85      # ≥ 它 = 高（正常显示）
CONF_MID_MIN = 0.55       # ≥ 它 = 中（正常显示，详情里写明来源）；< 它 = 低（灰显 + 「待确认」）

CONF_LEVEL_TEXT = {"high": "高", "mid": "中", "low": "低"}
CONF_SRC_TEXT = {
    "text": "PDF 文字层精确比对",
    "docx": "Word 直读精确比对",
    "ocr_line": "扫描件 OCR 行对拍",
    "ocr_est": "OCR 文本比对 + 位置估算",
    "graphic": "像素区域兜底（无可靠文本）",
}


def conf_of(src: str) -> Tuple[float, float]:
    """来源 → `(conf, loc_conf)`。未知来源按最保守的 `graphic` 处理（宁可报低，不可装准）。"""
    return SRC_CONF.get(src, SRC_CONF["graphic"])


def conf_level(conf: float, loc_conf: float) -> str:
    """取较低者定档：`high` / `mid` / `low`。"""
    c = min(float(conf or 0.0), float(loc_conf or 0.0))
    if c >= CONF_HIGH_MIN:
        return "high"
    return "mid" if c >= CONF_MID_MIN else "low"


def conf_level_of(item) -> str:
    """条目 → 档位。字段缺失或为 0 时按最保守处理（拿到 0 就当「低」，不装准）。"""
    try:
        return conf_level(float(getattr(item, "conf", 0.0) or 0.0),
                          float(getattr(item, "loc_conf", 0.0) or 0.0))
    except Exception:
        return "low"


# 选项
# ==================================================================

@dataclass
class Options:
    ignore_whitespace: bool = True      # 忽略空白差异
    ignore_case: bool = False           # 忽略大小写
    ignore_punctuation: bool = False    # 忽略标点差异
    # 忽略页眉页脚里的**无信息行**（页码 / 纯符号）。
    # ⚠️ 2026-09-30 语义修正：原名「按版面位置判定」，那是错的 ——
    # 位置只用来「提名」，内容才决定去留（见 `pdfdiff_edges`）。
    # 旧语义会把印在页脚的文件号 / 修订日期整段滤掉（GRA-4118 实测：
    # Rev06/DCR 2025-0850 → Rev07、修订日期三年一变，清单里一条都没有，
    # 而标注版标了 —— 两边对不上，用户没法判断谁对）。
    ignore_header_footer: bool = True
    ignore_patterns: List[str] = field(default_factory=list)  # 正则，命中即整行跳过
    min_line_len: int = 1               # 短于该长度的行不参与比对
    modify_threshold: float = 0.55      # 相似度 ≥ 此值才算「修改」，否则拆成删+增
    page_match_threshold: float = 0.45  # 页码对不上的页，按此相似度配对
    mode: str = "auto"                  # auto(表格优先) | text(纯文本) | table(仅表格)
    max_depth: int = 3                  # 行块内递归深度
    # 文档属性（标题 / 作者 / 创建时间 / 修改时间…）要不要进差异清单。
    # ⚠️ 默认 **False**（用户 2026-09-29 拍板）：这些是 PDF 工具链自己写的元数据，
    # 改版时本来就跟着变，混在正文差异里只是噪声 —— 「标题」「文档改动时间」
    # 排在清单最前面，用户看第一眼就是这些，等于把真改动埋了。
    # 保留开关而不是删函数：将来若要单独的「属性对比」视图，翻这个开关就行。
    compare_meta: bool = False
    # 舍去「只剩装饰符号」的差异（`• ` / `♦ ` / ○●◆▪ / 零宽字符…）。
    # ⚠️ 默认 **True**（用户 2026-09-29 拍板）：「那种一个一个的小点点，这种非文字类
    # 的、也没有任何含义的标点符号，没必要标记出来」。规则表见 `pdfdiff_noise`。
    # 是**事后过滤**：判定链一分未动，关掉它就得到原样清单。
    drop_noise: bool = True
    # 「前后文」上下各取几行（用户 2026-09-29 要求「扩大一点范围，多加一些内容」）。
    # 行数越多越能看出「这段落在原文哪儿」，但详情面板会变长 —— 4 行是实测好读的量。
    ctx_lines: int = 4

    # 字体变化检测（2026-10-12 用户「字体变化标深蓝」）：文本一字不差但字号/字体
    # 变了时，产出 `font` 条目（深蓝）。默认开 —— 这信息本来就在文字层里，零额外成本。
    font_diff: bool = True

    _compiled: List[re.Pattern] = field(default_factory=list, repr=False)

    def compile_patterns(self) -> None:
        self._compiled = []
        for p in self.ignore_patterns:
            if not p:
                continue
            try:
                self._compiled.append(re.compile(p))
            except re.error:
                pass

    def skip_line(self, raw: str) -> bool:
        for rgx in self._compiled:
            if rgx.search(raw):
                return True
        return False


# ==================================================================
# 第 2 节  文本层提取（pdfminer.six）
# ==================================================================

@dataclass
class TextLine:
    raw: str
    x0: float
    top: float
    x1: float
    bottom: float
    size: float = 0.0
    font: str = ""
    # 这行**怎么来的**："text" = pdfminer 文字层直读；"ocr" = 整页 OCR 补的
    # （见 `_mixed_ocr_fill_core`）。决定这条差异的置信度来源 —— 别把 OCR 补的
    # 行当成文字层，那正是「换一份新 PDF 就装准」的老毛病。
    src: str = "text"

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2.0


@dataclass
class PageText:
    index: int                 # 0-based
    width: float = 0.0
    height: float = 0.0
    lines: List[TextLine] = field(default_factory=list)


def _fitz_no_text_pages(pdf_path: str) -> Optional[set]:
    """用 fitz 快速预扫出「**没有文字层**」的页号集合；打不开返回 None。

    ⚠️ 为什么（2026-10-13，rev15 卡死 60s 的根治）
    ----------------------------------------------------------
    CAD 导出的 PDF 会把图面文字「转曲线」——1MB 的密集贝塞尔路径里一个字符都没有。
    pdfminer 的 `extract_pages` 会逐点解析这些路径做 `paint_path` 填充，复杂度爆炸
    （rev15 第 3 页单页 53s、三页累计 60s+，GUI 主线程同步等待 → 表现为「卡死」）。

    fitz 判断「该页有没有文字」是 O(1) 级别的（`page.get_text()` 直接读内容流的
    文字对象，0.02s），对「转曲线」「纯图片/扫描件」都返回空串。这三类页面 pdfminer
    逐点跑一遍也抽不出任何字，纯属白耗 —— 预扫出来后直接跳过 pdfminer，把空页交给
    下游 `_mixed_ocr_fill_core` / `graphic_regions` 走 OCR / 图形通道。
    """
    try:
        import pymupdf as fitz
    except Exception:
        import fitz                       # type: ignore
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return None                       # fitz 打不开 → 退回 pdfminer 全量解析，不冒险
    try:
        empty: set = set()
        for i in range(doc.page_count):
            try:
                if not doc[i].get_text().strip():
                    empty.add(i)
            except Exception:
                continue
        return empty
    finally:
        doc.close()


def extract_text_lines(pdf_path: str, prog=None) -> List[PageText]:
    """用 pdfminer.six 逐页抽取文本行（保留坐标与字号）。"""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTChar, LTTextLineHorizontal, LTTextContainer

    laparams = LAParams(line_margin=0.3, char_margin=2.0, boxes_flow=0.5)
    pages: List[PageText] = []

    # —— 无文字层页面预扫（2026-10-13）：根治 CAD 转曲线 PDF 卡死。
    #    pdfminer 的 `extract_pages` 是**逐页先完整解析再 yield**，即使 yield 后
    #    `continue` 也拦不住它对 1MB 曲线内容流的 `paint_path`/`get_data` 爆炸
    #    （rev15 第 3 页单页 53s）。所以这里**只在「有文字」的页号上**调 pdfminer，
    #    其余页直接产出空页，pdfminer 根本不碰那些转曲线/纯图页。
    no_text = _fitz_no_text_pages(pdf_path)
    try:
        import pymupdf as fitz
    except Exception:
        import fitz                       # type: ignore
    try:
        doc = fitz.open(pdf_path)
        total_pages = doc.page_count
    except Exception:
        total_pages = 0
        no_text = None                    # 页数都拿不到 → 退回全量 pdfminer
    finally:
        try:
            doc.close()
        except Exception:
            pass

    if no_text is None:
        # fitz 打不开 → 退回旧行为：pdfminer 全量解析（不做任何跳过）。
        text_page_numbers = None
    else:
        text_page_numbers = [i for i in range(total_pages) if i not in no_text]

    if text_page_numbers is not None and not text_page_numbers:
        # 整份 PDF 没有一页有文字层 → pdfminer 一页都不解析，直接产全空页。
        for i in range(total_pages):
            pages.append(PageText(index=i))
            if prog:
                prog(i + 1)
        return pages

    # 只解析「有文字层」的页；无文字页填 PageText(index) 占位（顺序保持连续）。
    extract_kwargs = {"laparams": laparams}
    if text_page_numbers is not None:
        extract_kwargs["page_numbers"] = text_page_numbers
    # `extract_pages(page_numbers=[...])` 只 yield 指定页，且按页码升序；
    # 用游标在 `text_page_numbers` 上逐一取回真实页号（pageid 不可靠，游标更稳）。
    by_index: Dict[int, PageText] = {}
    cursor = 0
    for layout in extract_pages(pdf_path, **extract_kwargs):
        if text_page_numbers is not None:
            pi = text_page_numbers[cursor]
            cursor += 1
        else:
            pi = len(pages)
        page = PageText(index=pi, width=float(getattr(layout, "width", 0) or 0),
                        height=float(getattr(layout, "height", 0) or 0))
        buckets: List[TextLine] = []

        def walk(obj):
            for el in obj:
                if isinstance(el, LTTextContainer):
                    if isinstance(el, LTTextLineHorizontal) or type(el).__name__ == "LTTextLineVertical":
                        txt = el.get_text()
                        if txt and txt.strip():
                            size, font = 0.0, ""
                            for ch in el:
                                if isinstance(ch, LTChar):
                                    size = max(size, float(getattr(ch, "size", 0) or 0))
                                    font = getattr(ch, "fontname", "") or font
                                    break
                            buckets.append(TextLine(
                                raw=txt.rstrip("\n"), x0=float(el.x0), top=float(el.y0),
                                x1=float(el.x1), bottom=float(el.y1), size=size, font=font))
                    else:
                        walk(el)
                elif hasattr(el, "__iter__"):
                    walk(el)

        walk(layout)

        # 同一视觉行可能被拆成多个 LTTextLine：按 top 归并
        buckets.sort(key=lambda t: (-t.top, t.x0))
        merged: List[TextLine] = []
        for ln in buckets:
            if merged and abs(merged[-1].top - ln.top) <= 1.5:
                prev = merged[-1]
                gap = ln.x0 - prev.x1
                # gap 必须 ≥ -1：y 相近但 x 明显往左跳 = 跨到了另一栏/上一行，
                # 硬并会把「名称：广东博迈医疗科技股份有限公司」和左栏正文缝成一句
                # （2026-09-29 GRA-4118 实测），后面整页 diff 跟着全错。
                if -1.0 <= gap < max(2.0, prev.size * 0.8):
                    sep = "" if gap < -0.5 else " "
                    prev.raw = prev.raw + sep + ln.raw
                    prev.x1 = max(prev.x1, ln.x1)
                    prev.bottom = max(prev.bottom, ln.bottom)
                    prev.top = min(prev.top, ln.top)
                    prev.x0 = min(prev.x0, ln.x0)
                    continue
            merged.append(ln)

        merged.sort(key=lambda t: (-t.top, t.x0))
        page.lines = merged
        by_index[pi] = page
        if prog:
            prog(pi + 1)
    # 无文字页补空占位，最终按页码升序输出（index 连续，下游按 pages[i] 取页）。
    if text_page_numbers is not None:
        for i in range(total_pages):
            pages.append(by_index.get(i, PageText(index=i)))
    else:
        pages.extend(by_index[i] for i in sorted(by_index))
    return pages


def mark_header_footer(pages: List[PageText]) -> None:
    """给每页的行打上 `is_edge` 标记：纵向落进页边带（上下各 8%）的行。

    ⚠️ 打标记 ≠ 要丢掉（2026-09-30 修正）。
    这里**只回答「它在不在页边」**，去留交给 `pdfdiff_edges.is_edge_noise()`
    按**内容**判定 —— 详见 `pdfdiff_edges` 模块开头那段：
    说明书的文件号/修订日期**就印在页脚**，按位置一刀切会把它们整段滤掉；
    而横向分栏的 IFU 里，页脚还会跟左栏正文**共享同一条水平线**，
    整行判定连正文一起丢（假一致）。带比例抽到 `edges.EDGE_BAND`，两条链路共用一个值。
    """
    for p in pages:
        if not p.height:
            continue
        for ln in p.lines:
            ln.is_edge = edges.in_edge_band(ln.top, ln.bottom, p.height)  # type: ignore[attr-defined]


# ---------------------------------------------------------------- 混合输入补行
# 病根与标注链路**同源**（见 `pdfdiff_locate._mixed_ocr_fill`）：一侧有文字层、
# 另一侧是扫描件/图片（旧版电子稿 vs 新版签字扫描件，或反过来）时，没有文字层那侧
# 在 pdfminer 里一行都读不到 → 整份文档被报成「全部删除」（反向「全部新增」），
# 而对面其实只改了几个字。
# 这里用**同一条判据、同一个 OCR 缓存**把空的那侧补成行，再走**同一条行级 diff** ——
# 两条链路口径因此天然一致（skill §4.11：「两条链路不一致」是最贵的一类 bug）。
MIXED_OCR = os.environ.get("PDFDIFF_MIXED_OCR", "1") != "0"


def _mixed_ocr_fill_core(pages_a: List[PageText], pages_b: List[PageText],
                         old_path: str, new_path: str) -> set:
    """给「**恰好一侧没有文字层**」的页补 OCR 行；返回**被补过的页号集合**。

    返回页号（而不是条数）是因为调用方要靠它做两件事：
      ① 那些页**不做表格比对** —— 一侧根本没有表格可言，硬比会把整表报成删除；
      ② 落在那些页上的结论**按 `ocr_line` 降档**（宁可报「待确认」，不可装准）。

    ⚠️ 只在「恰好一侧为空」时动手：两侧都有（普通对比）、两侧都没有（纯扫描件，
    交给扫描/转曲那条管线）、页数不等（页对齐另有其事）—— 一律不碰。
    """
    filled: set = set()
    if not MIXED_OCR or not pages_a or not pages_b:
        return filled
    if len(pages_a) != len(pages_b):
        return filled
    if not any(bool(p.lines) != bool(q.lines) for p, q in zip(pages_a, pages_b)):
        return filled                      # 没有任何一页是「单侧空」→ 快速短路
    try:
        import pdfdiff_locate as loc
        if not loc.ocr_available():
            return filled                  # OCR 不可用 → 退回旧行为，不猜
        try:
            import pymupdf as fitz
        except Exception:
            import fitz                    # type: ignore
        da, db = fitz.open(old_path), fitz.open(new_path)
    except Exception:
        return filled
    try:
        for pi, (pa, pb) in enumerate(zip(pages_a, pages_b)):
            ea, eb = not pa.lines, not pb.lines
            if ea == eb:
                continue
            tgt, doc, path = (pa, da, old_path) if ea else (pb, db, new_path)
            try:
                rows = loc.ocr_page_lines_pt(doc[pi], path, pi)
            except Exception:
                continue                   # 单页 OCR 失败 → 这页保持原样
            tgt.lines = [TextLine(raw=t, x0=x0, top=top, x1=x1, bottom=bottom,
                                  size=0.0, font="ocr", src="ocr")
                         for t, x0, top, x1, bottom in rows]
            if tgt.lines:
                filled.add(pi)
    finally:
        da.close()
        db.close()
    return filled


# ==================================================================
# 第 3 节  表格层提取（pdfplumber，底层同为 pdfminer）
# ==================================================================

@dataclass
class PageTable:
    index: int
    bbox: Tuple[float, float, float, float]
    nrows: int
    ncols: int
    rows: List[List[str]]


def extract_tables(pdf_path: str, prog=None) -> List[List[PageTable]]:
    """返回每页的表格列表；任一页失败则该页为空列表，不中断整体。"""
    import pdfplumber
    out: List[List[PageTable]] = []

    # —— 无文字层页面预扫（2026-10-13）：pdfplumber 的 find_tables 底层同样是
    #    pdfminer，对「CAD 转曲线 PDF」的巨型内容流会逐 token 解析卡死（rev15 第 3 页
    #    1MB 曲线 → get_edges 60s+）。fitz 判「这页没文字」就整页跳过表格提取。
    no_text = _fitz_no_text_pages(pdf_path)

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for pi, page in enumerate(pdf.pages):
                if no_text is not None and pi in no_text:
                    out.append([])         # 无文字层 → 不可能有表格，跳过 find_tables
                    if prog:
                        prog(pi + 1)
                    continue
                tables: List[PageTable] = []
                try:
                    for ti, tb in enumerate(page.find_tables()):
                        grid = tb.extract() or []
                        grid = [["" if c is None else str(c).replace("\n", " ").strip() for c in r] for r in grid]
                        if not grid:
                            continue
                        ncols = max(len(r) for r in grid)
                        for r in grid:
                            if len(r) < ncols:
                                r.extend([""] * (ncols - len(r)))
                        tables.append(PageTable(index=ti, bbox=tuple(tb.bbox),
                                                nrows=len(grid), ncols=ncols, rows=grid))
                except Exception:
                    pass
                out.append(tables)
                if prog:
                    prog(pi + 1)
    except Exception:
        out = []
    return out


def line_in_any_table(ln: TextLine, tables: List[PageTable], page_h: float) -> bool:
    """判断该文本行是否落在表格区域内（避免文本层与表格层重复报差异）。"""
    if not tables or not page_h:
        return False
    cy_t = page_h - ln.cy  # 转成 pdfplumber 坐标系（原点左上）
    cx = (ln.x0 + ln.x1) / 2.0
    for tb in tables:
        x0, t0, x1, t1 = tb.bbox
        if x0 - 2 <= cx <= x1 + 2 and t0 - 2 <= cy_t <= t1 + 2:
            return True
    return False


# ==================================================================
# 第 4 节  页面对齐
# ==================================================================

def page_fingerprint(p: PageText) -> List[str]:
    return [ln.raw for ln in p.lines]


def page_similarity(a: PageText, b: PageText) -> float:
    sa = set(normalize_text(x, Options()) for x in page_fingerprint(a))
    sb = set(normalize_text(x, Options()) for x in page_fingerprint(b))
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / max(1, len(sa | sb))


def align_pages(old: List[PageText], new: List[PageText], threshold: float
                ) -> List[Tuple[Optional[int], Optional[int], float]]:
    """返回 [(old_idx, new_idx, similarity), ...]，None 表示该页在另一侧不存在。"""
    pairs: List[Tuple[Optional[int], Optional[int], float]] = []
    n = min(len(old), len(new))
    used_new = set()
    for i in range(n):
        pairs.append((i, i, 1.0))
        used_new.add(i)

    rest_old = list(range(n, len(old)))
    rest_new = list(range(n, len(new)))
    matched_old, matched_new = set(), set()
    if rest_old and rest_new:
        cand = []
        for i in rest_old:
            for j in rest_new:
                s = page_similarity(old[i], new[j])
                if s >= threshold:
                    cand.append((s, i, j))
        cand.sort(reverse=True, key=lambda x: x[0])
        for s, i, j in cand:
            if i in matched_old or j in matched_new:
                continue
            pairs.append((i, j, s))
            matched_old.add(i)
            matched_new.add(j)
    for i in rest_old:
        if i not in matched_old:
            pairs.append((i, None, 0.0))
    for j in rest_new:
        if j not in matched_new:
            pairs.append((None, j, 0.0))
    return pairs


# ==================================================================
# 第 5 节  行级比对
# ==================================================================

@dataclass
class DiffItem:
    id: int = 0
    kind: str = ""            # added / removed / modified / numeric / page_added / page_removed / meta
    scope: str = "text"       # text / table / page / meta
    page_old: Optional[int] = None
    page_new: Optional[int] = None
    x: Optional[float] = None
    y: Optional[float] = None
    # 改动块在**页面上**的纵向范围（pdfminer 坐标，左上原点、pt）。
    # 标注时要靠它把搜索**裁剪到这几行内**：整页乱搜会把同名文字（公司名、产品名）
    # 全部标上 —— 2026-09-29 GRA-4118 实测误标一片，坐标框住之后才准。
    y2: Optional[float] = None      # 旧侧块底部
    nx: Optional[float] = None      # 新侧块左边
    ny: Optional[float] = None      # 新侧块顶部
    ny2: Optional[float] = None     # 新侧块底部
    old: str = ""
    new: str = ""
    context: str = ""         # 表格列名 / 行键 / 页号等
    row: Optional[int] = None
    col: Optional[int] = None
    similarity: float = 0.0
    note: str = ""
    # —— 2026-09-29 追加：段级「前后文」——
    # 界面要能像看 diff 一样知道「这条改动在原文的哪儿」，只有变动行是不够的：
    # 第一页最左侧新增的那一句话，光看它自己根本不知道插在哪个位置。
    # 所以比对时顺手记下**页面上紧邻的上一行 / 下一行原文**（见 _attach_ctx）。
    ctx_before: str = ""
    ctx_after: str = ""

    # ---- 置信度（2026-10-10 加，字段全带默认值 ⇒ 老调用方零回归风险）----
    #   conf     = 这条差异**是真的吗**（来源可靠性，见 `SRC_CONF`）
    #   loc_conf = 框的**位置准不准**（坐标是精确给的，还是估出来的）
    # 两者取低者定档（见 `conf_level`）：存在性再确定，位置是估的也要降档。
    conf: float = 0.0
    loc_conf: float = 0.0
    src: str = ""          # text / docx / ocr_line / ocr_est / graphic

    @property
    def level(self) -> str:
        """`high` / `mid` / `low`。"""
        return conf_level(self.conf, self.loc_conf)

    def set_src(self, src: str, conf: Optional[float] = None,
                loc_conf: Optional[float] = None) -> "DiffItem":
        """按来源打标（返回自身便于链式调用）。显式传值可覆盖来源默认值。"""
        c, lc = conf_of(src)
        self.src = src
        self.conf = c if conf is None else conf
        self.loc_conf = lc if loc_conf is None else loc_conf
        return self

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    # ---- 统一的位置读取 ----
    # ⚠️ 坐标字段的语义（两条比对路径必须一致，见 diff_lines / diff_page_text）：
    #     x / y / y2      → 这条内容在**旧版**页面上的位置
    #     nx / ny / ny2   → 这条内容在**新版**页面上的位置
    #   哪一侧没有这条内容（纯增/纯删），那一组就是 None。
    #   界面按「段」聚合时要判「上下相邻 / 左右同栏」，读的就是下面这几个。
    @property
    def page_key(self) -> int:
        """排序/分组用的页号（两侧取其一，都没有= 元数据，排最前）。"""
        p = self.page_new if self.page_new is not None else self.page_old
        return -1 if p is None else p

    @property
    def y_key(self) -> Optional[float]:
        """排序用的纵向基准。**优先新版**：新版是「改成什么样」的共同参照，
        每条改动基本都能在新版找到位置；纯删除的条目没有新侧坐标，退到旧版。
        ⚠️ 只用于**排序**。判「两条是不是同一段」必须用**同一侧**的坐标 ——
        改版会整段上移下移，旧侧 y=375 和新侧 y=373 看着只差 2pt，
        实际是页面两处不同的地方（实测 GRA-4118 第一页会误合）。
        """
        return self.ny if self.ny is not None else self.y

    @property
    def x_key(self) -> Optional[float]:
        """横向位置（判是否同一栏），口径与 `y_key` 一致。"""
        return self.nx if self.nx is not None else self.x


class _IdGen:
    def __init__(self):
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def _mk(gen: _IdGen, kind: str, scope: str, page_old, page_new,
        old="", new="", x=None, y=None, ctx="", row=None, col=None, sim=0.0, note="",
        y2=None, nx=None, ny=None, ny2=None, src="") -> DiffItem:
    # ⚠️ 这里**不能** clip：pdfplumber 常把整页表格抽成一个巨型单元格，
    # 真实差异往往在第 300 字之后 —— 截断后 GUI 拿残文做对比会「零高亮 + 误报」，
    # 详情里的「…」也不是省略号而是被砍掉的正文（2026-09-29 GRA-4118 实测）。
    it = DiffItem(id=gen.next(), kind=kind, scope=scope, page_old=page_old, page_new=page_new,
                  x=x, y=y, y2=y2, nx=nx, ny=ny, ny2=ny2,
                  old=str(old) if old else "", new=str(new) if new else "",
                  context=ctx, row=row, col=col, similarity=round(sim, 4), note=note)
    # 置信度来源：没显式传就按 scope 推断，且**推断偏保守**（宁可报低，不可装准）——
    # scope="graphic" 走扫描/转曲兜底，默认当最低的「像素区域」；调用方能判得更细时
    # （OCR 认出了字、坐标是估算的…）应当显式传 src 覆盖。
    it.set_src(src or ("graphic" if scope == "graphic" else "text"))
    return it


def _pos(la, lb) -> Dict[str, Any]:
    """按「差异类型」把行坐标摊成 `_mk` 的坐标参数 —— 两侧各归各的字段。

    | 差异类型 | 旧侧（la） | 新侧（lb） |
    |---|---|---|
    | removed | ✓ | — |
    | added   | — | ✓ |
    | modified / numeric | ✓ | ✓ |

    这样 `x/y/y2` 永远是旧版位置、`nx/ny/ny2` 永远是新版位置，
    界面的「段聚合」和 OCR 撤销都能按同一套规矩读，不用猜这条是哪一侧的。
    """
    d: Dict[str, Any] = {}
    if la is not None:
        d.update(x=la.x0, y=la.top, y2=la.bottom)
    if lb is not None:
        d.update(nx=lb.x0, ny=lb.top, ny2=lb.bottom)
    return d


def diff_lines(old_lines: List[TextLine], new_lines: List[TextLine], opts: Options, gen: _IdGen,
               page_old, page_new, scope="text", ctx_fn=None) -> List[DiffItem]:
    """对两份行序列做 diff，块内递归细化。"""
    items: List[DiffItem] = []

    def _block(a: List[TextLine], b: List[TextLine], depth: int):
        if not a and not b:
            return
        if not a:
            for ln in b:
                items.append(_mk(gen, "added", scope, page_old, page_new, new=ln.raw,
                                 ctx=ctx_fn(ln) if ctx_fn else "", **_pos(None, ln)))
            return
        if not b:
            for ln in a:
                items.append(_mk(gen, "removed", scope, page_old, page_new, old=ln.raw,
                                 ctx=ctx_fn(ln) if ctx_fn else "", **_pos(ln, None)))
            return

        na = [normalize_text(x.raw, opts) for x in a]
        nb = [normalize_text(x.raw, opts) for x in b]
        sm = SequenceMatcher(None, na, nb, autojunk=False)
        block_ratio = sm.ratio()

        if depth < opts.max_depth and len(a) > 1 and len(b) > 1 and block_ratio >= 0.30:
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    continue
                _block(a[i1:i2], b[j1:j2], depth + 1)
            return

        # 底层：逐对比较，剩余单挂
        n = min(len(a), len(b))
        for k in range(n):
            la, lb = a[k], b[k]
            ra, rb = na[k], nb[k]
            if ra == rb:
                # ⚠️ 2026-10-12 文字层位移（浅蓝）+ 字体变化（深蓝）：文本一字不差但
                #   **位置变了**或**字号/字体变了**。旧口径 `continue` 静默跳过。
                #   字体变化**优先**于位移（M4506 的 10℃ 既字体变又位移，用户拍板归
                #   深蓝，同 locate 层「font 先于 moved」）。字号用 fitz 回填的渲染
                #   字号（见 _backfill_fitz_sizes）。
                if opts.font_diff and _font_changed(la, lb):
                    items.append(_mk(gen, "font", scope, page_old, page_new,
                                     old=la.raw, new=lb.raw, sim=1.0,
                                     note=_font_note(la, lb),
                                     ctx=ctx_fn(lb) if ctx_fn else "",
                                     **_pos(la, lb)))
                    continue
                dy = None
                if la.top is not None and lb.top is not None:
                    dy = abs(lb.top - la.top)
                if dy is not None and dy >= _MOVED_MIN_DY:
                    items.append(_mk(gen, "moved", scope, page_old, page_new,
                                     old=la.raw, new=lb.raw, sim=1.0,
                                     ctx=ctx_fn(lb) if ctx_fn else "",
                                     **_pos(la, lb)))
                continue
            # 2026-10-13 用户拍板：彻底删掉「变动 / 修改 / 数值变动」档 ——
            #   任何内容差异都只拆成「删（旧值标红）+ 增（新值标绿）」两条，
            #   不再有第三档，也不保留数值的增量/百分比备注。
            items.append(_mk(gen, "removed", scope, page_old, page_new, old=la.raw,
                             ctx=ctx_fn(la) if ctx_fn else "", **_pos(la, None)))
            items.append(_mk(gen, "added", scope, page_old, page_new, new=lb.raw,
                             ctx=ctx_fn(lb) if ctx_fn else "", **_pos(None, lb)))
        for la in a[n:]:
            items.append(_mk(gen, "removed", scope, page_old, page_new, old=la.raw,
                             ctx=ctx_fn(la) if ctx_fn else "", **_pos(la, None)))
        for lb in b[n:]:
            items.append(_mk(gen, "added", scope, page_old, page_new, new=lb.raw,
                             ctx=ctx_fn(lb) if ctx_fn else "", **_pos(None, lb)))

    _block(old_lines, new_lines, 0)
    return items


def prepare_lines(page: Optional[PageText], opts: Options, drop_table_lines: bool = False,
                  tables: Optional[List[PageTable]] = None) -> List[TextLine]:
    """过滤掉被忽略的行（页眉页脚 / 正则 / 过短 / 表格内）。

    ⚠️ 「忽略页眉页脚」自 2026-09-30 起是**内容判定**，不是位置判定：
    落在页边带（`mark_header_footer` 打的 `is_edge`）**并且**内容属于
    「页码 / 纯符号」这类无信息行，才真的丢掉。原因见 `pdfdiff_edges`：
    文件号、修订日期就印在页脚，按位置一刀切会让清单凭空少掉整段真改动；
    横向分栏版式里页脚还与左栏正文同行，整行判定会连正文一起丢。
    """
    if page is None:
        return []
    keep = []
    for ln in page.lines:
        raw = ln.raw.strip()
        if len(raw) < opts.min_line_len:
            continue
        if opts.skip_line(raw):
            continue
        if (opts.ignore_header_footer and getattr(ln, "is_edge", False)
                and edges.is_edge_noise(raw)):
            continue
        if drop_table_lines and tables and line_in_any_table(ln, tables, page.height):
            continue
        keep.append(ln)
    return keep


# ==================================================================
# 第 6 节  表格级比对
# ==================================================================

_IDX_CELL_RE = re.compile(r"^(step|步骤|no\.?|序号|#|item)\s*[\d\.]+$", re.I)
_PURE_NUM_RE = re.compile(r"^[\d\s\.\-—~/x×]+$")


def _row_key(row: Sequence[str], ncols: int) -> str:
    """行键：优先「参数名」这类有语义的单元格，其次才是序号列。
    表格里第一列常常只是步骤号（1/2/3），拿它当行键会让差异项看不出改的是哪个参数。"""
    o = Options()
    cells = [str(c).strip() for c in row if c and str(c).strip()]
    if not cells:
        return normalize_text("|".join(str(c) for c in row), o)
    for c in cells:
        n = normalize_text(c, o)
        if n and not _PURE_NUM_RE.match(n) and not _IDX_CELL_RE.match(n):
            return n
    return normalize_text(cells[0], o)


def match_tables(old_t: List[PageTable], new_t: List[PageTable]) -> List[Tuple[Optional[int], Optional[int]]]:
    """按索引优先配对；数量不等时按列数+首行相似度贪心配对。"""
    pairs: List[Tuple[Optional[int], Optional[int]]] = []
    n = min(len(old_t), len(new_t))
    for i in range(n):
        pairs.append((i, i))
    ro, rn = list(range(n, len(old_t))), list(range(n, len(new_t)))
    used_o, used_n = set(), set()
    if ro and rn:
        cand = []
        for i in ro:
            for j in rn:
                a, b = old_t[i], new_t[j]
                shape_s = 1.0 if a.ncols == b.ncols else 0.4
                head_s = ratio("|".join(a.rows[0] if a.rows else []), "|".join(b.rows[0] if b.rows else []))
                cand.append((shape_s * 0.5 + head_s * 0.5, i, j))
        cand.sort(reverse=True, key=lambda x: x[0])
        for s, i, j in cand:
            if s < 0.35 or i in used_o or j in used_n:
                continue
            pairs.append((i, j))
            used_o.add(i)
            used_n.add(j)
    for i in ro:
        if i not in used_o:
            pairs.append((i, None))
    for j in rn:
        if j not in used_n:
            pairs.append((None, j))
    return pairs


def diff_tables(old_t: List[PageTable], new_t: List[PageTable], opts: Options, gen: _IdGen,
                page_old, page_new) -> Tuple[List[DiffItem], bool]:
    """返回 (差异项, 是否成功按结构化比对)。"""
    items: List[DiffItem] = []
    if not old_t and not new_t:
        return items, False

    pairs = match_tables(old_t, new_t)
    any_struct = False

    for oi, ni in pairs:
        to = old_t[oi] if oi is not None else None
        tn = new_t[ni] if ni is not None else None
        ctx_t = f"表{oi + 1 if oi is not None else '?'}"

        if to is None and tn is not None:
            items.append(_mk(gen, "added", "table", page_old, page_new, new="（整张表）",
                             ctx=f"{ctx_t} 新增", note=f"{tn.nrows}行×{tn.ncols}列"))
            continue
        if tn is None and to is not None:
            items.append(_mk(gen, "removed", "table", page_old, page_new, old="（整张表）",
                             ctx=f"{ctx_t} 删除", note=f"{to.nrows}行×{to.ncols}列"))
            continue

        any_struct = True
        ro, rn = to.rows, tn.rows

        # 表头行：前 min(1, nrows) 行当表头（简单策略：列数一致且首行含空/短文本时视为表头）
        header_n = _guess_header_rows(to, tn)
        col_names = _build_col_names(rn[0] if (header_n and rn) else [], tn.ncols)

        body_o = [[normalize_text(c, opts) for c in r] for r in ro[header_n:]]
        body_n = [[normalize_text(c, opts) for c in r] for r in rn[header_n:]]

        key_o = [_row_key(r, to.ncols) for r in body_o]
        key_n = [_row_key(r, tn.ncols) for r in body_n]

        def _row_diff(kind: str, rows: List[List[str]], rowno: int):
            """整行删/增的两种 kind（removed/added）共用一个 `_mk` 骨架。"""
            text = " | ".join(rows[header_n + rowno])
            kw = {"old": text} if kind == "removed" else {"new": text}
            act = "删除" if kind == "removed" else "新增"
            items.append(_mk(gen, kind, "table", page_old, page_new, row=header_n + rowno + 1,
                             ctx=f"{ctx_t} 第{header_n + rowno + 1}行{act}", **kw))

        sm = SequenceMatcher(None, key_o, key_n, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    items.extend(_diff_row(gen, page_old, page_new, ctx_t,
                                           header_n + i1 + k, ro[header_n + i1 + k],
                                           header_n + j1 + k, rn[header_n + j1 + k],
                                           col_names, opts))
            elif tag == "replace":
                n = min(i2 - i1, j2 - j1)
                for k in range(n):
                    items.extend(_diff_row(gen, page_old, page_new, ctx_t,
                                           header_n + i1 + k, ro[header_n + i1 + k],
                                           header_n + j1 + k, rn[header_n + j1 + k],
                                           col_names, opts))
                for k in range(i1 + n, i2):
                    _row_diff("removed", ro, k)
                for k in range(j1 + n, j2):
                    _row_diff("added", rn, k)
            elif tag == "delete":
                for k in range(i1, i2):
                    _row_diff("removed", ro, k)
            elif tag == "insert":
                for k in range(j1, j2):
                    _row_diff("added", rn, k)

    return items, any_struct


MAX_AVG_CELL = 150    # **平均每格**字数超过它 = 整页被塞进几个格子
MIN_TOTAL_CHARS = 300  # 文字太少的小表不参与判定（避免空表误判）


def _tables_blown_up(tabs: Sequence[PageTable]) -> bool:
    """表格抽歪了吗？——判据是**平均每格字数**，不是「有没有一个长格」。

    ⚠️ 别用「单格 > N 字」判断：真表格里本来就有长文本格
    （实测 WI-2013 的 22×13 表格里有 1695 字的格子 —— 那是操作步骤描述列，
    不是抽歪）。真正抽歪长这样：整页文字被塞进**少数几个格子**，
    于是平均每格好几百字（GRA-4118 实测 1018 字/格）。
    """
    for t in tabs or []:
        cells = [len(str(c or "")) for row in t.rows for c in row]
        cells = [n for n in cells if n]
        if len(cells) < 2:
            continue
        total = sum(cells)
        if total >= MIN_TOTAL_CHARS and total / len(cells) > MAX_AVG_CELL:
            return True
    return False


def _reading_order(lines):
    """把行按**视觉阅读顺序**排好：先上后下、同行从左到右。

    ⚠️ pdfminer 给的是 PDF 内容流顺序 —— 改版时作者挪一下元素，抽取顺序就变了，
    两版顺序不一致，后面不管行级还是字符级 diff 都会整段错位
    （实测 GRA-4118：旧版「维护与保养」被对上新版「6.0 警示」）。
    按坐标重排之后，顺序只跟排版有关，跟 PDF 内部怎么写无关。
    """
    # y 先按 4pt 归并成一行（行高约 7pt，避免浮点抖动把同一行拆开）
    return sorted(lines, key=lambda l: (-round((l.top or 0) / 4.0), l.x0 or 0))


PAIR_SIM = 0.62      # 差集里两条行相似度 ≥ 这个值 → 判「改了」而不是「删+增」


def diff_page_text(po, pn, opts: Options, gen: _IdGen, oi, ni) -> List[DiffItem]:
    """「表格抽歪」的页：按**行集合**比对（不看顺序）。

    为什么不能用整页字符串 diff：改版时版面会重排（插入一条禁忌症，后面全部下移），
    抽取顺序/位置都对不上了 —— 字符串 diff 会整段错位，行级 diff 会把每一行
    都报成「删除+新增」（实测 GRA-4118：166 条假差异）。
    **说明书改版关心的是内容增减，不是位置挪了没挪**，所以：
      ① 两版都有的行 → 一样（位置变了不算改动）
      ② 只在旧版 → 删除；只在新版 → 新增
      ③ 差集里彼此很像的（≥ PAIR_SIM）→ 配对成「修改」（13.0→14.0 这种）
    行是完整短语，标注时也好定位。
    """
    la = _reading_order(prepare_lines(po, opts))
    lb = _reading_order(prepare_lines(pn, opts))
    A = [(ln, normalize_text(ln.raw, opts)) for ln in la]
    B = [(ln, normalize_text(ln.raw, opts)) for ln in lb]
    A = [(ln, n) for ln, n in A if n]
    B = [(ln, n) for ln, n in B if n]

    ca = Counter(n for _, n in A)
    cb = Counter(n for _, n in B)

    # ⚠️ 2026-10-12 文字层位移（浅蓝）：第 ① 步「两版都有的行」原先直接抵消
    #   （`ca - cb` 里消失），位置变了也当无差异 —— M4506 右栏整列下移 8~25pt
    #   整片漏标（moved=0 的根因）。这里在抵消**之前**，把「文本相同的行」按
    #   位置配对：纵向差 ≥ _MOVED_MIN_DY 的 → 产出 moved，并从两边的计数里
    #   各扣掉一份（不再进差集）。
    moved_items: List[DiffItem] = []
    font_items: List[DiffItem] = []
    common = {n: min(ca[n], cb[n]) for n in ca if n in cb}
    if common:
        by_norm_a: Dict[str, List[Any]] = {}
        for ln, n in A:
            by_norm_a.setdefault(n, []).append(ln)
        by_norm_b: Dict[str, List[Any]] = {}
        for ln, n in B:
            by_norm_b.setdefault(n, []).append(ln)
        for n, k in common.items():
            la_list = by_norm_a.get(n, [])
            lb_list = by_norm_b.get(n, [])
            used_b: List[bool] = [False] * len(lb_list)
            for la_ in la_list:
                # 找对面位置最近（纵向上）的同文本行
                best_j, best_dy = None, None
                for j, lb_ in enumerate(lb_list):
                    if used_b[j]:
                        continue
                    dy = abs((lb_.top or 0) - (la_.top or 0))
                    if best_dy is None or dy < best_dy:
                        best_dy, best_j = dy, j
                if best_j is None:
                    continue
                lb_ = lb_list[best_j]
                # ⚠️ 字体变化**优先**于位移（M4506 的 10℃ 既字体变又位移，用户拍板
                #   归深蓝，同 locate 层「font 先于 moved」的口径）。字号用 fitz 回填
                #   的渲染字号（见 _backfill_fitz_sizes）。
                if opts.font_diff and _font_changed(la_, lb_):
                    font_items.append(_mk(
                        gen, "font", "text", oi, ni,
                        old=la_.raw.strip(), new=lb_.raw.strip(), sim=1.0,
                        note=_font_note(la_, lb_),
                        ctx=f"第{oi + 1}页 正文",
                        x=la_.x0, y=la_.top, y2=la_.bottom,
                        nx=lb_.x0, ny=lb_.top, ny2=lb_.bottom))
                # ⚠️ 过短的行（< _MOVED_MIN_CHARS 个实义字符）不做位移判定：
                #   pdfminer 常把「证」「：」「标识」这类孤立短字拆成独立一行，
                #   位置抖动是常态，判成位移只是噪声（11vs12 实测 44 条里
                #   混着一堆单字碎片）。短行位置变了照旧抵消（原语义）。
                elif best_dy >= _MOVED_MIN_DY \
                        and len(la_.raw.strip()) >= _MOVED_MIN_CHARS:
                    moved_items.append(_mk(
                        gen, "moved", "text", oi, ni,
                        old=la_.raw.strip(), new=lb_.raw.strip(), sim=1.0,
                        ctx=f"第{oi + 1}页 正文",
                        x=la_.x0, y=la_.top, y2=la_.bottom,
                        nx=lb_.x0, ny=lb_.top, ny2=lb_.bottom))
                # 无论是否位移/字体，这一对都从差集里扣掉（维持原抵消语义）
                used_b[best_j] = True
                ca[n] -= 1
                cb[n] -= 1

    rest_a = _leftover(A, {n: c for n, c in ca.items() if c > 0})
    rest_b = _leftover(B, {n: c for n, c in cb.items() if c > 0})

    pairs: List[Tuple[Any, Any]] = []       # (旧行, 新行) 判为「修改」
    for it_a in list(rest_a):
        best, bs = None, 0.0
        for it_b in rest_b:
            s = ratio(it_a[1], it_b[1])
            if s > bs:
                bs, best = s, it_b
        if best is not None and bs >= PAIR_SIM:
            pairs.append((it_a, best))
            rest_a.remove(it_a)
            rest_b.remove(best)

    items: List[DiffItem] = []
    for (la_, na_), (lb_, nb_) in pairs:
        a, b = la_.raw.strip(), lb_.raw.strip()
        # 2026-10-13 删「变动」档：配对上的两侧行统一拆成删+增两条。
        items.append(_mk(gen, "removed", "text", oi, ni, old=a,
                         ctx=f"第{oi + 1}页 正文",
                         x=la_.x0, y=la_.top, y2=la_.bottom))
        items.append(_mk(gen, "added", "text", oi, ni, new=b,
                         ctx=f"第{oi + 1}页 正文",
                         nx=lb_.x0, ny=lb_.top, ny2=lb_.bottom))
    for ln, _n in rest_a:
        items.append(_mk(gen, "removed", "text", oi, ni, old=ln.raw.strip(),
                         ctx=f"第{oi + 1}页 正文", x=ln.x0, y=ln.top, y2=ln.bottom))
    for ln, _n in rest_b:
        items.append(_mk(gen, "added", "text", oi, ni, new=ln.raw.strip(),
                         ctx=f"第{oi + 1}页 正文", nx=ln.x0, ny=ln.top, ny2=ln.bottom))
    items.extend(font_items)
    items.extend(moved_items)
    items.sort(key=lambda it: (-(it.y or 0), it.x or 0))
    return items


def _leftover(pairs_list, counter):
    """按「差集计数」取回对应的行对象（同一个归一化值可能出现多行）。"""
    pool: Dict[str, List[Any]] = {}
    for ln, n in pairs_list:
        pool.setdefault(n, []).append((ln, n))
    out: List[Any] = []
    for n, k in counter.items():
        bucket = pool.get(n, [])
        for i in range(min(k, len(bucket))):
            out.append(bucket[i])
    return out


# ==================================================================
# 第 6.5 节  段级「前后文」
# ==================================================================

CTX_LINES = 6       # 「前后文」上下各取几行的**默认值**（运行时取 opts.ctx_lines）
CTX_X_PAD = 60.0    # 取上下文时允许的横向偏差 (pt)：只认「同一栏」的上下行
CTX_LINE_SEP = "\n"  # 上下文内部的分行符 —— 界面要按行分色，所以**保留行边界**
_CTX_HAS_WORD = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


def _ctx_index(page: Optional[PageText], opts: Options) -> List[Tuple[float, float, str]]:
    """把一页的行整理成 `[(y, x0, 文本)]`，按 y 从大到小（页面上 → 下）。

    ⚠️ **不要按 y 把「同一视觉行」的各栏并成一条**：这份横向 IFU 一页里同一高度
    并排放着好几块互不相干的内容（左栏正文 / 中间图标列 / 右栏地址），
    并起来会得到「严重肾功能衰竭患者 名称：广东博迈… 生产地址：…」这种
    三块拼接的怪东西，不像上下文。改成**取的时候按 x 找同栏**（见 `_ctx_around`），
    上下数出来的才是「这一栏里紧邻的上一行 / 下一行」。

    行源用 `prepare_lines`（去掉页眉页脚与忽略正则命中的行），并丢掉纯符号行
    （孤零零的 `•` `♦`）：上下文是给人看的，页码和项目符号只会添乱。
    """
    if page is None:
        return []
    out: List[Tuple[float, float, str]] = []
    for ln in prepare_lines(page, opts):
        raw = (ln.raw or "").strip()
        if not raw or not _CTX_HAS_WORD.search(raw):
            continue
        out.append((float(ln.top or 0), float(ln.x0 or 0), raw))
    out.sort(key=lambda t: (-t[0], t[1]))
    return out


def _ctx_around(index: List[Tuple[float, float, str]], y: Optional[float],
                x: Optional[float] = None, n: int = CTX_LINES) -> Tuple[str, str]:
    """取「同一栏里」y **上方** n 行 / **下方** n 行的原文（各拼成一段，自上而下）。

    `x` 用来限定栏：只认与它横坐标相近的行，否则取到的会是并排另一栏的内容。
    `x is None` 时不做横向限制（表格等没有横向位置的情形）。
    同一行的抖动按 ±1pt 容差排除，免得把改动自己那一行也当成上下文。
    """
    if not index or y is None or n <= 0:
        return "", ""
    if x is not None:
        rows = [t for t in index if abs(t[1] - x) <= CTX_X_PAD]
        if not rows:
            rows = index                      # 这一栏认不出来就退回全页，宁可多给
    else:
        rows = index
    keys = [-t[0] for t in rows]              # 升序，供 bisect（-y 升序 == y 降序）
    k = bisect.bisect_left(keys, -(y + 1.0))       # 第一个 y <= 本行+1 → 前面全是「上方行」
    above = [t[2] for t in rows[max(0, k - n):k]]
    k2 = bisect.bisect_right(keys, -(y - 1.0))     # 第一个 y < 本行-1 → 后面才是「下方行」
    below = [t[2] for t in rows[k2:k2 + n]]
    # ⚠️ 用换行拼、不用空格：界面要**按行分色**（上下文灰、本段正常），
    # 拼成一长条就没法分行，也就没法让眼睛分清「哪句是改的」。
    return (CTX_LINE_SEP.join(x for x in above if x),
            CTX_LINE_SEP.join(x for x in below if x))


def _attach_ctx(items_page: List[DiffItem], po: Optional[PageText],
                pn: Optional[PageText], opts: Options) -> None:
    """给本页新产出的条目补上「前后文」（原地改，不返回）。

    两条比对路径（`diff_lines` / `diff_page_text`）**都**从这里统一补，
    所以坐标语义必须先统一（见 `_pos`）：`y/x` = 旧侧位置、`ny/nx` = 新侧位置。
    优先按旧侧取（详情左栏是旧版那一段）；纯新增/纯删除的条目缺一侧坐标，
    自然退到有坐标的那一侧，取到的仍是「它左右邻居长什么样」。
    """
    if not items_page:
        return
    io = _ctx_index(po, opts)
    inew = _ctx_index(pn, opts)
    n = max(0, int(getattr(opts, "ctx_lines", CTX_LINES)))
    for it in items_page:
        b, a = _ctx_around(io, it.y, it.x, n)
        if not b and not a:
            b, a = _ctx_around(inew, it.ny, it.nx, n)
        it.ctx_before, it.ctx_after = b, a


def _guess_header_rows(a: PageTable, b: PageTable) -> int:
    """两侧首行完全一致则视为 1 行表头，否则 0（保守，避免吃掉数据行）。"""
    if not a.rows or not b.rows:
        return 0
    return 1 if normalize_text("|".join(a.rows[0]), Options()) == normalize_text("|".join(b.rows[0]), Options()) else 0


def _build_col_names(header: List[str], ncols: int) -> List[str]:
    names = list(header) if header else []
    while len(names) < ncols:
        names.append("")
    return names[:ncols]


def _diff_row(gen: _IdGen, page_old, page_new, ctx_t: str, ro_idx: int, row_o: Sequence[str],
              rn_idx: int, row_n: Sequence[str], col_names: List[str], opts: Options) -> List[DiffItem]:
    """同一行逐单元格比对。"""
    out: List[DiffItem] = []
    n = max(len(row_o), len(row_n))
    for c in range(n):
        a = row_o[c] if c < len(row_o) else ""
        b = row_n[c] if c < len(row_n) else ""
        na, nb = normalize_text(a, opts), normalize_text(b, opts)
        if na == nb:
            continue
        cname = col_names[c] if c < len(col_names) and col_names[c] else f"第{c + 1}列"
        rk = _row_key(row_n, len(row_n)) or _row_key(row_o, len(row_o))
        ctx = f"{ctx_t} 第{rn_idx + 1}行 · {cname}"
        s = ratio(na, nb)
        # 2026-10-13 删「变动」档：单元格内容差异统一拆成删+增两条。
        if not a.strip():
            out.append(_mk(gen, "added", "table", page_old, page_new, new=b,
                           row=rn_idx + 1, col=c + 1, sim=s, ctx=f"{ctx} · {rk}"))
        elif not b.strip():
            out.append(_mk(gen, "removed", "table", page_old, page_new, old=a,
                           row=rn_idx + 1, col=c + 1, sim=s, ctx=f"{ctx} · {rk}"))
        else:
            out.append(_mk(gen, "removed", "table", page_old, page_new, old=a,
                           row=rn_idx + 1, col=c + 1, sim=s, ctx=f"{ctx} · {rk}"))
            out.append(_mk(gen, "added", "table", page_old, page_new, new=b,
                           row=rn_idx + 1, col=c + 1, sim=s, ctx=f"{ctx} · {rk}"))
    return out


# ==================================================================
# 第 7 节  元数据比对
# ==================================================================

_META_LABEL = {
    "Title": "标题", "Author": "作者", "Subject": "主题", "Creator": "创建程序",
    "Producer": "生成器", "CreationDate": "创建时间", "ModDate": "修改时间",
}


def read_meta(pdf_path: str) -> Dict[str, Any]:
    import pdfplumber
    try:
        with pdfplumber.open(pdf_path) as pdf:
            info = {k: str(v) for k, v in (pdf.metadata or {}).items()}
            info["Pages"] = str(len(pdf.pages))
            return info
    except Exception:
        return {}


def diff_meta(old_path: str, new_path: str, gen: _IdGen) -> List[DiffItem]:
    items: List[DiffItem] = []
    mo, mn = read_meta(old_path), read_meta(new_path)
    for k in sorted(set(mo) | set(mn)):
        a, b = mo.get(k, ""), mn.get(k, "")
        if a == b:
            continue
        if a and b:
            items.append(_mk(gen, "removed", "meta", None, None, old=a,
                             ctx=f"文档属性 · {_META_LABEL.get(k, k)}"))
            items.append(_mk(gen, "added", "meta", None, None, new=b,
                             ctx=f"文档属性 · {_META_LABEL.get(k, k)}"))
        else:
            items.append(_mk(gen, "added" if not a else "removed",
                             "meta", None, None, old=a, new=b,
                             ctx=f"文档属性 · {_META_LABEL.get(k, k)}"))
    return items


# ==================================================================
# 第 8 节  总入口
# ==================================================================

@dataclass
class Result:
    old_path: str = ""
    new_path: str = ""
    items: List[DiffItem] = field(default_factory=list)
    stats: Dict[str, int] = field(default_factory=dict)
    elapsed: float = 0.0
    pages_old: int = 0
    pages_new: int = 0
    mode_used: str = ""
    engine_line: str = ""               # 「到底用的是 pdfminer 还是 OCR」（界面单独一行）
    error: str = ""
    notes: List[str] = field(default_factory=list)   # 如实交代「哪些页没看成 / 为什么」

    def to_dict(self) -> Dict[str, Any]:
        return {
            "old_path": self.old_path, "new_path": self.new_path,
            "stats": self.stats, "elapsed": round(self.elapsed, 2),
            "pages_old": self.pages_old, "pages_new": self.pages_new,
            "mode_used": self.mode_used, "engine_line": self.engine_line,
            "error": self.error, "notes": list(self.notes),
            "items": [it.to_dict() for it in self.items],
        }


# ------------------------------------------------------------------ OCR 撤销假增删
OCR_RESCUE = os.environ.get("PDFDIFF_CORE_OCR", "1") != "0"
OCR_RESCUE_MIN_LEN = 10     # 短于这么多字的片段不做 OCR 核对（与 locate.OCR_MIN_LEN 同口径）
# 「最长**连续**匹配块」占比下限 —— 判「对面那句是不是**同一句话**」。
# ⚠️ 必须和 `_coverage` 一起用：只按累加覆盖率判会把「碰巧共用几个词的不同句子」
#    当成「内容还在对面」而撤销真改动（实测 0.941 假一致，见 `loc._longest_cover`）。
OCR_RESCUE_LONGEST = 0.45

# 「逐字无缺」覆盖率上限（方案 A「反向覆盖率」，2026-10-13）。
# ⚠️ 撤销 removed/added 的**真正前提**应该是「这段内容逐字无缺地还在对面」，
#    而不是「大部分字还在对面」（旧口径 `OCR_MIN_COVER=0.85` 太松）。
#    实测 11vs12 免责声明「单侧转曲 + 内容改写」：纯转曲 8 行 cov 都是 1.000，
#    被改写那行「定用途的」→「不对产品的适销性或针对特定」cov=0.963 ——
#    缺的那个「的」字正是真改动。0.995 正好把「逐字无缺」和「被改写了」分开。
#    判据放宽：OCR 抖动（Vision 对这份转曲正文几乎零错字）在锚点语料里
#    要么竖带 cov=1.0、要么整页兜底 cov=1.0，0.995 不会误伤它们（见 self_test）。
OCR_RESCUE_FULL_COVER = 0.995

# 位移标蓝（2026-10-12）：`_drop_relocated` 把「一删一增、文本一字不差」合并成
# moved 时的**纵向位移下限**（pt）。小于它 = 同一位置的重贴/图层抖动，照旧撤掉
# 不报 —— 避免把重复渲染刷成满屏浅蓝（M4506 右栏整列下移 8~25pt，正常排版
# 抖动 1~2pt，3pt 的空档足够分得开）。
_MOVED_MIN_DY = 3.0

# 位移判定（2026-10-12）：**原文**（含 ℃/单位符号）少于这个字符数的短行，
# 不做位移判定 —— pdfminer 常把「证」「：」「标识」这类孤立字拆成独立行。
# 用 raw 而非归一化文本：`10℃` 归一化成 `10` 只剩 2 字符，会误杀真位移。
_MOVED_MIN_CHARS = 3

# 字体变化判定（2026-10-12）：字号差 ≥ 此值（pt）才算「字体变化」（深蓝）。
# M4506 的 10℃ 实测 5.6→6.5pt；普通重排的渲染抖动 < 0.5pt，0.5 的空档足够分开。
_FONT_MIN_DSIZE = 0.5


def _fitz_dominant_size(spans: List[Dict[str, Any]]) -> float:
    """一行里**字数最多**的 span 的渲染字号（fitz 口径，与 locate 层 `_dominant_font` 同源）。

    ⚠️ 为什么这里要再读一次 fitz、而不信 pdfminer 的 `LTChar.size`：
    pdfminer 给的是 PDF 内容流里**声明的排版字号**（`LTChar.size`），fitz 给的是
    **最终渲染出来的字号**（`span["size"]`）。改版工具改的是渲染效果，所以判断
    「字号变没变」要信渲染字号 —— M4506 的 10℃ 实测：pdfminer 两侧都读 6.48
    （看起来没变），fitz 读到 5.58 vs 6.48（真变大了 0.9pt）。
    """
    best, best_n = 0.0, -1
    for sp in spans or []:
        n = len(sp.get("chars") or [])
        if n > best_n:
            best_n = n
            try:
                best = float(sp.get("size") or 0.0)
            except Exception:
                best = 0.0
    return best if best_n > 0 else 0.0


def _backfill_fitz_sizes(pages: List["PageText"], pdf_path: str) -> None:
    """用 fitz 的**渲染字号**回填 `TextLine.size`，覆盖 pdfminer 读不准的排版字号。

    ⚠️ 为什么（2026-10-13，用户拍板「清单层字体检测换 fitz」）
    ----------------------------------------------------------
    `extract_text_lines` 里 `size` 取自 pdfminer 的 `LTChar.size`（排版声明字号），
    对「字体声明没变、但实际渲染字号变了」的改动（M4506 的 10℃ 5.6→6.5）读不出来。
    locate 标注层用 fitz 已能正确报 font，清单层却漏 —— 两条链路口径不一致（§4.11
    最贵的一类 bug）。这里在**比对之前**用 fitz 重读一次渲染字号回填到 `size`，
    `_font_changed`/`_font_note` 一行不改就自动用上准确字号。

    匹配口径：fitz 的「行」与 pdfminer 的「行」切分可能不同（一字拆两行 / 两行并一
    行），故不按序号硬配，而按 `_norm_line_text` 指纹 + 同页计数贪心配对（同 core
    抵消逻辑）。回填失败（文本对不上 / fitz 打不开）保持原 `size` 不动 —— 宁可不
    改，也不把半吊子字号填进去污染判定。
    """
    try:
        import pymupdf as fitz
    except Exception:
        import fitz                       # type: ignore
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return
    try:
        for pi, page in enumerate(pages):
            if pi >= doc.page_count:
                break
            try:
                data = doc[pi].get_text("rawdict")
            except Exception:
                continue
            # 收集 fitz 侧每行：`(指纹, 渲染字号)`
            fitz_rows: List[Tuple[str, float]] = []
            for blk in data.get("blocks") or []:
                if blk.get("type") != 0:
                    continue
                for ln in blk.get("lines") or []:
                    txt = "".join(ch.get("c", "") for sp in ln.get("spans") or []
                                  for ch in sp.get("chars") or [])
                    if not txt.strip():
                        continue
                    fitz_rows.append((_norm_line_text(txt),
                                      _fitz_dominant_size(ln.get("spans") or [])))
            if not fitz_rows:
                continue
            # 按指纹分桶，供「同一句出现多次」时按顺序一一配对（同 core 抵消逻辑）
            by_fp: Dict[str, List[float]] = {}
            for fp, sz in fitz_rows:
                if sz:
                    by_fp.setdefault(fp, []).append(sz)
            used: Dict[str, int] = {}
            for ln in page.lines:
                if getattr(ln, "src", "text") != "text":
                    continue          # OCR 补的行（无文字层）没有 fitz 字号，别碰
                fp = _norm_line_text(ln.raw)
                sizes = by_fp.get(fp)
                if not sizes:
                    continue
                i = used.get(fp, 0)
                if i >= len(sizes):
                    continue
                ln.size = sizes[i]
                used[fp] = i + 1
    finally:
        doc.close()


def _digit_guard(needle: str, hay: str) -> bool:
    """数字守卫：待核文本里的**每一串数字**都要在对面的 OCR 文本里**独立成串**地出现。

    ⚠️ 为什么必须有（2026-09-30 实测打回来的）
    ------------------------------------------
    `loc._coverage` 是「按顺序的**模糊**覆盖率」，阈值 0.85。它有两个叠加的宽松处：

      ① 只要求 needle 的字**能按顺序凑齐**，不要求连续 —— 17 个字的短串在几千字的
         整页 OCR 里随手就能凑到 1.000（实测 `文件号：GRA-4118 Rev07` 的竖带覆盖率
         就是满分）；
      ② 于是「差一位数字」的改写会被误判成「内容还在对面」。

    实测后果：新版页脚 `文件号：GRA-4118 Rev07` 被撤销，而旧版那条
    `文件号：GRA-4118 Rev06/DCR 2025-0850` 却作为 removed 留着 ——
    「Rev06 → Rev07」这条**改版最核心的信息**在清单里只剩半个。

    数字是**最可信的改动锚点**（`Rev06→Rev07`、`2025年→2026年`、`4mm→5mm`…）：
    数字串在对面找不到 ⇒ 改的就是它 ⇒ 真改动，一律保留。
    ⚠️ 必须用「前后不是数字」的边界匹配：`07` 若允许子串匹配，会在
    `20233030395` 之类的长编号里被蒙中（实测就是这么漏过去的）。

    OCR 把数字认错（0↔O、1↔l）时守卫会偏严 —— 偏严只是多列一条待人工看，
    与全局策略一致（宁可多报，不可静默吃掉真改动）。
    """
    if not hay:
        return False
    for d in re.findall(r"\d+", needle or ""):
        if not re.search(r"(?<!\d)" + re.escape(d) + r"(?!\d)", hay):
            return False
    return True


# 撤销「删除」时，同处「新增」与它的纵向 / 横向容差（pt）——见 `_join_dropped_replaces`
_REPLACE_PAIR_PT = 30.0
_REPLACE_PAIR_X = 60.0


def _join_dropped_replaces(items: List[DiffItem], drop: set, gen: _IdGen) -> List[DiffItem]:
    """撤销「删除」时，被撤销的「删除」条目照旧丢掉，其余原样保留。

    ⚠️ 2026-10-13 删「变动」档后的语义收敛
    --------------------------------------
    旧版这里会把「一条被撤的删除 + 同处一条新增」合回一条「修改」——
    那是 GRA-4118 地址行的场景（旧行整串在新版里还在 → 删被撤；新行多了
    两个新门牌 → 新增留下），合回「修改」是为了让用户看出「这里其实是替换」。
    删掉「变动」档后，只剩「删 / 增」两类：被撤的删除就是「内容挪了位置，
    不算删」，照旧丢掉；留下的「新增」继续当新增（它确实携带了真正的新内容）。
    因此本函数退化为「按 drop 集合过滤」——不再做任何配对合并。
    """
    return [it for k, it in enumerate(items) if k not in drop]


def _strip_font_prefix(name: str) -> str:
    """剥掉嵌入子集的随机前缀（`ABCDEE+SimSun` → `SimSun`）。

    ⚠️ 为什么必须剥（2026-10-13 换 fitz 字号时抓出来的）
    --------------------------------------------------
    PDF 重新导出时，嵌入子集的前缀（`VNQHTN+`、`CIXYEU+`）是产出器**随机生成**的，
    同一台机器两次导出都会变。拿含前缀的字体名比「字体变没变」，会满篇假阳性
    （M4506 实测 49 条 font 里 46 条是 `TimesNewRomanPSMT` 前缀不同、字体根本没变）。
    与 locate 层 `_dominant_font` 的口径一致：剥掉 `+` 前缀再比。
    """
    if not name:
        return name
    return name.split("+", 1)[1] if "+" in name else name


def _font_changed(la: TextLine, lb: TextLine) -> bool:
    """文本相同的两行，字体/字号是否真变了（判 `font` 条目，深蓝）。

    判据：
      · 字号差 ≥ `_FONT_MIN_DSIZE`（pt）→ 变了（M4506 的 10℃ 5.6→6.5）；
      · 或字体名不同（剥掉子集前缀、都非空才比；一侧空 = 抽取缺失，不当证据）。
    ⚠️ 字号用 `size`（fitz 回填的渲染字号，见 _backfill_fitz_sizes），
       对字号混排的行是保守下界。
    """
    if la.size and lb.size and abs(lb.size - la.size) >= _FONT_MIN_DSIZE:
        return True
    fa = _strip_font_prefix(la.font)
    fb = _strip_font_prefix(lb.font)
    if fa and fb and fa != fb:
        return True
    return False


def _font_note(la: TextLine, lb: TextLine) -> str:
    """字体条目的 note：把「怎么变的」写成人话，界面直接展示。"""
    bits = []
    if la.size and lb.size:
        bits.append("%.1f→%.1fpt" % (la.size, lb.size))
    fa = _strip_font_prefix(la.font)
    fb = _strip_font_prefix(lb.font)
    if fa and fb and fa != fb:
        bits.append("%s→%s" % (fa, fb))
    return "字体变化：" + "、".join(bits) if bits else "字体变化"


def _drop_relocated(items: List[DiffItem], opts: Options,
                    gen: Optional[_IdGen] = None) -> Tuple[List[DiffItem], int]:
    """同页内「一删一增、文本一字不差」= 挪位，**合成一条 `moved`（浅蓝）**。

    ⚠️ 为什么必须有这一步（2026-10-11 合成留出集 S5 抓出来的）
    ----------------------------------------------------------
    文字层主路径 `diff_lines` 是**顺序敏感**的行 diff；而标注链路 `locate()` 在进
    字符流之前先做「完全相同行的**全局抵消**」（`pdfdiff_locate._cancel_identical`，
    位置无关）。于是「两版一字不差、只是段落换了顺序」这种文档：
        · 标注版报 **0 条**（相同的行全被抵消掉）；
        · 清单报 **「删除 + 新增」各一条**（顺序对不上，difflib 把它们当成删/增）。
    两条链路对不上 —— 用户没法判断谁对（即 §4.11 那类最贵的不一致）。

    ⚠️ 2026-10-12 用户「位移标蓝」需求：这类「内容一字不差、仅位置变」不是
    无差异，也不该报成「删除+新增」假象 —— 应归**位移（浅蓝）**。所以本函数从
    「两条都撤」升级成「两条合成一条 `moved`」，旧侧坐标 `x/y/y2` 取删条目、
    新侧坐标 `nx/ny/ny2` 取增条目 —— 标注层据此在两侧各画一个浅蓝框。

    判据刻意用**完全相等**而不是相似度：
      · 一字不差 ⇒ 是同一句话挪了位置（或换个图层重贴）→ moved；
      · 改过字（哪怕一个）⇒ 一律保留 —— 那可能是「A 处删、B 处另写一句」，不是挪位。
    所以这一步**不可能**吞掉任何真实的改写，只合并「两侧逐字相同的整行」。

    配对范围限定**同一页**（`(page_old, page_new)` 全等）：跨页的删/增更像真改动，
    宁可多留一条待人工看，也不跨页猜。
    """
    o = opts or Options()
    pool: Dict[Tuple[Any, Any, str, str], List[int]] = {}
    for k, it in enumerate(items):
        if it.kind != "added" or not it.new:
            continue
        n = normalize_text(it.new, o)
        if n:
            pool.setdefault((it.page_old, it.page_new, it.scope, n), []).append(k)
    if not pool:
        return items, 0

    moved: List[DiffItem] = []
    drop: set = set()
    n_pair = 0
    for k, it in enumerate(items):
        if it.kind != "removed" or k in drop or not it.old:
            continue
        n = normalize_text(it.old, o)
        if not n:
            continue
        bucket = pool.get((it.page_old, it.page_new, it.scope, n))
        if not bucket:
            continue
        j = next((x for x in bucket if x not in drop), None)
        if j is None:
            continue
        drop.add(k)
        drop.add(j)
        n_pair += 1
        added = items[j]
        # 位移量太小时（同一位置的重贴/图层抖动）不算位移，照旧撤掉不报 ——
        # 避免把「同一行重复渲染」刷成满屏浅蓝。
        dy = None
        if it.y is not None and added.ny is not None:
            dy = abs(added.ny - it.y)
        if dy is not None and dy < _MOVED_MIN_DY:
            continue
        moved.append(_mk(
            gen or _IdGen(), "moved", it.scope, it.page_old, it.page_new,
            old=it.old, new=it.new,
            x=it.x, y=it.y, y2=it.y2,
            nx=added.nx, ny=added.ny, ny2=added.ny2,
            sim=1.0, src=it.src or "text"))

    if not drop:
        return items, 0
    kept = [it for k, it in enumerate(items) if k not in drop]
    kept.extend(moved)
    # 保持稳定的页序（moved 条目带坐标，混进末尾不影响排序，但显式归位更稳）
    kept.sort(key=lambda it: (it.page_key, it.y_key if it.y_key is not None else 1e9))
    return kept, n_pair


def _peer_line(txt: str, band: str) -> str:
    """在对面 OCR 竖带里，取与 `txt` 最匹配的一行 —— 作为「改写后」的文字。

    方案 A「反向覆盖率」的配套（2026-10-13）：removed 的原文在对面
    「宽松命中但非逐字无缺」= 被改写了，不能静默撤销，要把对面改写后的
    那一行补成 added。取**双向覆盖率**（原文↔行）之和最高的一行，
    最接近「改写后的版本」。
    """
    import pdfdiff_locate as loc
    n_txt = loc._norm_cjk(txt)
    if not n_txt:
        return ""
    best = ("", 0.0)
    for line in (band or "").split("\n"):
        s = line.strip()
        if not loc._norm_cjk(s):
            continue
        score = (loc._coverage(txt, s) + loc._coverage(s, txt)) / 2.0
        if score > best[1]:
            best = (s, score)
    # 双向覆盖率全是 0（对面没有一行和原文沾边）→ 没有「改写后」的行，返回空。
    # 正常调用前有 `_band_hits`（coverage≥0.85）兜底，这里只是守住这条边界。
    return best[0] if best[1] > 0.0 else ""


def _ocr_rescue_core(items: List[DiffItem], old_path: str, new_path: str,
                     gen: Optional[_IdGen] = None,
                     pages_old: Optional[List[PageText]] = None,
                     pages_new: Optional[List[PageText]] = None
                     ) -> Tuple[List[DiffItem], int]:
    """用 OCR 撤销「文字层缺行 / 整段挪位」造成的假增删（2026-09-29）。

    病根与标注侧**同源**：改版后有一块被**转曲**（矢量曲线、没有文字层），
    或整段被推到别处 —— 列表引擎只认文字层，于是「旧版有、新版读不到」→ 报**删除**。
    实测 GRA-4118：免责声明整段在新版里明明还在（只是下移 + 转曲），却被报成 9 条
    removed；而标注侧已经靠同一判据把它们撤掉了 —— **两边对不上，用户没法判断谁对**。

    判据（直接复用 `pdfdiff_locate` 已在标注侧验证过的那套，含 OCR 缓存）：
    把**对面文档同一页**OCR 一遍，这段字还在里面 → 撤掉。
    与全局策略一致（**内容挪了位置不算改动**）；OCR 不可用/认不出来时**一律保留**
    （宁可多列一条待人工看，也不凭猜测抹掉真改动）。

    返回 `(撤销后的列表, 撤销条数)`。
    """
    if not items or not OCR_RESCUE:
        return items, 0
    try:
        import pdfdiff_locate as loc
    except Exception:
        return items, 0                      # 标注引擎不可用 → 保持原样，不猜
    if not loc.ocr_available():
        return items, 0
    try:
        try:
            import pymupdf as fitz
        except Exception:
            import fitz                       # type: ignore
        da, db = fitz.open(old_path), fitz.open(new_path)
    except Exception:
        return items, 0

    plan: List[Tuple[int, str, int, str, Optional[float]]] = []
    #   (下标, 对面文件, 对面页号, 待核文本, 该条的 x —— 用来只 OCR 它所在的那条栏)
    doc_of = {old_path: da, new_path: db}
    for k, it in enumerate(items):
        if it.kind == "removed":
            txt, who, xs = it.old or "", new_path, it.x
            pn = it.page_new if it.page_new is not None else it.page_old
        elif it.kind == "added":
            txt, who, xs = it.new or "", old_path, it.nx
            pn = it.page_old if it.page_old is not None else it.page_new
        else:
            continue          # modified / numeric 两侧都有内容，不是「整段增删」
        if pn is None or len(loc._norm_cjk(txt)) < OCR_RESCUE_MIN_LEN:
            continue
        # ⓪ 页边行（页眉 / 页脚）不做 OCR 撤销。
        #    ⚠️ 为什么单独排除（2026-09-30 实测）：
        #    页边行是**模板文字**，两版之间「长得几乎一样、只差版本号/日期」是常态。
        #    实测新版 `文件号：GRA-4118 Rev07` 与旧版 `文件号：GRA-4118 Rev06/DCR
        #    2025-0850` 共享 14 个连续字符，模糊覆盖率一路顶到 1.000 → 真改动被撤销，
        #    清单里「Rev06 → Rev07」只剩 removed 半个。这类误判**无法靠调阈值治**：
        #    阈值一高，真正挪位的整段又撤不掉了。
        #    页边行本来也不该指望 OCR 去救 —— 它是固定模板字段，转曲概率极低；
        #    真要转曲了，标注链路的 `graphic_regions` 会把整块圈出来。
        doc = doc_of.get(who)
        y = it.y if it.y is not None else it.ny
        if doc is not None and y is not None and 0 <= pn < doc.page_count:
            if edges.in_edge_band(y, y, float(doc[pn].rect.height)):
                continue
        plan.append((k, who, pn, txt, xs))
    if not plan:
        da.close()
        db.close()
        return items, 0

    # —— 对面文字层的「逐字精确存在」快通道（方案 A 配套，2026-10-13）——
    # 撤销 removed/added 的最强证据，不是 OCR 覆盖率，而是「对面文档的文字层里
    # **逐字连续**地存在这段文字」。它不受 OCR 抖动影响（Vision 把「以」认成「从」
    # 也不会让它消失），正好把「真改写」和「bullet 差异 / OCR 抖动」分开：
    #   · 文字层里有（精确连续）→ 内容确实还在 → 撤销；
    #   · 文字层里没有 → 才落到 OCR，进一步区分「纯转曲」与「转曲+改写」。
    # 用**整页归一化文本做连续子串**匹配，不用覆盖率 —— 覆盖率在地址插入这类
    # 「同页大量相似门牌」上会虚高（见下方 ① 的旧注释），连续子串不会。
    norm_of = {old_path: None, new_path: None}
    for who, pages in ((old_path, pages_old), (new_path, pages_new)):
        if pages:
            norm_of[who] = {p.index: loc._norm_cjk("".join(l.raw for l in p.lines))
                            for p in pages}

    drop: set = set()
    rewrites: Dict[int, str] = {}       # 改写候选：items 下标 -> 对面改写后的文字
    curve_drops: List[int] = []         # 纯转曲撤销（内容未变、渲染已改）→ 转 font 条目
    extra: List[DiffItem] = []
    try:
        ink_ratio: Dict[Tuple[str, int], float] = {}

        def _band_hits(band: Optional[str]) -> bool:
            """宽松命中：内容「大概」在对面（覆盖率 + 最长连续 + 数字守卫）。"""
            return bool(band) and loc._coverage(txt, band) >= loc.OCR_MIN_COVER \
                and loc._longest_cover(txt, band) >= OCR_RESCUE_LONGEST \
                and _digit_guard(txt, band)

        def _band_full(band: Optional[str]) -> bool:
            """逐字无缺命中（方案 A「反向覆盖率」，2026-10-13）。

            只比 `_band_hits` 把覆盖率门槛从 0.85 提到 0.995 —— 能撤销的
            前提是「这段内容**逐字无缺**地还在对面」（纯转曲 / 挪位）；
            「缺字」= 内容被改写了，必须保留，交给 `rewrites` 拆 removed+added。
            实测 11vs12 免责声明：纯转曲 8 行 cov=1.000，改写那行 0.963。
            """
            return bool(band) and loc._coverage(txt, band) >= OCR_RESCUE_FULL_COVER \
                and loc._longest_cover(txt, band) >= OCR_RESCUE_LONGEST \
                and _digit_guard(txt, band)

        for k, who, pn, txt, xs in plan:
            doc = db if who == new_path else da
            if not (0 <= pn < doc.page_count):
                continue
            # ⓪ 对面文字层**逐字连续**存在 → 内容确实还在（bullet 差异 / 挪位），
            #    直接撤销，不进 OCR —— 这条快通道不受 OCR 抖动影响。
            other_norm = norm_of.get(who)
            n_txt_norm = loc._norm_cjk(txt)
            if (other_norm is not None and n_txt_norm
                    and pn in other_norm and n_txt_norm in other_norm[pn]):
                drop.add(k)
                continue
            key = (who, pn)
            if key not in ink_ratio:
                ink_ratio[key] = loc._graphic_ink_ratio(doc[pn])
            if ink_ratio[key] < 0.02:
                continue      # 这页没有转曲区域 → OCR 无从下手，保留原判
            pg = doc[pn]
            w = float(pg.rect.width)
            # ① 竖带：只 OCR 这条所在的那一栏（整页是它的 3~5 倍耗时）
            if xs is not None:
                band = loc.ocr_strip(pn, who, float(xs), float(xs) + 200.0, pg)
                if _band_full(band):
                    drop.add(k)
                    curve_drops.append(k)
                    continue
                if _band_hits(band):
                    rewrites[k] = _peer_line(txt, band)
                    continue
            # ② 整页兜底：挪到别的栏去了（同一页只认一次，命中缓存后近零成本）
            band = loc.ocr_strip(pn, who, 0.0, w, pg)
            if _band_full(band):
                drop.add(k)
                curve_drops.append(k)
                continue
            if _band_hits(band):
                rewrites[k] = _peer_line(txt, band)
    finally:
        da.close()
        db.close()

    # —— 2026-10-13 晚：纯转曲撤销不再静默（11vs12 免责声明段实测）——
    # 「旧=文字层、新=转曲曲线、OCR 逐字无缺命中」= 文本内容未变，但**字体渲染
    # 变了**：整段变成矢量图形，渲染字重/外观随之改变（实测旧版 SimSun 加粗渲染、
    # 新版转曲后明显变细，用户肉眼可见却无任何标记）。按 2026-10-12 用户拍板的
    # 口径「文本一字不差但渲染变了 = 字体变化（深蓝）」，生成 kind="font" 条目，
    # 坐标沿用原条目有内容的那一侧。
    for k in curve_drops:
        it = items[k]
        t = (it.old or it.new or "").strip()
        if not t:
            continue
        if it.kind == "removed":
            extra.append(_mk(gen, "font", it.scope, it.page_old, it.page_new,
                             old=t, new=t, sim=1.0,
                             x=it.x, y=it.y, y2=it.y2, ctx=it.context,
                             note="文本未变，新版已转为曲线图形（字体渲染改变）",
                             src="ocr_line"))
        else:
            extra.append(_mk(gen, "font", it.scope, it.page_old, it.page_new,
                             old=t, new=t, sim=1.0,
                             nx=it.nx, ny=it.ny, ny2=it.ny2, ctx=it.context,
                             note="文本未变，旧版为曲线图形（字体渲染改变）",
                             src="ocr_line"))

    # —— 方案 A：把「被改写」的增删拆成 removed + added 两条 ——
    for k, peer in rewrites.items():
        it = items[k]
        n_peer = loc._norm_cjk(peer)
        n_txt = loc._norm_cjk(it.old or it.new or "")
        # 提取不出改写文字 / 对面文字和原文一样 → 保守：只保留原判，不硬凑对侧。
        if not n_peer or n_peer == n_txt:
            continue
        if it.kind == "removed":
            extra.append(_mk(
                gen, "added", it.scope, it.page_old, it.page_new, new=peer,
                nx=it.x, ny=it.y, ny2=it.y2, ctx=it.context, sim=0.0,
                note="转曲区内容改写（OCR 识别，仅供参考）", src="ocr_line"))
        elif it.kind == "added":
            extra.append(_mk(
                gen, "removed", it.scope, it.page_old, it.page_new, old=peer,
                x=it.nx, y=it.ny, y2=it.ny2, ctx=it.context, sim=0.0,
                note="转曲区内容改写（OCR 识别，仅供参考）", src="ocr_line"))

    kept = [it for k, it in enumerate(items) if k not in drop]
    kept.extend(extra)
    if not drop and not extra:
        return items, 0
    if gen is None:
        return kept, len(drop)
    return _join_dropped_replaces(kept, set(), gen), len(drop)


# ---------------------------------------------------------------- 转曲区兜底
GRAPHIC_RESCUE = os.environ.get("PDFDIFF_CORE_GRAPHIC", "1") != "0"
GRAPHIC_RESCUE_MIN_COVER = 0.6    # 这块 OCR 文字已有这么大比例被现有条目覆盖 → 不重复补
GRAPHIC_CTX = "转曲区（PDF 无文字层）"
GRAPHIC_NOTE = "矢量曲线绘制，无文字层；OCR 识别，仅供参考"
# 转曲段内的**文字层残留碎片**要不要撤下（2026-09-30）
GRAPHIC_FRAG = os.environ.get("PDFDIFF_CORE_GRAPHIC_FRAG", "1") != "0"
GRAPHIC_FRAG_PAD = 3.0     # 判「落在转曲段内」时四周留的容差 (pt)
FRAG_MAX_CHARS = 6         # 短于这么多字的条目才走「内容判据」（长句不算碎片）

# —— 同一视觉行的「重复去重」与「同行合并」（2026-09-30）——
LINE_DEDUP = os.environ.get("PDFDIFF_CORE_LINE_DEDUP", "1") != "0"
MERGE_LINE = os.environ.get("PDFDIFF_CORE_MERGE_LINE", "1") != "0"
MERGE_BLOCK_H = 2.5        # 参与合并的文本块高度上限（× 行高）：整段块不参与
MERGE_Y_TOL = 0.6          # 「同一视觉行」判据（× 行高）
MERGE_TEXT_SEP = "　"       # 合并后条目文本之间的分隔（全角空格，保持可读）


def _norm_line_text(s: str) -> str:
    """判重用的文本指纹：剥掉装饰符号 + 去掉所有空白。

    ⚠️ 必须剥装饰符号：重复图层里一层带 `♦`、一层不带（GRA-4118 实测），
    不剥的话两条永远不相等，去重就不生效。
    """
    try:
        import pdfdiff_noise as noise
        s = noise.strip_noise(s or "")
    except Exception:
        pass
    return re.sub(r"\s+", "", s or "")


def _page_height(pages, pi: int) -> float:
    """页高（pt）；页列表缺失 / 越界 / 没有 height 都返回 0（= 拿不到）。"""
    try:
        return float(getattr(pages[pi], "height", 0) or 0)
    except Exception:
        return 0.0


def _flip_y(y0: float, y1: float, page_h: float) -> Tuple[float, float]:
    """PyMuPDF 的纵向一对坐标（y 向下）→ **pdfminer 口径**（y 向上）。

    ⚠️ `page_h <= 0`（拿不到页高）时**原样返回** —— 宁可退化成改前的行为，
    也不凭空猜一个页高把坐标翻到别处去。
    """
    if page_h <= 0:
        return y0, y1
    return page_h - y0, page_h - y1


def _flip_from_pdfminer(y0: float, y1: float, page_h: float) -> Tuple[float, float]:
    """pdfminer 纵向一对坐标（y 向上）→ **PyMuPDF 口径**（y 向下），并排成升序。

    `_py_geom` 与 `_frag_segment` 两处都做「H - max/min」翻转，收敛到这里。
    页高拿不到（≤0）时原样返回（不凭空猜页高）。
    """
    if page_h <= 0:
        return min(y0, y1), max(y0, y1)
    return page_h - max(y0, y1), page_h - min(y0, y1)


def _py_geom(it: DiffItem, side: str, pages_old, pages_new):
    """条目换算到 **PyMuPDF 空间**的 `(页号, x左边界, y中心, 行高)`；拿不到返回 None。

    两套引擎的 y 轴**方向相反**（pdfminer 原点在左下、PyMuPDF 在左上），
    所以 `y / y2` 要翻过来：`py = H - y`。x 轴同源、直接可用
    （两个库给的页面宽高一致，实测 841.89 × 595.275）。
    """
    pages = pages_new if side == "new" else pages_old
    if not pages:
        return None
    p = it.page_new if side == "new" else it.page_old
    px, pa, pb = (it.nx, it.ny, it.ny2) if side == "new" else (it.x, it.y, it.y2)
    if p is None or px is None or pa is None or pb is None:
        return None
    if not (0 <= p < len(pages)):
        return None
    h = _page_height(pages, p)
    if not h:
        return None
    y0, y1 = _flip_from_pdfminer(pa, pb, h)
    return p, float(px), (y0 + y1) / 2.0, max(y1 - y0, 1.0)


def _in_block(pt, b, pad: float = 2.0) -> bool:
    """点是否落在文本块矩形内（留一点容差 —— 条目坐标与块的边界常差零点几磅）。"""
    x, y = pt
    return (b[0] - pad <= x <= b[2] + pad) and (b[1] - pad <= y <= b[3] + pad)


def _same_block(bs, p1, p2, lh: float) -> bool:
    """两个点是否落在**同一个**文本块里（且该块不是整段块）。

    整段块（高度 > `MERGE_BLOCK_H` × 行高）不参与 —— 否则一段十行的正文里，
    不同行上的改动会被缝成一条。
    """
    for b in (bs or ()):
        if (b[3] - b[1]) > MERGE_BLOCK_H * lh:
            continue
        if _in_block(p1, b) and _in_block(p2, b):
            return True
    return False


def _absorb(keep: DiffItem, extra: DiffItem, side: str) -> None:
    """把 `extra` 并进 `keep`：文本拼接、坐标取并集（**只动展示，不动判定**）。"""
    sep = MERGE_TEXT_SEP
    if side == "new":
        keep.new = (keep.new + sep + extra.new).strip(sep)
        for attr, fn in (("nx", min), ("ny", min), ("ny2", max)):
            vals = [v for v in (getattr(keep, attr), getattr(extra, attr)) if v is not None]
            setattr(keep, attr, fn(vals) if vals else None)
    else:
        keep.old = (keep.old + sep + extra.old).strip(sep)
        for attr, fn in (("x", min), ("y", min), ("y2", max)):
            vals = [v for v in (getattr(keep, attr), getattr(extra, attr)) if v is not None]
            setattr(keep, attr, fn(vals) if vals else None)


def _line_normalize_core(items: List[DiffItem], old_path: str, new_path: str,
                         pages_old=None, pages_new=None
                         ) -> Tuple[List[DiffItem], int, int]:
    """同一视觉行的**去重**（重复文字层）与**合并**（页脚的文件号 + 修订日期）。

    ① 去重（用户看到的「分散」有一半来自这里）
    ------------------------------------------
    源 PDF 自带**重复文字层**：GRA-4118 新版中栏那行「用充压装置使球囊导管产生
    负压，并保持；」在文件里叠了两层文字（一层带 `♦`、一层不带），清单于是
    报出两条一模一样的改动。判据：同页 + 同侧 + 同 kind +
    **剥掉装饰符号后文本完全相同** + 同一条视觉行 —— 四条同时成立时它只可能是
    重复图层，不可能是"同一行真有两次相同改动"。

    ② 合并（用户原话：「文件号，还有那个修订日期，这些其实可以放在一起」）
    ----------------------------------------------------------------------
    新版页脚的文字变短了，中间空出 65pt，pdfminer 就把一行切成两个 text line，
    清单里成了两条、标注上是两个相隔很远的方块。可它们印在同一行、说的是同一件事。
    判据：同页 + 同侧 + 同 kind + **同一个文本块** + **同一条视觉行**。

    ⚠️ 「同一个文本块」这条不能省：中栏与右栏相隔只有 27pt，比页脚内部那 65pt
    还近，纯按距离判会把两栏缝成一条；而 `loc.page_blocks()` 给的结构判据
    把这两种情况分得很干净（GRA-4118 实测：页脚两段同属 block#111，
    中栏是 block#75、右栏是 block#44）。
    """
    if not items or len(items) < 2:
        return items, 0, 0
    try:
        import pdfdiff_locate as loc
        blk = {("old", i): bs for i, bs in loc.page_blocks(old_path).items()}
        blk.update({("new", i): bs for i, bs in loc.page_blocks(new_path).items()})
    except Exception:
        return items, 0, 0

    out: List[DiffItem] = []
    dup = merged = 0
    for it in items:
        side = "new" if it.kind == "added" else ("old" if it.kind == "removed" else None)
        g = _py_geom(it, side, pages_old, pages_new) if side else None
        hit = None
        if g and it.scope == "text":
            txt = _norm_line_text(it.new or it.old)
            for k in out:
                if k.kind != it.kind or k.scope != "text":
                    continue
                gk = _py_geom(k, side, pages_old, pages_new)
                if not gk or gk[0] != g[0]:
                    continue
                if abs(g[2] - gk[2]) > MERGE_Y_TOL * max(g[3], gk[3]):
                    continue
                if LINE_DEDUP and txt and txt == _norm_line_text(k.new or k.old):
                    hit = ("dup", k)
                    break
                if MERGE_LINE and _same_block(blk.get((side, g[0])),
                                              (g[1], g[2]), (gk[1], gk[2]),
                                              max(g[3], gk[3])):
                    hit = ("join", k)
                    break
        if hit is None:
            out.append(it)
            continue
        if hit[0] == "dup":
            dup += 1
        else:
            _absorb(hit[1], it, side)
            merged += 1
    return out, dup, merged


def _frag_segment(it: DiffItem, segs, pages_old, pages_new):
    """这条差异是不是「属于某个转曲段」的文字层碎片。两条判据，命中任一即可。

    为什么必须撤下（2026-09-30 用户「这个生理我在原文里根本找不到」）
    --------------------------------------------------------------
    转曲段（用矢量曲线画的文字）**不是完全没有文字层** —— 个别字符会以「隐形文字」
    的形态残留。GRA-4118 新版中栏那段就残留了「生理」两个字，清单里于是多出一条
    孤零零的 `added「生理」`。用户对着原文怎么找都找不到它 —— 因为它视觉上是
    「需用浸有生理盐水的纱布擦拭球囊导管」的一部分，不是一处独立改动。
    撤下之后这两个字**反而看得见了**：它们就在该段 OCR 出来的完整句子里。

    两条判据（缺一条会漏）
    ----------------------
    ① **位置**：条目中心落在该段矩形里（留 `GRAPHIC_FRAG_PAD` 容差）；
    ② **内容**：条目很短（≤ `FRAG_MAX_CHARS` 字）且整段文本出现在该段的 OCR 文本里。

    ⚠️ 为什么光有 ① 不够：碎片**未必**落在段矩形内。实测那条「生理」的坐标是
    (x=506, py_y=94.5)，在段 [444.6, 105.0, 605.4, 149.4] **上方 10pt** ——
    段矩形是**按像素差异**框出来的，只框"变了的那几行"，而这半句两侧一样、
    所以没被框进去。位置够不着，内容够得着：该段的 OCR 文本里就有「生理」。
    ② 加了「短」这道闸：长句即便碰巧含相同词也不撤（那是真内容）。

    坐标映射已验证：「生理」pdfminer y=476.80 / y2=482.80 → PyMuPDF 112.5~118.5，
    与转曲段第 2 块的 111.9~118.8 逐点吻合。
    """
    if it.kind not in ("added", "removed"):
        return None
    for side in ("new", "old"):
        p = it.page_new if side == "new" else it.page_old
        pages = pages_new if side == "new" else pages_old
        if p is None or not pages or not (0 <= p < len(pages)):
            continue
        px, pa, pb = (it.nx, it.ny, it.ny2) if side == "new" else (it.x, it.y, it.y2)
        h = _page_height(pages, p)
        if not h or px is None or pa is None or pb is None:
            continue
        py0, py1 = _flip_from_pdfminer(pa, pb, h)
        cy = (py0 + py1) / 2.0
        for seg in segs:
            sp, sr, sside, stxt = seg
            if sp != p or sside != side:
                continue
            if (sr[0] - GRAPHIC_FRAG_PAD <= px <= sr[2] + GRAPHIC_FRAG_PAD
                    and sr[1] - GRAPHIC_FRAG_PAD <= cy <= sr[3] + GRAPHIC_FRAG_PAD):
                return seg
            t = _norm_line_text(it.new if side == "new" else it.old)
            if t and len(t) <= FRAG_MAX_CHARS and t in _norm_line_text(stxt):
                return seg
    return None


def _graphic_rescue_core(items: List[DiffItem], old_path: str, new_path: str,
                         gen: _IdGen, pages_old=None, pages_new=None, opts=None
                         ) -> Tuple[List[DiffItem], int, int]:
    """把「两侧都没有文字层、但画面真的变了」的**转曲区**补进清单（2026-09-30）。

    病根（与标注侧同源，见 `pdfdiff_locate.graphic_regions` 的长注释）
    ----------------------------------------------------------------
    改版会把整段内容**转曲**（改成矢量曲线），或整段挪走 —— 文字层随之消失，
    而列表引擎只认文字层，于是「新版真新增了一大段」却**一处都报不出来**。
    实测 GRA-4118 新版第 1 页中栏那段（「如果需要再次插入该球囊导管…」）：
    标注版上是 7 个绿块，清单里只有孤零零一条「生理」—— 那段里仅有的、
    还残留在文字层里的两个字。用户对着两个窗口数，怎么数都对不上。

    做法：**与标注链路读同一份 `loc.graphic_regions()`**，
    从此「标注上圈出来的，清单里一定找得到」不再是巧合。

    去重（转曲区是「块」、清单条目是「行」，粒度不同）
    -------------------------------------------------
    用**内容覆盖**去重：这块的 OCR 文字已有 ≥ `GRAPHIC_RESCUE_MIN_COVER` 的比例
    出现在现有条目里 → 认为文字层已经报过，不再重复补。
    ⚠️ OCR 本身有错字，这条判据只能放宽着用 —— 宁可重复补一条让人核对，
    也不能让整块真改动一处都不出现。

    坐标（2026-09-30 改：**写清单前先翻到 pdfminer 口径**）
    ----------------------------------------------------
    ⚠️ `graphic_regions` 给的是 **PyMuPDF 页面坐标**（原点在左上、y 向下），
    而 `text` 条目的坐标来自 pdfminer（原点在左下、y 向上）—— 两者**纵向差 180°**
    （实测 GRA-4118 同一行 pdfminer 报 top=13.8、PyMuPDF 报 y=575）。

    ⚠️ 2026-09-30：护栏里的 `not items` 删掉了 —— 它会把「两份 PDF **只有**
    转曲区差异、文字层零差异」的情形整个跳过（items 空 → 直接 return），
    恰好漏掉的是最依赖兜底的那类文件。空 items 时照样该跑：
    graphic_regions 自己有缓存与快速路径，完全相同的两页hot瓦片为 0，
    摊不了多少成本。
    """
    if not GRAPHIC_RESCUE:
        return items, 0, 0
    try:
        import pdfdiff_locate as loc
    except Exception:
        return items, 0, 0
    try:
        regions = loc.graphic_regions(old_path, new_path)
    except Exception:
        return items, 0, 0
    if not regions:
        return items, 0, 0

    # ① 先撤下「落在转曲段里的文字层残留碎片」（GRA-4118 的『生理』）——
    #    它们不是独立改动，是那块图形里零星残存的字符，视觉上属于该段的一句话。
    frag_dropped = 0
    if GRAPHIC_FRAG:
        segs = [(int(r.get("page", 0)),
                 tuple(float(v) for v in (r.get("rect") or (0, 0, 0, 0))),
                 "new" if (r.get("new_text") or "").strip() else "old",
                 (r.get("new_text") or r.get("old_text") or ""))
                for r in regions]
        kept_items: List[DiffItem] = []
        for it in items:
            if _frag_segment(it, segs, pages_old, pages_new) is None:
                kept_items.append(it)
            else:
                frag_dropped += 1
        items = kept_items

    # 现有条目的文本（按页攒），用来判「这块是不是已经报过了」
    seen: Dict[int, str] = {}
    for it in items:
        # ⚠️ 2026-10-12 位移标蓝：moved 条目**不参与** seen 去重 —— moved 只说明
        #   「位置变了」，不代表「这段内容已被文字层报过」。若把 moved 的文本
        #   塞进 seen，M4506 整栏下移会生成 19 条 moved，其文本把转曲区
        #   "Store in a dry, dark, cool place" 的 modified 判成「已报过」而吞掉
        #   （真改动消失）。位移与内容改动是两件事，不能互相覆盖。
        if it.kind == "moved":
            continue
        p = it.page_new if it.page_new is not None else it.page_old
        if p is None:
            continue
        seen[p] = seen.get(p, "") + loc._norm_cjk(it.old) + loc._norm_cjk(it.new)

    added = 0
    # 前后文索引（一页建一次，懒加载）：转曲区条目也要有「前文/后文」——
    # 用户 2026-09-30：「一个再折叠工具这句话怎么没有前后文」。孤零零一行 OCR 字
    # 没法对着页面找；把它同栏上下的文字层行接上，详情面板就和正文条目一样能定位。
    _ctx_n: Dict[int, list] = {}
    _ctx_o: Dict[int, list] = {}
    n_ctx = max(1, int(getattr(opts, "ctx_lines", CTX_LINES))) if opts else CTX_LINES

    def _idx(side: str, pi: int) -> list:
        pages = pages_new if side == "n" else pages_old
        cache = _ctx_n if side == "n" else _ctx_o
        if pi not in cache:
            pg = pages[pi] if pages and 0 <= pi < len(pages) else None
            cache[pi] = _ctx_index(pg, opts or Options()) if pg is not None else []
        return cache[pi]

    for r in regions:
        pi = int(r.get("page", 0))
        try:
            x0, y0, x1, y1 = (float(v) for v in (r.get("rect") or (0, 0, 0, 0)))
        except Exception:
            x0 = y0 = x1 = y1 = 0.0
        # 翻到 pdfminer 口径再写进清单（见函数头「坐标」一节）——
        # 分段要拿它跟同页正文比纵向距离，不翻就永远判不出「紧挨着」。
        fy0, fy1 = _flip_y(y0, y1, _page_height(pages_new, pi))
        t_old = (r.get("old_text") or "").strip()
        t_new = (r.get("new_text") or "").strip()
        if r.get("moved"):
            # —— 位移区域（2026-10-12 五色体系）——
            # 引擎像素配准（二维平移）证明「两侧内容相同、只是位置不同」：
            # `rect` = 源侧（有墨那侧）矩形，`rect_shift` = 对侧对应位置。
            # 生成一条 kind="moved"（两侧坐标各归各的），不再走 modified/增删。
            t = t_old or t_new
            if (seen.get(pi)
                    and loc._coverage(t, seen[pi]) >= GRAPHIC_RESCUE_MIN_COVER):
                continue                          # 文字层已报过这块，不重复补
            try:
                mx0, my0, mx1, my1 = (float(v) for v in
                                      (r.get("rect_shift") or (x0, y0, x1, y1)))
            except Exception:
                mx0, my0, mx1, my1 = x0, y0, x1, y1
            fy0s, fy1s = _flip_y(my0, my1, _page_height(pages_new, pi))
            src_new = (r.get("shift_src") or "new") == "new"
            nx0, ny0, ny20 = (x0, fy0, fy1) if src_new else (mx0, fy0s, fy1s)
            ox0, oy0, oy20 = (mx0, fy0s, fy1s) if src_new else (x0, fy0, fy1)
            it = _mk(gen, "moved", "graphic", pi, pi, old=t, new=t,
                     x=ox0, y=oy0, y2=oy20, nx=nx0, ny=ny0, ny2=ny20,
                     ctx=GRAPHIC_CTX, note=GRAPHIC_NOTE, sim=1.0,
                     src="ocr_line")
            b, a = _ctx_around(_idx("n", pi), min(fy0, fy1), x0, n_ctx)
            if not (b or a):
                b, a = _ctx_around(_idx("o", pi), min(fy0, fy1), x0, n_ctx)
            if b or a:
                it.ctx_before, it.ctx_after = b, a
            items.append(it)
            added += 1
            continue
        if t_old and t_new:
            # 两侧都有字 = 转曲区**行内修改**（locate 的 `_decide_region` "mod"）。
            # 2026-10-13 删「变动」档：拆成「删（旧值标红）+ 增（新值标绿）」两条，
            # 同一块同位 —— 用户要的是「旧被删、新被增」的直观口径，不再合并。
            if (seen.get(pi)
                    and loc._coverage(t_new, seen[pi]) >= GRAPHIC_RESCUE_MIN_COVER
                    and loc._coverage(t_old, seen[pi]) >= GRAPHIC_RESCUE_MIN_COVER):
                continue                          # 文字层已报过这块，不重复补
            # 相似度：text 条目都有，graphic 条目之前一直漏（2026-10-11 用户实测
            # GRA-3925 图片对 30 条 sim 全 0，`A35-070150-200→A35-070300-200`
            # 明明 93% 相同却显示 0%）。口径与 `_coverage` 一致：**归一化后**
            # 整串 SequenceMatcher ratio —— OCR 错一两个字不至于把相似度打穿。
            _no, _nn = loc._norm_cjk(t_old), loc._norm_cjk(t_new)
            sim = (SequenceMatcher(None, _no, _nn, autojunk=False).ratio()
                   if (_no or _nn) else 0.0)
            src = ("ocr_est" if r.get("narrowed") else "ocr_line")
            # OCR 认出了两侧文字 ⇒ 存在性「中」（风险是认错字）；
            # 坐标若被 `_narrow_mod_region` 收窄过，是按字符占比折算的 ⇒ 降 loc_conf。
            it = _mk(gen, "removed", "graphic", pi, None, old=t_old,
                     x=x0, y=fy0, y2=fy1, ctx=GRAPHIC_CTX, note=GRAPHIC_NOTE,
                     sim=sim, src=src)
            b, a = _ctx_around(_idx("n", pi), min(fy0, fy1), x0, n_ctx)
            if not (b or a):
                b, a = _ctx_around(_idx("o", pi), min(fy0, fy1), x0, n_ctx)
            if b or a:
                it.ctx_before, it.ctx_after = b, a
            items.append(it)
            it = _mk(gen, "added", "graphic", None, pi, new=t_new,
                     nx=x0, ny=fy0, ny2=fy1, ctx=GRAPHIC_CTX, note=GRAPHIC_NOTE,
                     sim=sim, src=src)
            b, a = _ctx_around(_idx("n", pi), min(fy0, fy1), x0, n_ctx)
            if not (b or a):
                b, a = _ctx_around(_idx("o", pi), min(fy0, fy1), x0, n_ctx)
            if b or a:
                it.ctx_before, it.ctx_after = b, a
            items.append(it)
            added += 2
            continue
        for kind, side in (("added", "new"), ("removed", "old")):
            t = (r.get(f"{side}_text") or "").strip()
            if not t:
                continue
            if seen.get(pi) and loc._coverage(t, seen[pi]) >= GRAPHIC_RESCUE_MIN_COVER:
                continue                          # 文字层已经报过这块，不重复补
            if kind == "added":
                items.append(_mk(gen, "added", "graphic", None, pi, new=t,
                                 nx=x0, ny=fy0, ny2=fy1,
                                 ctx=GRAPHIC_CTX, note=GRAPHIC_NOTE, src="ocr_line"))
            else:
                items.append(_mk(gen, "removed", "graphic", pi, None, old=t,
                                 x=x0, y=fy0, y2=fy1,
                                 ctx=GRAPHIC_CTX, note=GRAPHIC_NOTE, src="ocr_line"))
            # 前后文：y 用块的**下缘**（min，与文字行的 `top`=y0 同口径）、
            # x 用块左缘（栏判据 CTX_X_PAD=60 覆盖同栏行首的缩进差）
            it = items[-1]
            b, a = _ctx_around(_idx("n" if kind == "added" else "o", pi),
                               min(fy0, fy1), x0, n_ctx)
            if b or a:
                it.ctx_before, it.ctx_after = b, a
            added += 1
    return items, added, frag_dropped


def _merge_graphic_relocation(items: List[DiffItem], gen: Optional[_IdGen] = None
                              ) -> Tuple[List[DiffItem], int]:
    """把「同页一删一增、OCR 文本高度相似」的转曲区条目合并成「位置移动」。

    病根（2026-10-12 GRA-M30180 图片对实测）
    ----------------------------------------
    无文字层的图片/扫描件输入，结论全部来自 `_graphic_rescue_core` 的 OCR。
    当一处改动（如球囊长度 5mm→6mm）把下方整块多语言列表**顶下去换行**时，
    旧版位置和新版位置在像素层面对不上，各自被 `_decide_region` 判成
    「一边空白」，于是同一句话被拆成 `removed`（旧版）+ `added`（新版）两条。

    但两侧 OCR 出来的其实**是同一句话**，只是中间某语种被 OCR 认出了噪声
    （`世咎号叫 喜沓` vs `合号吗 喜杏`），导致 `_drop_relocated` 的「逐字相等」
    判据（在 OCR 兜底**之前**运行，且只覆盖文字层条目）根本拦不住它。

    判据：对同页内 `scope="graphic"` 的每条 `removed`，找一条 `added`，
    用 `_coverage` 双向判「是不是同一句话」。是 → 合并成 `kind="moved"` 一条
    （两侧文本都保留、坐标各取各的），撤掉两条假增删；否 → 保留。

    ⚠️ 为什么用 `_coverage` 而不是 `_longest_cover`：多语言列表中间混着
    韩文/日文 OCR 噪声，最长连续块会被打断（实测 `_longest_cover` 只到 0.51），
    而 `_coverage` 的累加口径能容忍这种「中间错几字、前后都对得上」的情形
    （实测 0.86~0.89，稳过 0.85 门槛）。这里的风险是「碰巧共词」的两句被误合并，
    所以**双向都要过门槛**，且只对 `src` 含 `ocr` 的条目生效（文字层条目
    早被 `_drop_relocated` 的精确判据处理过，不在这里猜）。
    """
    if not items:
        return items, 0
    try:
        import pdfdiff_locate as loc
    except Exception:
        return items, 0

    # 按页分组收集 graphic 的 removed / added（只碰 OCR 来源的）
    def _is_ocr_graphic(it: DiffItem, kind: str) -> bool:
        return (it.kind == kind and it.scope == "graphic"
                and "ocr" in (it.src or "") and (it.old or it.new))

    removed_by_page: Dict[int, List[int]] = {}
    added_by_page: Dict[int, List[int]] = {}
    for k, it in enumerate(items):
        if _is_ocr_graphic(it, "removed"):
            removed_by_page.setdefault(it.page_old if it.page_old is not None else -1,
                                       []).append(k)
        elif _is_ocr_graphic(it, "added"):
            added_by_page.setdefault(it.page_new if it.page_new is not None else -1,
                                     []).append(k)

    drop: set = set()
    moved_items: List[DiffItem] = []
    n_merge = 0
    for page, r_idxs in removed_by_page.items():
        a_idxs = added_by_page.get(page)
        if not a_idxs:
            continue
        used_a: set = set()
        for rk in r_idxs:
            if rk in drop:
                continue
            r = items[rk]
            for ak in a_idxs:
                if ak in drop or ak in used_a:
                    continue
                a = items[ak]
                # 双向 coverage 都要过门槛（只碰 OCR 来源的文本）
                if (loc._coverage(r.old, a.new) < loc.OCR_MIN_COVER
                        or loc._coverage(a.new, r.old) < loc.OCR_MIN_COVER):
                    continue
                # 命中：合成一条 moved，两侧文本与坐标各归各的
                moved = _mk(gen or _IdGen(), "moved", "graphic",
                            page_old=r.page_old, page_new=a.page_new,
                            old=r.old, new=a.new,
                            x=r.x, y=r.y, y2=r.y2, nx=a.nx, ny=a.ny, ny2=a.ny2,
                            ctx=GRAPHIC_CTX, note=GRAPHIC_NOTE,
                            sim=round((loc._coverage(r.old, a.new)
                                       + loc._coverage(a.new, r.old)) / 2, 4),
                            src="ocr_line")
                moved_items.append(moved)
                drop.add(rk)
                drop.add(ak)
                used_a.add(ak)
                n_merge += 1
                break

    # ---- 短文本「并入相邻 moved」----
    # GRA-M30180 的多语言列表被 OCR 切成了上下两段：上面那行英文标题
    # `Coronary I`（归一化后仅 9 字符，短于 OCR_MIN_LEN，`_coverage` 恒返 0，
    # 上面的配对永远够不到它）+ 下面那串多语正文。它俩其实是**同一块下移**，
    # 只是 `_decide_region` 按行切成了两段。对这类「同页同栏、垂直紧挨着一条
    # 已判 moved 的条目」的短文本增/删，视为同一块挪位的一部分，一并收掉。
    # ⚠️ 判据刻意收紧到「与 moved 同栏 + 垂直间距很小」，不碰孤立短文本——
    # 孤立的真增删（如单独加一个短标签）没有 moved 邻居，不会被误收。
    if moved_items and drop:
        for it in items:
            k = items.index(it)
            if k in drop or it.kind not in ("added", "removed"):
                continue
            if it.scope != "graphic" or "ocr" not in (it.src or ""):
                continue
            txt = it.new or it.old
            if not txt:
                continue
            # 短到 `_coverage` 没法判（归一化后 < OCR_MIN_LEN）才走这条路
            if len(loc._norm_cjk(txt)) >= loc.OCR_MIN_LEN:
                continue
            px = it.nx if it.nx is not None else it.x
            py = it.ny if it.ny is not None else it.y
            if px is None or py is None:
                continue
            for mv in moved_items:
                mpx = mv.nx if mv.nx is not None else mv.x
                mpy0 = mv.ny if mv.ny is not None else mv.y
                mpy1 = mv.ny2 if mv.ny2 is not None else mv.y2
                if mpx is None or mpy0 is None:
                    continue
                # 同栏：x 差 < 60pt（栏判据 CTX_X_PAD 同口径）
                if abs(px - mpx) > 60:
                    continue
                # 垂直相邻：短文本在 moved 的上下 60pt 内
                lo, hi = min(mpy0, mpy1), max(mpy0, mpy1)
                if lo - 60 <= py <= hi + 60:
                    drop.add(k)
                    n_merge += 1
                    break

    if not drop:
        return items, 0
    kept = [it for k, it in enumerate(items) if k not in drop]
    kept.extend(moved_items)
    return kept, n_merge


# ------------------------------------------------------------------ 状态栏「模式」串
# 状态栏那一行是用户唯一会逐字读完的说明，措辞必须**自证引擎来源**。
# 2026-09-30 用户原话：「你是不是用 OCR 识别了在软件里面？你不是应该优先用
# PDF Miner 吗？……要尽量优先用 PDF 的，OCR 是用来在那个 PDF 上标注用的。」
# —— 追问的起因就是旧文案只写「纯文本」+「OCR 撤销 11 处」，被读成「整份文件靠 OCR 读」。
#
# 口径（写死在这里，别散落到 f-string 里）：
#   正文 / 表格 —— 一律 pdfminer 抽文字层，任何情况下都不走 OCR；
#   转曲区     —— PDF 里以矢量曲线画的正文（没有文字层），只能 OCR，且只影响
#                 这一块；撤假增删、补录段落都在这里发生。
MODE_MAIN = {True: "表格结构化", False: "文字层 · pdfminer"}
MODE_STRUCT_FALLBACK = " + 文本兜底"
MODE_TAGS = (
    # (计数键, 文案模板)。模板里的 {n} 填计数；计数为 0 的条目不出现。
    ("blown", "※{n} 页表格抽歪，已按文本行比对"),
    ("ocr_cancel", "※转曲区 OCR 兜底撤销 {n} 处假增删"),
    ("graphic", "※转曲区补录 {n} 段（PDF 未提供文字层）"),
    ("graphic_frag", "※{n} 条文字层碎片并入转曲区"),
    ("dup", "※{n} 条重复图层已去重"),
    ("line", "※{n} 条同处差异已并成一行"),
    ("noise", "※舍去 {n} 条纯装饰符号差异"),
    ("reloc", "※{n} 处「删+增」实为内容未变的挪位，已忽略"),
)

# 引擎来源单独一行（状态栏一行放不下 200 字，会被窗口截断）。
# 这一行只回答一个问题：**这份文件到底是谁读的**。
ENGINE_LINE = {
    # 主路径真有文字层（text_lines > 0）
    "main": "识别引擎：正文 / 表格 = pdfminer 文字层（逐字读文字层，全程不 OCR）",
    "ocr": "　·　转曲区 {g} 段 = OCR 兜底（这几段 PDF 里是矢量曲线、没有文字层，仅供参考）",
    "cancel": "，并用它撤销 {n} 处假增删",
    "none": "　·　未使用 OCR（这份 PDF 的正文都带文字层）",
    # 主路径**一行文字都没读到**（纯扫描件 / 图片）：
    # ⚠️ 这时绝不能说「逐字读文字层，全程不 OCR」—— 那是**谎报**（2026-10-10 修）。
    #    这一行的存在意义就是回答「这份文件到底是谁读的」，说反了不如不说。
    "main_none": "识别引擎：没有文字层（扫描件 / 图片）—— 逐字比对用不上，结论全部来自 OCR",
    "scan_g": "　·　{g} 块区域用像素 + OCR 对拍得出（无文字层，仅供参考）",
    # 混合输入（恰好一侧有文字层、另一侧是扫描件/图片）
    "main_mixed": "识别引擎：正文 / 表格 = pdfminer 文字层（另一侧没有文字层，该侧用 OCR 补读）",
    "mixed": "　·　{n} 页只有一侧带文字层（另一侧是扫描件 / 图片），已用 OCR 补齐后比对",
}


def build_mode_used(struct_hits: bool, mode: str, **counts: int) -> str:
    """把各计数拼成状态栏「模式」串（条目与文案由 `MODE_TAGS` 决定）。"""
    out = MODE_MAIN[bool(struct_hits)]
    if struct_hits and mode == "auto":
        out += MODE_STRUCT_FALLBACK
    for key, tpl in MODE_TAGS:
        n = int(counts.get(key) or 0)
        if n:
            out += "　" + tpl.format(n=n)
    return out


def build_engine_line(graphic: int = 0, ocr_cancel: int = 0,
                      text_lines: Optional[int] = None, mixed: int = 0) -> str:
    """引擎来源那一行（界面单独显示，回答「到底用的是 pdfminer 还是 OCR」）。

    `text_lines` = 主路径（pdfminer 文字层）**实际读到的行数**。
    ⚠️ **传 0 必须如实说「没有文字层」** —— 纯图片 / 扫描件的「0 条差异」，
    用户分不清「真没改」和「根本没读」，这一行就是用来回答这个的。
    不传（`None`）时按「读了文字层」处理，只为老调用点兼容，别在新调用点省这个参数。
    """
    no_text = (text_lines == 0)
    if no_text:
        out = ENGINE_LINE["main_none"]
    elif mixed:
        out = ENGINE_LINE["main_mixed"]
    else:
        out = ENGINE_LINE["main"]
    if mixed:
        out += ENGINE_LINE["mixed"].format(n=mixed)
    if graphic:
        out += (ENGINE_LINE["scan_g"] if no_text else ENGINE_LINE["ocr"]).format(g=graphic)
        if ocr_cancel and not no_text:
            out += ENGINE_LINE["cancel"].format(n=ocr_cancel)
    elif not no_text and not mixed:
        out += ENGINE_LINE["none"]
    return out


def _note_no_layer(res: "Result", old_text, new_text, items) -> None:
    """第 8.6 节：把「没看成的页」和「结论的来源」写进 `Result.notes`。

    为什么必须有这一节（用户阶段 5 的原话：**没报错 ≠ 正确**）
    --------------------------------------------------------
    图片 / 扫描件这份输入**整份没有文字层**，清单里的每一条都是 OCR 给的。
    于是「0 条差异」有三种截然不同的含义：

      ① 两版真的没改；
      ② 两页尺寸对不上，引擎**整页跳过**（`loc.LAST_SIZE_SKIPS`）；
      ③ OCR 引擎不可用，**一处都没认**。

    不写出来，用户看到「0 处差异」只会以为是真的没改 —— 这是本工具最贵的假象。
    """
    skips: List[Dict[str, Any]] = []
    fullpage: List[Dict[str, Any]] = []
    ocr_ok = False
    try:
        import pdfdiff_locate as loc
        skips = list(getattr(loc, "LAST_SIZE_SKIPS", []) or [])
        fullpage = list(getattr(loc, "LAST_FULLPAGE_SKIPS", []) or [])
        ocr_ok = bool(loc.ocr_available())
    except Exception:
        pass
    if skips:
        detail = "、".join(
            "第%d页(%.0f×%.0f vs %.0f×%.0f)" % (s["page"] + 1, s["old"][0], s["old"][1],
                                               s["new"][0], s["new"][1])
            for s in skips[:3])
        res.notes.append(f"有 {len(skips)} 页两版页面尺寸对不上，未做核对：{detail}")
    if fullpage:
        # 整页图形重绘（嵌入图片替换 + 转曲文字重排）：这类块对 OCR 没有可对齐的
        # 对面参照，报出来就是横跨大半页的「新增」巨框。已跳过，但要如实告知 ——
        # 用户得知道「这页的图面整体重绘了」，而非「没看到」。
        # ⚠️ 2026-10-13 晚升级：巨框跳过后会用行级 OCR 对拍兜底里面的局部真差异
        #    （rev16 vs rev18 标签图面的有效期/条码/版本号就是这么找回的），
        #    rescued = 该块实际兜回来的行数 —— 有兜回的不再说「跳过」。
        rescued = [s for s in fullpage if s.get("rescued")]
        plain = [s for s in fullpage if not s.get("rescued")]
        if plain:
            detail = "、".join(
                "第%d页(覆盖页面 %.0f%%)" % (s["page"] + 1, s["frac"] * 100)
                for s in plain[:3])
            res.notes.append(f"有 {len(plain)} 处整页/大面积图形重绘，未逐块报图形差异（"
                             f"{detail}），这些页的文字层差异仍照常比对")
        if rescued:
            res.notes.append(
                f"{sum(int(s['rescued']) for s in rescued)} 处图面内差异经 OCR 逐行对拍报出"
                "（图面为位图，结论来自 OCR，仅供参考）")
    n_old = sum(len(p.lines) for p in (old_text or []))
    n_new = sum(len(p.lines) for p in (new_text or []))
    if n_old == 0 and n_new == 0 and max(res.pages_old, res.pages_new) > 0:
        res.notes.append("两份都没有文字层（扫描件或图片），全部结论来自 OCR")
        if not items:
            res.notes.append("OCR 未发现差异（可放大原图再人工确认）" if ocr_ok
                             else "OCR 引擎不可用，这份文件认不出内容")
    res.notes = res.notes[-6:]      # 别把状态栏撑爆


def _files_identical(a: str, b: str) -> bool:
    """两文件**字节级**是否完全一致（先比 size，再逐块比内容）。

    fail-fast 第一道闸：字节一致 ⇒ 内容一定一致，可以跳过全部比对。
    ⚠️ 只回答「字节是否相同」，不回答「内容是否相同」—— 字节不同 ≠ 有差异
    （元数据 / 内部结构不同也可能字节不同），所以短路到此为止，不往下猜。
    """
    try:
        sa, sb = os.path.getsize(a), os.path.getsize(b)
        if sa != sb:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                ba, bb = fa.read(1 << 20), fb.read(1 << 20)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False


def _empty_result(src_old: str, src_new: str, note: str) -> Result:
    """fail-fast 短路出口：0 差异、但如实交代「为什么一条都没比」——
    不能让用户把「跳过了」误会成「逐页比过、确实没差」。"""
    res = Result(old_path=src_old, new_path=src_new)
    res.items = []
    res.stats = summarize([])
    res.pages_old = res.pages_new = 0
    res.mode_used = build_mode_used(struct_hits=False, mode="auto", blown_pages=0)
    res.engine_line = build_engine_line()
    res.notes.append(note)
    return res


def compare_pdfs(old_path: str, new_path: str, opts: Optional[Options] = None,
                 prog=None) -> Result:
    """对比两份 PDF，返回带差异清单的 Result。"""
    t0 = time.time()
    opts = opts or Options()
    opts.compile_patterns()

    # —— fail-fast 第一道闸：原始文件字节完全一致 ⇒ 直接 0 差异 ——
    # 连「图片折页 / /Rotate 归零」都不用做：同一个字节串，内容必然一样。
    # 这是外部意见唯一认可的零风险短路点（哈希只判「完全相同」，不猜「内容相同」）。
    # ⚠️ 同一路径（自己比自己）也走这里 —— 必然 0 差异，不该白烧一次全量 OCR。
    if old_path and new_path and _files_identical(old_path, new_path):
        res = _empty_result(old_path, new_path,
                            "两文件字节完全一致（相同内容），已跳过逐页比对")
        res.elapsed = time.time() - t0
        return res

    # 图片输入（PNG/JPG/TIFF…）先折成单页 PDF：折出来是「整页一张位图、无文字层」，
    # 与扫描件 PDF 在下游眼里完全同一种东西，扫描件那条管线原样生效。
    # ⚠️ Result 里留**原件路径** —— 导出文件名、标注输出目录都用原件名，
    #    用户不该拿到一份叫 `xxx_sha1.pdf` 的标注版（见 pdfdiff_img.prepare）。
    import pdfdiff_img as _img
    src_old, src_new = old_path, new_path
    n_rot = 0
    try:
        # 带 /Rotate 的页先数出来（只报告用）：pdfminer 遇旋转页会把每个字读成一行，
        # 归一化是 `ensure_pdf` 的事，这里只负责把「我们改过口径」如实告诉用户。
        n_rot = _img.rotated_pages(src_old) + _img.rotated_pages(src_new)
        old_path, new_path = _img.ensure_pdf(old_path), _img.ensure_pdf(new_path)
    except Exception as e:
        res = Result(old_path=src_old, new_path=src_new)
        res.error = f"{e}"
        return res
    res = Result(old_path=src_old, new_path=src_new)

    for p in (old_path, new_path):
        if not os.path.isfile(p):
            res.error = f"文件不存在：{p}"
            return res

    # —— fail-fast 第二道闸：归一化后（图片折页 / /Rotate 归零）字节一致 ⇒ 0 差异 ——
    # 两个不同字节的源文件，归一化到同一份 PDF（如同一张图的不同编码、旋转归零后一致），
    # 也可安全短路 —— 此时「字节一致」是归一化产物层面的事实，不是猜测。
    if old_path != new_path and _files_identical(old_path, new_path):
        res = _empty_result(src_old, src_new,
                            "两文件归一化后内容完全一致（图片/旋转归一化后相同），已跳过逐页比对")
        res.elapsed = time.time() - t0
        return res

    try:
        old_text = extract_text_lines(old_path)
        new_text = extract_text_lines(new_path)
    except Exception as e:
        res.error = f"PDF 解析失败：{e}"
        return res
    # 字体变化判定用 fitz 渲染字号回填（pdfminer 排版字号读不准，见 _backfill_fitz_sizes）
    # —— 必须在比对之前、且只对「有文字层」的行生效；OCR 补行 size=0 不受影响。
    try:
        _backfill_fitz_sizes(old_text, old_path)
        _backfill_fitz_sizes(new_text, new_path)
    except Exception:
        pass                              # 回填失败不许拖垮整次比对，退回 pdfminer 字号
    # 混合输入补行：必须在 `mark_header_footer` **之前** —— 补进来的行也要打
    # `is_edge`（页脚的模板文字两条链路用同一套 `pdfdiff_edges` 规则判去留）。
    mixed_pages: set = set()
    try:
        mixed_pages = _mixed_ocr_fill_core(old_text, new_text, old_path, new_path)
    except Exception:
        mixed_pages = set()              # 补行失败不许拖垮整次比对
    mark_header_footer(old_text)
    mark_header_footer(new_text)

    old_tables = extract_tables(old_path) if opts.mode in ("auto", "table") else []
    new_tables = extract_tables(new_path) if opts.mode in ("auto", "table") else []

    res.pages_old, res.pages_new = len(old_text), len(new_text)
    gen = _IdGen()
    items: List[DiffItem] = []
    struct_hits = 0
    blown_pages = 0

    if opts.mode != "text" and opts.compare_meta:
        items.extend(diff_meta(old_path, new_path, gen))

    pairs = align_pages(old_text, new_text, opts.page_match_threshold)
    total = max(1, len(pairs))
    for pi, (oi, ni, _sim) in enumerate(pairs):
        po = old_text[oi] if oi is not None else None
        pn = new_text[ni] if ni is not None else None
        n0 = len(items)          # 本页新产出的条目从这儿开始（末尾统一补前后文）

        # 整页增删
        if po is None and pn is not None:
            items.append(_mk(gen, "page_added", "page", None, ni,
                             new=clip(" / ".join(l.raw for l in pn.lines[:3]), 200),
                             ctx=f"新增第 {ni + 1} 页"))
            continue
        if pn is None and po is not None:
            items.append(_mk(gen, "page_removed", "page", oi, None,
                             old=clip(" / ".join(l.raw for l in po.lines[:3]), 200),
                             ctx=f"删除第 {oi + 1} 页"))
            continue

        tb_o = old_tables[oi] if oi is not None and oi < len(old_tables) else []
        tb_n = new_tables[ni] if ni is not None and ni < len(new_tables) else []

        used_struct = False
        blown = False
        # 这一页是不是「混合输入」（恰好一侧没文字层、刚被 OCR 补过）：
        # 一侧压根没有表格可言，硬比会把整张表报成删除 —— 直接改走文本行级。
        mixed_here = ((oi is not None and oi in mixed_pages)
                      or (ni is not None and ni in mixed_pages))
        if opts.mode in ("auto", "table") and (tb_o or tb_n) and not mixed_here:
            blown = _tables_blown_up(tb_o) or _tables_blown_up(tb_n)
            if blown:
                blown_pages += 1          # 抽歪了：这一页改走文本行级，别硬比
            else:
                t_items, ok = diff_tables(tb_o, tb_n, opts, gen, oi, ni)
                if ok:
                    used_struct = True
                    struct_hits += 1
                    items.extend(t_items)

        if opts.mode != "table" or blown:
            if blown:
                # 表格抽歪的页：整页文字做字符级对齐（换行漂移不吃掉真正的改动）
                items.extend(diff_page_text(po, pn, opts, gen, oi, ni))
            else:
                # 文本层：表格已结构化比对过的页，跳过落在表格框内的行，避免重复报
                lo = prepare_lines(po, opts, drop_table_lines=used_struct, tables=tb_o)
                ln_ = prepare_lines(pn, opts, drop_table_lines=used_struct, tables=tb_n)
                items.extend(diff_lines(lo, ln_, opts, gen, oi, ni, scope="text"))

        # 本页的条目统一补「前后文」（两条比对路径在这里合流，见第 6.5 节）
        _attach_ctx(items[n0:], po, pn, opts)

        if prog:
            prog(pi + 1, total)

    # 同页「一删一增、文本一字不差」= 挪位 → 撤（与标注链路 `_drop_moved` 同口径）。
    # 放在 OCR 兜底**之前**：这一步不需要 OCR，先用精确文本撤掉最便宜的那批。
    items, reloc_dropped = _drop_relocated(items, opts, gen)
    items, ocr_dropped = _ocr_rescue_core(items, old_path, new_path, gen,
                                          old_text, new_text)
    # 第 8.5 节：转曲区兜底 —— 两侧都没有文字层、画面却真的变了，
    # 文字层原理上够不到（与标注链路读同一份 graphic_regions，见该函数注释）。
    items, graphic_added, graphic_frag = _graphic_rescue_core(
        items, old_path, new_path, gen, old_text, new_text, opts)
    # 第 8.5.1 节：转曲区「一删一增、OCR 高度相似」= 同一句挪位 → 合并成「移动」。
    # 必须在 `_graphic_rescue_core` 之后、其余判定之前跑：它吃的就是 graphic 兜底
    # 刚补出来的那批 OCR 条目，早跑没有、晚跑会被别的漏斗动过。
    items, graphic_reloc = _merge_graphic_relocation(items, gen)

    # 第 8.6 节：如实交代「哪些没看成 / 结论从哪来」（用户阶段 5：没报错 ≠ 正确）。
    # 图片输入这条尤其要紧 —— 整份没有文字层，0 条差异可能是「真没改」，
    # 也可能是「两页尺寸对不上根本没看」或「OCR 没装」，用户自己分不出来。
    _note_no_layer(res, old_text, new_text, items)
    if mixed_pages:
        # 落在「靠 OCR 补出来」的那几页上的结论，一律降档到 `ocr_line`
        # （取低者定档）—— 这些条目的存在性有 OCR 误认风险，界面要提示人核一眼。
        # 放在所有判定**之后**：补回来的 modified / 撤销配对条目也都覆盖得到。
        for it in items:
            if it.page_old in mixed_pages or it.page_new in mixed_pages:
                it.set_src("ocr_line")
        res.notes.append(
            f"有 {len(mixed_pages)} 页只有一侧带文字层（另一侧是扫描件/图片），"
            "已用 OCR 补齐后比对，这几页的结论请人工核对")
        res.notes = res.notes[-6:]
    if n_rot:
        # 口径变更要留痕：旋转页不是「读不到」，是「先归零再比」—— 用户得知道。
        res.notes.append(f"有 {n_rot} 页带 /Rotate（显示时旋转），已按 0 度归一化后再比对")
        res.notes = res.notes[-6:]
    # 同一视觉行的**去重**（源文件重复文字层）与**合并**（页脚的文件号 + 修订日期）。
    # 同样放在所有判定**之后**：只改展示粒度（少几条 / 并成一条），不改任何判定。
    items, dup_dropped, line_merged = _line_normalize_core(
        items, old_path, new_path, old_text, new_text)
    # 第 7.5 节：舍去「只剩装饰符号」的差异（`•` / `♦` / ○●◆▪ / 零宽字符…）。
    # 放在所有判定**之后** —— 这是展示层的漏斗，不改任何判定。
    items, noise_dropped = drop_noise(items, getattr(opts, "drop_noise", True))
    items.sort(key=lambda it: ((it.page_new if it.page_new is not None else
                                (it.page_old if it.page_old is not None else -1)), it.id))
    res.items = items
    res.stats = summarize(items)
    res.mode_used = build_mode_used(
        struct_hits=struct_hits, mode=opts.mode, blown_pages=blown_pages,
        ocr_cancel=ocr_dropped, graphic=graphic_added, graphic_frag=graphic_frag,
        dup=dup_dropped, line=line_merged, noise=noise_dropped,
        reloc=reloc_dropped + graphic_reloc)
    n_text_lines = (sum(len(p.lines) for p in old_text)
                    + sum(len(p.lines) for p in new_text))
    res.engine_line = build_engine_line(graphic=graphic_added, ocr_cancel=ocr_dropped,
                                        text_lines=n_text_lines, mixed=len(mixed_pages))
    res.elapsed = time.time() - t0
    return res


def summarize(items: List[DiffItem]) -> Dict[str, int]:
    s = {"total": len(items), "added": 0, "removed": 0,
         "moved": 0, "font": 0, "page_added": 0, "page_removed": 0,
         "table": 0, "text": 0, "graphic": 0, "meta": 0}
    for it in items:
        if it.kind in s:
            s[it.kind] += 1
        if it.scope in s:
            s[it.scope] += 1
    return s


# ==================================================================
# 第 9 节  导出
# ==================================================================

KIND_LABEL = {
    "added": "新增", "removed": "删除",
    "moved": "位置移动", "font": "字体变化",
    "page_added": "新增页", "page_removed": "删除页",
}
SCOPE_LABEL = {"text": "文本", "table": "表格", "page": "页面", "meta": "属性",
               "graphic": "图形区"}

_COLUMNS = [
    ("序号", 6), ("类型", 10), ("范围", 8), ("旧页", 6), ("新页", 6),
    ("位置(行/列)", 12), ("上下文", 26), ("旧值", 46), ("新值", 46), ("相似度", 8), ("备注", 22),
]


def _row_of(it: DiffItem) -> List[Any]:
    pos = ""
    if it.row:
        pos = f"行{it.row}" + (f"/列{it.col}" if it.col else "")
    return [
        it.id, KIND_LABEL.get(it.kind, it.kind), SCOPE_LABEL.get(it.scope, it.scope),
        (it.page_old + 1) if it.page_old is not None else "",
        (it.page_new + 1) if it.page_new is not None else "",
        pos, it.context, it.old, it.new,
        f"{it.similarity:.0%}" if it.similarity else "", it.note,
    ]


def export_excel(res: Result, path: str) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "差异清单"

    ws.append(["PDF 差异清单"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"旧版：{os.path.basename(res.old_path)}    新版：{os.path.basename(res.new_path)}"])
    ws.append([f"比对模式：{res.mode_used}    耗时：{res.elapsed:.2f}s    总变动：{res.stats.get('total', 0)} 处"
               f"（新增 {res.stats.get('added', 0)} / 删除 {res.stats.get('removed', 0)}）"])
    ws.append([])

    head_row = 5
    for c, (name, _w) in enumerate(_COLUMNS, start=1):
        cell = ws.cell(row=head_row, column=c, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F5597")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for c, (_n, w) in enumerate(_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(c)].width = w

    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    fills = {
        "added": PatternFill("solid", fgColor="E2F0D9"),
        "removed": PatternFill("solid", fgColor="FCE4E4"),
        "page_added": PatternFill("solid", fgColor="DEEBF7"),
        "page_removed": PatternFill("solid", fgColor="DEEBF7"),
    }
    for r, it in enumerate(res.items, start=head_row + 1):
        for c, v in enumerate(_row_of(it), start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=(c in (7, 8, 9, 11)))
            f = fills.get(it.kind)
            if f:
                cell.fill = f
    ws.freeze_panes = ws.cell(row=head_row + 1, column=1)
    ws.auto_filter.ref = f"A{head_row}:{get_column_letter(len(_COLUMNS))}{head_row + max(1, len(res.items))}"

    wb.save(path)
    return path


def export_csv(res: Result, path: str) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([c[0] for c in _COLUMNS])
        for it in res.items:
            w.writerow(_row_of(it))
    return path


def export_json(res: Result, path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res.to_dict(), f, ensure_ascii=False, indent=2)
    return path


_HTML_TPL = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>PDF 差异清单</title><style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:24px;color:#222;background:#fff}
h1{font-size:20px;margin:0 0 6px}.sub{color:#666;font-size:13px;margin-bottom:14px}
.sum{background:#f6f8fa;border:1px solid #e1e4e8;border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:13px}
.sum b{font-size:15px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{background:#2f5597;color:#fff;padding:8px 6px;text-align:left;position:sticky;top:0}
td{border:1px solid #e5e7eb;padding:6px;vertical-align:top;word-break:break-all}
tr:nth-child(even) td{background:#fafafa}
.k{display:inline-block;padding:1px 7px;border-radius:10px;font-size:12px;white-space:nowrap}
.added{background:#e2f0d9;color:#256029}.removed{background:#fce4e4;color:#9c1c1c}
.page_added,.page_removed{background:#deebf7;color:#1f4e79}
.old{color:#9c1c1c}.new{color:#256029}
</style></head><body>
<h1>PDF 差异清单</h1>
<div class="sub">旧版：<b>$old</b> ｜ 新版：<b>$new</b> ｜ 模式：$mode ｜ 耗时：$elapsed s</div>
<div class="sum">共 <b>$total</b> 处变动 —— 新增 $added ｜ 删除 $removed ｜ 新增页 $page_added ｜ 删除页 $page_removed</div>
<table><thead><tr>$heads</tr></thead><tbody>$rows</tbody></table>
</body></html>"""


def export_html(res: Result, path: str) -> str:
    import html as _h
    from string import Template
    heads = "".join(f"<th>{_h.escape(c[0])}</th>" for c in _COLUMNS)
    rows = []
    for it in res.items:
        vals = _row_of(it)
        tds = []
        for i, v in enumerate(vals):
            v = "" if v is None else str(v)
            if i == 1:
                tds.append(f'<td><span class="k {it.kind}">{_h.escape(v)}</span></td>')
            elif i == 7:
                tds.append(f'<td class="old">{_h.escape(v)}</td>')
            elif i == 8:
                tds.append(f'<td class="new">{_h.escape(v)}</td>')
            else:
                tds.append(f"<td>{_h.escape(v)}</td>")
        rows.append("<tr>" + "".join(tds) + "</tr>")
    s = res.stats
    out = Template(_HTML_TPL).safe_substitute(
        old=_h.escape(os.path.basename(res.old_path)), new=_h.escape(os.path.basename(res.new_path)),
        mode=_h.escape(res.mode_used), elapsed=f"{res.elapsed:.2f}", total=s.get("total", 0),
        added=s.get("added", 0), removed=s.get("removed", 0),
        page_added=s.get("page_added", 0), page_removed=s.get("page_removed", 0),
        heads=heads, rows="".join(rows))
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    return path


# ==================================================================
# 第 10 节  自检
# ==================================================================

def self_test() -> Tuple[int, int]:
    """纯逻辑自检（不依赖样本 PDF）：验证归一化、数值、行 diff、页面对齐、表格 diff、导出。"""
    ok, fail = 0, 0

    def chk(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name} {extra}")

    o = Options()
    chk("归一化-全角", normalize_text("压　力（Bar）", o) == normalize_text("压力(Bar)", o))
    chk("归一化-破折号", normalize_text("10—20", o) == "10-20")
    chk("数值解析-带单位", parse_number("φ4.0mm") == 4.0)
    chk("数值解析-千分位", parse_number("1,250.5") == 1250.5)
    nd = numeric_delta("温度 150°C", "温度 180°C")
    chk("数值变动识别", nd is not None and nd["delta"] == 30)
    chk("非数值不误判", numeric_delta("温度 150°C", "压力 150Bar") is None)
    chk("数值未变不误判", numeric_delta("设备编号 WI-2001 rev01", "设备编号 WI-2001 rev02") is None)
    chk("编号负号不误读", parse_number("编号 WI-2001") == 2001.0)

    g = _IdGen()
    a = [TextLine("压力 150", 0, 100, 50, 110), TextLine("时间 30s", 0, 90, 50, 100)]
    b = [TextLine("压力 180", 0, 100, 50, 110), TextLine("时间 30s", 0, 90, 50, 100)]
    items = diff_lines(a, b, Options(), g, 0, 0)
    chk("行diff-数值改动拆成删+增", len(items) == 2
        and items[0].kind == "removed" and items[0].old == "压力 150"
        and items[1].kind == "added" and items[1].new == "压力 180")

    a2 = [TextLine("AAA", 0, 100, 20, 110), TextLine("压力设定值 200", 0, 90, 20, 100)]
    b2 = [TextLine("AAA", 0, 100, 20, 110), TextLine("压力设定值 210", 0, 90, 20, 100), TextLine("DDD", 0, 80, 20, 90)]
    items2 = diff_lines(a2, b2, Options(), g, 0, 0)
    kinds = [i.kind for i in items2]
    chk("行diff-数值改(拆删+增)+新增行", kinds.count("removed") == 1 and kinds.count("added") == 2)

    a3 = [TextLine("操作方法见附录 A", 0, 100, 20, 110)]
    b3 = [TextLine("操作方法见附录 B", 0, 100, 20, 110)]
    it3 = diff_lines(a3, b3, Options(), g, 0, 0)
    chk("行diff-相似改动拆成删+增", len(it3) == 2
        and it3[0].kind == "removed" and it3[1].kind == "added")

    a4 = [TextLine("设备编号 WI-2001", 0, 100, 20, 110)]
    b4 = [TextLine("设备编号 WI-2002", 0, 100, 20, 110)]
    it4 = diff_lines(a4, b4, Options(), g, 0, 0)
    chk("行diff-编号变化拆成删+增", len(it4) == 2
        and it4[0].kind == "removed" and it4[1].kind == "added")

    # 页面对齐：新增一页
    def mkpage(idx, lines):
        p = PageText(index=idx, height=800)
        p.lines = [TextLine(t, 0, 700 - 20 * i, 100, 710 - 20 * i) for i, t in enumerate(lines)]
        return p

    po = [mkpage(0, ["第一章", "内容A"]), mkpage(1, ["第二章", "内容B"])]
    pn = [mkpage(0, ["第一章", "内容A"]), mkpage(1, ["第二章", "内容B"]), mkpage(2, ["第三章", "内容C"])]
    pairs = align_pages(po, pn, 0.45)
    chk("页面对齐-新增页", any(x[0] is None and x[1] == 2 for x in pairs))

    # 表格 diff
    t1 = PageTable(0, (0, 0, 100, 100), 3, 3, rows=[["参数", "A", "B"], ["压力", "10", "20"], ["时间", "30", "40"]])
    t2 = PageTable(0, (0, 0, 100, 100), 3, 3, rows=[["参数", "A", "B"], ["压力", "12", "20"], ["时间", "30", "45"]])
    ti, ok_struct = diff_tables([t1], [t2], Options(), _IdGen(), 0, 0)
    chk("表格-结构化生效", ok_struct)
    chk("表格-两处数值改动拆成删+增", len(ti) == 4
        and sum(x.kind == "removed" for x in ti) == 2
        and sum(x.kind == "added" for x in ti) == 2)
    chk("表格-带列名", all("压力" in x.context or "时间" in x.context for x in ti))

    # 表格整表新增
    ti2, _ = diff_tables([t1], [], Options(), _IdGen(), 0, 0)
    chk("表格-整表删除", len(ti2) == 1 and ti2[0].kind == "removed")

    # 元数据
    meta_o = Options(ignore_patterns=[r"^REV"])
    meta_o.compile_patterns()
    chk("忽略正则生效", meta_o.skip_line("REV: B2") and not meta_o.skip_line("压力 10"))

    # 导出
    r = Result(old_path="a.pdf", new_path="b.pdf", items=items + ti, stats=summarize(items + ti))
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for fn, ext in ((export_excel, ".xlsx"), (export_csv, ".csv"),
                        (export_json, ".json"), (export_html, ".html")):
            p = os.path.join(d, "t" + ext)
            try:
                fn(r, p)
                chk(f"导出{ext}", os.path.getsize(p) > 0)
            except Exception as e:
                chk(f"导出{ext}({e})", False)

    # OCR 撤销（假增删）——只验「不炸、不越界」，不真的调 tesseract（自检要快）
    chk("OCR撤销-空列表原样返回", _ocr_rescue_core([], "a.pdf", "b.pdf") == ([], 0))
    _d = DiffItem(id=1, kind="removed", scope="text", old="6")
    chk("OCR撤销-不动纯删", _ocr_rescue_core([_d], "a.pdf", "b.pdf")[1] == 0)
    _d2 = DiffItem(id=2, kind="removed", scope="text", old="生产日期")
    chk("OCR撤销-短于阈值不处理",
        _ocr_rescue_core([_d2], "a.pdf", "b.pdf") == ([_d2], 0))
    _saved = OCR_RESCUE
    try:
        globals()["OCR_RESCUE"] = False
        chk("OCR撤销-开关关闭时原样返回",
            _ocr_rescue_core([_d2], "a.pdf", "b.pdf") == ([_d2], 0))
    finally:
        globals()["OCR_RESCUE"] = _saved

    # ---- 2026-10-13 方案 A「反向覆盖率」：改写行不再被静默撤销 ----
    # 病根：旧口径覆盖率 ≥0.85 就把「缺字」的改写也当「内容还在」撤销。
    # 现在 `_band_full` 要求 ≥0.995（逐字无缺），改写行落进 `rewrites` 拆 removed+added。
    # 以下三条是纯逻辑判据，不调 OCR：
    chk("方案A判据-逐字无缺(cov=1.0)算 full", 1.0 >= OCR_RESCUE_FULL_COVER)
    chk("方案A判据-缺一字(cov=0.963)不算 full（改写）", 0.963 < OCR_RESCUE_FULL_COVER)
    chk("方案A判据-宽松口径仍命中(0.963≥0.85)", 0.963 >= 0.85)

    # `_peer_line`：从对面 OCR 竖带里挑「改写后」最像的那一行（双向覆盖率最高）。
    # 用假竖带不依赖真 OCR：三行里只有一行是「定用途的」改写后的版本。
    _fake_band = "\n".join([
        "尽管球囊导管，以下简称为产品，一直在严格控制的条件下",
        "不对产品的适销性或针对特定用途适用性作出任何明示或者暗示的担保；博迈医疗及其联属公司不对",
        "上文所载的排斥和限制，不应解释为违反适用法律的强制性规定",
    ])
    _peer = _peer_line("定用途的适用性作出任何明示或者暗示的担保;博迈医疗及其联",
                       _fake_band)
    chk("方案A-改写后行提取：命中最像的那一行",
        "适用性作出任何明示或者暗示的担保" in _peer,
        _peer)
    chk("方案A-空竖带返回空串", _peer_line("任意文字", "") == "")
    chk("方案A-无匹配行返回空串",
        _peer_line("甲乙丙丁戊己庚辛壬癸", "完全不同的另一句话") == "")

    # ---- 2026-10-13 删「变动」档：撤销「删除」不再把同处「新增」合回一条「修改」----
    # 旧语义（2026-09-30）会把「一删一增同处」合回「修改」；删掉变动档后，
    # `_join_dropped_replaces` 退化为「按 drop 集合过滤」——被撤的删除照旧丢掉，
    # 留下的新增继续当新增（它携带了真正的新内容）。
    def _di(kind, *, x=None, y=None, nx=None, ny=None, old="", new=""):
        return DiffItem(id=0, kind=kind, scope="text", page_old=0, page_new=0,
                        x=x, y=y, nx=nx, ny=ny, old=old, new=new)

    _rm = _di("removed", x=641.1, y=503.2,
              old="4栋201室，8栋201室，13栋101室、201室，15栋，16栋")
    _ad = _di("added", nx=633.4, ny=502.2,
              new="201室，4栋201室，8栋201室，10栋301室，11栋301室，13栋101室、")
    _j1 = _join_dropped_replaces([_rm, _ad], {0}, _IdGen())
    chk("撤销删除：被撤的删除丢掉、同处新增保留（不再合并成修改）",
        len(_j1) == 1 and _j1[0].kind == "added" and _j1[0].new.startswith("201室，4栋"),
        str([(i.kind, i.old[:12], i.new[:12]) for i in _j1]))
    chk("撤销删除：drop 集合外的新增照旧保留",
        _join_dropped_replaces([_rm, _ad], {0}, _IdGen())[0].kind == "added")
    chk("撤销删除：找不到可配对时照旧按 drop 撤销",
        _join_dropped_replaces([_rm], {0}, _IdGen()) == [])
    _keep = _di("added", nx=633.4, ny=502.2, new="没被撤销的新增")
    _j4 = _join_dropped_replaces([_keep, _rm], {1}, _IdGen())
    chk("撤销删除：删除被撤、新增留下（顺序无关）",
        len(_j4) == 1 and _j4[0].kind == "added" and _j4[0].new == "没被撤销的新增",
        str([(i.kind) for i in _j4]))

    # ---- 2026-09-30 新增：同一行归一（去重 / 合并）+ 转曲碎片 ----
    chk("判重指纹：剥掉装饰符号后相等（重复图层靠它认出来）",
        _norm_line_text("♦ 用充压装置") == _norm_line_text("用充压装置"))
    chk("判重指纹：忽略空白", _norm_line_text("A B") == _norm_line_text("AB"))

    class _Pg:                       # 极简页面桩：只要 height
        height = 595.275

    class _It:                       # 极简条目桩
        scope = "text"
        kind = "added"
        page_new = 0
        page_old = None
        x = y = y2 = nx = None
        old = new = ""

    def _mk_it(kind, nx, ny, ny2, text):
        it = _It()
        it.kind, it.nx, it.ny, it.ny2, it.new = kind, nx, ny, ny2, text
        return it

    _g = _py_geom(_mk_it("added", 548.42, 476.80, 482.80, "生理"), "new", [_Pg()], [_Pg()])
    chk("坐标映射：pdfminer（左下原点）→ PyMuPDF（左上原点）纵向翻转",
        _g is not None and abs(_g[2] - 115.475) < 0.01, f"实得 {_g}")

    # 转曲区条目写清单**前**的翻转（2026-09-30）：不翻的话同页转曲区与正文
    # 在纵向相差 180°，分段永远判不出「紧挨着」。
    _fy0, _fy1 = _flip_y(105.0, 149.4, 595.275)
    chk("转曲区坐标：PyMuPDF → pdfminer 口径（y 翻转、上边仍在上边）",
        abs(_fy0 - 490.275) < 1e-6 and abs(_fy1 - 445.875) < 1e-6, f"实得 {(_fy0, _fy1)}")
    chk("转曲区坐标：翻转后「上边」数值更大（与 text 条目同向）", _fy0 > _fy1)
    chk("转曲区坐标：拿不到页高就原样返回，不凭空猜",
        _flip_y(105.0, 149.4, _page_height([], 3)) == (105.0, 149.4))
    chk("页高：正常取到", abs(_page_height([_Pg()], 0) - 595.275) < 1e-6)
    chk("页高：越界返回 0（= 拿不到，不抛异常）", _page_height([_Pg()], 9) == 0.0)

    # ---- graphic 条目相似度（2026-10-11 GRA-3925 用户实测 sim 全 0）----
    # `_graphic_rescue_core` 的 mod 分支现在按归一化文本算 SequenceMatcher ratio；
    # 这里用同口径直接验证计算式，防止将来有人把 sim 又删回默认 0。
    from difflib import SequenceMatcher as _sm_chk
    _sim_chk = _sm_chk(None, "a35070150200", "a35070300200",
                       autojunk=False).ratio()
    chk("转曲区相似度：`A35-070150-200→A35-070300-200` 应 ≥0.8（不是 0）",
        _sim_chk > 0.8, f"ratio={_sim_chk:.3f}")
    chk("转曲区相似度：完全相同的两侧 = 1.0",
        _sm_chk(None, "abc", "abc").ratio() == 1.0)

    chk("点落块内判定", _in_block((5, 5), (0, 0, 10, 10)) and not _in_block((50, 5), (0, 0, 10, 10)))
    chk("同块判定：条块（单行）算同块",
        _same_block([(0, 0, 100, 8)], (5, 4), (50, 4), 8.0))
    chk("同块判定：整段块不参与（否则一段里的改动会被缝起来）",
        not _same_block([(0, 0, 100, 60)], (5, 4), (50, 40), 8.0))

    _segs = [(0, (444.6, 105.0, 605.4, 149.4), "new", "…需用浸有生理盐水的纱布擦拭…")]
    _f = _mk_it("added", 548.42, 476.80, 482.80, "生理")
    chk("转曲碎片：位置落在段内 → 命中", _frag_segment(_f, _segs, [_Pg()], [_Pg()]) is not None)
    _f2 = _mk_it("added", 506.42, 497.80, 503.80, "生理")
    chk("转曲碎片：段外但内容属于该段 → 也命中（位置判据够不着的那半句）",
        _frag_segment(_f2, _segs, [_Pg()], [_Pg()]) is not None)
    _f3 = _mk_it("added", 30.0, 100.0, 106.0, "生产日期")
    chk("转曲碎片：无关短行不误撤", _frag_segment(_f3, _segs, [_Pg()], [_Pg()]) is None)
    _f4 = _mk_it("added", 30.0, 100.0, 106.0, "生理盐水纱布擦拭球囊导管使用再折叠工具")
    chk("转曲碎片：长句不做内容判据（只有短碎片才可能是残留）",
        _frag_segment(_f4, _segs, [_Pg()], [_Pg()]) is None)

    # ⑩ 状态栏「模式」串：必须自证引擎来源（用户 2026-09-30 的追问点）
    _m_plain = build_mode_used(False, "auto")
    chk("模式串：无表格时写明 pdfminer", "pdfminer" in _m_plain and "OCR" not in _m_plain,
        _m_plain)
    _m_tab = build_mode_used(True, "auto")
    chk("模式串：抽到表格写「表格结构化 + 文本兜底」",
        _m_tab.startswith("表格结构化") and "文本兜底" in _m_tab, _m_tab)
    chk("模式串：仅 table 模式不加「文本兜底」",
        "文本兜底" not in build_mode_used(True, "table"))
    _m_ocr = build_mode_used(False, "auto", ocr_cancel=11)
    chk("模式串：OCR 只挂在「转曲区」下，绝不让正文看起来像 OCR 读的",
        "转曲区 OCR 兜底撤销" in _m_ocr and "11 处" in _m_ocr
        and _m_ocr.startswith("文字层 · pdfminer"), _m_ocr)
    chk("模式串：计数为 0 的条目一律不出现",
        "补录" not in _m_ocr and "装饰符号" not in _m_ocr, _m_ocr)
    _m_all = build_mode_used(True, "auto", blown=2, ocr_cancel=1, graphic=1,
                             graphic_frag=1, dup=1, line=4, noise=3)
    _keys = ["抽歪", "转曲区 OCR 兜底", "转曲区补录", "碎片并入", "已去重",
             "并成一行", "装饰符号"]
    _pos = [_m_all.index(k) for k in _keys]
    chk("模式串：条目顺序与 MODE_TAGS 表一致（人工按表核过一遍）",
        _pos == sorted(_pos), _m_all)
    chk("模式串：graphic 条目文案点明「PDF 未提供文字层」",
        "PDF 未提供文字层" in build_mode_used(False, "auto", graphic=1))

    # ⑪ 引擎来源那一行：这是回答用户「你是不是用 OCR 识别了」的地方
    _e_no = build_engine_line()
    chk("引擎行：没用 OCR 时要明说「未使用 OCR」",
        "pdfminer" in _e_no and "未使用 OCR" in _e_no, _e_no)
    _e_yes = build_engine_line(graphic=1, ocr_cancel=11)
    chk("引擎行：用了 OCR 要点明只有转曲区那几段",
        "pdfminer" in _e_yes and "转曲区 1 段" in _e_yes
        and "撤销 11 处假增删" in _e_yes and "仅供参考" in _e_yes, _e_yes)
    chk("引擎行：没有转曲段时不许出现 OCR 兜底字样",
        "转曲区" not in _e_no, _e_no)
    # 2026-10-10：以前不管读没读到文字层，这行都写「逐字读文字层，全程不 OCR」，
    # 而 notes 同时写着「全部结论来自 OCR」—— 两句自相矛盾（用户会以为真读到了）。
    _e_scan = build_engine_line(graphic=2, text_lines=0)
    chk("引擎行：纯图片/扫描件不许说「逐字读文字层」",
        "没有文字层" in _e_scan and "逐字读文字层" not in _e_scan, _e_scan)
    _e_mix = build_engine_line(text_lines=5, mixed=1)
    chk("引擎行：混合输入要点明「已用 OCR 补齐」且不再号称全程不 OCR",
        "已用 OCR 补齐" in _e_mix and "全程不 OCR" not in _e_mix, _e_mix)

    # ⑫ 转曲区条目也要有「前后文」（2026-09-30 用户：「一个再折叠工具这句话
    #    怎么没有前后文」）。用桩替掉 graphic_regions，喂一页假文字层，
    #    验证上下行按栏接得上、且 y 用块下缘（与文字行 top=y0 同口径）。
    import pdfdiff_locate as _loc_mod
    _orig_regions = _loc_mod.graphic_regions
    _pt = PageText(index=0, width=841.89, height=595.275)
    _pt.lines = [
        TextLine(raw="上面的行", x0=55.0, top=400.0, x1=200.0, bottom=406.0),
        TextLine(raw="下面的行", x0=55.0, top=370.0, x1=200.0, bottom=376.0),
    ]
    _loc_mod.graphic_regions = lambda a, b: [
        {"page": 0, "rect": (50.0, 200.0, 200.0, 210.0),
         "old_text": "", "new_text": "转曲新增"}]
    try:
        _gitems, _gadd, _ = _graphic_rescue_core(
            [], "fake_old.pdf", "fake_new.pdf", _IdGen(),
            pages_old=None, pages_new=[_pt], opts=Options())
        chk("转曲区条目补录 1 条", _gadd == 1 and len(_gitems) == 1)
        if _gitems:
            chk("转曲区条目有前文（同栏上方行）",
                _gitems[0].ctx_before == "上面的行", repr(_gitems[0].ctx_before))
            chk("转曲区条目有后文（同栏下方行）",
                _gitems[0].ctx_after == "下面的行", repr(_gitems[0].ctx_after))
            # OCR 认出了字 ⇒ 不该算最低档「像素区域」（那是真的没文本时的兜底）
            chk("单侧有字的转曲条目 = ocr_line（有文本、坐标实测）",
                _gitems[0].src == "ocr_line", _gitems[0].src)
    finally:
        _loc_mod.graphic_regions = _orig_regions

    # 两侧都有字 + 被 `_narrow_mod_region` 收窄过 ⇒ ocr_est：
    # 坐标是按字符占比**折算**的（实测误差 <1%，但终究是估的）⇒ 必须降 loc_conf。
    _loc_mod.graphic_regions = lambda a, b: [
        {"page": 0, "rect": (50.0, 200.0, 200.0, 210.0),
         "old_text": "旧版这一行", "new_text": "新版这一行", "narrowed": True}]
    try:
        _gi2, _gadd2, _ = _graphic_rescue_core(
            [], "fake_old.pdf", "fake_new.pdf", _IdGen(),
            pages_old=None, pages_new=[_pt], opts=Options())
        chk("收窄过的改写条目 = ocr_est（位置是估的）",
            bool(_gi2) and _gi2[0].src == "ocr_est",
            _gi2[0].src if _gi2 else "无条目")
        chk("收窄条目档位为中（不装高）",
            bool(_gi2) and conf_level_of(_gi2[0]) == "mid",
            _gi2[0].level if _gi2 else "无条目")
    finally:
        _loc_mod.graphic_regions = _orig_regions

    # ---- 置信度（2026-10-10 加，治「换一份新 PDF 就出错」的总根）----
    # ⚠️ 这些用例钉的是**口径本身**，不是某份语料的行为 ——
    #    引擎原先永远给确定结论、从不说「我没把握」，这里把「多可信」显式化；
    #    以后谁改阈值/加来源，都得先过这一关。
    chk("来源表：文字层精确比对最可靠", SRC_CONF["text"] == (0.95, 0.95))
    chk("来源表：Word 直读不低于文字层", SRC_CONF["docx"][0] >= SRC_CONF["text"][0])
    chk("来源表：像素兜底最低", SRC_CONF["graphic"][0] < SRC_CONF["ocr_line"][0])
    chk("未知来源按最保守处理（宁可报低，不可装准）",
        conf_of("根本不存在的来源") == SRC_CONF["graphic"])
    chk("分档：取 conf 与 loc_conf 的**较低者**",
        conf_level(0.95, 0.40) == "low" and conf_level(0.40, 0.95) == "low")
    chk("分档：两者都高才是高", conf_level(0.95, 0.95) == "high")
    chk("分档：中档下边界", conf_level(CONF_MID_MIN, CONF_MID_MIN) == "mid"
        and conf_level(CONF_MID_MIN - 0.01, 0.99) == "low")
    chk("分档：坐标估的（ocr_est）落到中档，不装高",
        conf_level(*SRC_CONF["ocr_est"]) == "mid")
    _ci = DiffItem(id=1, kind="removed", old="a")
    chk("set_src 写入来源与置信度",
        _ci.set_src("ocr_est").conf == SRC_CONF["ocr_est"][0]
        and _ci.src == "ocr_est" and _ci.level == "mid")
    chk("set_src 可显式覆盖（位置没把握时单独降 loc_conf）",
        DiffItem(id=2).set_src("text", loc_conf=0.3).level == "low")
    chk("字段缺失按最保守（拿到 0 当低，绝不装准）", conf_level_of(DiffItem(id=3)) == "low")

    # ---- 挪位撤销（2026-10-11 加，合成留出集 S5 抓出来的两条链路不一致）----
    # 文字层走**顺序敏感**的行 diff，而标注侧先做「完全相同行的全局抵消」——
    # 段落只换顺序时，清单报「删+增」、标注报 0 条，用户没法判断谁对。
    # 这里把口径补齐（`_drop_relocated`）并用用例钉住判据的三条边界。
    _lo = DiffItem(id=1, kind="removed", scope="text", old="乙段落：核对批号。",
                   page_old=0, page_new=0, x=10, y=100, y2=112)
    _ld = DiffItem(id=2, kind="added", scope="text", new="乙段落：核对批号。",
                   page_old=0, page_new=0, nx=10, ny=130, ny2=142)
    _rl, _rn = _drop_relocated([_lo, _ld], Options())
    chk("挪位合并：同页「删+增」一字不差 → 合成一条 moved（2026-10-12 位移标蓝）",
        len(_rl) == 1 and _rn == 1 and _rl[0].kind == "moved"
        and _rl[0].y == 100 and _rl[0].ny == 130)
    # 同一位置重贴（dy < _MOVED_MIN_DY）→ 仍撤掉不报
    _ld_same = DiffItem(id=2, kind="added", scope="text", new="乙段落：核对批号。",
                        page_old=0, page_new=0, nx=10, ny=101, ny2=113)
    _rl_same, _rn_same = _drop_relocated([_lo, _ld_same], Options())
    chk("挪位合并：同位置重贴（dy<阈值）照旧撤掉，不刷浅蓝",
        _rl_same == [] and _rn_same == 1)
    _ld2 = DiffItem(id=2, kind="added", scope="text", new="乙段落：核对批号！",
                    page_old=0, page_new=0)
    _rl2, _rn2 = _drop_relocated([_lo, _ld2], Options())
    chk("挪位合并：改过一个字的一概保留（那是 A 删 B 增，不是挪位）",
        len(_rl2) == 2 and _rn2 == 0)
    _ld3 = DiffItem(id=2, kind="added", scope="text", new="乙段落：核对批号。",
                    page_old=1, page_new=1)
    _rl3, _rn3 = _drop_relocated([_lo, _ld3], Options())
    chk("挪位合并：跨页不合并（更像真改动）", len(_rl3) == 2 and _rn3 == 0)
    chk("挪位合并：只有单侧、找不到另一半时原样保留",
        _drop_relocated([_lo], Options())[0] == [_lo])

    print(f"自检：{ok} 项通过，{fail} 项失败")
    return ok, fail


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        op = Options()
        if "--text" in sys.argv:
            op.mode = "text"
        r = compare_pdfs(sys.argv[1], sys.argv[2], op, prog=lambda i, t=None: None)
        print(json.dumps({"stats": r.stats, "mode": r.mode_used,
                          "elapsed": round(r.elapsed, 2), "error": r.error}, ensure_ascii=False))
        for it in r.items[:200]:
            print(f"[{it.id:03d}] {KIND_LABEL.get(it.kind, it.kind):6s} "
                  f"P{it.page_old if it.page_old is not None else '-'}→"
                  f"P{it.page_new if it.page_new is not None else '-'} "
                  f"{it.context[:30]:30s} | {it.old[:40]} => {it.new[:40]} {it.note}")
    else:
        self_test()
