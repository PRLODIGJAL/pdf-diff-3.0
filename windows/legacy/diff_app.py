# -*- coding: utf-8 -*-
"""
diff_app.py —— PDF 差异对比桌面工具（GUI）
==================================================================
选两份 PDF → 一键对比 → 差异清单（每条差异占上下两行：上行旧值、下行新值）
→ 选中看详情 → 「预览标注效果」在软件里直接看改动前/改动后的红绿标记。

引擎见 pdfdiff_core.py（文本层 pdfminer.six，表格层 pdfplumber）；
标注标记见 pdf_annotate.py 的 `locate_marks()`（**只算不落盘**）。

布局（2026-10-01 第二次精简）：
  · 上方只留「① 选择文件」，其余全给结果。
    用户原话：「员工其实没有那么多闲心思来回去跳转的，选用一套对比规则
    固定下来之后就不用再改动了。如果真的有规则需求，他会再找我去进行修改。」
    —— 所以「② 对比选项」（模式/四个忽略/阈值/正则）与「筛选」
    （类型/只看改动/全选/页码/搜索/清除）**整块删除**，
    对比规则固定成引擎默认那一套（见 `FIXED_COMPARE`）。
  · 操作条：「开始对比」＋「预览标注效果」＋「导出 JSON」。
    ⚠️ 原来的「生成标注版 PDF」已删：它会往用户文件夹里落两份带时间戳的副本，
    员工反馈「导出来还需要反复删文件、清内存，很麻烦」。
    现在改成在软件里预览，**用原文件渲染、不复制不改写**（见 diff_preview）。
  · 下方是一条**可拖动的横条**（ttk.PanedWindow）：上面「变动明细」、
    下面「选中项详情」，两块高度随便拖（各自带滚动条）。
  · 上次选的文件、横条位置 —— 都记在同目录
    「上次选择.json」，下次打开原样带回（机制照搬 WI 工具）。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from difflib import SequenceMatcher
from tkinter import filedialog, messagebox, ttk

import pdfdiff_core as core
import pdfdiff_group as grp

# 拖放文件（把 PDF/图片直接拖到文件框）：tkinter 原生不支持，用 tkinterdnd2 补上。
# 只在「能 import 出来」时才启用拖放；装不上就退回纯浏览按钮，不影响主流程
# （setup_env.py 里已把它加进依赖清单，正常都会装上）。
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    _HAS_DND = False

APP_TITLE = "PDF 差异对比工具"
FONT = "PingFang SC"
MONO = "Menlo"

# ---------------------------------------------------------------- 对比规则
# 用户 2026-10-01 拍板：界面上**不再给任何对比选项**，固定成一套。
#   「你就进行全面的对比……没必要那个一对比就全面全方位的全面对比就行了。」
#   「选用一套对比规则固定下来之后就不用再改动了。如果真的有规则需求，
#     他会再找我去进行修改。」
# 取值就是**引擎自己的默认值**（`pdfdiff_core.Options`）—— 用户原话是
# 「按照系统中的对比就行了」，所以这里不做任何「调参」，只做「照抄 + 写明白」。
#
# 表格驱动：将来要改规则只动这张表，`_opts()` 与提示文案都从它来，
# 不会再出现「界面上一处开关、代码里另一处默认值」的两套口径。
_FIXED = {
    "mode":                ("auto", "自动（表格优先 ＋ 文本兜底）"),
    "ignore_whitespace":   (True,   "空白与重新折行不算改动"),
    "ignore_case":         (False,  "大小写算改动（Rev/REV、F/f 都是真信息）"),
    "ignore_punctuation":  (False,  "标点算改动（`6.5/7.0` 与 `6570` 不能混为一谈）"),
    "ignore_header_footer": (False, "页脚标识行（文件号/修订日期）的真改动**照标**；页码/纯符号仍由内容规则滤掉"),
    "modify_threshold":    (0.55,   "相似度阈值：低于它算「整段重写」而不是「改了几个字」"),
    "ignore_patterns":     ((),     "忽略正则：默认一条不设"),
}
# 操作条右侧那行小字：**故意短** —— 长文案会被挤到窗口外面（实测 1240px 宽时
# 「…页码筛选不进清单…」后半截直接看不见）。完整的规则说明在
# `01工作/PDF差异对比工具_说明.md`，界面上只留「现在用的是哪一套」。
# 模式那一小段文案也从 `_FIXED` 派生，免得改了表、界面还写着老话。
_MODE_SHORT = {"auto": "自动", "table": "仅表格", "text": "纯文本"}
FIXED_NOTE = "规则固定：%s（全面对比）　·　阈值 %.2f" % (
    _MODE_SHORT.get(_FIXED["mode"][0], _FIXED["mode"][0]),
    _FIXED["modify_threshold"][0])

# ---------------- 配色 ----------------
# 2026-10-10 视觉精调：主色更清爽、层次更分明（只动观感，不碰红/绿/黄三色语义，
# 那些在 ROW_OLD / ROW_NEW 里）。
BG = "#f3f4f6"
CARD = "#ffffff"
BORDER = "#e6e8ec"
TEXT = "#1f2328"
TEXT2 = "#6b7280"
PRIMARY = "#3b82f6"
PRIMARY_D = "#2563eb"
HEADBG = "#eef2f7"

ROW_H = 24          # 结果表行高
MIN_TREE_PANE = 90      # 变动明细最小高度（px）
MIN_DETAIL_PANE = 90    # 详情最小高度（px）

ROW_OLD = {
    "added":        ("#f7f8fa", "#9aa1a9"),
    "removed":      ("#fdecec", "#a8071a"),
    "moved":        ("#f3f0ff", "#5b3fa8"),
    "page_added":   ("#eef4ff", "#1f4e79"),
    "page_removed": ("#fdecec", "#a8071a"),
    "mixed":        ("#fff1f0", "#a8071a"),
    # 字体变化（2026-10-13 用户拍板「② 里只应出现红/绿/深蓝」）：深蓝底深蓝字，
    # 与 chip 的「字体」色一致；两侧同文，两行同色。
    "font":         ("#dbe6ff", "#1e3a8a"),
}
ROW_NEW = {
    "added":        ("#e9f9ef", "#166534"),
    "removed":      ("#f7f8fa", "#9aa1a9"),
    "moved":        ("#eef4ff", "#1f4e79"),
    "page_added":   ("#eef4ff", "#1f4e79"),
    "page_removed": ("#f7f8fa", "#9aa1a9"),
    "mixed":        ("#eefaf0", "#166534"),
    "font":         ("#dbe6ff", "#1e3a8a"),
}
KIND_LABEL = {
    "added": "新增", "removed": "删除",
    "moved": "位置移动", "page_added": "新增页", "page_removed": "删除页",
    "mixed": "增删", "font": "字体变化",
}
KINDS = ("added", "removed", "moved", "page_added", "page_removed", "font")
CHIP = {
    # 2026-10-13 删「变动」档：去掉「修改」「数值变动」两档；
    # 保留 新增（绿）/ 删除（红）/ 位移（浅蓝）/ 字体（深蓝）。
    "added": ("#e9f9ef", "#166534"),
    "removed": ("#fdecec", "#a8071a"),
    "moved": ("#e3f0ff", "#1d4ed8"),     # 浅蓝底 + 深蓝字（位移）
    "font": ("#dbe6ff", "#1e3a8a"),      # 更深的蓝（字体变动）
}
# 置信度档位 → 清单里怎么显示。低档写「待确认」而不是「低」——
# 用户要的是「这条要人看一眼」这个动作，不是「这条分数低」这个评价。
CONF_LABEL = {"high": "高", "mid": "中", "low": "待确认"}

# 表头：一段差异占上下两行（上行旧版那一段、下行新版那一段）。
# 「内容」列里 `…前文 ‖ 本段 ‖ 后文…` 是把上下文压在了一行里 —— 明细行只有一行高，
# 塞不下三行；完整内容在下面的「④ 选中项详情」。
COLS = (("kind", "类型", 78, tk.CENTER), ("flag", "", 30, tk.CENTER),
        # 「把握」列：这条结论是怎么得出来的、有多可信（见 pdfdiff_core.SRC_CONF）。
        # 低档会连同行底色一起灰掉并写「待确认」—— 出错从「给了错答案」变成
        # 「明确告诉你这里要人核一眼」。
        ("conf", "把握", 62, tk.CENTER),
        ("page", "页码", 62, tk.CENTER), ("scale", "规模", 62, tk.CENTER),
        ("context", "所在位置", 148, tk.W),
        ("value", "内容（…前文 ‖ 本段 ‖ 后文…）", 590, tk.W),
        ("note", "变动说明", 100, tk.W))


# ---------------------------------------------------------------- 上次选择记忆
# 机制照搬 WI 一致性检查工具：写在 **本文件同目录** 的「上次选择.json」。
# 记的东西：两份 PDF 的完整路径、横条位置。下次打开原样带回。
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "上次选择.json")


def load_last():
    """读取上次选择；文件缺失、损坏或不是 JSON 对象时一律返回空 dict。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_last(d):
    """写回上次选择；写失败不影响主流程（只丢记忆，不影响比对）。"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _resolve_path(last, key):
    """决定启动时预填哪个路径。

    - 有记忆且文件还在 → 用它；
    - 有记忆但文件已被删除/改名 → 留空，让用户重新选（不偷偷回落到默认）；
    - 没有记忆 → 空。
    """
    p = str(last.get(key) or "").strip()
    return p if p and os.path.exists(p) else ""


def _fit(s, maxw):
    """超宽就截断加省略号：状态栏一行的地方有限，长文件名会把按钮挤走。"""
    import unicodedata
    s = str(s)
    w = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)
    if w <= maxw:
        return s
    out, acc = [], 0
    for ch in s:
        acc += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if acc > maxw - 1:
            break
        out.append(ch)
    return "".join(out) + "…"


_KIND_TIP = {
    "page_added":   "整页新增：新版多出这一页，旧版没有对应页",
    "page_removed": "整页删除：旧版有这一页，新版没有对应页",
    "added":        "这一段是新版新增的，旧版没有对应内容",
    "removed":      "这一段是旧版有、新版删掉的",
    "moved":        "位置移动：内容没变，位置变了",
    "mixed":        "这一段里旧版的句子被改成了新版这几句（既有删除也有新增）",
    "font":         "文本一字未改，但字体渲染变了（字号/字重，或整段转成了曲线图形）",
}


_AppBase = TkinterDnD.Tk if _HAS_DND else tk.Tk


class App(_AppBase):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE}　v{core.__version__}")
        h = min(900, max(680, self.winfo_screenheight() - 90))
        w = min(1310, max(1080, self.winfo_screenwidth() - 60))
        self.geometry(f"{w}x{h}")
        self.minsize(1000, 600)
        self.configure(bg=BG)

        # —— 先按记忆建变量，再建界面（界面里的初值就直接用这些）——
        # 只记「跟文件/布局有关」的：对比规则已经固定，没什么可记的（见 `_FIXED`）。
        last = load_last()
        self._last = last
        # 完整路径存 old_var/new_var（内部比对、记忆、导出都用它）；
        # 界面只显示 basename（old_show/new_show），路径太长会把框撑爆
        # （用户 2026-10-13：「只要给我显示最终文件的名字就好了，不然太占位置」）。
        self.old_var = tk.StringVar(value=_resolve_path(last, "old"))
        self.new_var = tk.StringVar(value=_resolve_path(last, "new"))
        self.old_show = tk.StringVar(value=os.path.basename(self.old_var.get()))
        self.new_show = tk.StringVar(value=os.path.basename(self.new_var.get()))
        self.status_var = tk.StringVar(value="选择两份 PDF，然后点「开始对比」")
        # 「引擎来源」单独一行：状态栏那行要放模式明细 + 页数 + 耗时，再加一句
        # 「哪些字是 pdfminer 读的、哪些是 OCR 读的」就会被窗口截断（用户
        # 2026-09-30 追问「你是不是用 OCR 识别了在软件里面」—— 得有一处写明）。
        self.engine_var = tk.StringVar(value="")

        # 布局记忆：横条位置（_apply_sash 里用）
        self._sash0 = last.get("sash")

        self.result: core.Result | None = None
        self.items: list[core.DiffItem] = []
        # 界面按「段」展示（一段差异 = 上下一对行）；self.items 仍是逐条的原始清单，
        # 导出走的就是它 —— 导出的粒度不该被界面的展示方式绑住。
        self.paras: list[grp.Para] = []
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self._sel_pair: tuple[str, str] = ("", "")
        self._spans: list[tuple[str, str, str, str]] = []   # 逐处跳转用的差异区间
        self._cur_span = 0
        self._pv = None                        # 当前打开的标注预览窗（只留一个，避免堆窗）
        self._stages = []                      # 2.0 编排器的阶段日志（耗时/跳过），状态栏可显示
        self._ready = False                    # 启动阶段不写盘，避免覆盖刚恢复的记忆
        self._save_job = None
        self._clamp_job = None
        self._clamping = False

        self._style()
        self._build_ui()
        self._poll()

        # 输入一变就（防抖后）写盘。只在「点过按钮 / 关窗」才写的话，
        # 直接关终端或程序被强杀时，用户明明选好的文件根本记不下来。
        for var in (self.old_var, self.new_var):
            var.trace_add("write", self._schedule_save)
        self.after(150, lambda: (self._apply_sash(self._sash0), self._hint_restored()))

    # ---------------- 样式 ----------------
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")     # macOS 的 aqua 会忽略背景色，卡片配色出不来
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, font=(FONT, 11))
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 11))
        st.configure("TCheckbutton", background=CARD, font=(FONT, 10), padding=(0, 0))
        st.configure("TRadiobutton", background=CARD, font=(FONT, 10), padding=(0, 0))
        st.configure("TButton", font=(FONT, 11), padding=(8, 4),
                     background=CARD, bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
        st.map("TButton", background=[("active", "#f0f2f5"), ("pressed", "#e6e9ee")])
        st.configure("Small.TButton", font=(FONT, 10), padding=(6, 2),
                     background=CARD, bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
        st.map("Small.TButton", background=[("active", "#f0f2f5")])
        st.configure("Primary.TButton", font=(FONT, 11, "bold"), padding=(14, 5),
                     background=PRIMARY, foreground="#ffffff",
                     bordercolor=PRIMARY, lightcolor=PRIMARY, darkcolor=PRIMARY)
        st.map("Primary.TButton", background=[("active", PRIMARY_D), ("pressed", PRIMARY_D)])
        st.configure("TProgressbar", background=PRIMARY, troughcolor="#e6e9ee",
                     bordercolor=BORDER, lightcolor=PRIMARY, darkcolor=PRIMARY)
        st.configure("Treeview", font=(FONT, 10), rowheight=ROW_H,
                     background=CARD, fieldbackground=CARD, bordercolor=BORDER)
        st.configure("Treeview.Heading", font=(FONT, 10, "bold"),
                     background=HEADBG, foreground=TEXT, bordercolor=BORDER, relief="flat",
                     padding=(4, 3))
        st.map("Treeview.Heading", background=[("active", "#e4e8ee")])
        # 分隔条粗一点才抓得住：clam 默认只有几像素，鼠标很难命中
        st.configure("TPanedwindow", background=BG)
        st.configure("Sash", sashthickness=7, gripcount=0)
        # ⚠️ 2026-10-01：界面上的勾选框随「② 对比选项」「筛选」一起删光了，
        # 自绘勾选框（clam 选中时画叉那套）已搬到 `diff_preview.py` —— 那边
        # 「显示标注」是现在唯一的勾选框。别在这里留一份没人用的样式。

    # ---------------- 布局 ----------------
    def _card(self, parent, title="", pady=(0, 5)):
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.X, pady=pady)
        card = tk.Frame(wrap, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill=tk.X)
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill=tk.X, padx=11, pady=5)
        if not title:
            return inner
        tk.Label(inner, text=title, bg=CARD, fg=TEXT, font=(FONT, 11, "bold")).pack(anchor="w")
        body = tk.Frame(inner, bg=CARD)
        body.pack(fill=tk.X, pady=(3, 0))
        return body

    def _build_ui(self):
        # ===== 常驻的设置区 =====
        top = ttk.Frame(self)
        top.pack(fill=tk.X)
        self.top_area = top

        # 标题栏
        bar = ttk.Frame(top, padding=(12, 4, 12, 0))
        bar.pack(fill=tk.X)
        tk.Label(bar, text=APP_TITLE, bg=BG, fg=TEXT, font=(FONT, 12, "bold")).pack(side=tk.LEFT)
        tk.Label(bar, text=f"v{core.__version__}　·　识别两份 PDF / 图片到底改了什么",
                 bg=BG, fg=TEXT2, font=(FONT, 10)).pack(side=tk.LEFT, padx=(8, 0))

        # ① 文件（标题与首行并排，省一行高度）
        f1 = self._card(top)
        f1.columnconfigure(2, weight=1)
        tk.Label(f1, text="① 选择文件", bg=CARD, fg=TEXT,
                 font=(FONT, 11, "bold")).grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 8))
        self._file_row(f1, 0, "旧版", self.old_var, 1)
        self._file_row(f1, 1, "新版", self.new_var, 1)
        ttk.Button(f1, text="⇅", width=3, style="Small.TButton",
                   command=self._swap).grid(row=0, column=4, rowspan=2, padx=(8, 0))

        # ② 操作条（对比规则不再给开关，只在右侧写一句「已经固定成什么」）
        f3 = ttk.Frame(top)
        f3.pack(fill=tk.X, pady=(0, 5))
        self.btn_run = ttk.Button(f3, text="开始对比", style="Primary.TButton", command=self._run)
        self.btn_run.pack(side=tk.LEFT)
        # ⚠️ 这里以前是「生成标注版 PDF」——会往用户文件夹里落两份带时间戳的副本，
        #    员工反馈「导出来还需要反复删文件、清内存，很麻烦」。现在改成
        #    在软件里预览：用原文件渲染，不复制、不改写、不留伴生产物。
        self.btn_preview = ttk.Button(f3, text="预览标注效果", style="Small.TButton",
                                      command=self._preview, state="disabled")
        self.btn_preview.pack(side=tk.LEFT, padx=(5, 0))
        # 2026-09-29 用户要求：去掉「导出 Excel」「导出 CSV」两个按钮，只留 JSON。
        ttk.Button(f3, text="导出 JSON", style="Small.TButton",
                   command=lambda: self._export("json")).pack(side=tk.LEFT, padx=(5, 0))
        self.pb = ttk.Progressbar(f3, mode="determinate", length=110)
        self.pb.pack(side=tk.LEFT, padx=(12, 0))
        self.chip_bar = tk.Frame(f3, bg=BG)
        self.chip_bar.pack(side=tk.LEFT, padx=(14, 0))
        self._font_n = 0        # 「字体」chip 计数（对比完成后后台补算，见 _on_done）
        self._font_task = 0     # 递增代币：只接受最新一次任务的回传
        tk.Label(f3, text=FIXED_NOTE, bg=BG, fg="#9aa1a9",
                 font=(FONT, 9)).pack(side=tk.RIGHT, padx=(0, 4))

        # ===== 可拖动的两块：上=变动明细，下=选中项详情 =====
        # 抄 WI 工具的写法（ttk.PanedWindow）。写死高度必然有一边不够看：
        # 有人只想扫清单、有人要逐条看详情，各人各时候都不一样。
        self.split = ttk.PanedWindow(self, orient="vertical")
        self.split.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 6))

        # --- 上块：变动明细 ---
        pane_t = ttk.Frame(self.split)
        pane_t.rowconfigure(1, weight=1)
        pane_t.columnconfigure(0, weight=1)
        head_t = tk.Frame(pane_t, bg=CARD)
        head_t.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(head_t, text="② 变动明细", bg=CARD, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(side=tk.LEFT, padx=(12, 0), pady=(6, 3))
        tk.Label(head_t, text="上行旧版 · 下行新版，双击看全文",
                 bg=CARD, fg=TEXT2, font=(FONT, 9)).pack(side=tk.LEFT, padx=(8, 0))
        wrap_t = tk.Frame(pane_t, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        wrap_t.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 4))
        wrap_t.rowconfigure(0, weight=1)
        wrap_t.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(wrap_t, columns=[c[0] for c in COLS],
                                 show="headings", selectmode="browse", height=10)
        for c, t, w_, a in COLS:
            self.tree.heading(c, text=t, anchor=a)
            self.tree.column(c, width=w_, anchor=a, stretch=(c == "value"))
        # 页码列**只有一页时不显示**（用户 2026-10-01：「像那个页码也可以删掉」）：
        # 单页文档里它每行都写「1」，纯噪声；多页文档里又必须留着。
        # 所以列定义一直保留，只靠 `displaycolumns` 决定显不显示 ——
        # `tree.item(iid, "values")` 取到的仍是完整一组值，回归与导出都不受影响。
        self._cols_all = [c[0] for c in COLS]
        self.tree["displaycolumns"] = self._cols_all
        tvs = ttk.Scrollbar(wrap_t, orient=tk.VERTICAL, command=self.tree.yview)
        ths = ttk.Scrollbar(wrap_t, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscroll=tvs.set, xscroll=ths.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tvs.grid(row=0, column=1, sticky="ns")
        ths.grid(row=1, column=0, sticky="ew")
        for k in KINDS:
            self.tree.tag_configure(f"{k}_o", background=ROW_OLD[k][0], foreground=ROW_OLD[k][1])
            self.tree.tag_configure(f"{k}_n", background=ROW_NEW[k][0], foreground=ROW_NEW[k][1])
        self.tree.tag_configure("pair", font=(FONT, 10, "bold"))
        # 「没把握」的行整行灰掉：放在颜色 tag **之后**配，靠 tag 顺序盖住红/绿底色。
        # 灰不是「这条不重要」，而是「这条是估的，请人看一眼」—— 颜色语义见
        # `pdfdiff_core.conf_level`（conf 与 loc_conf 取低者定档）。
        self.tree.tag_configure("low", background="#f0f0f0", foreground="#8c8c8c")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _e: self._popup_detail())
        self.tree_pane = pane_t
        self.tree_wrap = wrap_t
        pane_t.bind("<Configure>", self._on_pane_resize)
        wrap_t.bind("<Configure>", self._on_pane_resize)

        # --- 下块：选中项详情 ---
        pane_d = ttk.Frame(self.split)
        pane_d.rowconfigure(1, weight=1)
        pane_d.columnconfigure(0, weight=1)
        pane_d.columnconfigure(1, weight=1)
        head_d = tk.Frame(pane_d, bg=CARD)
        head_d.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(head_d, text="③ 选中项详情", bg=CARD, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(side=tk.LEFT, padx=(12, 0), pady=(6, 3))
        tk.Label(head_d, text="全文及前后文 · 红=旧版 · 绿=新版",
                 bg=CARD, fg=TEXT2, font=(FONT, 9)).pack(side=tk.LEFT, padx=(8, 0))
        # 逐处跳转：长文本（整行规格、整段条文）里改动常在第几百个字，
        # 只靠自己滚很难找到 —— 所以给「第几处 / 共几处」和前后翻的按钮。
        nav = tk.Frame(head_d, bg=CARD)
        nav.pack(side=tk.RIGHT, padx=(0, 12))
        self.btn_prev = ttk.Button(nav, text="◀", width=2, style="Small.TButton",
                                   command=lambda: self._goto(-1))
        self.btn_prev.pack(side=tk.LEFT)
        self.nav_var = tk.StringVar(value="—")
        tk.Label(nav, textvariable=self.nav_var, bg=CARD, fg=TEXT,
                 font=(FONT, 10, "bold"), width=6, anchor="center").pack(side=tk.LEFT, padx=3)
        self.btn_next = ttk.Button(nav, text="▶", width=2, style="Small.TButton",
                                   command=lambda: self._goto(1))
        self.btn_next.pack(side=tk.LEFT)
        self.tip_var = tk.StringVar(value="")
        tk.Label(head_d, textvariable=self.tip_var, bg=CARD, fg=TEXT2,
                 font=(FONT, 9)).pack(side=tk.RIGHT, padx=(0, 12))
        self.detail_pane = pane_d
        pane_d.bind("<Configure>", self._on_pane_resize)
        self.txt_old = self._detail_col(pane_d, 1, 0, "旧版", "#a8071a")
        self.txt_new = self._detail_col(pane_d, 1, 1, "新版", "#166534")
        try:
            self.split.add(pane_t, weight=1)
            self.split.add(pane_d, weight=0)
        except tk.TclError:
            pass

        # 状态栏：最底下一行 = 模式明细 + 页数 + 耗时；它上面一行 = 引擎来源
        # （`side=BOTTOM` 先 pack 的落在最底，所以先状态、后引擎）
        ttk.Label(self, textvariable=self.status_var, background=HEADBG, foreground=TEXT2,
                  anchor="w", padding=(10, 3), font=(FONT, 10)).pack(fill=tk.X, side=tk.BOTTOM)
        tk.Label(self, textvariable=self.engine_var, background=HEADBG, foreground=TEXT2,
                 anchor="w", padx=10, font=(FONT, 9)).pack(fill=tk.X, side=tk.BOTTOM,
                                                            pady=(2, 0))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _detail_col(self, parent, row, col, title, color):
        """详情面板里的一栏：标题 + 可滚动文本框，高度跟着所在窗格走。"""
        box = tk.Frame(parent, bg=CARD)
        box.grid(row=row, column=col, sticky="nsew",
                 padx=(10 if col == 0 else 5, 5 if col == 0 else 10), pady=(0, 6))
        box.rowconfigure(1, weight=1)
        box.columnconfigure(0, weight=1)
        head = tk.Frame(box, bg=CARD)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Label(head, text="●", bg=CARD, fg=color, font=(FONT, 9)).pack(side=tk.LEFT)
        tk.Label(head, text=title, bg=CARD, fg=TEXT, font=(FONT, 10, "bold")).pack(side=tk.LEFT)
        inner = tk.Frame(box, bg="#fbfcfd", highlightbackground=BORDER, highlightthickness=1)
        inner.grid(row=1, column=0, sticky="nsew", pady=(3, 0))
        inner.rowconfigure(0, weight=1)
        inner.columnconfigure(0, weight=1)
        t = tk.Text(inner, wrap="word", font=(MONO, 11), relief="flat", bg="#fbfcfd",
                    fg=TEXT, padx=6, pady=4, spacing1=0, spacing3=0, height=3)
        t.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(inner, orient=tk.VERTICAL, command=t.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        t.configure(yscrollcommand=vsb.set)
        self._style_text(t)
        # 不设黄色「当前处」标记：红绿里再掺黄，反而分不清哪处是改动（用户 2026-09-29 点名去掉）。
        # 逐处跳转只负责把视线滚到那一处。
        t.configure(state="disabled")
        return t

    @staticmethod
    def _style_text(t):
        """详情文本框的统一配色（详情面板与双击弹窗共用一份，免得两处走样）。

        · 本段：`del` 红底 / `ins` 绿底 —— 改动的字符
        · 上下文：**压灰**（用户 2026-09-29：「上下文……多加一些内容，这样子能够让人眼
          更明确地看出来哪些地方发生了变动」）—— 灰掉之后，彩色剩下的就是改动本身
        · 分隔条 / 空占位：更浅的灰，只起标尺作用
        """
        t.tag_configure("del", background="#ffc7ce", foreground="#9c0006")
        t.tag_configure("ins", background="#c6efce", foreground="#006100")
        for role in (grp.R_BEFORE, grp.R_AFTER):
            t.tag_configure(role, foreground="#949aa3")
        for role in (grp.R_RULE, grp.R_EMPTY):
            t.tag_configure(role, foreground="#c2c8d0")

    def _file_row(self, parent, row, label, var, col0=0):
        # var 是完整路径变量（内部用）；显示的是 basename（show_var）。
        show_var = self.old_show if var is self.old_var else self.new_show
        tk.Label(parent, text=label, bg=CARD, fg=TEXT2, font=(FONT, 10), width=3,
                 anchor="w").grid(row=row, column=col0, sticky="w", pady=1)
        e = tk.Entry(parent, textvariable=show_var, font=(FONT, 10), relief="flat",
                     bg="#fbfcfd", highlightbackground=BORDER, highlightthickness=1)
        e.grid(row=row, column=col0 + 1, sticky="ew", padx=(6, 6), ipady=2)
        # 拖放：把文件直接拖到这个输入框即可填入路径（只显示文件名，路径在内部）。
        if _HAS_DND:
            try:
                e.drop_target_register(DND_FILES)
                e.dnd_bind("<<Drop>>", lambda ev, v=var: self._on_drop(ev, v))
            except Exception:
                pass
        ttk.Button(parent, text="浏览…", width=7, style="Small.TButton",
                   command=lambda: self._pick(var)).grid(row=row, column=col0 + 2)

    # ---------------- 高度调节 ----------------
    def _on_pane_resize(self, event=None):
        """窗格高度变了 → 结果表按新高度重排行数，并把横条夹在合理范围里。"""
        self._fit_tree()
        if self._clamp_job is None:
            self._clamp_job = self.after(60, self._clamp_split)

    def _fit_tree(self):
        """Treeview 的行数是写死的，窗格拉大它不会自己变多 —— 手动重算。
        按**表格外框**的高度算（不是整个窗格）：窗格里还有标题行，
        拿窗格高度算会平白少算两三行。"""
        try:
            h = self.tree_wrap.winfo_height()
        except Exception:
            return
        if h <= 40:
            return
        rows = max(3, int((h - 22) // (ROW_H + 2)))     # 22 ≈ 横向滚动条 + 边框
        try:
            if int(self.tree["height"]) != rows:
                self.tree.configure(height=rows)
        except Exception:
            pass

    def _clamp_split(self):
        """Tk 9 的 ttk.PanedWindow 没有 minsize（只有 weight），
        所以两块的最小高度得自己夹 —— 不然能把其中一块拖没。"""
        self._clamp_job = None
        if self._clamping:
            return
        try:
            h = self.split.winfo_height()
            pos = self.split.sashpos(0)
        except Exception:
            return
        lo, hi = MIN_TREE_PANE, h - MIN_DETAIL_PANE
        if h < MIN_TREE_PANE + MIN_DETAIL_PANE or hi <= lo:
            return
        new = min(max(pos, lo), hi)
        if abs(new - pos) >= 2:
            self._clamping = True
            try:
                self.split.sashpos(0, new)
            except Exception:
                pass
            finally:
                self._clamping = False

    def _set_split(self, ratio):
        """按预设比例分配上下两块（ratio = 上块占比）。"""
        try:
            h = self.split.winfo_height()
            pos = int(h * ratio)
            pos = min(max(pos, MIN_TREE_PANE), max(MIN_TREE_PANE, h - MIN_DETAIL_PANE))
            self.split.sashpos(0, pos)
        except Exception:
            pass
        self._fit_tree()

    def _apply_sash(self, pos):
        """启动时把上次拖到的横条位置还原回去。"""
        if pos is None:
            self._set_split(0.72)
            return
        try:
            pos = int(pos)
            h = self.split.winfo_height()
            if h > MIN_TREE_PANE + MIN_DETAIL_PANE:
                self.split.sashpos(0, min(max(pos, MIN_TREE_PANE), h - MIN_DETAIL_PANE))
        except (TypeError, ValueError, tk.TclError):
            self._set_split(0.72)
        self._fit_tree()
        self._ready = True

    def _set_path(self, var, path):
        """统一入口：设置某栏的完整路径，并同步刷新界面只显示文件名。

        `var` 是完整路径变量（old_var / new_var），`show` 是界面上那个
        只显示 basename 的框。所有改文件的地方（浏览、拖放、交换）都走这里，
        保证「内部存的」和「界面显示的」不会走样。
        """
        path = (path or "").strip()
        var.set(path)
        show_var = self.old_show if var is self.old_var else self.new_show
        show_var.set(os.path.basename(path))

    def _on_drop(self, event, var):
        """拖放文件到输入框：取路径（tkdnd 可能给 `{路径}` 花括号包裹），填入。"""
        data = event.data.strip()
        if data.startswith("{") and data.endswith("}"):
            data = data[1:-1]
        # 一次拖多个文件只取第一个（这里只有一个框，多文件没意义）
        data = data.split("} {")[0].strip()
        if data:
            self._set_path(var, data)
            self._after_file_set(data)

    def _after_file_set(self, path):
        """选了文件后，若是图片则给一句「会先折成 PDF」的提示（复用浏览逻辑）。"""
        try:
            import pdfdiff_img as _img
            if _img.is_image_file(path):
                self.status_var.set(
                    f"已选图片（{_img.image_kind(path)}）：会先折成单页 PDF，"
                    f"走扫描件链路（结论来自 OCR，请核对「待确认」项）")
        except Exception:
            pass

    # ---------------- 记忆读写 ----------------
    def _schedule_save(self, *_a):
        """输入变化后延迟写盘：连续输入只落一次，避免频繁碰磁盘。"""
        if not getattr(self, "_ready", False):
            return
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
        try:
            self._save_job = self.after(400, self._save_now)
        except Exception:
            self._save_job = None

    def _save_now(self):
        self._save_job = None
        self._save_last()

    def _sash_now(self):
        try:
            return int(self.split.sashpos(0))
        except Exception:
            return None

    def _save_last(self):
        save_last({
            "old": self.old_var.get().strip(),
            "new": self.new_var.get().strip(),
            "sash": self._sash_now(),
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    def _on_close(self):
        """关窗时立刻落盘（不等防抖），手输路径也不会丢。"""
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = None
        # 预览窗持有两份只读 PDF 句柄，先把它们放掉再关主窗。
        if self._pv is not None:
            try:
                self._pv.close()
            except Exception:
                pass
            self._pv = None
        self._save_last()
        self.destroy()

    def _hint_restored(self):
        """状态栏如实说明这次的预填是哪来的 ——
        不然「没记住」和「上次的文件没了」两件事根本分不清。"""
        last = self._last
        lost = [_fit(os.path.basename(str(last[k])), 30) for k in ("old", "new")
                if last.get(k) and not os.path.exists(str(last[k]))]
        back = [_fit(os.path.basename(p), 30)
                for k, p in (("old", self.old_var.get()), ("new", self.new_var.get()))
                if p and last.get(k) == p]
        if lost:
            self.status_var.set("上次选择的文件已不存在，该栏已留空：" + "、".join(lost))
        elif back:
            self.status_var.set("已恢复上次选择：" + "、".join(back))

    # ---------------- 交互 ----------------
    def _pick(self, var):
        cur = var.get().strip()
        init = os.path.dirname(cur) if cur and os.path.isdir(os.path.dirname(cur)) else None
        # 图片（PNG/JPG/TIFF…）也能选：进引擎前由 pdfdiff_img 折成单页 PDF，
        # 与扫描件 PDF 走同一条链路（用户 2026-09-30：「甚至 PNG、JPG 也试着识别标记」）。
        # Word（.docx）也能选：pdfdiff_doc 直读精确文本，不转换、不产生中间文件
        # （用户 2026-10-02：「Word 本身就是一个很好的读取文件，没必要转 PDF」）。
        import pdfdiff_img as _img
        kinds = [("PDF / Word / 图片",
                  ["*.pdf", "*.PDF", "*.docx", "*.DOCX"] + ["*" + e for e in _img.IMG_EXT_MAP]),
                 ("PDF 文件", "*.pdf"), ("Word 文档", "*.docx"),
                 ("图片文件", ["*" + e for e in _img.IMG_EXT_MAP]),
                 ("所有文件", "*.*")]
        p = filedialog.askopenfilename(title="选择 PDF / Word / 图片", initialdir=init,
                                       filetypes=kinds)
        if p:
            self._set_path(var, p)
            self._after_file_set(p)

    def _swap(self):
        a, b = self.old_var.get(), self.new_var.get()
        self._set_path(self.old_var, b)
        self._set_path(self.new_var, a)

    # ---------------- 对比规则（固定，界面不再提供开关）----------------
    def _opts(self) -> core.Options:
        """按 `_FIXED` 那张表拼出引擎选项。

        ⚠️ 唯一入口：`_run()` 与 `_preview()` 都从这里取，不许在别处再写一份
        「默认值」—— 两份口径必然走样（2026-09-30 就踩过：界面勾了「忽略页眉页脚」
        而导出的标注版没传，两边结论对不上）。
        """
        o = core.Options(**{k: (list(v[0]) if k == "ignore_patterns" else v[0])
                            for k, v in _FIXED.items()})
        o.compile_patterns()
        return o

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
        # Word 走独立链路（pdfdiff_doc 直读）：两边必须同类型，混比直接劝退
        self._is_docx = a.lower().endswith(".docx")
        if self._is_docx != b.lower().endswith(".docx"):
            messagebox.showerror(APP_TITLE,
                                 "Word 和 PDF 暂不支持混着比：请两边选同类型文件"
                                 "（都是 Word，或都是 PDF / 图片）")
            return
        self.busy = True
        self.btn_run.config(state="disabled", text="对比中…")
        self.pb["value"] = 0
        self.status_var.set("正在读取 Word…" if self._is_docx else "正在解析 PDF…")
        self.engine_var.set("")
        opts = self._opts()

        def work():
            try:
                if self._is_docx:
                    import pdfdiff_doc as _doc
                    r = _doc.compare_docx(a, b, prog=lambda i, t: self.q.put(("prog", i, t)))
                else:
                    # 2.0 统一入口：走分层编排器（PDF / 图片同一套七段流水线）。
                    # annotate=False —— 标注留到「预览标注效果」按需生成，不提前落盘。
                    import orchestrator as orch
                    pipe = orch.run(a, b, annotate=False,
                                    prog=lambda i, t: self.q.put(("prog", i, t)))
                    r = pipe.compare
                    self.q.put(("stages", pipe.stages))   # 阶段日志单独回传，界面可显示
                self.q.put(("done", r))
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
                    if getattr(self, "_is_docx", False):
                        self.status_var.set("正在比对 Word 内容…")
                    elif total and i >= total and getattr(core, "OCR_RESCUE", False):
                        # 页都比对完了，剩下的时间几乎全花在 OCR 上（转曲文档冷启动可达 1 分钟，
                        # 命中磁盘缓存后几秒）—— 不说一声的话界面就停在「第 N/N 页」像卡死了
                        self.status_var.set("正在做像素级核对（转曲 / 图形区，这一步较慢）…")
                    else:
                        self.status_var.set(f"正在比对第 {i}/{total or '?'} 页…")
                elif msg[0] == "done":
                    self._on_done(msg[1])
                elif msg[0] == "stages":
                    self._stages = msg[1]
                elif msg[0] == "font_count":
                    if msg[1] == self._font_task:   # 只认最新一次的回传
                        self._font_n = int(msg[2])
                        if self.result:
                            self._chips(self.result.stats)
                elif msg[0] == "preview_ready":
                    self._on_preview_ready(msg[1])
                elif msg[0] == "err":
                    self.busy = False
                    self.btn_run.config(state="normal", text="开始对比")
                    self.btn_preview.config(
                        state="normal" if (self.result and self.items) else "disabled",
                        text="预览标注效果")
                    self.status_var.set(f"失败：{msg[1]}")
                    messagebox.showerror(APP_TITLE, f"失败：\n{msg[1]}")
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _on_done(self, r):
        self.busy = False
        self.btn_run.config(state="normal", text="开始对比")
        self.btn_preview.config(state="normal" if r.items else "disabled")
        self.pb["value"] = self.pb["maximum"] or 100
        if r.error and not r.items:
            self.status_var.set(r.error)
            messagebox.showerror(APP_TITLE, r.error)
            return
        self.result, self.items = r, r.items
        self._apply_page_col()
        self._font_n = 0
        self._chips(r.stats)
        self._font_task += 1
        if not getattr(self, "_is_docx", False):
            threading.Thread(target=self._font_count_work,
                             args=(r.old_path, r.new_path, self._font_task),
                             daemon=True).start()
        self.engine_var.set(getattr(r, "engine_line", "") or "")
        unit_verb = "块" if getattr(self, "_is_docx", False) else "页"
        self.btn_preview.config(text="预览改动高亮" if getattr(self, "_is_docx", False)
                                else "预览标注效果")
        # 图片 / 扫描件输入时「0 处差异」可能是「根本没看」（尺寸对不上、OCR 不可用），
        # notes 就是为了让这句不再被误读成「真的没改」。
        _notes = list(getattr(r, "notes", []) or [])
        # 置信度分布：明确交代「这堆结论里多少是实打实的、多少要人核一眼」。
        # 这是本轮的核心 —— 引擎不再假装全能判定，而是把没把握的地方指出来。
        _lv = {"high": 0, "mid": 0, "low": 0}
        for _it in r.items:
            _lv[core.conf_level_of(_it)] += 1
        if len(r.items):
            _conf = (f"　·　把握：高 {_lv['high']} / 中 {_lv['mid']}"
                     f" / 待确认 {_lv['low']}")
        else:
            _conf = ""
        self.status_var.set(f"完成　·　模式：{r.mode_used}　·　旧版 {r.pages_old} {unit_verb} / "
                            f"新版 {r.pages_new} {unit_verb}　·　耗时 {r.elapsed:.2f}s"
                            + _conf
                            + ("　⚠ " + "；".join(_notes) if _notes else ""))
        self._refresh()

    def _font_count_work(self, old_path: str, new_path: str, task: int):
        """后台补算「字体变动」条数（2026-10-12 五色体系）。

        字体变动只在标注链路（`locate._extract_font_changes`）检测，
        `compare_pdfs` 的清单里没有 font 条目 —— chip 的数字在这里补：
        文字层逐页抽行 + 字体签名配对，纯文字层操作，秒级；失败静默
        （chip 显示 0，不算错 —— 字体计数是锦上添花，不能拖垮主流程）。
        """
        try:
            import pdfdiff_img as _img
            import pdfdiff_locate as _loc
            op, np_ = _img.ensure_pdf(old_path), _img.ensure_pdf(new_path)
            opts = self._opts()
            da, db = fitz.open(op), fitz.open(np_)
            try:
                pa = [_loc.extract_rows(da[i], i, opts.ignore_case,
                                        opts.ignore_punct, opts.drop_header_footer)
                      for i in range(da.page_count)]
                pb = [_loc.extract_rows(db[i], i, opts.ignore_case,
                                        opts.ignore_punct, opts.drop_header_footer)
                      for i in range(db.page_count)]
            finally:
                da.close()
                db.close()
            n = len(_loc._extract_font_changes(pa, pb))
            self.q.put(("font_count", task, n))
        except Exception:
            self.q.put(("font_count", task, 0))

    def _apply_page_col(self):
        """差异全落在第 1 页时，把「页码」列收起来；出现第 2 页及以上再放回来。

        判据是**这次比出来的差异落在第几页**，不是「两份 PDF 各有几页」——
        两页的文档若只在第 1 页有改动，用户看到的仍然是一列「1」。
        ⚠️ 不能改成删除列（`tree["columns"]`）：列一少，`values` 的下标就整体
        前移，`_refresh` 里按位置写的那 7 个值会全部错位。
        """
        used = set()
        for it in self.items:
            for p in (it.page_old, it.page_new):
                if p is not None:
                    used.add(p)
        show = bool(used) and max(used) > 0
        cols = [c for c in self._cols_all if c != "page" or show]
        try:
            if list(self.tree["displaycolumns"]) != cols:
                self.tree["displaycolumns"] = cols
        except Exception:
            pass

    def _chips(self, s):
        """「开始对比」右侧的统计 chips（2026-10-13 定稿）：
        新增 / 删除 / 字体 —— 与 ② 清单的展示口径一致（moved 已不进清单，
        位移 chip 一并去掉，免得「chip 计了 34 条清单里一条没有」）。"""
        for w in self.chip_bar.winfo_children():
            w.destroy()
        for key, label in (("added", "新增"), ("removed", "删除"),
                           ("font", "字体")):
            bg, fg = CHIP[key]
            chip = tk.Frame(self.chip_bar, bg=bg)
            chip.pack(side=tk.LEFT, padx=(0, 6))
            tk.Label(chip, text=label, bg=bg, fg=fg, font=(FONT, 9)).pack(side=tk.LEFT, padx=(7, 3), pady=2)
            tk.Label(chip, text=str(s.get(key, 0) if key != "font" else self._font_n),
                     bg=bg, fg=fg,
                     font=(FONT, 11, "bold")).pack(side=tk.LEFT, padx=(0, 7))

    def _filtered(self):
        """把清单交给「分段渲染」之前的那道闸。

        2026-10-13 用户拍板：② 变动明细里**只展示 增 / 删 / 字体变化**，
        位置移动（moved）不再进清单 —— 「内容没变只是挪了位置」对核对没有
        帮助，38 条 moved 反而把真差异淹没（11vs12 实测）。导出 JSON 仍走
        `self.items` 全量，不在这里过滤。

        ⚠️ 之所以**留着这个函数**而不是把 `self.items` 直接塞给 `_refresh()`：
        分段渲染、导出、将来可能回来的筛选都会经过它，逻辑只有这一处 ——
        真有一天要加筛选，改这里一行即可，不会散落到三个地方。
        """
        return [it for it in self.items if it.kind != "moved"]

    def _refresh(self):
        """**按「段」渲染**：一段差异占上下两行（上行旧版那一段、下行新版那一段）。

        逐条渲染时，中栏新增一整节会变成十几行一条条往下滚 —— 用户要看的是
        「哪一大段被改了、改了哪句」（见 `pdfdiff_group` 的模块注释）。
        段内条目数写进「规模」列，前后文压在「内容」列两端，全文在详情面板。

        2026-10-13 用户拍板：「为什么我总是能在那里见到白色的颜色？不应该，
        在那个 2 里面应该只会出现红色、绿色还有深蓝色才对」—— 白行来自
        「空的那一侧」：新增段只有新版有内容，旧版那行一直是近白底空串。
        现在空侧行**不再渲染**，一行只在一侧真有内容时才出现。
        """
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.paras = grp.group_paragraphs(self._filtered())
        for k, p in enumerate(self.paras):
            page = self._para_page(p)
            tk_ = p.kind if p.kind in ROW_OLD else "mixed"      # 没配色就退回默认
            lv = self._para_level(p)
            conf_txt = CONF_LABEL.get(lv, lv)
            tg = (f"{tk_}_o",) + (("low",) if lv == "low" else ())
            tg_n = (f"{tk_}_n",) + (("low",) if lv == "low" else ())
            txt_o, txt_n = grp.brief(p, "old"), grp.brief(p, "new")
            if txt_o:
                self.tree.insert("", "end", iid=f"p{k}o", tags=tg,
                                 values=(KIND_LABEL.get(p.kind, p.kind), "旧", conf_txt, page,
                                         p.scale, p.context, txt_o, p.note))
            if txt_n:
                self.tree.insert("", "end", iid=f"p{k}n", tags=tg_n,
                                 values=("" if txt_o else KIND_LABEL.get(p.kind, p.kind),
                                         "新", conf_txt, page, p.scale, p.context,
                                         txt_n, ""))

    @staticmethod
    def _para_level(p) -> str:
        """一段的档位 = 段内**最差**的那条。

        一段里只要有一处是估的，整段就提示人核 —— 用户是按「段」看的，
        把段内 9 条判得很准、1 条是蒙的混在一起显示成「高」，等于骗人。
        """
        its = list(getattr(p, "items", None) or ())
        if not its:                      # 传进来的是**单条**差异而不是「段」
            return core.conf_level_of(p)
        lv = "high"
        for it in its:
            l = core.conf_level_of(it)
            if l == "low":
                return "low"
            if l == "mid":
                lv = "mid"
        return lv

    @staticmethod
    def _para_page(p):
        """段的页码：段内旧/新页给出「1 → 2」；两侧都没有 = 文档属性。

        ⚠️ 取**段内第一个有页号的那条**，不能只看 `items[0]`（2026-09-30）。
        转曲区补录的条目只带新侧页号（它本来就没有旧版文字层），
        而一段里转曲区排在正文前面 —— 只看段首就会把「第1页的整段新增」
        显示成「- → 1」，同一页上别的段却是「1」，看着像两个地方。
        规则 1 保证了一段必在同一页，逐条扫不会引入歧义。
        """
        po = pn = None
        for it in p.items:
            if po is None:
                po = it.page_old
            if pn is None:
                pn = it.page_new
        if po is None and pn is None:
            return "属性"
        a = f"{po + 1}" if po is not None else "-"
        b = f"{pn + 1}" if pn is not None else "-"
        return f"{a} → {b}" if a != b else b

    def _sel_para(self, iid):
        """从结果表的行 id（`p3o` / `p3n`）取回那一段。"""
        try:
            k = int(str(iid)[1:-1])
        except (TypeError, ValueError):
            return None
        return self.paras[k] if 0 <= k < len(self.paras) else None

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel or not self.result:
            return
        para = self._sel_para(sel[0])
        if para is None:
            return
        prefix = sel[0][:-1]
        for old in self._sel_pair:
            if old and self.tree.exists(old):
                self.tree.item(old, tags=tuple(self.tree.item(old, "tags")[:1]))
        pair = (f"{prefix}o", f"{prefix}n")
        for p in pair:
            if self.tree.exists(p):
                self.tree.item(p, tags=tuple(self.tree.item(p, "tags")[:1]) + ("pair",))
        self._sel_pair = pair

        self._show_para(para)
        # 填完立刻滚到第一处改动 —— 长文本（整段条文）的改动常常在第几百个字，
        # 停在开头看上去就是「什么都没显示」。
        self._cur_span = 0
        self._apply_nav(para)
        self._goto(0)

    def _apply_nav(self, obj):
        """把「这一段到底是什么情况」写清楚，免得看着像误报。"""
        kind = getattr(obj, "kind", "")
        n = len(self._spans)
        if kind in _KIND_TIP:
            self.tip_var.set(_KIND_TIP[kind])
        elif not n:
            # 界面已经没有「忽略规则」可调了（2026-10-01 固定成一套），
            # 所以只能说清「为什么这一段的字符级看不出差别」，不能再让用户去调参数。
            self.tip_var.set("这一段是按「整段替换」报出来的：两侧内容在同一位置对不上，"
                             "但没有逐字可比的小改动 —— 看上面两栏的整段差异即可")
        else:
            self.tip_var.set(f"这一段里有 {n} 处字符级差异")
        # 追加「这条结论是怎么得出来的、有多可信」（2026-10-10 置信度层）。
        # 光说「有几处差异」不够 —— 用户要能判断**这条该不该信**。
        # 这正是本轮要治的根：引擎不再假装全能判定，没把握的地方直说。
        _tip = self.tip_var.get()
        _src = ""
        for _it in (getattr(obj, "items", None) or (obj,)):
            if getattr(_it, "src", ""):
                _src = _it.src
                break
        if _src:
            _tip += f"　·　来源：{core.CONF_SRC_TEXT.get(_src, _src)}"
        if self._para_level(obj) == "low":
            _tip += "　⚠ 把握低，请对照原文件核一眼"
        self.tip_var.set(_tip)
        self.nav_var.set(f"{self._cur_span + 1}/{n}" if n else "—")

    def _goto(self, delta):
        """跳到上一处 / 下一处改动（delta=0 = 跳到第一处）。只滚动，不加黄标。"""
        if not self._spans:
            for t in (self.txt_old, self.txt_new):
                t.see("1.0")
            self.nav_var.set("—")
            return
        self._cur_span = (self._cur_span + delta) % len(self._spans)
        i = self._cur_span
        o1, _o2, n1, _n2 = self._spans[i]
        for txt, at in ((self.txt_old, o1), (self.txt_new, n1)):
            txt.configure(state="normal")
            txt.see(at)          # 只有一侧有字时另一侧区间为空，滚到插入点即可
            txt.configure(state="disabled")
        self.nav_var.set(f"{i + 1}/{len(self._spans)}")

    def _highlight(self, obj):
        """兼容入口：传 `DiffItem`（回归脚本）或 `Para`（界面）都行。"""
        self._show_para(obj if isinstance(obj, grp.Para) else grp.Para.from_item(obj))

    def _fill_detail(self, txt, p: grp.Para, side: str):
        """把一段差异按「前文 / 本段 / 后文」**逐行**填进一栏，本段内做字符级高亮。

        返回该侧每一处差异的区间（`"1.0+Nc"`；空区间 = 这一侧没有对应字符），
        顺序与 `get_opcodes()` 的非 equal 项一一对应 —— 两侧各调一次再 zip，
        就是「◀ 1/3 ▶」要的 `(旧起, 旧止, 新起, 新止)`。

        为什么按行填而不是整坨 insert：上下文要压灰、本段要上红绿，
        得逐行知道角色（见 `grp.render_lines`）。高亮下标基于 `grp.body_text()` ——
        与插入的文本同一次拆行，不会错位。
        """
        txt.configure(state="normal")
        txt.delete("1.0", tk.END)
        for t in (grp.R_BEFORE, grp.R_AFTER, grp.R_RULE, grp.R_EMPTY, "del", "ins"):
            txt.tag_remove(t, "1.0", tk.END)
        pos = 0
        for role, s in grp.render_lines(p, side):
            txt.insert("end", s + "\n")
            if role != grp.R_BODY:              # 本段不上灰，留给红绿
                txt.tag_add(role, f"1.0+{pos}c", f"1.0+{pos + len(s)}c")
            pos += len(s) + 1

        off = grp.body_offset(p, side)
        sm = SequenceMatcher(None, grp.body_text(p, "old"), grp.body_text(p, "new"),
                             autojunk=False)
        spans: list[tuple[str, str]] = []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                continue
            a, b = (i1, i2) if side == "old" else (j1, j2)
            if b > a:
                # Tk 的 `1.0+Nc` 按**字符**算，跨行一样准。
                txt.tag_add("del" if side == "old" else "ins",
                            f"1.0+{off + a}c", f"1.0+{off + b}c")
            spans.append((f"1.0+{off + a}c", f"1.0+{off + b}c"))
        txt.configure(state="disabled")
        return spans

    def _show_para(self, p: grp.Para):
        """把这一段的「前文 / 本段 / 后文」填进两栏，并在本段内做字符级高亮。

        两侧用**同一份前后文**（没改的部分两版本来就一样），所以红绿只会落在
        段内那几句上 —— 「这一段里哪句是新加的、哪句被删了」一眼可见。
        这就是用户要的「再仔细标出来」那一层：明细是粗看，这里是细看。
        """
        so = self._fill_detail(self.txt_old, p, "old")
        sn = self._fill_detail(self.txt_new, p, "new")
        self._spans = [(a, b, c, d) for (a, b), (c, d) in zip(so, sn)]

    def _popup_detail(self):
        """双击一行 → 弹出这一段的大窗口（含前后文，全文可看可复制）。"""
        sel = self.tree.selection()
        if not sel or not self.result:
            return
        p = self._sel_para(sel[0])
        if p is None:
            return
        win = tk.Toplevel(self)
        win.title(f"差异详情 · {KIND_LABEL.get(p.kind, p.kind)}　·　{p.context}")
        win.geometry("840x580")
        win.configure(bg=BG)
        for title, color, side in (("旧版", "#a8071a", "old"), ("新版", "#166534", "new")):
            head = tk.Frame(win, bg=BG)
            head.pack(fill=tk.X, padx=14, pady=(12, 3))
            tk.Label(head, text=title, bg=BG, fg=color, font=(FONT, 12, "bold")).pack(side=tk.LEFT)
            sc = f"{p.n} 条差异　" if p.n > 1 else ""
            tk.Label(head, text=f"{sc}{p.context}　{p.note}", bg=BG, fg=TEXT2,
                     font=(FONT, 10)).pack(side=tk.LEFT, padx=10)
            t = tk.Text(win, wrap="word", font=(MONO, 12), relief="solid", bd=1,
                        padx=8, pady=8, height=8)
            t.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))
            self._style_text(t)
            self._fill_detail(t, p, side)       # 与详情面板同一套渲染，不会两处走样
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 12))

    def _preview(self):
        """在软件里直接看「改动前 / 改动后标出来的样子」。

        用户 2026-10-01 的要求（原话见 `diff_preview` 模块头）：
        把「导出标注版 PDF」那一步删掉 —— 它会往用户文件夹里落两份带时间戳的副本，
        员工得反复删文件、清内存。现在改成：**用原文件渲染到内存里显示**，
        不复制、不改写、不留伴生产物。

        ⚠️ 标记计算走 `ann.locate_marks()`，与（已从界面撤下的）落盘版
        `annotate_locate()` 共用 `_prepare` + `_side_marks` ——
        屏幕上看到的形状就是导出来会看到的形状，不会两个入口两个样。
        """
        if self.busy or not self.result or not self.items:
            messagebox.showinfo(APP_TITLE, "请先完成一次对比")
            return
        # Word 走文字流预览窗（docx 里没有「页」，渲染不出纸面图——见 diff_docview 模块头）
        if getattr(self, "_is_docx", False):
            self._preview_docx()
            return
        a, b = self.result.old_path, self.result.new_path
        self.busy = True
        self.btn_preview.config(state="disabled", text="准备中…")
        self.status_var.set("正在算标注位置（扫描件 / 转曲页要跑 OCR，稍慢）…")

        def work():
            try:
                import pdf_annotate as ann
                marks = ann.locate_marks(
                    a, b,
                    ignore_case=bool(_FIXED["ignore_case"][0]),
                    ignore_punct=bool(_FIXED["ignore_punctuation"][0]),
                    drop_header_footer=bool(_FIXED["ignore_header_footer"][0]),
                    prog=lambda i, t: self.q.put(("prog", i, t)))
                self.q.put(("preview_ready", marks))
            except Exception as e:
                self.q.put(("err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _preview_docx(self):
        """Word 的预览：文字流高亮窗（左右并排 + 同步滚动 + 红删/绿增）。

        与 PDF 预览同一套「只留一个窗」的规矩：开新前先关旧。
        文本在打开时一次性读进内存，原文件全程只读。
        """
        try:
            import diff_docview as dv
            if self._pv is not None:
                try:
                    self._pv.close()
                except Exception:
                    pass
                self._pv = None
            self._pv = dv.open_doc_preview(self, self.result.old_path,
                                           self.result.new_path, self.items)
            self._pv.lift()
            self.status_var.set(
                "Word 预览已打开　·　共 %d 处改动　·　原文件未被修改、未产生任何临时文件"
                "（预览窗里可翻处、同步滚动，关掉即释放）" % len(self.items))
        except Exception as e:
            self.status_var.set(f"预览失败：{e}")
            messagebox.showerror(APP_TITLE, f"预览失败：\n{e}")

    def _on_preview_ready(self, marks):
        """打开预览窗；只留一个预览窗，避免越点越多（那正是用户嫌「乱」的那件事）。"""
        self.busy = False
        self.btn_preview.config(state="normal", text="预览标注效果")
        try:
            import diff_preview as pv
            if self._pv is not None:
                try:
                    self._pv.close()
                except Exception:
                    pass
                self._pv = None
            self._pv = pv.open_preview(self, marks, self.result.old_path,
                                       self.result.new_path)
            self._pv.lift()
        except Exception as e:
            self.status_var.set(f"预览失败：{e}")
            messagebox.showerror(APP_TITLE, f"预览失败：\n{e}")
            return
        so = (marks.get("old") or {}).get("stats") or {}
        sn = (marks.get("new") or {}).get("stats") or {}
        no, nn = int(so.get("marked", 0)), int(sn.get("marked", 0))
        nd = len(marks.get("diffs") or [])
        if no + nn == 0:
            self.status_var.set("两份 PDF 没有可标注的改动（正文与转曲区都没算出差）")
            return
        self.status_var.set(
            f"标注预览已打开　·　共 {nd} 处改动　·　旧版标红 {no} 处 / 新版标绿 {nn} 处"
            f"　·　原文件未被修改、未被复制（预览窗里可缩放、翻页，关掉即释放）")

    def _export(self, kind):
        if not self.result or not self.items:
            messagebox.showinfo(APP_TITLE, "还没有对比结果可导出")
            return
        b1 = os.path.splitext(os.path.basename(self.result.old_path))[0]
        b2 = os.path.splitext(os.path.basename(self.result.new_path))[0]
        prefix = "Word差异" if getattr(self, "_is_docx", False) else "PDF差异"
        ts = time.strftime("%m%d_%H%M")
        meta = {"xlsx": ("Excel 工作簿", [("Excel", "*.xlsx")], core.export_excel),
                "csv": ("CSV", [("CSV", "*.csv")], core.export_csv),
                "json": ("JSON", [("JSON", "*.json")], core.export_json)}[kind]
        p = filedialog.asksaveasfilename(
            title=f"导出{meta[0]}", defaultextension=f".{kind}",
            initialfile=f"{prefix}_{b1}_vs_{b2}_{ts}.{kind}", filetypes=meta[1])
        if not p:
            return
        try:
            sub = core.Result(old_path=self.result.old_path, new_path=self.result.new_path,
                              items=self._filtered(), stats=core.summarize(self._filtered()),
                              elapsed=self.result.elapsed, pages_old=self.result.pages_old,
                              pages_new=self.result.pages_new, mode_used=self.result.mode_used)
            meta[2](sub, p)
            self.status_var.set(f"已导出：{p}（{len(sub.items)} 条差异）")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"导出失败：{e}")


if __name__ == "__main__":
    App().mainloop()
