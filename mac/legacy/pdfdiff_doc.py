#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pdfdiff_doc.py —— Word 文档（.docx）直读比对。

用户 2026-10-02 的口径（原话）：「Word 本身就是一个很好的读取文件，没必要转 PDF，
转成 PDF 之后又要增加识别难度，还容易出错；转成 PDF 还要多产生文件。」
拍板时补了一句底线：「**只要不改变原文件就行，哪个准确率高选哪个**。」

为什么直读而不是转换
--------------------
docx 是 ZIP + 结构化 XML：正文段落、表格都是**现成的精确文本**——读出来
比扫描件 OCR 准得多（零认错字），全程内存进行、**不产生任何中间文件**、
原文件只读不写。转换成 PDF 反而引入排版引擎依赖 + 临时文件 + 渲染误差，
三条全踩用户的顾虑。

「页」去哪了（诚实的代价）
--------------------------
PDF 每页画死、字有坐标；docx 是流式格式，文件里**没有「页」**——页面是
Word 打开时才排出来的，同一份文件换个纸张设置页数都不同。所以 docx 链路
的清单没有页码列（「页码」列自动隐藏），预览窗是**文字流高亮**（左右并排、
同步滚动、红删/绿增/黄变动），不是纸面图。这是格式本身决定的，不是实现取舍。

比对粒度
--------
- 段落为一块；表格按「行」为一块（各格文本用 ` | ` 连接）——与 PDF 链路
  「逐行对拍」同一种粒度，清单/详情/导出全复用；
- 块对齐用 SequenceMatcher（**归一化**：小写 + 去所有空白，与 OCR 链路同款，
  空格多少不产生假差异）；对不上的段内先按相似度配对成「修改」，配不上的
  拆成删+增——阈值沿用 core 的 `modify_threshold`（0.55）；
- 「修改」条目在预览窗里只高亮**字符级变化片段**（复用 SequenceMatcher
  opcodes），比整行标黄更准。
