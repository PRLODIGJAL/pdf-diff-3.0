# -*- coding: utf-8 -*-
"""pdfdiff_edges.py —— 「页边行」（页眉 / 页脚）的**共享去留规则**。

一句话原则
==========
**位置只负责「提名」，内容才决定「去留」。**


它为什么存在（2026-09-30 用户实测反馈）
=======================================
用户拿 GRA-4118 的**标注版**对着**软件清单**数，发现「标注上有 4 处红/绿，
清单里一条都找不到」：

    p1 / p2 页脚   文件号：GRA-4118 Rev06/DCR 2025-0850  →  Rev07
    p1 / p2 页脚   修订日期：2025年12月19日              →  2026年05月27日

根因是**两条链路对页眉页脚的口径相反**：

  · 标注链路 `pdfdiff_locate.extract_rows(drop_header_footer=False)`
    —— 早就修好了，模块里还留着警示注释「**不要按位置砍页眉页脚**」；
  · 清单链路 `pdfdiff_core.mark_header_footer` + `Options.ignore_header_footer=True`
    —— 仍然按「整行落在页面上下 8% 高度带里就丢弃」。

同一份差异，标注看得见、清单看不见 —— 用户没法判断到底谁对。
本模块把那条教训**固化成全局规则**，两条链路共用一张表。


顺带挖出的更危险的隐患
======================
这份 IFU 是**横向页 + 分栏**版式，页脚（x≈624）与左栏正文（x≈31~200）
**共享同一条水平线**（实测 p1：`造影剂有过敏反应或不能用药者` 与
`文件号：GRA-4118 …` 同处页边带）。按「整行是否落进带里」判定，
会把左栏那几行**正文**一起当成页脚丢掉 —— 只要它一改，清单**永远看不到**，
属典型的「假一致」，比漏判更伤。本模块的「内容才决定去留」正好堵住它。


规则表
======
    EDGE_NOISE_RULES   命中即「可以忽略」（页边带内的**无信息**行）
    EDGE_KEEP_RULES    命中即「必须保留」（反向白名单，挡在噪声判定之前）

判定顺序：KEEP → NOISE → 默认保留。
先看 KEEP 是因为两类模式会撞车：页脚上孤零零一个 `2025-0850` 长得就是
「纯编号」，但它其实是文件号的一部分（`06/DCR 2025-0850`），必须留。

⚠️ 这里**只放「无信息」规则**。页边的公司名、产品名、免责声明——
凡是**有语义**的内容一律走「默认保留」分支：宁可多报一条让人扫一眼，
也不能把真改动静默吃掉（与全局策略一致，见 `_ocr_rescue_core` 的注释）。
"""

from __future__ import annotations

import re
from typing import List, Tuple


# ==================================================================
# 第 1 节  位置带
# ==================================================================

# 页边带：页面上下各占页高的比例。**两条链路共用同一个值**
# （2026-09-30 之前 locate 侧是 0.055、core 侧是 0.08 —— 同一个概念两个数，
#  正是「口径不一致」得以藏身的地方）。
EDGE_BAND = 0.08


def in_edge_band(y0: float, y1: float, page_height: float,
                 band: float = EDGE_BAND) -> bool:
    """这一行的纵向范围**是否落进页边带**（页面顶部 / 底部的 `band` 比例内）。

    ⚠️ 只回答「在不在页边」，**不代表就该忽略** —— 去留看 `is_edge_noise()`。
    坐标系：调用方给什么坐标系就用什么（本函数只做比例判断，与原点方向无关，
    上下对称，所以对「内容旋转 180° 绘制」的文档同样成立）。
    """
    if not page_height or page_height <= 0:
        return False
    h = float(page_height)
    top_edge = h * (1.0 - band)
    bottom_edge = h * band
    return (y1 <= bottom_edge) or (y0 >= top_edge)


# ==================================================================
# 第 2 节  规则表
# ==================================================================

