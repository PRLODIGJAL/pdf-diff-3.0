# -*- coding: utf-8 -*-
"""
pdfdiff3.py —— PDF 差异对比 3.0：像素级差异引擎（推倒重来版）
==================================================================

为什么重写（2026-10-13）：
    2.0 的字符流 + OCR 引擎（pdfdiff_locate 6804 行 / pdfdiff_core 3349 行）
    为了「精确到字」堆了几十层「防护 / 止损 / 兜底 / 撤销」，层层嵌套互相打架，
    越改越错、越改越卡（rev15 转曲线 PDF 卡死 300s）。用户拍板：

    > 「反正变动明细也不一定全对，也没人专门去看，更多还是看图片。
    >    只要开始对比，就直接展示两份 PDF 的差异标注。」

    所以 3.0 放弃「文字清单」这条路线，只做一件事：

        **把两页渲染成位图 → 逐像素比对 → 找出不同区块 → 红绿框出来。**

思路（简单、直接、不装准）：
  1. 逐页对齐（页数不同按索引配对，多出的页整页判增删）。
  2. 每页渲染成**同一 dpi 的灰度位图**（转 PDF 坐标 → 像素坐标可逆）。
  3. `diff = |old - new|`，超过亮度阈值的像素 = 「这里不一样」。
  4. 形态学膨胀把碎点连成块，再做连通域聚类 → 一个个差异区块。
  5. 每个区块看「旧版有墨 / 新版有墨」：
       · 旧版有、新版空 → 删减（红，标在旧版）
       · 旧版空、新版有 → 新增（绿，标在新版）
       · 两边都有（改了内容 / 换了字）→ 两侧同框（旧红新绿）
  6. 位移（浅蓝）与字体（深蓝）：像素级引擎原理上分不出来，这里**尽力而为**——
     位移用「相似墨迹在两版的位置偏移」粗略估，字体靠文字层辅助，做不出就宁缺毋滥。

依赖：numpy（唯一新增）；pymupdf（fitz，原有）。**不依赖 pdfdiff_core / pdfdiff_locate**。

输出：兼容 `diff_preview.open_preview()` 的 marks 结构（复用现成的并排渲染界面），
      `{"old": {...}, "new": {...}, "diffs": [...]}`，`by_kind` 的 key 用
      `pdf_annotate` 的 `KIND_DEL/KIND_ADD/KIND_MOV/KIND_FONT` 常量。
"""

from __future__ import annotations

import difflib
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

# ==================================================================
# 可调参数（集中在这，改起来不迷路）
# ==================================================================
RENDER_DPI = 120          # 渲染分辨率：够定位差异、又不至于让大图太慢
DIFF_THRESH = 24          # 灰度差超过它算「不一样」（0~255，越高越保守）
DILATE_RADIUS = 2         # 膨胀半径（像素）：把碎点连成块
MIN_BLOCK_AREA = 120      # 小于这么多像素的差异块忽略（去噪，扫去 1~2px 抖动）
INK_THRESH = 200          # 亮度低于它算「有墨」（白纸=255，黑字=0）
DENSITY_GRID = 8          # 密度过滤的网格大小（像素）：真改动致密、噪声稀疏
DENSITY_MIN = 0.18        # 8x8 网格内差异像素占比低于它 → 视为重栅格化噪声丢弃
FULLPAGE_COVER = 0.35     # 差异块覆盖页面超过它 → 判「整页重绘」，降级为整页框（不逐块标）
MERGE_GAP = 14            # 差异块合并间距（像素）：间距小于它的块并成一个（CAD 重排区
                          # 几十个碎框并成几大块，看图的人一眼就知道「这块重写了」）

# 页面对齐：页数不同时，这一页算「整页增/删」的阈值由调用方决定；这里给默认。


