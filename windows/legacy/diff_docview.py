#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""diff_docview.py —— Word 文档的软件内预览窗（文字流高亮视图）。

与 PDF 预览窗（`diff_preview.py`）的关系
----------------------------------------
同一套交互习惯、不同的载体：PDF 预览渲染的是「纸面图」（docx 里没有页，
渲染不出纸面），这里渲染的是**文字流**——左右并排、同步滚动、
删减红 / 新增绿（2026-10-13 删「变动」档，改写已拆成删+增两条）。

铁律（与 PDF 预览同款）
------------------------
- **只读原文件**：文本在打开时一次性读进内存，之后不再碰磁盘；
  不复制、不改写、不留任何伴生产物。
- 预览窗**只留一个**（由 diff_app 负责关旧开新）。
"""

import os
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pdfdiff_doc as doc

FONT = ("PingFang SC", 12) if sys.platform == "darwin" else ("Microsoft YaHei", 11)
FONT = None    # 跟随主题默认，避免跨平台字体名打炮

# 红删 / 绿增，配浅底深字——预览是文字不是纸
C_RED = {"bg": "#ffd9d6", "fg": "#8a1a12"}
C_GREEN = {"bg": "#d6f5dd", "fg": "#0d5c27"}
C_PAIR = {"bg": "#eef1f5"}          # 选中改动处的行底

KIND_ORDER = ("removed", "added")
KIND_CN = {"removed": "删减", "added": "新增"}


class DocPreview(tk.Toplevel):
    """左右并排的文字流预览。`items` 必须是 `pdfdiff_doc.compare_docx` 的产物。"""

    def __init__(self, master, old_path: str, new_path: str, items: List[Any]):
        super().__init__(master)
        self.title("Word 预览 —— %s ↔ %s" % (os.path.basename(old_path),
                                             os.path.basename(new_path)))
        self.geometry("1180x820")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._syncing = False
        self._nav: List[Dict[str, Any]] = []       # [{o:(i0,i1), n:(i0,i1), kind}]
        self._cur = 0

        blocks_o = doc.read_blocks(old_path)
        blocks_n = doc.read_blocks(new_path)

        top = tk.Frame(self)
        top.pack(fill="x", padx=8, pady=(6, 2))
        cnt = {k: 0 for k in KIND_ORDER}
        for it in items:
            if it.kind in cnt:
                cnt[it.kind] += 1
        tk.Label(top, text="共 %d 处改动：删减 %d · 新增 %d"
                 % (len(items), cnt["removed"], cnt["added"]),
                 font=(FONT or "TkDefaultFont", 10, "bold")).pack(side=tk.LEFT)
        self.nav_var = tk.StringVar(value="—")
        tk.Button(top, text="◀ 上一处", width=9, command=lambda: self.goto(-1)).pack(side=tk.RIGHT, padx=3)
        tk.Button(top, text="下一处 ▶", width=9, command=lambda: self.goto(1)).pack(side=tk.RIGHT, padx=3)
        tk.Label(top, textvariable=self.nav_var, font=(FONT or "TkDefaultFont", 10)).pack(side=tk.RIGHT, padx=6)

        pane = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        pane.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        self.txt: Dict[str, tk.Text] = {}
        for side, blocks, path in (("old", blocks_o, old_path), ("new", blocks_n, new_path)):
            frm = tk.Frame(pane)
            head = tk.Label(frm, text=("旧版　" if side == "old" else "新版　") + os.path.basename(path),
                            anchor="w", font=(FONT or "TkDefaultFont", 10, "bold"))
            head.pack(fill="x")
            wrap = tk.Frame(frm)
            wrap.pack(fill="both", expand=True)
            txt = tk.Text(wrap, wrap="word", width=60, height=30, borderwidth=0,
                          font=FONT, padx=10, pady=8, cursor="arrow",
                          takefocus=0, state="disabled")
            sb = ttk.Scrollbar(wrap, orient="vertical", command=self._bar_scroll(side))
            txt.configure(yscrollcommand=lambda *a, s=sb, sd=side: self._from_bar(sd, s, *a))
            sb.pack(side="right", fill="y")
            txt.pack(side="left", fill="both", expand=True)
            for k, c in (("hl_red", C_RED), ("hl_green", C_GREEN),
                         ("pair", C_PAIR)):
                txt.tag_configure(k, background=c["bg"],
                                  **({"foreground": c["fg"]} if "fg" in c else {}))
            txt.tag_configure("hit", background="#c9dff5")     # 导航定位条
            txt.bind("<MouseWheel>", lambda e, sd=side: self._wheel(sd, e))
            txt.bind("<Button-4>", lambda e, sd=side: self._wheel(sd, e, num=4))
            txt.bind("<Button-5>", lambda e, sd=side: self._wheel(sd, e, num=5))
            pane.add(frm, weight=1)
            self.txt[side] = txt
            self._bars = getattr(self, "_bars", {})
            self._bars[side] = sb

        # 填充正文 + 记录每块的字符区间
        self._spans: Dict[str, List[Tuple[int, int]]] = {"old": [], "new": []}
        for side, blocks in (("old", blocks_o), ("new", blocks_n)):
            t = self.txt[side]
            t.configure(state="normal")
            for _, text in blocks:
                start = t.index("end-1c")
                t.insert("end", text + "\n")
                end = t.index("end-1c")
                self._spans[side].append((start, end))
            t.configure(state="disabled")

        self._paint(items)
        self._paint_nav(items)

    # —— 高亮：红删 / 绿增 ——
    def _paint(self, items):
        ro, rn = self.txt["old"], self.txt["new"]
        for it in items:
            kind = it.kind
            if kind == "removed" and it.old:
                i = self._find("old", it.old, it)
                if i >= 0:
                    ro.tag_add("hl_red", *self._spans["old"][i])
            elif kind == "added" and it.new:
                j = self._find("new", it.new, it)
                if j >= 0:
                    rn.tag_add("hl_green", *self._spans["new"][j])

    def _find(self, side: str, text: str, it) -> int:
        """条目 → 块下标：先按 ctx_before 锚定（上一块 = 前后文 → 当前块就在其后），
        失败再全文精确匹配。都找不到返回 -1（宁可少标，不可标错行）。"""
        blocks = self._spans[side]
        key = _norm_key(text)
        if not key:
            return -1
        cb = _norm_key(getattr(it, "ctx_before", "") or "")
        if cb:
            for i in range(len(blocks)):
                b0, _b1 = blocks[i]
                t = self.txt[side].get(b0, b0 + "+%dc" % len(text))
                if _norm_key(t) != key:
                    continue
                if i > 0:
                    pb0, pb1 = blocks[i - 1]
                    if _norm_key(self.txt[side].get(pb0, pb1)) == cb:
                        return i
        for i in range(len(blocks)):
            b0, b1 = blocks[i]
            if _norm_key(self.txt[side].get(b0, b1)) == key:
                return i
        return -1

    def _paint_nav(self, items):
        for it in items:
            e_o = e_n = None
            if it.kind == "removed" and it.old:
                i = self._find("old", it.old, it)
                if i >= 0:
                    e_o = self._spans["old"][i]
            elif it.kind == "added" and it.new:
                j = self._find("new", it.new, it)
                if j >= 0:
                    e_n = self._spans["new"][j]
            if e_o or e_n:
                self._nav.append({"o": e_o, "n": e_n, "kind": it.kind})
        if self._nav:
            self._show()

    # —— 导航 ——
    def _show(self):
        if not self._nav:
            self.nav_var.set("—")
            return
        self._cur %= len(self._nav)
        for side, key in (("old", "o"), ("new", "n")):
            t = self.txt[side]
            t.tag_remove("hit", "1.0", "end")
            e = self._nav[self._cur][key]
            if e:
                t.tag_add("hit", e[0], e[1])
                t.see(e[0])
        self.nav_var.set("%d / %d" % (self._cur + 1, len(self._nav)))

    def goto(self, delta: int):
        if not self._nav:
            self.nav_var.set("—")
            return
        self._cur = (self._cur + delta) % len(self._nav)
        self._show()

    # —— 滚动同步（两侧同一位置；与 PDF 预览同一套手感）——
    def _bar_scroll(self, side: str):
        def h(*args):
            self.txt[side].yview(*args)
            self._sync(side)
        return h

    def _from_bar(self, side: str, sb, first, last):
        sb.set(first, last)
        self._sync(side)

    def _sync(self, src: str):
        if self._syncing:
            return
        self._syncing = True
        try:
            frac = self.txt[src].yview()[0]
            for s, t in self.txt.items():
                if s != src:
                    t.yview_moveto(frac)
        except Exception:
            pass
        finally:
            self._syncing = False

    def _wheel(self, side: str, event, num: int = 0):
        step = 0
        d = getattr(event, "delta", 0)
        if d:
            step = -1 if d > 0 else 1
        elif num:
            step = -1 if num == 4 else 1
        if step:
            self.txt[side].yview_scroll(step * 3, "units")
            self._sync(side)
        return "break"

    def close(self):
        self.destroy()


def _norm_key(s: str) -> str:
    import re
    return re.sub(r"\s+", "", (s or "").lower())


def open_doc_preview(master, old_path: str, new_path: str, items) -> DocPreview:
    return DocPreview(master, old_path, new_path, items)


# ================================================================== 自检
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

    tmp = tempfile.mkdtemp(prefix="diff_docview_")
    pa, pb = os.path.join(tmp, "a.docx"), os.path.join(tmp, "b.docx")
    base = ["产品技术要求", "本产品适用于成人雾化吸入治疗。", "贮存条件：常温避光。", "包装规格：10 支/盒。"]
    doc._mk_docx(pa, base)
    doc._mk_docx(pb, ["产品技术要求", "本产品适用于儿童雾化吸入治疗。", "贮存条件：常温避光、干燥。", "包装规格：10 支/盒。", "新增：使用前请阅读说明书。"])

    r = doc.compare_docx(pa, pb)
    chk("样例 = 2 处改写(各拆删+增) + 1 新增 = 5 条",
        len(r.items) == 5
        and sorted(i.kind for i in r.items) == ["added", "added", "added", "removed", "removed"],
        str([(i.kind, i.old[:8], i.new[:8]) for i in r.items]))

    root = tk.Tk()
    root.withdraw()
    win = DocPreview(root, pa, pb, r.items)
    win.withdraw()

    ro, rn = win.txt["old"], win.txt["new"]
    chk("旧版红标 2 处（两处改写的旧值整行标红）",
        len(ro.tag_ranges("hl_red")) == 2)             # 两块相邻 → 并成一段 start/end
    chk("新版绿标 3 处（两处改写新值 + 1 处整段新增）",
        len(rn.tag_ranges("hl_green")) == 4)           # 两块相邻并一段 + 新增段 = 2 段

    # 导航：5 处可来回跳，hit 标记跟着走
    n0 = win._cur
    win.goto(1)
    chk("下一处前进", win._cur == n0 + 1 and win.nav_var.get() == "2 / 5", win.nav_var.get())
    win.goto(1)
    win.goto(1)
    win.goto(1)
    win.goto(1)
    chk("循环回绕", win._cur == 0, str(win._cur))
    chk("hit 标在当前处", len(ro.tag_ranges("hit")) + len(rn.tag_ranges("hit")) >= 2)

    # 滚动同步：滚旧版，新版跟到同一比例
    ro.configure(state="normal")
    ro.insert("1.0", "\n" * 80)      # 垫高，制造可滚空间
    ro.configure(state="disabled")
    win._syncing = False
    ro.yview_moveto(0.0)
    ro.yview_scroll(40, "units")
    win._sync("old")
    chk("滚动同步生效", abs(rn.yview()[0] - ro.yview()[0]) < 0.2 or ro.yview()[0] == 0,
        "old=%.3f new=%.3f" % (ro.yview()[0], rn.yview()[0]))

    # 滚轮事件分发：纵轴只碰 yview
    class _Ev:
        delta = -120
    win._syncing = False
    y0 = ro.yview()[0]
    win._wheel("old", _Ev())
    chk("滚轮驱动 yview", ro.yview()[0] != y0 or y0 in (0.0, 1.0))

    raw_a = open(pa, "rb").read()
    win.close()
    chk("关窗后原文件未动", open(pa, "rb").read() == raw_a)
    root.destroy()

    print(f"Word 预览窗自检：{ok} 项通过，{fail} 项失败")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    _self_test()