# —— 必须保留（反向白名单，先判）——
# 每项： (名字, 正则, 说明)
EDGE_KEEP_RULES: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("文件号 / 版本号", re.compile(
        r"文件号|文档号|编号|doc\s*no|document\s*no|rev\s*\.?\s*[\dA-Z]|\bver\b|版本",
        re.I),
     "改版核心信息：Rev 号一变，整份文件的适用性就变了"),
    ("修订 / 生效日期", re.compile(
        r"修订日期|生效日期|版本日期|发布日期|变更日期|有效日期|"
        r"\d{4}\s*年\s*\d{1,2}\s*月",
        re.I),
     "改版核心信息：日期直接决定现场用的是哪一版"),
    ("带年份的编号", re.compile(r"(19|20)\d{2}\s*[-/]\s*\d"),
     "形如 2025-0850 的 DCR / 工单号：看着像「纯编号」，其实是文件号主体"),
]

# —— 可以忽略（页边带内的无信息行）——
# 每项： (名字, 正则, 说明)
EDGE_NOISE_RULES: List[Tuple[str, "re.Pattern[str]", str]] = [
    ("页码 / 页数", re.compile(
        r"^\s*(?:第\s*)?\d{1,3}\s*(?:[/／\-–—]\s*\d{1,3})?\s*(?:页)?\s*$"),
     "`1/2` `2` `3-1` —— 页数一变就满篇刷差异，是真噪声"),
    ("第 N 页 / 共 N 页", re.compile(
        r"^\s*(?:第\s*\d+\s*页(?:\s*[/／]\s*共?\s*\d+\s*页)?|共\s*\d+\s*页|"
        r"page\s*\d+(?:\s*(?:of|/)\s*\d+)?|p\.?\s*\d+)\s*$", re.I),
     "中英文页码的各种写法"),
    ("纯符号 / 装饰", re.compile(
        r"^[\s\W_]+$", re.U),
     "整行只有标点或装饰符号（`•` `♦` `※`…），本身不承载信息"),
]

# 预编译汇总（正则已带 ^…$，直接 search 即可）
_KEEP = [(n, p) for n, p, _ in EDGE_KEEP_RULES]
_NOISE = [(n, p) for n, p, _ in EDGE_NOISE_RULES]


# ==================================================================
# 第 3 节  判定
# ==================================================================

def hit_keep(text: str) -> str:
    """命中哪条「必须保留」规则；没命中返回空串（便于日志/自检解释原因）。"""
    s = text or ""
    for name, pat in _KEEP:
        if pat.search(s):
            return name
    return ""


def page_number_only(text: str) -> bool:
    """**整行只有一个页码/编号**（与位置无关）。

    这是最严的一档：整行剥掉空白后就是个页码，那它在页面上哪个位置都没有
    信息量。`pdfdiff_locate.extract_rows` 一直用这个口径（它不按位置过滤），
    现在两条链路共用这里。
    ⚠️ 白名单优先：`Rev 3/2`、`2025-0850` 这类先被 KEEP 拦下，不会被误杀。
    """
    s = (text or "").strip()
    if not s:
        return False
    if hit_keep(s):
        return False
    for name, pat in _NOISE[:2]:          # 只看前两条：页码 / 第N页
        if pat.match(s):
            return True
    return False


def is_edge_noise(text: str) -> bool:
    """**页边带里**这一行能不能忽略。

    调用方必须先确认「它在页边带」（`in_edge_band`）——
    本函数只看内容，不看位置。

    判定顺序：KEEP → NOISE → 默认保留。
    """
    s = (text or "").strip()
    if not s:
        return True                        # 空行：无信息，忽略
    if hit_keep(s):
        return False                       # ① 白名单：是实质信息，必须留
    for _name, pat in _NOISE:
        if pat.match(s):
            return True                    # ② 命中「无信息」模式，可以忽略
    return False                           # ③ 默认保留


def classify(text: str) -> str:
    """给自检 / 排查用：返回 `keep:<规则名>` / `noise:<规则名>` / `keep:默认`。"""
    s = (text or "").strip()
    if not s:
        return "noise:空行"
    k = hit_keep(s)
    if k:
        return f"keep:{k}"
    for name, pat in _NOISE:
        if pat.match(s):
            return f"noise:{name}"
    return "keep:默认"