def _gray(page, dpi: int) -> np.ndarray:
    """把一页渲染成灰度 numpy 数组（0~255，白底，二维 (h, w)）。"""
    pm = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=False)
    n = pm.n
    arr = np.frombuffer(pm.samples, dtype=np.uint8)
    if n == 1:
        arr = arr.reshape(pm.height, pm.width)
    else:
        arr = arr.reshape(pm.height, pm.width, n)[:, :, 0]
    return arr


def _resize_to(a: np.ndarray, shape: Tuple[int, int]) -> np.ndarray:
    """把位图缩放到目标 (h, w)，用最近邻（避免插值引入半灰造成假差异）。"""
    h, w = shape
    if a.shape == (h, w):
        return a
    # 最近邻采样
    yy = (np.linspace(0, a.shape[0] - 1, h)).astype(int)
    xx = (np.linspace(0, a.shape[1] - 1, w)).astype(int)
    return a[np.ix_(yy, xx)]


def _density_filter(mask: np.ndarray) -> np.ndarray:
    """密度过滤：把「细碎边缘噪声」从掩码里剔掉，只留「致密的真差异」。

    重栅格化（同一张图被不同分辨率重新采样）会让所有笔画边缘产生 1~2px 的
    细碎差异——它们铺满整页但**每个 8x8 网格里差异像素占比很低**（边缘线）。
    真改动的字/块是**致密**的（网格里差异占比高）。用 DENSITY_GRID 网格统计
    占比，低于 DENSITY_MIN 的网格整格清零。
    """
    if not mask.any():
        return mask
    g = DENSITY_GRID
    H, W = mask.shape
    # 把图像 pad 到网格整数倍
    ph = (g - H % g) % g
    pw = (g - W % g) % g
    m = np.pad(mask, ((0, ph), (0, pw)))
    gh, gw = m.shape[0] // g, m.shape[1] // g
    blocks = m.reshape(gh, g, gw, g).transpose(0, 2, 1, 3).reshape(gh, gw, g * g)
    density = blocks.mean(axis=2)          # 每个网格的差异占比
    keep = (density >= DENSITY_MIN).repeat(g, axis=0).repeat(g, axis=1)
    return keep[:H, :W] & mask


