# -*- coding: utf-8 -*-
"""
diff_app3.py —— PDF 差异对比工具 3.0（单窗口直显版）
==================================================================
最终方案（用户 2026-10-13 晚拍板）：
  「3.0 就是改 UI」—— 识别**用老引擎**（pdf_annotate.locate_marks：字符级
  diff + 转曲区像素兜底，识别质量久经考验、能用），只把**展示**换成新的
  单窗口直显界面：上面选文件点对比，下面左右并排标注，一个窗口完事。
  不再弹第二个预览窗，不再有文字清单层。

  曾试过纯像素引擎（pdfdiff3.py），对文字 PDF 太糙（整段糊红绿），弃用；
  pdfdiff3.py 保留但不再被界面调用。

渲染管线（自包含，不依赖 diff_preview）：
  fitz.open(路径) → ann.paint_into(内存画标注) → get_pixmap → PNG → Tk 位图。
  「屏幕看到的」与 paint_into 同源，屏幕=导出。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "PDF 差异对比工具 3.0"
FONT = "PingFang SC"
BG = "#f5f6f8"
CARD = "#ffffff"
BORDER = "#d9dee5"
TEXT = "#2a2f36"
TEXT2 = "#7a828c"
PRIMARY = "#2f6fed"
PRIMARY_D = "#2257c4"

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    _HAS_DND = False

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

# 上次选择的文件记忆（沿用 2.0 的机制：同目录「上次选择.json」）
_HERE = os.path.dirname(os.path.abspath(__file__))
# 识别引擎已归入 core/ 子文件夹，把 core 加进模块搜索路径（老引擎入口）
_sys = __import__("sys")
_CORE = os.path.join(_HERE, "core")
if os.path.isdir(_CORE) and _CORE not in _sys.path:
    _sys.path.insert(0, _CORE)

import pdf_annotate as ann
CONFIG_PATH = os.path.join(_HERE, "上次选择.json")

DPI_STEPS = (40, 55, 80, 110, 140, 180, 240)
DPI_MIN, DPI_MAX = 40, 240
CACHE_MAX = 6            # 渲染缓存张数（PhotoImage 很占内存，别无限攒）


class App(TkinterDnD.Tk if _HAS_DND else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1240x860+80+60")
        self.configure(bg=BG)
        self.minsize(900, 560)

        self.old_var = tk.StringVar()
        self.new_var = tk.StringVar()
        # 文件框显示用变量：只显示文件名（真实路径存在 old_var/new_var）
        self.old_disp = tk.StringVar()
        self.new_disp = tk.StringVar()
        self.status_var = tk.StringVar(
            value="选择两份 PDF（可直接拖进文件框）→ 点「开始对比」，结果直接显示在下方")
        self.busy = False
        self.q = queue.Queue()
        self.marks = None
        # 分类开关：删减/新增/字体默认开，位移默认关（沿用 ann.KIND_DEFAULT_ON）
        self.kind_on = {k: tk.BooleanVar(value=(k in ann.KIND_DEFAULT_ON))
                        for k in ann.KIND_ORDER}
        self.page = 0                    # 当前页（0 基）
        self.dpi = 110                   # 当前渲染档
        self._auto_fit = True            # 打开/换文档后自动适应宽度
        self._reset_view = True          # 翻页后回顶部/左上角（缩放/勾选时不回）
        self._imgs: dict = {}            # side → PhotoImage（留引用防空白）
        self._cache: dict = {}           # (side, page, dpi) → PhotoImage
        self.docs: dict = {}             # side → 画好标注的内存文档
        self._load_cfg()
        self._build_ui()
        self.after(120, self._poll)
        self.bind("<Configure>", self._on_resize)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 记忆 ----------------
    def _load_cfg(self):
        self.export_dir = ""          # 导出目录单独记，不和文件选择混在一起
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.old_var.set(cfg.get("old", ""))
            self.new_var.set(cfg.get("new", ""))
            self.export_dir = cfg.get("export_dir", "")
        except Exception:
            pass
        self._sync_disp()

    def _save_cfg(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"old": self.old_var.get(), "new": self.new_var.get(),
                           "export_dir": getattr(self, "export_dir", "")},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _sync_disp(self):
        """文件框只显示**文件名**（完整路径太长看不清哪个是哪个版）。

        路径仍完整存在 old_var/new_var 里，显示与存储分离。
        """
        for var, disp in ((self.old_var, self.old_disp), (self.new_var, self.new_disp)):
            p = var.get().strip()
            disp.set(os.path.basename(p) if p else "")

    # ---------------- 界面 ----------------
    def _build_ui(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, font=(FONT, 11))
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 11))
        st.configure("TButton", font=(FONT, 11), padding=(8, 4),
                     background=CARD, bordercolor=BORDER,
                     lightcolor=CARD, darkcolor=CARD)
        st.map("TButton", background=[("active", "#f0f2f5"), ("pressed", "#e6e9ee")])
        st.configure("Small.TButton", font=(FONT, 10), padding=(6, 2),
                     background=CARD, bordercolor=BORDER,
                     lightcolor=CARD, darkcolor=CARD)
        st.map("Small.TButton", background=[("active", "#f0f2f5")])
        st.configure("Primary.TButton", font=(FONT, 11, "bold"), padding=(14, 5),
                     background=PRIMARY, foreground="#ffffff",
                     bordercolor=PRIMARY, lightcolor=PRIMARY, darkcolor=PRIMARY)
        st.map("Primary.TButton", background=[("active", PRIMARY_D), ("pressed", PRIMARY_D)])
        st.configure("TProgressbar", background=PRIMARY, troughcolor="#e6e9ee",
                     bordercolor=BORDER, lightcolor=PRIMARY, darkcolor=PRIMARY)

        # ===== 顶部：选文件 + 操作 =====
        top = ttk.Frame(self)
        top.pack(fill=tk.X)

        bar = ttk.Frame(top, padding=(12, 6, 12, 0))
        bar.pack(fill=tk.X)
        tk.Label(bar, text=APP_TITLE, bg=BG, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(side=tk.LEFT)
        tk.Label(bar, text="文字级 + 像素级混合　·　对比结果直接显示在下方",
                 bg=BG, fg=TEXT2, font=(FONT, 10)).pack(side=tk.LEFT, padx=(8, 0))

        f1 = ttk.Frame(top, padding=(12, 6, 12, 0))
        f1.pack(fill=tk.X)
        f1.columnconfigure(2, weight=1)
        tk.Label(f1, text="① 选择文件", bg=BG, fg=TEXT,
                 font=(FONT, 11, "bold")).grid(row=0, column=0, rowspan=2,
                                               sticky="w", padx=(0, 8))
        self._file_row(f1, 0, "旧版", self.old_var, self.old_disp)
        self._file_row(f1, 1, "新版", self.new_var, self.new_disp)
        ttk.Button(f1, text="⇅", width=3, style="Small.TButton",
                   command=self._swap).grid(row=0, column=4, rowspan=2, padx=(8, 0))

        f3 = ttk.Frame(top, padding=(12, 8, 12, 2))
        f3.pack(fill=tk.X)
        self.btn_run = ttk.Button(f3, text="开始对比", style="Primary.TButton",
                                  command=self._run)
        self.btn_run.pack(side=tk.LEFT)
        ttk.Button(f3, text="导出 JSON", style="Small.TButton",
                   command=self._export).pack(side=tk.LEFT, padx=(5, 0))
        self.pb = ttk.Progressbar(f3, mode="determinate", length=110)
        self.pb.pack(side=tk.LEFT, padx=(12, 0))
        tk.Label(f3, text="红=删减（旧版）　绿=新增（新版）　整页重绘画边框",
                 bg=BG, fg="#9aa1a9", font=(FONT, 9)).pack(side=tk.RIGHT)

        # ===== 中部：预览工具条 =====
        self.toolbar = ttk.Frame(self, padding=(12, 2, 12, 2))
        self.toolbar.pack(fill=tk.X)
        self.btn_prev = ttk.Button(self.toolbar, text="◀", width=3,
                                   style="Small.TButton", command=self._prev_page,
                                   state="disabled")
        self.btn_prev.pack(side=tk.LEFT)
        self.lbl_page = tk.Label(self.toolbar, text="– / –", bg=BG, fg=TEXT,
                                 font=(FONT, 10), width=7)
        self.lbl_page.pack(side=tk.LEFT)
        self.btn_next = ttk.Button(self.toolbar, text="▶", width=3,
                                   style="Small.TButton", command=self._next_page,
                                   state="disabled")
        self.btn_next.pack(side=tk.LEFT)
        self._zoom_out = ttk.Button(self.toolbar, text="−", width=3,
                                    style="Small.TButton",
                                    command=lambda: self._zoom(-1),
                                    state="disabled")
        self._zoom_out.pack(side=tk.LEFT, padx=(14, 0))
        self.lbl_dpi = tk.Label(self.toolbar, text="110 dpi", bg=BG, fg=TEXT2,
                                font=(FONT, 10), width=8)
        self.lbl_dpi.pack(side=tk.LEFT)
        self._zoom_in = ttk.Button(self.toolbar, text="+", width=3,
                                   style="Small.TButton",
                                   command=lambda: self._zoom(1), state="disabled")
        self._zoom_in.pack(side=tk.LEFT)
        self._btn_fit = ttk.Button(self.toolbar, text="适应宽度",
                                   style="Small.TButton",
                                   command=self._fit_width, state="disabled")
        self._btn_fit.pack(side=tk.LEFT, padx=(10, 0))

        # 分类勾选开关（颜色图例）：删减红 / 新增绿 / 位移浅蓝 / 字体深蓝
        # 默认勾删减+新增+字体，位移默认不勾（KIND_DEFAULT_ON）。
        tk.Frame(self.toolbar, bg=BORDER, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=2)
        for _k in ann.KIND_ORDER:
            tk.Label(self.toolbar, text="●", bg=BG, fg=self._rgb_hex(ann.COLOR_OF[_k]),
                     font=(FONT, 10)).pack(side=tk.LEFT, padx=(7, 0))
            ttk.Checkbutton(self.toolbar, text=ann.KIND_TEXT[_k],
                            variable=self.kind_on[_k],
                            command=self._on_kind_toggle).pack(
                side=tk.LEFT, padx=(1, 0))

        self.lbl_old = tk.Label(self.toolbar, text="旧版 PDF", bg=BG, fg=TEXT,
                                font=(FONT, 10, "bold"))
        self.lbl_old.pack(side=tk.LEFT, padx=(60, 0))
        self.lbl_new = tk.Label(self.toolbar, text="新版 PDF", bg=BG, fg=TEXT,
                                font=(FONT, 10, "bold"))
        self.lbl_new.pack(side=tk.RIGHT, padx=(0, 60))

        # ===== 下部：并排标注区（主体） =====
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self.canv = {}
        self.vsb = {}
        self.hsb = {}
        self._syncing = False          # 联动滚动防回环
        self._drag = None              # 鼠标拖拽平移状态
        for side, col in (("old", 0), ("new", 1)):
            holder = tk.Frame(body, bg=CARD,
                              highlightbackground=BORDER, highlightthickness=1)
            holder.grid(row=0, column=col, sticky="nsew",
                        padx=(6 if col else 2, 6))
            cv = tk.Canvas(holder, bg="#eef0f3", highlightthickness=0, takefocus=1)
            vsb = ttk.Scrollbar(holder, orient=tk.VERTICAL,
                                command=lambda *a, s=side: self._scroll(s, *a))
            hsb = ttk.Scrollbar(holder, orient=tk.HORIZONTAL,
                                command=lambda *a, s=side: self._hscroll(s, *a))
            cv.configure(yscrollcommand=lambda *a, s=side: self._from_bar(s, *a),
                         xscrollcommand=lambda *a, s=side: self._from_hbar(s, *a))
            vsb.pack(side=tk.RIGHT, fill=tk.Y)
            hsb.pack(side=tk.BOTTOM, fill=tk.X)
            cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self.canv[side] = cv
            self.vsb[side] = vsb
            self.hsb[side] = hsb
            # 滚轮：纵向（两栏同步）
            cv.bind("<MouseWheel>", lambda e, s=side: self._wheel(s, e))
            cv.bind("<Button-4>", lambda e, s=side: self._wheel(s, e))
            cv.bind("<Button-5>", lambda e, s=side: self._wheel(s, e))
            # 横向滚动：Shift+滚轮 / 触控板横滑 / 鼠标左右滚轮
            cv.bind("<Shift-MouseWheel>", lambda e, s=side: self._wheel_h(s, e))
            cv.bind("<Button-6>", lambda e, s=side: self._wheel_h(s, e))
            cv.bind("<Button-7>", lambda e, s=side: self._wheel_h(s, e))
            # 鼠标按住左键拖动 = 平移（放大后比滚动条快）
            cv.bind("<ButtonPress-1>", self._drag_start)
            cv.bind("<B1-Motion>", self._drag_move)
            cv.bind("<ButtonRelease-1>", lambda *_a: setattr(self, "_drag", None))

        # 状态栏
        sb = tk.Frame(self, bg="#eceef1", highlightbackground=BORDER,
                      highlightthickness=0)
        sb.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Label(sb, textvariable=self.status_var, bg="#eceef1", fg=TEXT2,
                 font=(FONT, 10), anchor="w").pack(fill=tk.X, padx=12, pady=4)

    def _file_row(self, parent, row, label, var, disp):
        ttk.Label(parent, text=label).grid(row=row, column=1, sticky="e", padx=(0, 4))
        ent = ttk.Entry(parent, textvariable=disp, font=(FONT, 10))
        ent.grid(row=row, column=2, sticky="ew", pady=2)
        ent.configure(state="readonly")
        ttk.Button(parent, text="浏览…", style="Small.TButton",
                   command=lambda: self._pick(var)).grid(row=row, column=3, padx=(4, 0))
        if _HAS_DND:
            ent.drop_target_register(DND_FILES)
            ent.dnd_bind("<<Drop>>", lambda e, v=var: self._on_drop(e, v))

    def _on_drop(self, event, var):
        p = event.data.strip()
        if p.startswith("{") and p.endswith("}"):
            p = p[1:-1]
        if p:
            var.set(os.path.normpath(p))
            self._sync_disp()
            self._save_cfg()

    def _pick(self, var):
        p = filedialog.askopenfilename(
            title="选择 PDF",
            initialdir=os.path.dirname(var.get()) or None,
            filetypes=[("PDF / 图片", "*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                       ("所有文件", "*.*")])
        if p:
            var.set(p)
            self._sync_disp()
            self._save_cfg()

    def _swap(self):
        a, b = self.old_var.get(), self.new_var.get()
        self.old_var.set(b)
        self.new_var.set(a)
        self._sync_disp()
        self._save_cfg()

    # ---------------- 对比 ----------------
    def _run(self):
        if self.busy:
            return
        a, b = self.old_var.get().strip(), self.new_var.get().strip()
        if not a or not b:
            messagebox.showwarning(APP_TITLE, "请先选择旧版和新版 PDF")
            return
        if not os.path.isfile(a) or not os.path.isfile(b):
            messagebox.showerror(APP_TITLE, "文件路径无效，请重新选择")
            return
        self.busy = True
        self.btn_run.config(state="disabled", text="对比中…")
        self.pb["value"] = 0
        self.status_var.set("正在比对…")
        t0 = time.time()

        def work():
            try:
                # 老引擎（字符级 + 转曲区像素兜底）：识别质量久经考验，比纯像素强
                marks = ann.locate_marks(
                    a, b, prog=lambda i, t: self.q.put(("prog", i, t)))
                marks["elapsed"] = time.time() - t0
                self.q.put(("done", marks))
            except Exception as e:
                self.q.put(("err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "prog":
                    i, total = msg[1], msg[2]
                    if total:
                        self.pb["maximum"] = total
                        self.pb["value"] = i
                    self.status_var.set(f"正在比对第 {i + 1}/{total or '?'} 页…")
                elif msg[0] == "done":
                    self._on_done(msg[1])
                elif msg[0] == "err":
                    self.busy = False
                    self.btn_run.config(state="normal", text="开始对比")
                    self.status_var.set(f"失败：{msg[1]}")
                    messagebox.showerror(APP_TITLE, f"失败：\n{msg[1]}")
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _on_done(self, marks):
        self.busy = False
        self.marks = marks
        self.btn_run.config(state="normal", text="开始对比")
        self.btn_prev.config(state="normal")
        self.btn_next.config(state="normal")
        for w in (self._zoom_out, self._zoom_in, self._btn_fit):
            w.config(state="normal")
        self.pb["value"] = self.pb["maximum"] or 100
        no = marks["old"]["stats"]["marked"]
        nn = marks["new"]["stats"]["marked"]
        self._update_counts()
        self.status_var.set(
            f"完成　·　旧版标红 {no} 处 / 新版标绿 {nn} 处"
            + f"　·　耗时 {marks.get('elapsed', 0):.1f}s")
        # 重开标注文档 + 回到第 1 页 + 自适应宽度
        self.page = 0
        self._auto_fit = True
        self._reset_view = True
        self._add_annots()
        self.render()

    # ---------------- 标注文档 ----------------
    def _rgb_hex(self, rgb) -> str:
        """把 (0..1, 0..1, 0..1) 的浮点颜色转成 Tk 用的 #RRGGBB。"""
        r, g, b = (int(round(max(0.0, min(1.0, c)) * 255)) for c in rgb)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _enabled_kinds(self) -> tuple:
        """当前勾开的颜色桶（按 KIND_ORDER 钉序）。"""
        return tuple(k for k in ann.KIND_ORDER if self.kind_on[k].get())

    def _update_counts(self):
        """栏标题计数按**当前勾选的桶**统计。

        ⚠️ 之前直接用 `stats["marked"]`（含没勾的位移），勾掉位移后画面上
        只有 6 处红、状态栏却写 11 处 —— 数字和画面对不上，看着像标错。
        """
        if not self.marks:
            return
        en = set(self._enabled_kinds())
        for side, lbl in (("old", self.lbl_old), ("new", self.lbl_new)):
            bk = (self.marks.get(side) or {}).get("by_kind") or {}
            bits = [f"{ann.KIND_TEXT[k]} {sum(len(v) for v in bk[k].values())}"
                    for k in ann.KIND_ORDER if k in en and bk.get(k)]
            name = "旧版 PDF" if side == "old" else "新版 PDF"
            lbl.config(text=name + ("　·　" + "　".join(bits) if bits else "　·　无标注"))

    def _on_kind_toggle(self):
        """分类开关切换：注释要**重画**（内存文档里的旧注释摘不掉，整个重开）。"""
        if not self.marks:
            return
        self._reset_view = False
        self._add_annots()
        self._update_counts()
        self.render()

    def _add_annots(self):
        """把标记画进内存里的文档对象（原文件一个字节不动）。

        每次新对比 / 切换勾选都重开干净文档 —— PDF 注释画上就摘不掉，只能重开重画。
        过滤走 `ann.filter_kinds`：没勾的桶（如默认关的位移）不画。
        """
        enabled = self._enabled_kinds()
        for side, fallback in (("old", ann.RED), ("new", ann.GREEN)):
            old = self.docs.pop(side, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            path = (self.marks.get(side) or {}).get("path")
            if not path or not os.path.isfile(path):
                continue
            try:
                doc = fitz.open(path)
                ann.paint_into(doc,
                               ann.filter_kinds(self.marks.get(side), enabled),
                               fallback)
                self.docs[side] = doc
            except Exception:
                pass
        self._cache.clear()
        self._imgs.clear()

    # ---------------- 渲染 ----------------
    def render(self):
        """按当前页 / 当前缩放重画两侧。只读渲染，不写盘。"""
        if not self.marks:
            return
        self.configure(cursor="watch")
        try:
            self.update_idletasks()
            if self._auto_fit:
                self.dpi = self._fit_dpi()
                self._auto_fit = False
            self.lbl_dpi.config(text=f"{self.dpi} dpi")
            for side in ("old", "new"):
                self._render_side(side)
            n = max(self.marks["old"].get("pages", 0),
                    self.marks["new"].get("pages", 0))
            self.lbl_page.config(text=f"{self.page + 1} / {n}")
            self.btn_prev.config(state="normal" if self.page > 0 else "disabled")
            self.btn_next.config(state="normal" if self.page < n - 1 else "disabled")
            self._sync_scrollbars()
        finally:
            self.configure(cursor="")

    def _fit_dpi(self) -> int:
        """按画布宽度算「适应宽度」的 dpi（两张页并排，各占一半窗宽）。"""
        cv = self.canv["old"]
        w = cv.winfo_width()
        if w < 80:                       # 还没布局好，别照 1px 算
            return 110
        pw = 595.0
        try:
            doc = self.docs.get("old")
            if doc is not None and self.page < doc.page_count:
                pw = max(200.0, float(doc[self.page].rect.width))
        except Exception:
            pass
        dpi = int(w / pw * 72.0)
        return max(DPI_MIN, min(DPI_MAX, dpi))

    def _render_side(self, side: str):
        cv = self.canv[side]
        cv.delete("all")
        doc = self.docs.get(side)
        if doc is None or self.page >= doc.page_count:
            cv.configure(scrollregion=(0, 0, 10, 10))
            cv.create_text(20, 20, anchor="nw", fill="#6b7280", font=(FONT, 11),
                           text="（此侧没有这一页）" if self.marks else "选好文件点「开始对比」")
            return
        img = self._cached_pixmap(side, doc[self.page])
        self._imgs[side] = img           # ⚠️ 不留引用 = 画布空白
        cv.create_image(0, 0, anchor="nw", image=img)
        cv.configure(scrollregion=(0, 0, img.width(), img.height()))
        # 翻页才回顶部（缩放/勾选切换保持当前视角，见 _zoom/_on_kind_toggle）
        if self._reset_view:
            cv.yview_moveto(0)
            cv.xview_moveto(0)

    def _cached_pixmap(self, side: str, page) -> tk.PhotoImage:
        key = (side, self.page, self.dpi)
        img = self._cache.get(key)
        if img is not None:
            return img
        pm = page.get_pixmap(dpi=self.dpi, annots=True)
        img = tk.PhotoImage(
            data=base64.b64encode(pm.tobytes("png")).decode("ascii"))
        while len(self._cache) >= CACHE_MAX * 2:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = img
        return img

    # ---------------- 翻页 / 缩放 / 滚动 ----------------
    def _prev_page(self):
        if self.marks and self.page > 0:
            self.page -= 1
            self._reset_view = True
            self.render()

    def _next_page(self):
        if self.marks:
            n = max(self.marks["old"].get("pages", 0),
                    self.marks["new"].get("pages", 0))
            if self.page < n - 1:
                self.page += 1
                self._reset_view = True
                self.render()

    def _zoom(self, d: int):
        idx = min(range(len(DPI_STEPS)),
                  key=lambda i: abs(DPI_STEPS[i] - self.dpi))
        idx = max(0, min(len(DPI_STEPS) - 1, idx + d))
        self.dpi = DPI_STEPS[idx]
        self._reset_view = False
        self.render()

    def _fit_width(self):
        self._auto_fit = True
        self._reset_view = False
        self.render()

    # ---------------- 联动滚动（两栏横向 + 纵向都同步） ----------------
    def _scroll(self, side, *args):
        self.canv[side].yview(*args)
        self._sync_scrollbars(side)

    def _hscroll(self, side, *args):
        self.canv[side].xview(*args)
        self._sync_scrollbars(side)

    def _from_bar(self, side, first, last):
        self.vsb[side].set(first, last)
        self._sync_scrollbars(side)

    def _from_hbar(self, side, first, last):
        self.hsb[side].set(first, last)
        self._sync_scrollbars(side)

    def _sync_scrollbars(self, src=None):
        """左右两块**横向 + 纵向**都对齐到 `src` 那一侧的位置。

        参考 2.0 `diff_preview._sync_scrollbars`：放大之后主要靠横向拖，
        横向纵向都要一起跟，否则「左边滑一次、右边滑一次」。
        """
        if self._syncing:
            return
        self._syncing = True
        try:
            order = ("old", "new")
            if src in order:
                yf = self.canv[src].yview()[0]
                xf = self.canv[src].xview()[0]
                for s in order:
                    if s != src:
                        self.canv[s].yview_moveto(yf)
                        self.canv[s].xview_moveto(xf)
            for s in order:
                self.vsb[s].set(*self.canv[s].yview())
                if self.hsb.get(s) is not None:
                    self.hsb[s].set(*self.canv[s].xview())
        except Exception:
            pass
        finally:
            self._syncing = False

    def _wheel(self, side, event):
        """鼠标滚轮 / 触控板**纵向**滚动（两栏同步）。

        macOS 与 Windows 的 delta 符号相反，只取方向 ±1 折算滚动单位，
        方向交给 delta 本身，不猜正负。
        """
        d = getattr(event, "delta", 0)
        if d == 0:
            num = getattr(event, "num", 0)
            step = -1 if num == 4 else (1 if num == 5 else 0)
        else:
            step = int(d / max(1, abs(d)))
        if step:
            self.canv[side].yview_scroll(-step * 3, "units")
            self._sync_scrollbars(side)
        return "break"

    def _wheel_h(self, side, event):
        """横向滚动（Shift+滚轮 / 触控板双指横滑 / 鼠标左右滚轮）。"""
        d = getattr(event, "delta", 0)
        num = getattr(event, "num", 0)
        if d == 0 and num:
            step = -1 if num == 6 else (1 if num == 7 else 0)
        elif d:
            step = int(d / max(1, abs(d)))
        else:
            step = 0
        if step:
            self.canv[side].xview_scroll(-step * 3, "units")
            self._sync_scrollbars(side)
        return "break"

    def _drag_start(self, event):
        self._drag = (event.x, event.y)

    def _drag_move(self, event):
        """按住左键拖动 = 平移（放大之后靠滚动条太慢），两栏一起跟。"""
        if self._drag is None:
            self._drag = (event.x, event.y)
            return
        x0, y0 = self._drag
        side = "old" if self.canv["old"] is event.widget else "new"
        cv = self.canv[side]
        bx0, by0 = cv.canvasx(0), cv.canvasy(0)
        self._drag = (event.x, event.y)
        bb = cv.bbox("all") or (0, 0, 1, 1)
        cv.xview_moveto(max(0.0, (bx0 - (event.x - x0)) / max(1.0, bb[2])))
        cv.yview_moveto(max(0.0, (by0 - (event.y - y0)) / max(1.0, bb[3])))
        self._sync_scrollbars(side)

    def _on_resize(self, _e=None):
        if self.marks and self._auto_fit:
            self.render()

    # ---------------- 导出 ----------------
    @staticmethod
    def _diffs_json(diffs) -> list:
        """把 diffs 转成可 JSON 序列化的 dict 列表。

        ⚠️ 老引擎返回的是 `pdfdiff_locate.LocDiff` 对象，直接塞进 `json.dump`
        会在**写文件写到一半**时才抛 TypeError —— 弹「导出失败」但半截文件
        已经落盘，看起来像导出成功了（2026-10-13 用户踩到的就是这个）。
        所以这里先在内存里转干净，序列化成功才碰磁盘。
        """
        out = []
        for it in (diffs or []):
            d = it.__dict__ if hasattr(it, "__dict__") else it
            if isinstance(d, dict):
                out.append({k: (list(v) if isinstance(v, (list, tuple)) else v)
                            for k, v in d.items()})
            else:
                out.append(str(it))
        return out

    def _export(self):
        if not self.marks:
            messagebox.showinfo(APP_TITLE, "还没有对比结果可导出")
            return
        b1 = os.path.splitext(os.path.basename(self.old_var.get()))[0]
        b2 = os.path.splitext(os.path.basename(self.new_var.get()))[0]
        ts = time.strftime("%m%d_%H%M")

        def side_json(side):
            def rects(d):
                return {str(pi): [[round(r.x0, 1), round(r.y0, 1),
                                   round(r.x1, 1), round(r.y1, 1)] for r in rs]
                        for pi, rs in d.items()}
            bk = {k: rects(v) for k, v in side["by_kind"].items()}
            return {
                "marks": rects(side["marks"]),
                "by_kind": bk,
                "boxes": {k: v for k, v in side["boxes_by_kind"].items()},
                "pages": side["pages"],
                "marked": side["stats"]["marked"],
            }

        out = {
            "version": "3.0-classic",
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "old": self.old_var.get(),
            "new": self.new_var.get(),
            "elapsed": round(self.marks.get("elapsed", 0), 2),
            "diffs": self._diffs_json(self.marks.get("diffs")),
            "old_side": side_json(self.marks["old"]),
            "new_side": side_json(self.marks["new"]),
        }
        # ⚠️ 先整体序列化成字符串（这步可能抛错），成功才写盘 ——
        #    失败时绝不留半截 JSON 假装导出成功。
        try:
            text = json.dumps(out, ensure_ascii=False, indent=1)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"导出失败（未写任何文件）：{e}")
            return
        p = filedialog.asksaveasfilename(
            title="导出 JSON", defaultextension=".json",
            initialdir=self.export_dir or None,
            initialfile=f"PDF差异3.0_{b1}_vs_{b2}_{ts}.json",
            filetypes=[("JSON", "*.json")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            # 导出目录单独记忆（与文件选择记忆分开）
            self.export_dir = os.path.dirname(p)
            self._save_cfg()
            self.status_var.set(f"已导出：{p}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"导出失败：{e}")

    def _on_close(self):
        self._save_cfg()
        for d in self.docs.values():
            try:
                d.close()
            except Exception:
                pass
        self.destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