# ==================================================================
# 第 4 节  自检（`python -B pdfdiff_edges.py`）
# ==================================================================

_CASES: List[Tuple[str, bool, str]] = [
    # —— 真实数据（GRA-4118 页脚）：必须保留 ——
    ("文件号：GRA-4118 Rev06/DCR 2025-0850  修订日期：2025年12月19日  博迈医疗 ", False, "旧版整条页脚"),
    ("文件号：GRA-4118 Rev07 ", False, "新版页脚（改后）"),
    (" 修订日期：2026年05月27日    博迈医疗    1/2", False, "新版页脚拆行 + 尾随页码"),
    ("06/DCR 2025-0850", False, "裸文件号：看着像编号，KEEP 必须拦下"),
    ("2025-0850", False, "只有编号 + 年份 → 仍算文件号主体"),
    ("博迈医疗", False, "公司名：有语义，默认保留"),
    # —— 真实数据（GRA-4118 左栏正文误入页边带）：必须保留 ——
    ("造影剂有过敏反应或不能用药者", False, "左栏正文被整行判进页边带，绝不能丢"),
    ("无法进行抗凝治疗的患者", False, "同上"),
    ("以下情况的患者术者使用器械时应予周密考虑，包括但不限于：", False, "同上"),
    # —— 真噪声：必须忽略 ——
    (" 1/2", True, "页码"),
    ("2", True, "纯页码"),
    ("3-1", True, "编号"),
    ("第 3 页", True, "中文页码"),
    ("共 8 页", True, "总页数"),
    ("Page 2 of 5", True, "英文页码"),
    ("•", True, "纯装饰符号"),
    ("♦", True, "纯装饰符号"),
    ("", True, "空行"),
    ("   ", True, "空白行"),
    # —— 边界：容易误判的两侧 ——
    ("1/2", True, "页码（无前导空格）"),
    ("Rev 1/2", False, "带 Rev 的编号：白名单优先，不能当页码丢"),
    ("版本 2", False, "带「版本」二字"),
    ("GRA-4118", False, "文件号但不是「纯页码」形态 → 默认保留"),
]

_BAND_CASES = [
    # (y0, y1, page_h, 期望, 说明)
    (13.77, 19.58, 595.275, True, "GRA-4118 页脚带"),
    (572.87, 578.87, 595.275, True, "另一侧页脚带（纵向翻转的同一位置）"),
    (200.0, 210.0, 595.275, False, "页面中部正文"),
    (0.0, 5.0, 0.0, False, "页高未知 → 不判边缘"),
    (0.0, 47.6, 595.275, True, "正好压在带边界上"),
]


def _self_test() -> int:
    bad = 0
    for text, want_ignore, why in _CASES:
        got = is_edge_noise(text)
        if got != want_ignore:
            bad += 1
            print(f"  [FAIL] {why}: {text!r} 期望={'忽略' if want_ignore else '保留'} "
                  f"实得={'忽略' if got else '保留'} ({classify(text)})")
    for y0, y1, h, want, why in _BAND_CASES:
        got = in_edge_band(y0, y1, h)
        if got != want:
            bad += 1
            print(f"  [FAIL] 位置带 {why}: y=({y0},{y1}) h={h} 期望={want} 实得={got}")
    # 顺序性：白名单必须先于噪声
    assert not is_edge_noise("Rev 1/2"), "白名单未生效"
    assert page_number_only(" 1/2") and not page_number_only("Rev 1/2")
    total = len(_CASES) + len(_BAND_CASES)
    # 输出口径与其它模块一致（回归脚本统一断言「0 项失败 in stdout」）
    print(f"页边行规则自检：{total} 项{'通过，0 项失败' if not bad else f'，{bad} 项失败'}")
    return bad


if __name__ == "__main__":
    raise SystemExit(_self_test())