"""

import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):            # 允许 `python -B pdfdiff_doc.py` 直跑
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdfdiff_core as core

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MODIFY_THR = 0.55        # 与 core.Options.modify_threshold 同值：≥ 算修改，< 拆删+增
DOC_EXT = ".docx"


def is_docx(path: str) -> bool:
    """按内容判断是不是可读的 .docx（zip 里必须有 word/document.xml）。

    旧版二进制 .doc 不是 zip，会在这里被识别出来并给出明确提示，
    而不是抛一个看不懂的 BadZipFile。
    """
    if not path.lower().endswith(DOC_EXT):
        return False
    try:
        with zipfile.ZipFile(path) as z:
            return "word/document.xml" in z.namelist()
    except Exception:
        return False


def _p_text(p) -> str:
    """一个 w:p 的纯文本：w:t 连接，w:tab / w:br 记一个空格。"""
    out: List[str] = []
    for node in p.iter():
        tag = node.tag
        if tag == W_NS + "t":
            out.append(node.text or "")
        elif tag in (W_NS + "tab", W_NS + "br", W_NS + "cr"):
            out.append(" ")
    return "".join(out)


def _tbl_rows(tbl) -> List[str]:
    """一张 w:tbl → 每行一块文本，各格用 ` | ` 连接（格内多段并成一行）。"""
    rows: List[str] = []
    for tr in tbl.findall(W_NS + "tr"):
        cells: List[str] = []
        for tc in tr.findall(W_NS + "tc"):
            cell = " ".join(_p_text(p) for p in tc.findall(W_NS + "p")).strip()
            cells.append(cell)
        if any(c.strip() for c in cells):
            rows.append(" | ".join(cells))
    return rows


def read_blocks(path: str) -> List[Tuple[str, str]]:
    """读出正文流：`[(块类型, 文本)…]`，类型 `p`=段落、`t`=表格行。

    ⚠️ 顺序就是文档顺序（段落与表格穿插时原样保留）；空段落跳过——
    空行不携带任何差异信息，混进来只会把相邻配对搅乱。
    只读，不写：原文件全程不被触碰。
    """
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W_NS + "body")
    if body is None:
        return []
    out: List[Tuple[str, str]] = []
    for el in body:
        if el.tag == W_NS + "p":
            t = _p_text(el).strip()
            if t:
                out.append(("p", t))
        elif el.tag == W_NS + "tbl":
            for row in _tbl_rows(el):
                out.append(("t", row))
    return out


def _norm(s: str) -> str:
    """块级归一化：小写 + 去所有空白（与 OCR 链路同口径，空白多寡不算差异）。"""
    return re.sub(r"\s+", "", (s or "").lower())


def _pair_run(old_blocks, new_blocks, oi0, oi1, ni0, ni1
              ) -> Tuple[List[Tuple[int, int, float]], List[int], List[int]]:
    """一段 replace 区间内：把旧块和新块按相似度贪心配对。

    返回 `(pairs, dels, adds)`：`pairs` 是 `(旧下标, 新下标, 相似度)`，
    `dels` / `adds` 是没配上的旧/新下标。贪心按**相似度从高到低**取配对，
    同分时先到先得——排序键里带上下标，跨进程顺序确定。
    """
    cands = []
    for i in range(oi0, oi1):
        for j in range(ni0, ni1):
            r = SequenceMatcher(None, _norm(old_blocks[i][1]),
                                _norm(new_blocks[j][1]), autojunk=False).ratio()
            if r >= MODIFY_THR:
                cands.append((-r, i, j))
    cands.sort()
    used_i, used_j, pairs = set(), set(), []
    for neg_r, i, j in cands:
        if i in used_i or j in used_j:
            continue
        used_i.add(i)
        used_j.add(j)
        pairs.append((i, j, -neg_r))
    dels = [i for i in range(oi0, oi1) if i not in used_i]
    adds = [j for j in range(ni0, ni1) if j not in used_j]
    return pairs, dels, adds


def _ctx(blocks, idx: int, n: int = 1) -> Tuple[str, str]:
    """前后文：紧邻的上一块 / 下一块原文（清单里「这段在哪儿」的锚点）。"""
    before = blocks[idx - 1][1] if idx - 1 >= 0 else ""
    after = blocks[idx + n][1] if idx + n < len(blocks) else ""
    return before, after


def compare_docx(old_path: str, new_path: str, prog=None) -> core.Result:
    """对比两份 .docx，返回与 PDF 链路同形的 Result（清单/详情/导出全复用）。

    ⚠️ 只读两条路径，任何情况下不写、不复制、不改名原文件。
    """
    t0 = time.time()
    res = core.Result(old_path=old_path, new_path=new_path)
    for p in (old_path, new_path):
        if not os.path.isfile(p):
            res.error = f"文件不存在：{p}"
            return res
        if not is_docx(p):
            if p.lower().endswith(".doc"):
                res.error = ("这是旧版 .doc 格式，请先用 Word 另存为 .docx 再比"
                             "（文件 → 另存为 → 选 .docx）")
            else:
                res.error = f"不是有效的 .docx 文件：{p}"
            return res
    try:
        blocks_old = read_blocks(old_path)
        blocks_new = read_blocks(new_path)
    except Exception as e:
        res.error = f"读取 Word 失败：{e}"
        return res
    res.pages_old, res.pages_new = len(blocks_old), len(blocks_new)   # 段数口径
    res.mode_used = "Word 直读"
    res.engine_line = f"docx 直读 · 精确文本比对（未用 OCR）· 旧版 {len(blocks_old)} 块 / 新版 {len(blocks_new)} 块"
    if prog:
        prog(1, 2)
    if not blocks_old and not blocks_new:
        res.notes = ["两份文档正文都是空的"]
        res.stats = core.summarize(res.items)
        return res

    texts_o = [_norm(t) for _, t in blocks_old]
    texts_n = [_norm(t) for _, t in blocks_new]
    sm = SequenceMatcher(None, texts_o, texts_n, autojunk=False)
    items: List[core.DiffItem] = []

    def add(kind: str, old: str, new: str, ctxb: str, ctxa: str,
            similarity: float = 0.0, context: str = "") -> None:
        it = core.DiffItem(id=len(items) + 1, kind=kind, scope="text",
                           old=old, new=new, context=context,
                           similarity=round(similarity, 3),
                           ctx_before=ctxb, ctx_after=ctxa)
        # docx 是 ZIP+XML，读出来的是**结构化原文**，不走 OCR —— 置信度最高一档。
        it.set_src("docx")
        items.append(it)

    for tag, oi0, oi1, ni0, ni1 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            pairs, dels, adds = _pair_run(blocks_old, blocks_new, oi0, oi1, ni0, ni1)
            for i, j, r in sorted(pairs):
                # 2026-10-13 删「变动」档：替换拆成删（旧值）+ 增（新值）两条。
                cb, ca = _ctx(blocks_old, i)
                add("removed", blocks_old[i][1], "", cb, ca, r,
                    context="表格行" if blocks_old[i][0] == "t" else "")
                cb2, ca2 = _ctx(blocks_new, j)
                add("added", "", blocks_new[j][1], cb2, ca2, r,
                    context="表格行" if blocks_new[j][0] == "t" else "")
            for i in dels:
                cb, ca = _ctx(blocks_old, i)
                add("removed", blocks_old[i][1], "", cb, ca,
                    context="表格行" if blocks_old[i][0] == "t" else "")
            for j in adds:
                cb, ca = _ctx(blocks_new, j)
                add("added", "", blocks_new[j][1], cb, ca,
                    context="表格行" if blocks_new[j][0] == "t" else "")
        elif tag == "delete":
            for i in range(oi0, oi1):
                cb, ca = _ctx(blocks_old, i)
                add("removed", blocks_old[i][1], "", cb, ca,
                    context="表格行" if blocks_old[i][0] == "t" else "")
        elif tag == "insert":
            for j in range(ni0, ni1):
                cb, ca = _ctx(blocks_new, j)
                add("added", "", blocks_new[j][1], cb, ca,
                    context="表格行" if blocks_new[j][0] == "t" else "")
        if prog:
            prog(min(2, oi1 + 1), 2)

    res.items = items
    res.stats = core.summarize(items)
    res.elapsed = time.time() - t0
    return res


# ================================================================== 自检
def _mk_docx(path: str, paras: List[str], table: Optional[List[List[str]]] = None) -> None:
    """造一份最小可读 docx（只有 word/document.xml，本模块自足读取）。"""
    body: List[str] = []
    for t in paras:
        body.append("<w:p><w:r><w:t xml:space=\"preserve\">%s</w:t></w:r></w:p>" % _esc(t))
    if table:
        trs = []
        for row in table:
            tcs = "".join(
                "<w:tc><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:tc>" % _esc(c)
                for c in row)
            trs.append("<w:tr>%s</w:tr>" % tcs)
        body.append("<w:tbl>%s</w:tbl>" % "".join(trs))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           "<w:body>%s</w:body></w:document>" % "".join(body))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", doc)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _self_test() -> int:
    ok = fail = 0
    import tempfile

    def chk(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("  [✗] %s  %s" % (name, detail))

    tmp = tempfile.mkdtemp(prefix="pdfdiff_doc_")
    p_a = os.path.join(tmp, "a.docx")
    p_b = os.path.join(tmp, "b.docx")

    # ---- 1) 完全相同 → 0 条 ----
    paras = ["产品技术要求", "本产品适用于成人雾化吸入治疗。", "贮存条件：常温避光。"]
    _mk_docx(p_a, paras)
    _mk_docx(p_b, paras)
    r = compare_docx(p_a, p_b)
    chk("相同文档 0 条差异", r.error == "" and len(r.items) == 0, r.error)
    chk("模式标记为 Word 直读", r.mode_used == "Word 直读")
    chk("块数口径", r.pages_old == 3 and r.pages_new == 3)

    # ---- 2) 改一句话 → 拆成删+增两条，old/new 各归其位 ----
    _mk_docx(p_b, ["产品技术要求", "本产品适用于儿童雾化吸入治疗。", "贮存条件：常温避光。"])
    r = compare_docx(p_a, p_b)
    chk("单句修改拆成删+增两条", len(r.items) == 2, str(len(r.items)))
    if r.items:
        kinds = {it.kind for it in r.items}
        chk("类型 = 删 + 增", kinds == {"removed", "added"}, str(kinds))
        rm = next(it for it in r.items if it.kind == "removed")
        ad = next(it for it in r.items if it.kind == "added")
        chk("old 是旧文本", "成人" in rm.old and "儿童" not in rm.old, rm.old[:30])
        chk("new 是新文本", "儿童" in ad.new, ad.new[:30])
        chk("相似度 > 0.5", rm.similarity >= MODIFY_THR, str(rm.similarity))
    chk("删 1 + 增 1 统计", r.stats.get("removed") == 1 and r.stats.get("added") == 1
        and r.stats.get("total") == 2)

    # ---- 3) 新增 / 删除段落 ----
    _mk_docx(p_b, paras + ["新增的注意事项一段。"])
    r = compare_docx(p_a, p_b)
    chk("文末新增 → 1 条 added", len(r.items) == 1 and r.items[0].kind == "added",
        str([(i.kind, i.new[:12]) for i in r.items]))
    chk("新增条带前后文", r.items and "贮存条件" in r.items[0].ctx_before)

    _mk_docx(p_b, paras[:2])
    r = compare_docx(p_a, p_b)
    chk("文末删除 → 1 条 removed", len(r.items) == 1 and r.items[0].kind == "removed",
        str([(i.kind, i.old[:12]) for i in r.items]))

    # ---- 4) 表格：改一格 → 拆成删+增；前后文里能看到行文本 ----
    tbl_a = [["型号", "长度", "直径"], ["A35", "150", "5.0"]]
    tbl_b = [["型号", "长度", "直径"], ["A35", "200", "5.0"]]
    _mk_docx(p_a, ["规格表如下："], table=tbl_a)
    _mk_docx(p_b, ["规格表如下："], table=tbl_b)
    r = compare_docx(p_a, p_b)
    chk("表格改格拆成删+增", len(r.items) == 2
        and {i.kind for i in r.items} == {"removed", "added"},
        str([(i.kind, i.old, i.new) for i in r.items]))
    if r.items:
        rm = next(i for i in r.items if i.kind == "removed")
        chk("表格行文本用 | 连接", "A35" in rm.old and " | " in rm.old,
            rm.old[:40])
        chk("标记为表格行", rm.context == "表格行", rm.context)

    # ---- 5) 空段落不产生差异、不掺和配对 ----
    _mk_docx(p_a, ["第一段。", "", "　", "第二段。"])
    _mk_docx(p_b, ["第一段。", "第二段。"])
    r = compare_docx(p_a, p_b)
    chk("空段落被跳过", len(r.items) == 0, str(len(r.items)))

    # ---- 6) .doc / 非 docx 的明确报错 ----
    p_doc = os.path.join(tmp, "old.doc")
    with open(p_doc, "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0not-a-zip")
    r = compare_docx(p_doc, p_doc)
    chk(".doc 给出另存提示", ".docx" in r.error and r.error != "", r.error[:60])
    r = compare_docx(p_a, os.path.join(tmp, "nope.docx"))
    chk("缺文件报错", "不存在" in r.error, r.error[:50])

    # ---- 7) 一段话同时有增删改 → 全部拆成删/增 ----
    _mk_docx(p_a, ["条款一：产品应符合 YY/T 标准。", "条款二：包装应密封。", "独有旧段落内容。"])
    _mk_docx(p_b, ["条款一：产品应符合 GB/T 标准。", "条款二：包装应密封并加贴标签。", "全新增段落内容。"])
    r = compare_docx(p_a, p_b)
    kinds = sorted(i.kind for i in r.items)
    chk("混合改动至少 3 条", len(r.items) >= 3, str(kinds))
    chk("不再有修改条目（只剩删/增）", set(kinds) <= {"removed", "added"}, str(kinds))

    # ---- 8) 确定性：同输入两次结果一致（含顺序）----
    r1 = compare_docx(p_a, p_b)
    r2 = compare_docx(p_a, p_b)
    chk("两次比对结果一致",
        [(i.kind, i.old, i.new) for i in r1.items] == [(i.kind, i.old, i.new) for i in r2.items])

    # ---- 9) 铁律：原文件一个字节都不动 ----
    raw_a = open(p_a, "rb").read()
    raw_b = open(p_b, "rb").read()
    compare_docx(p_a, p_b)
    chk("原文件未被修改", open(p_a, "rb").read() == raw_a
        and open(p_b, "rb").read() == raw_b)

    # ---- 10) 与 grp / 详情面板的字段契约 ----
    _mk_docx(p_a, ["甲段内容。", "乙段内容。"])
    _mk_docx(p_b, ["甲段内容。", "乙段改过的内容。"])
    r = compare_docx(p_a, p_b)
    try:
        import pdfdiff_group as grp
        paras_g = grp.group_paragraphs(r.items)
        chk("可被 grp 分段", len(paras_g) == 1)
        p0 = grp.Para.from_item(r.items[0])
        # 2026-10-13 删「变动」档：改写拆成删+增，单条只有一侧文本，
        # 整段（删+增两条）才同时有 old_text / new_text。
        chk("可转 Para（详情面板契约）",
            bool(paras_g[0].old_text) and bool(paras_g[0].new_text))
    except Exception as e:
        chk("grp 契约", False, repr(e))

    print(f"Word 直读模块自检：{ok} 项通过，{fail} 项失败")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    _self_test()