def _connected_components(mask: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """对二值掩码做 4-连通域聚类，返回每个块的 (y0, x0, y1, x1) 包围盒（含边）。"""
    H, W = mask.shape
    labels = np.zeros((H, W), dtype=np.int32)
    boxes: List[Tuple[int, int, int, int]] = []
    cur = 0
    # 迭代式 BFS（不用递归，防栈溢出）
    from collections import deque
    for y in range(H):
        for x in range(W):
            if mask[y, x] and labels[y, x] == 0:
                cur += 1
                q = deque([(y, x)])
                labels[y, x] = cur
                miny = maxy = y
                minx = maxx = x
                while q:
                    cy, cx = q.popleft()
                    if cy < miny:
                        miny = cy
                    if cy > maxy:
                        maxy = cy
                    if cx < minx:
                        minx = cx
                    if cx > maxx:
                        maxx = cx
                    for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                        if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and labels[ny, nx] == 0:
                            labels[ny, nx] = cur
                            q.append((ny, nx))
                boxes.append((miny, minx, maxy, maxx))
    return boxes


def _merge_boxes(boxes: List[Tuple[int, int, int, int]],
                 gap: int = MERGE_GAP) -> List[Tuple[int, int, int, int]]:
    """把间距 ≤ gap 的差异块并成一个大块（迭代到不动点）。

    CAD 重排区的差异是几十个碎框（每个字一个），不合并的话预览里糊成
    一堆小框根本看不清；并成几大块后「哪一块重写了」一眼可见。
    """
    if not boxes:
        return []
    bs = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        out: List[List[int]] = []
        for b in bs:
            merged = False
            for o in out:
                # 两块的外接范围（含 gap 膨胀）相交 → 并
                if (b[0] - gap <= o[2] and b[2] + gap >= o[0]
                        and b[1] - gap <= o[3] and b[3] + gap >= o[1]):
                    o[0] = min(o[0], b[0]); o[1] = min(o[1], b[1])
                    o[2] = max(o[2], b[2]); o[3] = max(o[3], b[3])
                    merged = True
                    changed = True
                    break
            if not merged:
                out.append(list(b))
        bs = out
    return [tuple(b) for b in bs]


def _px_to_rect(box: Tuple[int, int, int, int], page, dpi: int) -> fitz.Rect:
    """像素包围盒 → 页面坐标 Rect（pt）。渲染 dpi 固定，比例可逆。"""
    y0, x0, y1, x1 = box
    s = 72.0 / dpi
    # get_pixmap 的像素 y 向下、页面坐标 y 也向下（fitz 左上原点），直接乘比例。
    return fitz.Rect(x0 * s, y0 * s, (x1 + 1) * s, (y1 + 1) * s)


def _page_diff_blocks(old_arr: np.ndarray, new_arr: np.ndarray
                      ):
    """对两页位图做像素比对。

    返回：
      · `("FULLPAGE", [], [], [])` —— 差异像素铺满整页（位图整体重绘），调用方
        应降级为整页框，不要逐块标；
      · 否则 `(删减块, 新增块, 改写块)` 三组像素包围盒。
    """
    # 对齐尺寸（不同扫描件差一两像素）
    h = max(old_arr.shape[0], new_arr.shape[0])
    w = max(old_arr.shape[1], new_arr.shape[1])
    oa = _resize_to(old_arr, (h, w))
    na = _resize_to(new_arr, (h, w))

    # 差异掩码：亮度差 > 阈值
    diff = np.abs(oa.astype(np.int16) - na.astype(np.int16)) > DIFF_THRESH

    # 有墨掩码（旧 / 新）
    ink_o = oa < INK_THRESH
    ink_n = na < INK_THRESH

    # 删减：旧有墨、新无墨，且该处有差异
    del_mask = diff & ink_o & (~ink_n)
    # 新增：旧无墨、新有墨，且有差异
    add_mask = diff & (~ink_o) & ink_n
    # 改写：两边都有墨，但内容不同
    mod_mask = diff & ink_o & ink_n

    # 形态学膨胀（把碎点连成块）
    def _dilate(m: np.ndarray) -> np.ndarray:
        r = DILATE_RADIUS
        if r <= 0 or not m.any():
            return m
        out = m.copy()
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dy == 0 and dx == 0:
                    continue
                out |= np.roll(np.roll(m, dy, axis=0), dx, axis=1)
        return out

    del_d = _dilate(del_mask)
    add_d = _dilate(add_mask)
    mod_d = _dilate(mod_mask)

    # 密度过滤（在膨胀后做：膨胀已把碎点连起来，此时按密度剔边缘噪声最有效）
    del_d = _density_filter(del_d)
    add_d = _density_filter(add_d)
    mod_d = _density_filter(mod_d)

    # 连通域 + 面积过滤
    def _boxes(m: np.ndarray) -> List[Tuple[int, int, int, int]]:
        out = []
        for b in _connected_components(m):
            y0, x0, y1, x1 = b
            if (y1 - y0 + 1) * (x1 - x0 + 1) >= MIN_BLOCK_AREA:
                out.append(b)
        return out

    del_b, add_b, mod_b = _boxes(del_d), _boxes(add_d), _boxes(mod_d)

    # —— 整页重绘检测（按**差异块覆盖面积**判，降级出口）——
    # CAD 标签类文档：图面是一整张嵌入位图，改版 = 位图整体重新导出，
    # 每个字的抗锯齿边缘都重排 → 差异块连成一片、铺满页面（原始差异像素占比
    # 只有 ~14%，但**块外接矩形覆盖了页面的大半**）。这时逐块标注必然
    # 「糊成一片」，语义上真正的改动（几个字段）被淹没 → 调用方降级为整页框。
    H, W = diff.shape

    def _coverage(boxes) -> float:
        cov = np.zeros((H // 8 + 1, W // 8 + 1), dtype=bool)   # 8px 粒度够用
        for y0, x0, y1, x1 in boxes:
            cov[y0 // 8:(y1 + 1) // 8 + 1, x0 // 8:(x1 + 1) // 8 + 1] = True
        return cov.mean()

    if _coverage(del_b + add_b + mod_b) > FULLPAGE_COVER:
        return "FULLPAGE", [], [], []
    # 相邻碎块合并（CAD 重排区几十个碎框 → 几大块）
    return _merge_boxes(del_b), _merge_boxes(add_b), _merge_boxes(mod_b)


# ==================================================================
# 文字级对比（两侧都有文字层的页走这里，精确到行）
# ==================================================================
_TEXT_NORM = re.compile(r"\s+")


def _page_lines(page) -> List[Tuple[str, fitz.Rect]]:
    """把一页的文字层抽成行：(归一化文本, 行包围盒)，按阅读顺序排好。

    用 `get_text("words")` 按 (block, line) 分组——比 get_text("lines") 稳，
    坐标取行内所有词的外接矩形。
    """
    lines: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for x0, y0, x1, y1, w, b, l, _wn in page.get_text("words"):
        d = lines.setdefault((b, l), {"ws": [], "x0": x0, "y0": y0,
                                      "x1": x1, "y1": y1})
        d["ws"].append((x0, w))
        d["x0"] = min(d["x0"], x0)
        d["y0"] = min(d["y0"], y0)
        d["x1"] = max(d["x1"], x1)
        d["y1"] = max(d["y1"], y1)
    out: List[Tuple[str, fitz.Rect]] = []
    for d in lines.values():
        ws = [w for _, w in sorted(d["ws"], key=lambda t: t[0])]
        text = "".join(ws)                    # 中文字间无空格
        if not text:
            continue
        text = _TEXT_NORM.sub("", text)       # 去全部空白再比，避免排版抖动
        out.append((text, fitz.Rect(d["x0"], d["y0"], d["x1"], d["y1"])))
    out.sort(key=lambda t: (round(t[1].y0, 1), t[1].x0))
    return out


def _text_diff_page(po, pn, min_lines: int = 4
                    ) -> Optional[Tuple[List[fitz.Rect], List[fitz.Rect],
                                        List[fitz.Rect]]]:
    """两侧都有文字层的页：逐行对齐（difflib）。

    返回 `(删行矩形, 增行矩形, 文字区矩形全集)` —— 文字区全集给调用方
    用来过滤像素块（文字区内的像素差异是重排噪声，归文字引擎管；
    文字区外的是图形/位图差异，像素引擎管）。
    任一侧文字太少（< min_lines 行，比如整页标签位图）→ 返回 None，
    调用方回退像素管线。文字行匹配是**精确**的：11vs12 那种「整段重排」
    只会标出真正不同的行，而不是像像素级那样整页糊红绿。
    """
    lo = _page_lines(po)
    ln = _page_lines(pn)
    if len(lo) < min_lines or len(ln) < min_lines:
        return None
    zone = [r for _, r in lo] + [r for _, r in ln]
    sm = difflib.SequenceMatcher(a=[t for t, _ in lo],
                                 b=[t for t, _ in ln], autojunk=False)
    dels: List[fitz.Rect] = []
    adds: List[fitz.Rect] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            dels.extend(r for _, r in lo[i1:i2])
        if tag in ("insert", "replace"):
            adds.extend(r for _, r in ln[j1:j2])
    return dels, adds, zone


def _pad(r: fitz.Rect, pad: float = 1.5) -> fitz.Rect:
    """行矩形外扩一点点，标注别勒着字。"""
    return fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad)


def _outside_zone(boxes, zone: List[fitz.Rect]
                  ) -> List[Tuple[int, int, int, int]]:
    """像素块过滤：与文字区重叠超过 30% 的块丢弃（那是文字重排的噪声，
    归文字引擎管），剩下的才是真正的图形/位图差异。"""
    out = []
    for b in boxes:
        y0, x0, y1, x1 = b
        r = fitz.Rect(x0 * 72.0 / RENDER_DPI, y0 * 72.0 / RENDER_DPI,
                      (x1 + 1) * 72.0 / RENDER_DPI, (y1 + 1) * 72.0 / RENDER_DPI)
        area = max(1.0, r.width * r.height)
        hit = False
        for z in zone:
            iw = min(r.x1, z.x1) - max(r.x0, z.x0)
            ih = min(r.y1, z.y1) - max(r.y0, z.y0)
            if iw > 0 and ih > 0 and (iw * ih) / area > 0.3:
                hit = True
                break
        if not hit:
            out.append(b)
    return out


def compare_pixels(old_path: str, new_path: str, prog=None) -> Dict[str, Any]:
    """像素级对比两份 PDF，返回兼容 `diff_preview.open_preview` 的 marks 结构。

    prog(当前页, 总页数) 供进度条（可选）。
    """
    do = fitz.open(old_path)
    dn = fitz.open(new_path)
    try:
        no, nn = do.page_count, dn.page_count
        pages = max(no, nn)
        old_marks: Dict[int, List[fitz.Rect]] = {}
        new_marks: Dict[int, List[fitz.Rect]] = {}
        old_by_kind: Dict[str, Dict[int, List[fitz.Rect]]] = {}
        new_by_kind: Dict[str, Dict[int, List[fitz.Rect]]] = {}
        old_boxes: Dict[str, List[int]] = {}      # kind → 页号（画页面边框）
        new_boxes: Dict[str, List[int]] = {}
        old_full_marks: Dict[int, List[fitz.Rect]] = {}   # 整页重绘的边框矩形
        new_full_marks: Dict[int, List[fitz.Rect]] = {}
        diffs: List[Dict[str, Any]] = []
        fullpages: List[int] = []

        for pi in range(pages):
            if prog:
                prog(pi, pages)
            if pi >= no or pi >= nn:
                # 一侧缺页 → 整页增/删（画页面边框，不涂满高亮）
                side = "old" if pi < no else "new"
                pg = (do if pi < no else dn)[pi]
                r = pg.rect
                box = fitz.Rect(r.x0 + 12, r.y0 + 12, r.x1 - 12, r.y1 - 12)
                kind = "del" if side == "old" else "add"
                if side == "old":
                    old_boxes.setdefault(kind, []).append(pi)
                else:
                    new_boxes.setdefault(kind, []).append(pi)
                diffs.append({"page": pi, "kind": kind,
                              "old_text": "", "new_text": "",
                              "note": f"第{pi+1}页只在{('旧版' if side=='old' else '新版')}存在"})
                continue

            po, pn = do[pi], dn[pi]
            # —— 先走文字级（两侧都有文字层时，精确到行，这是主路径）——
            tdiff = _text_diff_page(po, pn)
            if tdiff is not None:
                dels, adds, zone = tdiff
                for r in dels:
                    rr = _pad(r)
                    old_marks.setdefault(pi, []).append(rr)
                    old_by_kind.setdefault("del", {}).setdefault(pi, []).append(rr)
                for r in adds:
                    rr = _pad(r)
                    new_marks.setdefault(pi, []).append(rr)
                    new_by_kind.setdefault("add", {}).setdefault(pi, []).append(rr)
                # 图形/位图差异：文字页也跑像素，但只留「文字区以外」的块
                # （位图重绘、图片更换这类）。文字区内的像素噪声全部丢弃。
                pres = _page_diff_blocks(_gray(po, RENDER_DPI),
                                         _gray(pn, RENDER_DPI))
                ng = 0
                if pres[0] == "FULLPAGE":
                    # 位图占了大半页 → 画整页边框提示，文字标注照常保留
                    r = po.rect
                    box = fitz.Rect(r.x0 + 12, r.y0 + 12, r.x1 - 12, r.y1 - 12)
                    old_boxes.setdefault("del", []).append(pi)
                    new_boxes.setdefault("add", []).append(pi)
                    old_full_marks.setdefault(pi, []).append(box)
                    new_full_marks.setdefault(pi, []).append(box)
                    fullpages.append(pi)
                else:
                    for b in _outside_zone(pres[0], zone):
                        r = _px_to_rect(b, po, RENDER_DPI)
                        old_marks.setdefault(pi, []).append(r)
                        old_by_kind.setdefault("del", {}).setdefault(pi, []).append(r)
                        ng += 1
                    for b in _outside_zone(pres[1], zone):
                        r = _px_to_rect(b, pn, RENDER_DPI)
                        new_marks.setdefault(pi, []).append(r)
                        new_by_kind.setdefault("add", {}).setdefault(pi, []).append(r)
                        ng += 1
                if dels or adds or ng:
                    diffs.append({"page": pi, "del": len(dels),
                                  "add": len(adds), "graphic": ng,
                                  "mode": "text"})
                continue

            # —— 像素级（扫描件 / 图片页 / 一侧无文字层）——
            oa = _gray(po, RENDER_DPI)
            na = _gray(pn, RENDER_DPI)
            res = _page_diff_blocks(oa, na)
            if res[0] == "FULLPAGE":
                # 整页重绘（CAD 标签位图整体重新导出）：降级为两侧各画一个
                # 「整页重绘」边框，不逐块标 —— 逐块标必然糊成一片（详见
                # `_page_diff_blocks` 的注释）。记录进 diffs 供 JSON 导出。
                r = po.rect
                box = fitz.Rect(r.x0 + 12, r.y0 + 12, r.x1 - 12, r.y1 - 12)
                old_boxes.setdefault("del", []).append(pi)
                new_boxes.setdefault("add", []).append(pi)
                old_full_marks.setdefault(pi, []).append(box)
                new_full_marks.setdefault(pi, []).append(box)
                fullpages.append(pi)
                diffs.append({"page": pi, "kind": "fullpage",
                              "old_text": "", "new_text": "",
                              "note": f"第{pi+1}页整页重绘（位图整体重新导出），"
                                      f"具体改动需人工比对"})
                continue
            dels, adds, mods = res

            # 删减 → 旧版标红
            for b in dels:
                r = _px_to_rect(b, po, RENDER_DPI)
                old_marks.setdefault(pi, []).append(r)
                old_by_kind.setdefault("del", {}).setdefault(pi, []).append(r)
            # 新增 → 新版标绿
            for b in adds:
                r = _px_to_rect(b, pn, RENDER_DPI)
                new_marks.setdefault(pi, []).append(r)
                new_by_kind.setdefault("add", {}).setdefault(pi, []).append(r)
            # 改写 → 两侧同框（旧红新绿）
            for b in mods:
                ro = _px_to_rect(b, po, RENDER_DPI)
                rn = _px_to_rect(b, pn, RENDER_DPI)
                old_marks.setdefault(pi, []).append(ro)
                new_marks.setdefault(pi, []).append(rn)
                old_by_kind.setdefault("del", {}).setdefault(pi, []).append(ro)
                new_by_kind.setdefault("add", {}).setdefault(pi, []).append(rn)

            if dels or adds or mods:
                diffs.append({
                    "page": pi,
                    "del": len(dels), "add": len(adds), "mod": len(mods),
                })

        def _side(doc, marks, by_kind, boxes, full_marks, path):
            return {
                "marks": {pi: [r for r in rs] for pi, rs in marks.items()},
                "by_kind": {k: {pi: [r for r in rs] for pi, rs in v.items()}
                            for k, v in by_kind.items()},
                "boxes": [pi for v in boxes.values() for pi in v],
                "boxes_by_kind": {k: list(v) for k, v in boxes.items()},
                # 整页重绘页：边框矩形单独放（预览里在图上画个明显的整页框）
                "full_marks": {pi: [r for r in rs] for pi, rs in full_marks.items()},
                "stats": {"marked": sum(len(v) for v in marks.values())
                          + len(full_marks), "items": 0,
                          "missed": 0, "graphic": 0, "blocks": 0},
                "pages": doc.page_count, "path": path,
            }

        return {
            "old": _side(do, old_marks, old_by_kind, old_boxes,
                         old_full_marks, old_path),
            "new": _side(dn, new_marks, new_by_kind, new_boxes,
                         new_full_marks, new_path),
            "diffs": diffs,
            "graphic": len(diffs),
            "fullpages": fullpages,
        }
    finally:
        do.close()
        dn.close()


def self_test() -> Tuple[int, int]:
    """引擎自检：用两张合成位图验证删/增/改三类判定。"""
    ok = fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        print(f"  {'✓' if cond else '✗'} {name}")
        if cond:
            ok += 1
        else:
            fail += 1

    # 白底黑块：旧版左上有一块、新版没有 → 删
    old_a = np.full((100, 100), 255, dtype=np.uint8)
    new_a = np.full((100, 100), 255, dtype=np.uint8)
    old_a[20:40, 20:40] = 0
    d, a, m = _page_diff_blocks(old_a, new_a)
    chk("删减块：旧有墨新无墨 → 1 块", len(d) == 1 and len(a) == 0 and len(m) == 0)

    # 新版加一块 → 增
    old_a = np.full((100, 100), 255, dtype=np.uint8)
    new_a = np.full((100, 100), 255, dtype=np.uint8)
    new_a[60:80, 60:80] = 0
    d, a, m = _page_diff_blocks(old_a, new_a)
    chk("新增块：旧无墨新有墨 → 1 块", len(a) == 1 and len(d) == 0 and len(m) == 0)

    # 同位置「改字」：旧版一竖、新版一横，重叠区两边都黑（不算差异），
    # 不重叠的部分旧有墨（删）+ 新有墨（增）—— 像素级下「改写」天然拆成删+增，
    # 这正是用户要的「旧的删掉标红、新的加上标绿」。
    old_a = np.full((100, 100), 255, dtype=np.uint8)
    new_a = np.full((100, 100), 255, dtype=np.uint8)
    old_a[10:40, 10:20] = 0      # 旧版竖条
    new_a[10:20, 10:40] = 0      # 新版横条
    d, a, m = _page_diff_blocks(old_a, new_a)
    chk("改写拆成删+增：旧笔画标删、新笔画标增", len(d) >= 1 and len(a) >= 1)

    # 纯改写（同位置都黑、形状不同但完全重叠）：两团墨重叠区外各有差异。
    # 这是「字变了但位置没变」的典型——像素级下同样拆成删+增，不单独出 mod。
    old_a = np.full((100, 100), 255, dtype=np.uint8)
    new_a = np.full((100, 100), 255, dtype=np.uint8)
    old_a[20:40, 20:40] = 0
    new_a[20:40, 20:40] = 0
    new_a[25:35, 25:35] = 255    # 新版中间挖个洞 → 洞的区域旧有墨新无墨 = 删
    d, a, m = _page_diff_blocks(old_a, new_a)
    chk("同位置改字：中间挖洞 → 判删", len(d) >= 1)

    # 完全相同 → 零差异
    old_a = np.full((100, 100), 255, dtype=np.uint8)
    new_a = old_a.copy()
    d, a, m = _page_diff_blocks(old_a, new_a)
    chk("完全一致 → 零差异", len(d) == 0 and len(a) == 0 and len(m) == 0)

    print(f"\n像素引擎自检：{ok} 项通过，{fail} 项失败")
    return ok, fail


if __name__ == "__main__":
    raise SystemExit(1 if self_test()[1] else 0)
