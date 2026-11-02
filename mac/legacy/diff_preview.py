# -*- coding: utf-8 -*-
"""
diff_preview.py —— 在软件里直接看「改动前 / 改动后标出来的样子」
==================================================================
用户 2026-10-01 的原话：

> 「我如果真的想看的话，需要导出来，导到 PDF。员工跟我反馈说，导出来还需要反复删文件、
>   清内存，很麻烦。有没有什么办法，就是直接在软件上面就可以预览那个改变前、改变后
>   标记出来的样子？就是把原来的那个导出来的那一步给它删掉，变成了直接在软件当中
>   显示这个效果，利用原文件来显示这个效果……但是不改变原文件的任何东西，
>   也不复制原文件，这样子节省内存嘛。」

于是原来的「生成标注版 PDF → 落到用户文件夹 → 用系统阅读器打开」被替换成：

    左侧「旧版」 ‖ 右侧「新版」，同一页并排，改动位置直接高亮 —— 颜色按**改动性质**
    分两种（用户 2026-10-01 定，2026-10-13 删「变动」档，判据与配色都在
    `pdf_annotate`，这里只负责显示）：

        红 = 纯删减（只在旧版有）　绿 = 纯新增（只在新版有）
        （内容改写已拆成「删红 + 增绿」两条，不再有第三档黄）

    ⚠️ 颜色**不参与任何判定** —— 它只是把已经算出来的差异类型画成不同颜色。
       所以「换配色」这件事本身既不会让差异变多、也不会让差异变少。

三条硬约束（用户明确要求的，本模块的每个实现细节都为它们服务）：

1. **不落盘**。全程只有 `fitz.open(路径)` 只读打开 + `get_pixmap()` 渲染到内存，
   从不 `save()`、从不写临时文件、更不在用户文件夹里留伴生产物。
   渲完的位图编码成 PNG 直接塞给 Tk，闭窗即释放。
2. **不复制原文件**。没有任何 `shutil.copy` / `os.link`；同一份文件既给比对引擎读、
   也给这里渲染，用户磁盘上始终只有那两份原始 PDF。
3. **和导出的样子完全一致**。高亮矩形不是这里另算的 —— 走
   `pdf_annotate.locate_marks()`（与落盘版 `annotate_locate()` 共用
   `_prepare` + `_side_marks` 同一份计算），连「大段按行画」「页边标识行补全」
   「碎框合并」这些细节都一样。**屏幕上看到的，就是导出来会看到的那一份。**

⚠️ 三个踩过的坑，写在这儿免得重犯：
  · **画注释必须走 `pdf_annotate.paint_into()`，不能自己写循环**：
    写成 `doc[pi].add_highlight_annot(r)` 时那个 Page 是**临时对象**，表达式一结束
    就被 GC，紧接着的 `set_colors()` 抛 `annotation not bound to any page`；
    异常被 `except` 吞掉，而高亮**已经用默认黄色画上去了** —— 界面上就是
    「两边都是黄的」（用户 2026-10-01 报的正是这个）。`paint_into` 里先把 Page
    取到局部变量并钉住，自检还有「颜色真的落到注释上」的断言守着。
  · 页面带 `/Rotate` 时，`get_text()` 给的矩形是**未旋转**坐标，而 `get_pixmap()`
    渲染的是**旋转后**的画面 —— 两者之间必须过一道 `rect * page.rotation_matrix`。
    这里不用手动换算：注释是加在页面对象上的，PyMuPDF 自己会处理旋转
    （实测 rot=90 的高亮像素位置正好等于 `rect × rotation_matrix`）。
  · Tk 的 `PhotoImage` 必须留引用，否则被 GC 掉后画布上是一片空白
    （见 `_imgs` / `_cache`）。Tk 9 直接吃 PNG，不需要 Pillow。

清晰度 + 「一眼看全」（2026-10-01 用户三条反馈叠在一起，取舍如下）：
  ① 默认档由**画布宽度反算**（`_fit_first_frame` / `_on_canvas_resize`）：
     真实 WI 是 842×595 的横版 A4，写死 180dpi 出图有 2105px 宽，而两栏每栏
     画布只有 ~640px —— 用户看到的就是「只截取了文件的一小部分」。
     按宽度算出来通常是 40~60dpi，整页横竖都进得来。
  ② 档位表 `PAGE_DPI_STEPS` 最低到 40dpi（横版 A4 铺进 640px 栏 ≈ 55dpi），
     渲染结果按 `(侧, 页, dpi, 标注开关)` 缓存（`_cached_pixmap`，最多 8 张）。
  ③ **高分屏超采样：本机 Tk 走不通，已按能力探测关掉** ——
     Tk 在 Retina 上把 1 个图像像素铺成 2 个物理像素（按 1x 渲染的图一定是被
     放大的）；而「按 2 倍像素出图、再缩回逻辑尺寸」唯一能用的
     `PhotoImage.configure(width=, height=)` 在 Tk 9.0 上是**裁切不是缩放**
     （实测 400×400 → 200×200 后只剩左上角，见 `photo_can_scale()`）。
     上一版没探这一步，于是每页被**切成了左上角四分之一** —— 用户 2026-10-01
     报的「依旧没有显示完全这份 PDF」就是它。
     现在：能缩放（未来某个 Tk 版本）才超采样，不能就按逻辑 dpi 渲染。
     自检里有一条**内容级**探针（页面右下角画黑方块）守着 ——
     ⚠️ 只查位图宽高是**查不出来**的：裁切是把数据截断成目标尺寸，两种情况
        位图都是 1050×750，一模一样。
     ⚠️ 代价要说实话：**在 Tk 里 Retina 位图的清晰度上限就是「1 个数据像素对
        1 个逻辑点」**（屏幕上相当于放大一倍）。想看更细只有两条路 ——
        点 ＋ 放大（数据像素变多，字变大），或者干脆导出去用系统阅读器看。
  ④ 想放大看细节就点 ＋ / 拖滚动条；「整页」按钮 = 横竖都要塞得下（竖版 A4 用），
     右侧那颗按钮写的就是它的档位名（`180 dpi`）。

布局：
  ┌ 工具条：页码 ◀ 1/2 ▶ ｜ 缩放 − 55 dpi · 76% + ｜ 适应宽度 ｜ 整页 ｜ 180 dpi
  │        ｜ ☑显示标注 ｜ ●删减 ●新增 ●位移 ●字体 ｜ 关闭 ┐
  ├ 旧版 PDF  删减 1 ‖ 新版 PDF  新增 1                                ┤
  └ 状态行：共 N 处标记 + 操作提示                                        ┘
左右两块**联动滚动**（同一页同一位置，眼睛不用来回找），鼠标滚轮、拖拽平移、
方向键翻页、+/− 缩放都支持。窗口一变形就重新「铺满整栏」（用户没手动选过档位时）。

⚠️ 颜色图例只放在工具条上，**不写进左右栏标题**：旧版侧＝删减、新版侧＝新增，
   栏标题右侧只报**这一侧各色有几处**。
"""

from __future__ import annotations

import base64
import os
import sys
import tkinter as tk
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

try:
    import pymupdf as fitz
except ImportError:                       # pragma: no cover
    import fitz                           # type: ignore

# 画法与**配色**都和标注模块共用同一份（`paint_into` / `COLOR_OF`）——
# 绝不在这里另写一套，那正是「预览有黄、导出没黄」这类偏差的来源。
import pdf_annotate as ann

FONT = "PingFang SC"

# 配色（用户 2026-10-01 定，2026-10-13 删「变动」档）：
#   红 = 纯删减　绿 = 纯新增（内容改写已拆成删+增两条，不再有第三档黄）
# ⚠️ 旧版那侧只有「删减」，新版那侧只有「新增」。这几个十六进制只用于
#    **界面文字/图例**，页面上的高亮颜色一律由 `ann.COLOR_OF` 决定，
#    别在这里各写一份。
BG = "#f4f5f7"
CARD = "#ffffff"
BORDER = "#e3e6ea"
TEXT = "#1f2328"
TEXT2 = "#656d76"
HEADBG = "#eef1f5"
RED_TXT = "#a8071a"       # 红字（深一档，白底上读得清）
GREEN_TXT = "#166534"
BLUEL_TXT = "#4a90d9"     # 浅蓝（位移）图例字色
BLUED_TXT = "#3b4fc4"     # 深蓝（字体）图例字色
PRIMARY = "#2563eb"

#: 工具条上的颜色图例：`(中文名, 那颗点的颜色)`。文字取自 `ann.KIND_TEXT`、
#: 顺序对齐 `ann.KIND_ORDER`，所以改一处就够。
#: 2026-10-12 五色体系 → 2026-10-13 删「变动」档后剩四色：
#: 图例从纯展示升级成**勾选开关**（见 _build），
#: 删减/新增/字体默认开，位移默认关（`ann.KIND_DEFAULT_ON`，2026-10-13）。
LEGEND = tuple(
    (k, ann.KIND_TEXT[k], c) for k, c in (
        (ann.KIND_DEL, RED_TXT), (ann.KIND_ADD, GREEN_TXT),
        (ann.KIND_MOV, BLUEL_TXT),
        (ann.KIND_FONT, BLUED_TXT)))

# 缩放档位（渲染 dpi）。
# ⚠️ 2026-10-01 用户反馈「分辨率有点低，可以拉高一些」：默认从 110 提到 **180**，
#    档位上限从 260 提到 **560**（页脚那种 6pt 小字、标签上的点阵码都看得清）。
#    代价是位图变大 —— 所以渲染结果按 (侧, 页, dpi, 标注开关) 缓存，见 `_cached_pixmap`。
#
# ⚠️ 同一天用户又反馈「没有全局显示，只截取了文件的一小部分」：真实 WI 是
#    **842×595 的横版 A4**，180dpi 下位图宽 2105px，而两栏每栏画布只有 ~640px
#    —— 一眼只能看到左边三成。默认档必须由**画布宽度**算，不能写死。
#    于是低档位也要有（横版 A4 铺进 640px 栏 ≈ 55dpi），`FIT_DPI_MIN` 从 80 降到 40。
PAGE_DPI_STEPS = (40, 55, 80, 110, 140, 180, 240, 320, 420, 560)
#: 「实际大小」按钮用的档（72dpi 的 2.5 倍，一屏能看清正文与页脚小字）
DEFAULT_DPI = 180
#: 首帧还没量到画布宽时先按这个渲染（量到之后立刻由 `_fit_first_frame` 改正）
BOOT_DPI = 110
# 「适应宽度 / 整页」允许的 dpi 范围（太小看不清、太大反而要横向拖）
FIT_DPI_MIN, FIT_DPI_MAX = 40, 560
# 首帧自检重试次数（窗口要等 Tk 布局完才量得到画布宽）
FIT_RETRY, FIT_RETRY_MS = 20, 60
# 窗口变形后重新「铺满整栏」的防抖（拖窗口时别每帧都重渲）
FIT_RESIZE_MS = 160
# 「适应宽度」要求画布至少有这么宽，否则说明窗口还没布局出来 —— 别照着 1px 算 dpi
FIT_CANVAS_MIN = 160
# 渲染缓存张数（PhotoImage 很占内存，别无限攒）
RENDER_CACHE_MAX = 8

# ---------------------------------------------------------------- 高分屏
# 目标：在 Retina（2x）屏上别把页面画成「放大一倍的糊图」。
#
# 两条**实测出来的事实**（2026-10-01，Tk 9.0 / macOS，这台机器）：
#   ① Tk 把 **1 个图像像素铺成 2 个物理像素**（图像像素 = 逻辑点），所以按 1x
#      渲染的位图在 2x 屏上一定是被放大过的 —— 页脚那种 6pt 小字首当其冲。
#   ② 想把「按 2 倍像素出的图」缩回逻辑尺寸，只能靠
#      `PhotoImage.configure(width=, height=)` —— 而 Tk 9.0 的这个选项是
#      **裁切，不是缩放**：把 400×400 的四色图 configure 成 200×200 之后，
#      整张图只剩左上角那 1/4（四个探针点全读回左上角的颜色）。
#   ⇒ 上一版据此实现的「超采样」于是把每一页**切成了左上角四分之一**，用户
#     看到的就是「没有全局显示，只截取了文件的一小部分」。
#
# 所以超采样必须先**探测**这台 Tk 到底会不会缩放（`photo_can_scale()`）：
#   会（缩）→ 按屏幕倍率出数据、缩回逻辑尺寸：画面不变、细节 ×N；
#   不会（裁）→ 老实按逻辑 dpi 渲染：画面完整，细节交给 Tk 自己放大。
# 探测结果按进程缓存，自检里还会拿一张新图**独立复核**这个结论。
# ⚠️ 防这道坑**不能靠查宽高**：`configure` 是把数据**截断**到目标尺寸，所以
#    「正常」和「被裁掉 3/4」两种情况的位图尺寸一模一样（实测都是 1050×750）。
#    自检因此另有一条**内容级**探针：在页面右下角画个黑方块，看渲染结果里还在不在。
#
# ⚠️ 渲染 dpi 无论走哪条路都要封顶：超采样是平方级吃内存的
#    （A4 @ 600dpi ≈ 4960×7016 像素），高档位再乘 2 会直接把内存吃光。
HIDPI_MIN, HIDPI_MAX = 1, 3
RENDER_DPI_MAX = 600
#: `photo_can_scale()` 的缓存（None = 还没探过）
_PHOTO_SCALES: Optional[bool] = None


def _rgb(v) -> tuple:
    """把 `PhotoImage.get()` 的返回值统一成 `(r, g, b)`。

    Tk 8 给 `"#rrggbb"` 字符串、Tk 9 给 `(r, g, b)` 元组，两边都得认。
    """
    if isinstance(v, (tuple, list)):
        return tuple(int(x) for x in v[:3])
    s = str(v).strip()
    if s.startswith("#") and len(s) >= 7:
        return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))
    return ()


def photo_can_scale() -> bool:
    """这台 Tk 的 `PhotoImage.configure(width=, height=)` 是**缩放**还是**裁切**？

    探法（纯读回像素，不需要窗口、不需要截图，确定性）：

      造一张 8×8 的图 —— 整体红色、右下 4×4 涂绿；把它 configure 成 4×4：
        · **缩放** → 4×4 的 (3,3) 取到的是原图右下的绿；
        · **裁切** → 只剩原图左上角那 4×4，全是红。

      两个语义必然给出不同像素值，所以读一个点就能判死。
      （⚠️ 别用「读越界会报错」来判：越界与否是实现细节，两种语义都可能报错。）

    探测失败（没有 Tk、建不出图）时返回 `False` —— **保守等于不裁图**，
    宁可不高清也不能把页面切掉。
    """
    global _PHOTO_SCALES
    if _PHOTO_SCALES is None:
        _PHOTO_SCALES = False
        try:
            img = tk.PhotoImage(width=8, height=8)
            img.put("#ff0000", to=(0, 0, 8, 8))
            img.put("#00ff00", to=(4, 4, 8, 8))
            img.configure(width=4, height=4)
            _PHOTO_SCALES = _rgb(img.get(3, 3)) == (0, 255, 0)
        except Exception:
            _PHOTO_SCALES = False
    return bool(_PHOTO_SCALES)


def backing_scale() -> int:
    """屏幕每个逻辑像素对应几个物理像素（Retina = 2）。测不到就返回 1。

    ⚠️ 只能问系统，别指望 Tk：`winfo_fpixels('1i')` 在 Retina 上照样报 96、
       `tk scaling` 也是 1.33 —— 从 Tk 这边根本看不出这是块 2x 屏。
    ⚠️ 也**别用 `CGDisplayPixelsWide`**：它报的是「当前显示模式的点数」，
       缩放模式下在本机实测返回 1680（等于点宽），比值恒为 1 —— 白忙一场。
       正解是拿**同一个 display mode** 的两个宽：像素宽 ÷ 点宽（实测 3360/1680 = 2）。
       不引 pyobjc，`ctypes` 直接问 CoreGraphics 就够；失败也只是退回老行为。
    """
    if sys.platform != "darwin":
        return 1
    try:
        import ctypes
        import ctypes.util

        cg = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
        cg.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
        for _fn in ("CGDisplayModeGetWidth", "CGDisplayModeGetPixelWidth"):
            _f = getattr(cg, _fn)
            _f.restype = ctypes.c_size_t
            _f.argtypes = [ctypes.c_void_p]
        mode = cg.CGDisplayCopyDisplayMode(cg.CGMainDisplayID())
        if mode:
            pts = float(cg.CGDisplayModeGetWidth(mode))
            px = float(cg.CGDisplayModeGetPixelWidth(mode))
            try:
                cg.CGDisplayModeRelease.argtypes = [ctypes.c_void_p]
                cg.CGDisplayModeRelease(mode)
            except Exception:
                pass
            if pts > 0:
                n = int(round(px / pts))
                if HIDPI_MIN <= n <= HIDPI_MAX:
                    return n
    except Exception:
        pass
    return 1

# 「显示标注」那个勾选框是自绘的 —— 从 `diff_app` 搬过来的（那边勾选框随
# 「② 对比选项」「筛选」一起删光了，这里是全项目唯一还需要勾选框的地方）。
_CHECK_IMGS: List[Any] = []       # PhotoImage 必须留引用，否则勾选框会变空白


def _check_image(on: bool, bg: str = HEADBG):
    """自绘勾选框：未勾=空框，勾上=蓝底白勾。

    clam 主题自带的 indicator **选中时画的是一个叉**，看着像「打叉=不显示」，
    语义完全是反的 —— 所以自己画，选中/未选中一眼可分。
    ⚠️ `PhotoImage.put` 画**单像素行/列**（y1==y2 的退化矩形）不生效 ——
    边框必须用「先整块边框色、再叠一块缩进一圈的底色」这种两层画法。
    """
    size, bd = 14, "#b9c0c9"
    img = tk.PhotoImage(width=size, height=size)
    if on:
        img.put(PRIMARY, to=(0, 0, size - 1, size - 1))
        for x, y in ((3, 6), (4, 7), (5, 8), (6, 7), (7, 6), (8, 5), (9, 4), (10, 3)):
            img.put("#ffffff", to=(x, y, x + 1, y + 1))
    else:
        img.put(bd, to=(0, 0, size - 1, size - 1))          # 外圈 = 边框色
        img.put("#ffffff", to=(1, 1, size - 2, size - 2))   # 内里 = 白
    for x, y in ((0, 0), (size - 2, 0), (0, size - 2), (size - 2, size - 2)):
        img.put(bg, to=(x, y, x + 1, y + 1))       # 四角涂成底色，看着像圆角
    return img


def _install_check_style(st: ttk.Style, bg: str = HEADBG):
    """把 `Pv.TCheckbutton` 换成自绘 indicator（换不了就退回主题自带的，不影响能用）。"""
    try:
        off_img, on_img = _check_image(False, bg), _check_image(True, bg)
        _CHECK_IMGS.extend((off_img, on_img))
        st.element_create("Pv.Checkbutton.indicator", "image", on_img,
                          ("!selected", off_img))
        st.layout("Pv.TCheckbutton", [
            ("Checkbutton.padding", {"children": [
                ("Pv.Checkbutton.indicator", {"side": "left"}),
                ("Checkbutton.label", {"side": "left"}),
            ], "sticky": "nswe"})])
        st.configure("Pv.TCheckbutton", background=bg, foreground=TEXT,
                     font=(FONT, 10), padding=(0, 0))
        st.map("Pv.TCheckbutton", background=[("active", bg)])
    except tk.TclError:
        pass


class PreviewWindow(tk.Toplevel):
    """标注预览窗口。

    `marks` 是 `pdf_annotate.locate_marks()` 的返回值（只算不落盘的那一份）。
    窗口持有两份 `fitz.Document`（只读）—— 关窗才 close，期间反复缩放/翻页
    都只是重新渲染，不会碰磁盘。
    """

    def __init__(self, master, marks: Dict[str, Any],
                 old_name: str = "旧版", new_name: str = "新版"):
        super().__init__(master)
        self.title("标注预览 — " + old_name + "　‖　" + new_name)
        self.configure(bg=BG)
        self.minsize(760, 480)
        self.geometry("%dx%d" % self._default_size(master))

        self.marks = marks or {}
        self.docs: Dict[str, Any] = {}
        self._imgs: Dict[str, Any] = {}          # ⚠️ PhotoImage 必须留引用
        self._cache: Dict[tuple, Any] = {}       # 渲染结果缓存（键见 _cached_pixmap）
        self.canv: Dict[str, tk.Canvas] = {}
        self._bars: Dict[str, ttk.Scrollbar] = {}
        self._hbars: Dict[str, ttk.Scrollbar] = {}
        self.lb_head: Dict[str, tk.Label] = {}   # 栏标题右侧的「各色几处」
        self._bscale: Optional[int] = None       # 屏幕倍率（懒探测，见 `_scale`）
        self._syncing = False                    # 联动滚动防回环
        self._drag: Optional[tuple] = None
        self._keep_view: Optional[tuple] = None   # 缩放前后保持视角
        # 「自动铺满整栏」：用户还没自己选过缩放时才为真。窗口一变形就重算 ——
        # 首帧那次测量可能是布局完成前的旧值（实测落到过下限 40dpi，页面小得离谱），
        # 绑在 <Configure> 上就不会被一次偏差永久钉死。
        self._auto_fit = True
        self._fit_job: Optional[str] = None

        self.page = 0
        self._win_w, self._win_h = self._default_size(master)
        self.dpi = BOOT_DPI                      # `_add_annots()` 之后会被 `_boot_dpi()` 覆盖
        st_o = ((self.marks.get("old") or {}).get("stats") or {})
        st_n = ((self.marks.get("new") or {}).get("stats") or {})
        self.n_o, self.n_n = int(st_o.get("marked", 0)), int(st_n.get("marked", 0))
        pg_o = int((self.marks.get("old") or {}).get("pages") or 0)
        pg_n = int((self.marks.get("new") or {}).get("pages") or 0)
        self.pages = max(1, pg_o, pg_n)
        self.page_count = {"old": pg_o, "new": pg_n}

        # 四颗分类开关（2026-10-13 删「变动」档）：删减/新增/字体默认开，位移默认关。
        # 切换 → `_on_kind_toggle` 重画注释 + 清渲染缓存。
        # （「显示标注」总开关已按用户要求移除 —— 标注永远显示。）
        self.kind_on = {k: tk.BooleanVar(value=(k in ann.KIND_DEFAULT_ON))
                        for k in ann.KIND_ORDER}
        self._build()
        self._bind_keys()
        self._add_annots()
        # 文档已开好 → 知道页面多宽，先估一档，别按写死档位渲一张会被裁的大图
        self.dpi = self._boot_dpi()
        self.render()
        # 首帧渲染完再看一眼：**按画布宽把缩放调到铺满整栏**，保证第一眼看到的是
        # 整页而不是被裁的局部（窗口要等 Tk 布局完才量得到宽度，所以带重试）。
        self.after_idle(self._fit_first_frame)
        self.protocol("WM_DELETE_WINDOW", self.close)
        if master is not None:
            try:
                self.transient(master)
            except Exception:
                pass

    def _page_pt(self) -> Optional[Tuple[float, float]]:
        """当前页的**显示尺寸**（pt）。`page.rect` 已经把 `/Rotate` 算进去了。"""
        doc = self.docs.get("new") or self.docs.get("old")
        if doc is None or self.page >= doc.page_count:
            return None
        r = doc[self.page].rect
        return (float(r.width) or 595.0, float(r.height) or 842.0)

    def _fit_dpi(self, whole: bool = False) -> Optional[int]:
        """算「刚好铺满这一栏」的 dpi；画布还没布局好就返回 None。

        `whole=True` 时横竖**都要**塞得下（竖版 A4 用「整页」按钮看全篇）。
        ⚠️ 夹在 `FIT_DPI_MIN..FIT_DPI_MAX`：上下限是防呆（0 或负数会让
           `get_pixmap` 直接抛错；过大的 dpi 只是白吃内存）。
        """
        try:
            cw = self.canv["new"].winfo_width() - 8
            ch = self.canv["new"].winfo_height() - 8
        except Exception:
            return None
        if cw < FIT_CANVAS_MIN:
            return None
        pt = self._page_pt()
        if pt is None:
            return None
        pw, ph = pt
        d = cw / pw * 72.0
        if whole and ch > FIT_CANVAS_MIN:
            d = min(d, ch / ph * 72.0)
        return int(min(max(int(round(d)), FIT_DPI_MIN), FIT_DPI_MAX))

    def _fit_first_frame(self, left: int = FIT_RETRY):
        """首帧把缩放调到「整页铺得下」，而不是留一个被裁的局部。

        ⚠️ 2026-10-01 用户原话：「这个文件在那里面，基本上它没有全局显示，
           它好像只截取了文件的一小部分」。起因是默认档写死 180dpi，而真实 WI
           是 **842×595 的横版 A4** —— 180dpi 下位图宽 2105px，两栏每栏画布
           只有 ~640px，一眼只能看到左边三成。
        ⚠️ 同一天早些时候我还**反着**改过一次（宁可让用户横向拖也不缩），
           那是因为当时还没有高分屏超采样：缩到 55dpi 就是缩糊。现在超采样到位了
           （55dpi 逻辑 → 110dpi 数据 → 缩回逻辑尺寸显示 = 1:1 物理像素），
           「铺满整栏」既看得全、又是这台屏幕上能有的最高清晰度 —— 两条不再冲突。
        ⚠️ 只在窗口**真的显示出来**之后才动：自检 / 回归里窗口是 `withdraw()` 的，
           `winfo_width()` 只能给出 1，照着算会把 dpi 一路夹到下限。
           所以量不到就重试，重试次数用完就保持 `BOOT_DPI`（**不猜**）。
           —— 而且这条**不是一次性的**：窗口真正显示、布局稳定后，`<Configure>`
           会再来一次（`_on_canvas_resize`），把首帧量到的旧值纠正掉。
        """
        try:
            if not self.winfo_ismapped():
                if left > 0:
                    self.after(FIT_RETRY_MS, lambda: self._fit_first_frame(left - 1))
                return
            want = self._fit_dpi(whole=False)
            if want:
                self._apply_dpi(want)
        except Exception:
            pass

    # ---------------------------------------------------------------- 构造
    @staticmethod
    def _default_size(master) -> Tuple[int, int]:
        try:
            sw, sh = master.winfo_screenwidth(), master.winfo_screenheight()
        except Exception:
            sw, sh = 1440, 900
        w = min(1360, max(820, sw - 120))
        h = min(920, max(560, sh - 140))
        return w, h

    def _boot_dpi(self) -> int:
        """开窗前先按**预估画布宽**挑一档。

        ⚠️ 不这么做的话，首帧会按写死的 `BOOT_DPI` 渲一张被裁的大图，
           等 Tk 布局完（~100ms）再重渲一次 —— 用户看到画面「先裁后缩」闪一下，
           而且白多渲一张大位图（A4 横版 110dpi ≈ 1286×908）。
           这里用窗口的目标宽度反算，落点基本就是 `_fit_first_frame` 的最终值。
        """
        pt = self._page_pt()
        if pt is None:
            return BOOT_DPI
        col = max(1.0, (float(self._win_w) - 48.0) / 2.0)   # 两栏 + 滚动条 + 边距
        return int(min(max(int(round(col / pt[0] * 72.0)), FIT_DPI_MIN), FIT_DPI_MAX))

    def _build(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Pv.TButton", font=(FONT, 10), padding=(7, 2),
                     background=CARD, bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
        st.map("Pv.TButton", background=[("active", "#f0f2f5")])
        _install_check_style(st, HEADBG)

        bar = tk.Frame(self, bg=HEADBG)
        bar.pack(fill=tk.X)
        tk.Label(bar, text="页码", bg=HEADBG, fg=TEXT2, font=(FONT, 10)).pack(
            side=tk.LEFT, padx=(10, 3), pady=5)
        self.btn_prev = ttk.Button(bar, text="◀", width=2, style="Pv.TButton",
                                   command=lambda: self.goto(-1))
        self.btn_prev.pack(side=tk.LEFT)
        self.lb_page = tk.Label(bar, text="1/1", bg=HEADBG, fg=TEXT,
                                font=(FONT, 10, "bold"), width=7)
        self.lb_page.pack(side=tk.LEFT)
        self.btn_next = ttk.Button(bar, text="▶", width=2, style="Pv.TButton",
                                   command=lambda: self.goto(1))
        self.btn_next.pack(side=tk.LEFT)

        tk.Frame(bar, bg=BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=4)
        tk.Label(bar, text="缩放", bg=HEADBG, fg=TEXT2, font=(FONT, 10)).pack(side=tk.LEFT)
        ttk.Button(bar, text="−", width=2, style="Pv.TButton",
                   command=lambda: self.zoom(-1)).pack(side=tk.LEFT, padx=(4, 0))
        self.lb_zoom = tk.Label(bar, text="", bg=HEADBG, fg=TEXT, font=(FONT, 10), width=13)
        self.lb_zoom.pack(side=tk.LEFT)
        ttk.Button(bar, text="+", width=2, style="Pv.TButton",
                   command=lambda: self.zoom(1)).pack(side=tk.LEFT)
        ttk.Button(bar, text="适应宽度", style="Pv.TButton",
                   command=self.fit_width).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(bar, text="整页", style="Pv.TButton",
                   command=self.fit_page).pack(side=tk.LEFT, padx=(4, 0))
        # （「180 dpi」快捷键与「显示标注」勾选框已按用户要求移除 ——
        #   档位用 −/+/适应宽度/整页就够，标注永远显示。）

        # —— 颜色图例（兼分类开关）——
        # ⚠️ 图例放在这里、**不写进左右栏标题**：两侧都可能出现多种颜色，
        #    标题里写「旧版＝红」会把人带偏。
        # 2026-10-12 五色体系：每颗图例是一颗**勾选框**——勾掉即隐藏该类标记
        # （含图像/转曲区标记，见 `ann.filter_kinds`）。默认开闭见 `KIND_DEFAULT_ON`。
        tk.Frame(bar, bg=BORDER, width=1).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=4)
        for _k, _txt, _col in LEGEND:
            tk.Label(bar, text="●", bg=HEADBG, fg=_col, font=(FONT, 10)).pack(
                side=tk.LEFT, padx=(7, 0))
            ttk.Checkbutton(bar, text=_txt, variable=self.kind_on[_k],
                            command=self._on_kind_toggle,
                            style="Pv.TCheckbutton").pack(
                side=tk.LEFT, padx=(1, 0))

        ttk.Button(bar, text="关闭", style="Pv.TButton",
                   command=self.close).pack(side=tk.RIGHT, padx=(0, 10))
        tk.Label(bar, text="滚轮/拖拽平移 · ← → 翻页",
                 bg=HEADBG, fg="#9aa1a9", font=(FONT, 9)).pack(side=tk.RIGHT, padx=(0, 12))

        # —— 左右两块（旧版 / 新版）——
        mid = tk.Frame(self, bg=BG)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=(6, 0))
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)
        mid.rowconfigure(1, weight=1)
        # 栏标题只说「这是哪一版」，颜色含义交给工具条上的图例 ——
        # 旧版只有「删减红」、新版只有「新增绿」，标题里不再写「红＝删除」。
        for col, side in enumerate(("old", "new")):
            head = tk.Frame(mid, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            head.grid(row=0, column=col, sticky="ew",
                      padx=(0, 4) if col == 0 else (4, 0))
            tk.Label(head, text="旧版 PDF" if side == "old" else "新版 PDF",
                     bg=CARD, fg=TEXT, font=(FONT, 10, "bold")).pack(
                side=tk.LEFT, padx=(8, 0), pady=3)
            self.lb_head[side] = tk.Label(head, text="", bg=CARD, fg=TEXT2,
                                          font=(FONT, 9))
            self.lb_head[side].pack(side=tk.RIGHT, padx=(0, 8))

            wrap = tk.Frame(mid, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            wrap.grid(row=1, column=col, sticky="nsew",
                      padx=(0, 4) if col == 0 else (4, 0), pady=(3, 0))
            wrap.rowconfigure(0, weight=1)
            wrap.columnconfigure(0, weight=1)
            cv = tk.Canvas(wrap, bg="#d9dde2", highlightthickness=0, takefocus=1)
            vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL,
                                command=lambda s=side, *a: self._scroll(s, *a))
            hsb = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=cv.xview)
            cv.configure(yscrollcommand=lambda *a, s=side: self._from_bar(s, *a),
                         xscrollcommand=lambda *a, s=side: self._from_hbar(s, *a))
            cv.grid(row=0, column=0, sticky="nsew")
            # ⚠️ 滚动条精简（2026-10-10）：左右两栏**本就联动滚动**（`_sync_scrollbars`
            #   把 x/y 一起对齐），每栏各配一套滚动条是纯冗余、视觉上还显乱
            #   （中间一根竖条 + 右边一根竖条 + 底部两根横条）。改成**只在右侧栏
            #   （新版，col==1）显示一套**（一根竖条 + 一根横条），拖动它经
            #   `_scroll`/`_from_bar`/`_from_hbar` → `_sync_scrollbars` 自动驱动两栏。
            #   ⚠️ 旧版栏的 vsb/hsb **仍创建、仍进 `_bars`/`_hbars` dict**（自检与
            #   同步逻辑都按 dict 取用），只是不再 grid 显示 —— 逻辑零改动、纯视觉精简。
            if side == "new":
                vsb.grid(row=0, column=1, sticky="ns")
                hsb.grid(row=1, column=0, sticky="ew")
            cv.bind("<Configure>", self._on_canvas_resize)
            cv.bind("<MouseWheel>", lambda e, s=side: self._wheel(s, e))
            cv.bind("<Shift-MouseWheel>", lambda e, s=side: self._wheel_h(s, e))
            # macOS 触控板横向双指滑走的是 <MouseWheel> 里 state 带 Shift 的事件，
            # 有的 Tk 版本则发 <Button-6>/<Button-7>（旧鼠标的左右滚轮）——
            # 两个都接上，横向滚动才不会只在某些机器上失灵。
            cv.bind("<Button-6>", lambda e, s=side: self._wheel_h(s, e))
            cv.bind("<Button-7>", lambda e, s=side: self._wheel_h(s, e))
            cv.bind("<ButtonPress-1>", self._drag_start)
            cv.bind("<B1-Motion>", self._drag_move)
            cv.bind("<ButtonRelease-1>", lambda *_a: setattr(self, "_drag", None))
            self.canv[side] = cv
            self._bars[side] = vsb
            self._hbars[side] = hsb

        # —— 状态行 ——
        self.status = tk.Label(
            self, bg=HEADBG, fg=TEXT2, anchor="w", padx=10, pady=3, font=(FONT, 10),
            text="")
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

    def _bind_keys(self):
        # ⚠️ 事件回调一律写成 `lambda *_a:` —— Tk 9 在窗口销毁/事件重派发的
        #    偶发时刻会**不带参数**地调用已注册的回调（smoke_gui 实测 ~1/3 概率
        #    抛 `missing 1 required positional argument: '_e'`），带死参数的
        #    lambda 会在这时炸出 "Exception in Tkinter callback"。
        self.bind("<Escape>", lambda *_a: self.close())
        self.bind("<Left>", lambda *_a: self.goto(-1))
        self.bind("<Right>", lambda *_a: self.goto(1))
        for k in ("<plus>", "<equal>", "<KP_Add>"):
            self.bind(k, lambda *_a: self.zoom(1))
        for k in ("<minus>", "<KP_Subtract>"):
            self.bind(k, lambda *_a: self.zoom(-1))

    # ---------------------------------------------------------------- 注释
    def _enabled_kinds(self) -> tuple:
        """当前勾开的颜色桶（按 KIND_ORDER 钉序 —— 也用作渲染缓存 key 的一部分）。"""
        return tuple(k for k in ann.KIND_ORDER if self.kind_on[k].get())

    def _on_kind_toggle(self):
        """分类开关切换：注释要**重画**（内存文档里的旧注释摘不掉，整个重开），
        渲染缓存按旧开关组合存的全作废。"""
        self._cache.clear()
        self._add_annots()
        self.render()

    def _add_annots(self):
        """把标记矩形加进**内存里的**文档对象（原文件一个字节都不动）。

        画法就是落盘版那一份 —— `pdf_annotate.paint_into()`，所以屏幕上的高亮
        形状、颜色与导出 PDF 里的注释是同一种东西（连配色常量都共用）。

        ⚠️ 这里**不能**自己写 `doc[pi].add_highlight_annot(r)` 再设色：那个 Page
           是临时对象，表达式一结束就被 GC，紧跟着的 `set_colors` 抛
           `annotation not bound to any page`，异常被吞掉 → 注释带着**默认黄色**
           留在页面上。用户 2026-10-01 报的「两个颜色都是黄的」就是这条。
        ⚠️ 2026-10-12 分类开关：本方法可**反复调用**（每次开关切换都来一遍），
           所以先把旧文档关掉重开 —— PDF 注释一旦画上就摘不掉，只能重开干净文档
           按当前勾选的桶重画。过滤走 `ann.filter_kinds`（图像/转曲区标记同样受控）。
        """
        enabled = self._enabled_kinds()
        for side, fallback in (("old", ann.RED), ("new", ann.GREEN)):
            old_doc = self.docs.pop(side, None)
            if old_doc is not None:
                try:
                    old_doc.close()
                except Exception:
                    pass
            path = (self.marks.get(side) or {}).get("path")
            if not path or not os.path.isfile(path):
                continue
            try:
                doc = fitz.open(path)
            except Exception:
                continue
            try:
                # `paint_into` 见到 `by_kind` 就按桶分色画（先经 filter_kinds
                # 滤掉没勾的桶）；`fallback` 只在不带 by_kind 的老结构上兜底。
                ann.paint_into(doc, ann.filter_kinds(self.marks.get(side), enabled),
                               fallback)
            except Exception:
                pass                      # 画不上不该毁掉整个预览
            self.docs[side] = doc

    # ---------------------------------------------------------------- 渲染
    def render(self):
        """按当前页 / 当前缩放重画两侧。**只读渲染，不写盘。**"""
        self.configure(cursor="watch")
        try:
            self.update_idletasks()
            keep = self._keep_view
            self._keep_view = None
            for side in ("old", "new"):
                self._render_side(side, keep)
            self._update_labels()
            self._sync_scrollbars()
        finally:
            self.configure(cursor="")
            self.focus_set()

    def _scale(self) -> int:
        """屏幕倍率（Retina = 2），懒探测一次就记住。

        ⚠️ `backing_scale()` 要调 CoreGraphics，不该每渲染一页问一次。
        """
        k = self._bscale
        if k is None:
            k = self._bscale = backing_scale()
        return k

    def _render_dpi(self, scale: Optional[int] = None) -> int:
        """实际拿去渲染的 dpi。

        · 这台 Tk **能**把大位图缩小显示（`photo_can_scale()`）→
          `逻辑 dpi × 屏幕倍率`，再在 `_cached_pixmap` 里缩回逻辑尺寸；
        · 不能（Tk 9.0 就是这种）→ 老实按逻辑 dpi 渲染。
          ⚠️ 这一条是**必须**的：缩不了还硬出 2 倍图，`configure` 会把页面
             裁成左上角 1/4 —— 用户 2026-10-01 报「没有全局显示」的根因。

        ⚠️ 无论哪条路都要封顶 `RENDER_DPI_MAX`：超采样平方级吃内存。
        """
        k = self._scale() if scale is None else scale
        if not photo_can_scale():
            k = 1
        return min(int(self.dpi * k), RENDER_DPI_MAX)

    @staticmethod
    def _to_photo(pm) -> Any:
        """pixmap → Tk 位图。Tk 9 直接吃 PNG，不用过一遍 Pillow。"""
        return tk.PhotoImage(
            data=base64.b64encode(pm.tobytes("png")).decode("ascii"))

    def _cached_pixmap(self, side: str, page) -> Any:
        """渲染 → PNG → Tk 位图，按 `(侧, 页, dpi, 是否带标注)` 缓存。

        高 dpi 下渲染一次要几百毫秒（560dpi 的 A4 更久），来回缩放 / 翻页不该
        反复付这个钱。缓存同时充当 `PhotoImage` 的引用持有者（不留引用画布会空白）。

        **高分屏**（只有 `photo_can_scale()` 为真时才走）：数据按「逻辑 dpi ×
        屏幕倍率」出，再 `configure(width=, height=)` 缩回逻辑尺寸，1 个数据
        像素正好落在 1 个物理像素上。**Tk 9.0 这条走不通**（`configure` 是裁切），
        于是退回 1x —— 详见文件顶部那段说明。

        ⚠️ 收尾那道**尺寸自检**只挡「dpi 算错 / 位图大小不对」这一类；它**挡不住
           裁切** —— 实测：正常路径与「被 configure 裁掉 3/4」两种情况，位图都是
           1050×750，一模一样（数据是被**截断**成目标尺寸的，宽高看着完全正常）。
           所以防裁切靠的是**行为探测**（`photo_can_scale()`），自检里另有一条
           **内容级**探针（在页面右下角画黑方块，看它还在不在）盯着这件事。
        """
        key = (side, self.page, self.dpi,
               self._enabled_kinds())   # 开关组合变了 = 注释变了，缓存必须换钥匙
        img = self._cache.get(key)
        if img is not None:
            return img
        k = self._scale()
        want_w = int(round(float(page.rect.width) / 72.0 * self.dpi))
        want_h = int(round(float(page.rect.height) / 72.0 * self.dpi))
        pm = page.get_pixmap(dpi=self._render_dpi(k), annots=True)
        img = self._to_photo(pm)
        if k > 1 and photo_can_scale():
            img.configure(width=max(1, int(round(pm.width / k))),
                          height=max(1, int(round(pm.height / k))))
        if (abs(img.width() - want_w) > 2 or abs(img.height() - want_h) > 2):
            pm1 = page.get_pixmap(dpi=self.dpi, annots=True)
            img = self._to_photo(pm1)
        while len(self._cache) >= RENDER_CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = img
        return img

    def _render_side(self, side: str, keep=None):
        cv = self.canv[side]
        cv.delete("all")
        cv.xview_moveto(0)
        doc = self.docs.get(side)
        if doc is None or self.page >= doc.page_count:
            cv.configure(scrollregion=(0, 0, 10, 10))
            cv.create_text(20, 20, anchor="nw", fill="#6b7280", font=(FONT, 11),
                           text="这一侧没有第 %d 页" % (self.page + 1))
            self._imgs.pop(side, None)
            return
        img = self._cached_pixmap(side, doc[self.page])
        self._imgs[side] = img                       # ⚠️ 不留引用 = 画布空白
        cv.create_image(0, 0, anchor="nw", image=img)
        cv.configure(scrollregion=(0, 0, img.width(), img.height()))

    def _kind_totals(self) -> Dict[str, Dict[str, int]]:
        """`{颜色桶: {侧: 处数}}` —— 栏标题与状态行的统计都从这一处取。

        ⚠️ 只在 `by_kind` 存在时才有值。老结构（回归里手工造的样本、或外部
           调用方只给了 `marks`）返回 `{}`，调用点要退回「按两侧报」的说法。
        """
        out: Dict[str, Dict[str, int]] = {}
        for side in ("old", "new"):
            bk = (self.marks.get(side) or {}).get("by_kind") or {}
            for kind, pages in bk.items():
                out.setdefault(kind, {})[side] = sum(len(v) for v in pages.values())
        return out

    def _update_labels(self):
        self.lb_page.config(text="%d/%d" % (self.page + 1, self.pages))
        pct = int(round(self.dpi / 72.0 * 100))
        self.lb_zoom.config(text="%d%%" % pct)
        self.btn_prev.config(state="normal" if self.page > 0 else "disabled")
        self.btn_next.config(state="normal" if self.page < self.pages - 1 else "disabled")

        by_kind = self._kind_totals()
        # 栏标题右侧：**这一侧**有哪几种颜色、各几处。颜色含义看工具条上的图例。
        for side in ("old", "new"):
            lb = self.lb_head.get(side)
            if lb is None:
                continue
            n = self.n_o if side == "old" else self.n_n
            if n == 0:
                lb.config(text="无标记")
                continue
            per = ["%s %d" % (ann.KIND_TEXT[k], by_kind.get(k, {}).get(side, 0))
                   for k in ann.KIND_ORDER]
            per = [p for p in per if not p.endswith(" 0")]
            lb.config(text=" · ".join(per) or "%d 处" % n)

        tot = self.n_o + self.n_n
        if tot == 0:
            msg = "这份 PDF 没有可标注的改动（文字层与转曲区都没算出差）"
        else:
            bits = []
            for k in ann.KIND_ORDER:
                n = (by_kind.get(k, {}).get("old", 0)
                     + by_kind.get(k, {}).get("new", 0))
                if n:
                    bit = "%s %d" % (ann.KIND_TEXT[k], n)
                    if not self.kind_on[k].get():
                        bit += "（已隐藏）"     # 分类开关关着 → 提醒统计里含它
                    bits.append(bit)
            if not bits:          # 老结构：没有 by_kind，只能按两侧报
                bits = ["旧版 %d" % self.n_o, "新版 %d" % self.n_n]
            msg = "共 %d 处标记：%s" % (
                tot, " · ".join(bits))
        self.status.config(
            text=msg + "　·　原文件未被修改、未被复制，关闭本窗即释放")

    # ---------------------------------------------------------------- 交互
    def goto(self, delta: int):
        p = min(max(self.page + delta, 0), self.pages - 1)
        if p != self.page:
            self.page = p
            self._keep_view = (0.0, 0.0)      # 翻页回到页首
            self.render()

    def _apply_dpi(self, dpi: int):
        """真正换档（内部用，不动「自动铺满」开关）。"""
        dpi = int(min(max(dpi, FIT_DPI_MIN), FIT_DPI_MAX))
        if dpi == self.dpi:
            return
        # 缩放前记下左右两侧的**视角比例**，缩放后还原（否则一放大就跳回页首）
        try:
            self._keep_view = tuple(self.canv[s].yview()[0] for s in ("old", "new"))
        except Exception:
            self._keep_view = None
        self.dpi = dpi
        self.render()

    def set_dpi(self, dpi: int):
        """**用户**换档：从此不再自动铺满（他要的就是这个档）。"""
        self._auto_fit = False
        self._apply_dpi(dpi)

    def _on_canvas_resize(self, _event=None):
        """画布尺寸变了 → 若还没被用户接管，重新「铺满整栏」。

        ⚠️ 加防抖：拖窗口时 `<Configure>` 会连着发几十次，每次都重渲两张 A4
           位图卡得没法用。
        """
        if not self._auto_fit:
            return
        if self._fit_job is not None:
            try:
                self.after_cancel(self._fit_job)
            except Exception:
                pass
        self._fit_job = self.after(FIT_RESIZE_MS, self._refit)

    def _refit(self):
        self._fit_job = None
        if not self._auto_fit:
            return
        want = self._fit_dpi(whole=False)
        if want:
            self._apply_dpi(want)

    def zoom(self, delta: int):
        steps = list(PAGE_DPI_STEPS)
        if self.dpi not in steps:
            steps.append(self.dpi)
            steps.sort()
        i = min(range(len(steps)), key=lambda k: abs(steps[k] - self.dpi))
        self.set_dpi(steps[min(max(i + delta, 0), len(steps) - 1)])

    def fit_width(self):
        """按窗口宽度挑一个「刚好铺满这一栏」的缩放（首帧默认就是它）。"""
        want = self._fit_dpi(whole=False)
        if want:
            self.set_dpi(want)

    def fit_page(self):
        """横竖**都要**塞得下 —— 竖版 A4 并排时看全篇用这个。"""
        want = self._fit_dpi(whole=True)
        if want:
            self.set_dpi(want)

    # —— 联动滚动：两块**同一位置**（横向 + 纵向都跟），眼睛不用在左右之间来回找 ——
    def _scroll(self, side: str, *args):
        self.canv[side].yview(*args)
        self._sync_scrollbars(side)

    def _from_bar(self, side: str, first, last):
        self._bars[side].set(first, last)
        self._sync_scrollbars(side)

    def _from_hbar(self, side: str, first, last):
        self._hbars[side].set(first, last)
        self._sync_scrollbars(side)

    def _sync_scrollbars(self, src: Optional[str] = None):
        """左右两块**横向 + 纵向**都对齐到 `src` 那一侧的位置。

        ⚠️ 用户 2026-10-01 原话：「旧版的和新版的预览改成同步滑动，不然操作人
           左边滑一次、右边滑一次很麻烦；页数多或细节多的话不知道滑到哪里去了」。
           放大之后主要靠**横向**拖 —— 早先这里只同步纵向 `yview`，横向被漏掉，
           于是「左右各滑一次」的抱怨就来自横向。现在 x / y 一起跟。
        """
        if self._syncing:
            return
        self._syncing = True
        try:
            order = ["old", "new"]
            if src in order:
                yf = self.canv[src].yview()[0]
                xf = self.canv[src].xview()[0]
                for s in order:
                    if s != src:
                        self.canv[s].yview_moveto(yf)
                        self.canv[s].xview_moveto(xf)
            for s in order:
                self._bars[s].set(*self.canv[s].yview())
                hb = self._hbars.get(s)
                if hb is not None:
                    hb.set(*self.canv[s].xview())
        except Exception:
            pass
        finally:
            self._syncing = False

    def _wheel(self, side: str, event):
        """鼠标滚轮 / 触控板**纵向**滚动。

        ⚠️ macOS 与 Windows/Linux 的 `delta` 符号**相反**：Windows 向下滚是
           `delta<0`、向上是 `delta>0`；macOS 触控板向下滑是 `delta>0`（指节向上
           推动内容下移）。光看 `delta` 正负会在某一台上把方向做反。统一用
           `event.delta` 的相对符号**并**照顾平台差异 —— 这里不猜正负，只把
           「滚动量」折算成 `yview_scroll` 的单位，方向交给 delta 本身。
        """
        d = getattr(event, "delta", 0)
        if d == 0:
            # 老式滚轮 / 某些驱动走 Button-4/5（在 `_wheel_legacy` 里接）
            return "break"
        # macOS 触控板惯性滚动 delta 是像素级（可达数百），Windows 是 ±120 的倍数。
        # 归一化成「向上滚为正」的步数：delta>0 = 内容往上（= 向上滚）。
        step = int(d / max(1, abs(d)))        # 只取方向 ±1
        self.canv[side].yview_scroll(-step * 3, "units")
        self._sync_scrollbars(side)
        return "break"

    def _wheel_h(self, side: str, event):
        """横向滚动（Shift+滚轮 / 触控板双指横滑 / 鼠标左右滚轮）。"""
        d = getattr(event, "delta", 0)
        if d == 0:
            d = getattr(event, "delta", None)
            # <Button-6>/<Button-7> 没有 delta，用「是哪个按钮」定方向
            num = getattr(event, "num", None)
            if num == 6:
                self.canv[side].xview_scroll(-3, "units")
            elif num == 7:
                self.canv[side].xview_scroll(3, "units")
            self._sync_scrollbars(side)
            return "break"
        step = int(d / max(1, abs(d)))
        self.canv[side].xview_scroll(-step * 3, "units")
        self._sync_scrollbars(side)
        return "break"

    def _drag_start(self, event):
        self._drag = (event.x, event.y)

    def _drag_move(self, event):
        """按住左键拖动 = 平移（放大之后靠滚动条太慢）。"""
        if self._drag is None:
            self._drag = (event.x, event.y)
            return
        x0, y0 = self._drag
        side = "old" if self.canv["old"] is event.widget else "new"
        cv = self.canv[side]
        (w, h) = (max(1, cv.winfo_width()), max(1, cv.winfo_height()))
        bx0, by0 = cv.canvasx(0), cv.canvasy(0)
        self._drag = (event.x, event.y)
        cv.xview_moveto(max(0.0, (bx0 - (event.x - x0)) / max(1.0, cv.bbox("all")[2])))
        cv.yview_moveto(max(0.0, (by0 - (event.y - y0)) / max(1.0, cv.bbox("all")[3])))
        self._sync_scrollbars(side)

    def close(self):
        """关窗：关掉只读文档句柄并释放位图，**不动任何文件**。"""
        for d in self.docs.values():
            try:
                d.close()
            except Exception:
                pass
        self.docs.clear()
        self._imgs.clear()
        self._cache.clear()
        self.destroy()


def open_preview(master, marks: Dict[str, Any],
                 old_path: str = "", new_path: str = "") -> PreviewWindow:
    """按两份文件名开一个预览窗（文件名只为标题好看，不参与渲染）。"""
    on = os.path.basename(old_path or "") or "旧版"
    nn = os.path.basename(new_path or "") or "新版"
    return PreviewWindow(master, marks, on, nn)


# ------------------------------------------------------------------ 演示样本
#: 「红绿演示」的正文。自检与「给用户看的示例」**共用这一份**（改一处就够）——
#: 以前两边各造一份，示例改了自检没改，结果就是「自检全绿、演示却不是红绿」。
#:
#: 造这份样本的规矩（2026-10-13 删「变动」档后只剩红/绿）：
#:
#:   ① **红**：某一行只写在旧版（新版整行没有）→ 引擎给 `removed`；
#:      **绿**：某一行只写在新版 → `added`；
#:      内容改写（如 6.5→7.0、干燥→阴凉）也拆成删+增两条，各归红/绿。
#:   ② **一行一字不差的行当不了锚点** —— `_cancel_identical` 会把两侧内容完全相同的行
#:      先抵消掉（那是为了不被换位重排刷假差异），抵消后它们根本不在字符流里。
#:   ③ 所以锚点只能藏在**改动行自己**里：相邻两处改动之间必须有一段 ≥4 字的公共子串
#:      （`_TINY_ANCHOR`），否则引擎把它们并成一个跨行的改写窗口。
#:      这就是下面「，长度 20mm」「处，常温」这些看着有点啰嗦的措辞的由来。
DEMO_LINES = {
    "old": [
        "GRA-9001 作业指导书",
        "规格尺寸 6.5mm，长度 20mm",              # → 新版 7.0mm      ：红+绿
        "贮存条件：贮存在干燥处，常温",            # → 新版 阴凉处      ：红+绿
        "备注：本器械为一次性使用，禁止重复灭菌",   # 新版整行没有        ：红
        "操作前须核对器械型号与规格",
        "本工具用于折叠与定位。",
    ],
    "new": [
        "GRA-9001 作业指导书",
        "规格尺寸 7.0mm，长度 20mm",
        "注意事项：每次使用前请检查包装完整性。",   # 旧版整行没有        ：绿
        "贮存条件：贮存在阴凉处，常温",
        "操作前须核对器械型号与规格",
        "本工具用于折叠与定位。",
    ],
}
#: 上面这份样本的预期分桶（自检直接拿它断言，改样本就要改这里）
#: 删「变动」档后：旧侧 = 删（2 处改写 + 1 处整行删）；新侧 = 增（2 处改写 + 1 处整行增）。
DEMO_WANT = {"old": {ann.KIND_DEL: 3},
             "new": {ann.KIND_ADD: 3}}


def make_demo_pair(out_dir: str) -> Tuple[str, str]:
    """在 `out_dir` 里造一对**红绿齐全**的示例 PDF，返回 `(旧版, 新版)` 路径。

    ⚠️ 只在临时目录里用（自检的 tmp、或启动器给用户看的 /tmp）；
       绝不要往用户的桌面 / 下载目录写示例文件。
    """
    paths = []
    for tag in ("old", "new"):
        doc = fitz.open()
        page = doc.new_page(width=420, height=300)
        y = 46
        for line in DEMO_LINES[tag]:
            page.insert_text((30, y), line, fontname="china-s", fontsize=11)
            y += 28
        fp = os.path.join(out_dir, "示例-%s（红绿）.pdf" % ("旧版" if tag == "old" else "新版"))
        doc.save(fp)
        doc.close()
        paths.append(fp)
    return paths[0], paths[1]


# ------------------------------------------------------------------ 自检
def _self_test() -> int:
    """自检：造两份带文字层的 PDF，跑一遍「算标记 → 预览」，
    并**用文件系统指纹钉住「不写盘、不改原文件」这条硬约束**。

    样本用模块级的 `DEMO_LINES`（`make_demo_pair` 造的**红绿齐全**那一对）——
    与「给用户看的示例」是同一份，否则会出现「自检过了、演示却不是红绿」。
    分桶结果按 `DEMO_WANT` 断言：改样本没改期望值就会红。
    """
    import tempfile

    import pdf_annotate as ann

    ok = fail = 0

    def chk(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name}{('　→ ' + extra) if extra else ''}")

    tmp = tempfile.mkdtemp(prefix="pdfdiff_preview_")
    srcs = list(make_demo_pair(tmp))

    def fingerprint(path):
        st = os.stat(path)
        return (os.path.getsize(path), round(st.st_mtime, 3))

    def snapshot():
        """整个临时目录的文件清单 + 每个文件的指纹（用来抓「偷偷落盘」）。"""
        out = {}
        for name in sorted(os.listdir(tmp)):
            fp = os.path.join(tmp, name)
            if os.path.isfile(fp):
                out[name] = fingerprint(fp)
        return out

    before = snapshot()
    marks = ann.locate_marks(srcs[0], srcs[1])

    root = tk.Tk()
    root.withdraw()
    win = None
    try:
        win = PreviewWindow(root, marks, "old.pdf", "new.pdf")
        win.withdraw()
        root.update()

        chk("预览窗拿到两侧标记", "old" in marks and "new" in marks)
        chk("两侧都算出了要标的矩形",
            sum(len(v) for v in marks["old"]["marks"].values()) > 0
            and sum(len(v) for v in marks["new"]["marks"].values()) > 0)
        chk("两侧文档只在内存里打开（未写盘）", set(win.docs) == {"old", "new"})
        chk("两侧画布都建好了", set(win.canv) == {"old", "new"})
        chk("页面已渲染成位图",
            win._imgs.get("old") is not None and win._imgs["old"].width() > 10)
        chk("页数取两侧较大者", win.pages == max(marks["old"]["pages"],
                                                marks["new"]["pages"]), str(win.pages))
        chk("状态行写明标记处数",
            "处标记" in win.status.cget("text"), win.status.cget("text")[:60])
        chk("状态行写明不动原文件", "原文件未被修改" in win.status.cget("text"))

        # —— 演示样本本身必须**红绿齐全**（这是本轮的核心口径）——
        # 用户 2026-10-01 的原话：「你标的红色的不是纯删减，你标的绿色它也不是纯新增
        # ……你没有真正的演示出来红色和绿色所应该有的效果」。所以样本被换成
        # `DEMO_LINES`（真有一行只在旧版、一行只在新版），这里按 `DEMO_WANT` 钉死。
        for _side in ("old", "new"):
            _got = {k: sum(len(v) for v in p.values())
                    for k, p in (marks[_side]["by_kind"] or {}).items()}
            chk("样本 %s 侧分桶与 DEMO_WANT 一致（红绿都要真出现）"
                % ("旧" if _side == "old" else "新"),
                _got == DEMO_WANT[_side], "%s ≠ %s" % (_got, DEMO_WANT[_side]))
        chk("样本里红/绿两种颜色都真的出现了（不再有黄）",
            set(marks["old"]["by_kind"]) | set(marks["new"]["by_kind"])
            == {ann.KIND_DEL, ann.KIND_ADD},
            str(set(marks["old"]["by_kind"]) | set(marks["new"]["by_kind"])))
        chk("红只落在旧版、绿只落在新版（「纯」字的含义）",
            ann.KIND_DEL in marks["old"]["by_kind"]
            and ann.KIND_DEL not in marks["new"]["by_kind"]
            and ann.KIND_ADD in marks["new"]["by_kind"]
            and ann.KIND_ADD not in marks["old"]["by_kind"])

        # ⚠️ 红绿回归（2026-10-13 删「变动」档）：
        #    ① 注释上的 `/C` 必须真的**落到注释上** —— 用户报「两个颜色都是黄的」，
        #       根因是 `doc[pi].add_highlight_annot(...)` 里的临时 Page 被 GC，
        #       `set_colors` 抛错又被 `except` 吞掉，注释带着**默认黄**留在页面上；
        #    ② 渲染出来的像素真有那两种颜色，而且**不串色**
        #       （旧版侧不该出现绿、新版侧不该出现红）。
        def annot_colors(doc):
            out = []
            for pi in range(doc.page_count):
                for a in doc[pi].annots():          # ⚠️ 直接迭代，别先 list()
                    try:
                        if a.type[1] == "Highlight":
                            out.append(tuple(round(float(v), 2)
                                             for v in a.colors["stroke"]))
                    except Exception:
                        pass
            return out

        want_red = tuple(round(v, 2) for v in ann.RED)
        want_grn = tuple(round(v, 2) for v in ann.GREEN)
        c_old, c_new = annot_colors(win.docs["old"]), annot_colors(win.docs["new"])
        chk("旧版高亮只有「红」（不再有黄/绿串色）",
            bool(c_old) and set(c_old) <= {want_red} and want_red in c_old,
            str(set(c_old)))
        chk("新版高亮只有「绿」",
            bool(c_new) and set(c_new) <= {want_grn} and want_grn in c_new,
            str(set(c_new)))
        chk("红绿互不相同", want_red != want_grn)
        chk("剔除配对不成立的红绿：旧版侧没有绿、新版侧没有红",
            want_grn not in c_old and want_red not in c_new)

        def hue_count(pm, which):
            """数一数图里「被某种色相主导」的像素（白/灰/黑都不算）。

            ⚠️ 阈值是照**实际渲染值**定的，别随手改：
               实测红 ≈ (255,88,88)、绿 ≈ (45,195,90)，
               高亮注释**不透明**（`/C` 原样渲染，不和底下的字混色）。
               「红」那条必须带 `g < 140` —— 黄的 R 同样远大于 B，
               不排掉的话黄会被算成红，断言就失去意义。
            """
            s, n = pm.samples, 0
            for i in range(0, len(s) - 2, pm.n):
                r, g, b = s[i], s[i + 1], s[i + 2]
                if which == "red":
                    ok = r > g + 60 and r > b + 60 and g < 140
                elif which == "green":
                    ok = g > r + 60 and g > b + 40
                else:                                   # yellow
                    ok = r > 200 and 140 < g < 235 and b < 120
                if ok:
                    n += 1
            return n

        pm_o = win.docs["old"][0].get_pixmap(dpi=140, annots=True)
        pm_n = win.docs["new"][0].get_pixmap(dpi=140, annots=True)
        chk("旧版图上有红（删减）", hue_count(pm_o, "red") > 0,
            str(hue_count(pm_o, "red")))
        chk("旧版图上没有黄（删「变动」档）", hue_count(pm_o, "yellow") == 0,
            str(hue_count(pm_o, "yellow")))
        chk("旧版图上没有绿（两侧不串色）", hue_count(pm_o, "green") == 0,
            str(hue_count(pm_o, "green")))
        chk("新版图上有绿（新增）", hue_count(pm_n, "green") > 0,
            str(hue_count(pm_n, "green")))
        chk("新版图上没有黄（删「变动」档）", hue_count(pm_n, "yellow") == 0,
            str(hue_count(pm_n, "yellow")))
        chk("新版图上没有红（两侧不串色）", hue_count(pm_n, "red") == 0,
            str(hue_count(pm_n, "red")))

        # 栏标题 / 状态行必须按**颜色桶**报数（不是笼统的「旧版 N 处」）
        t_o, t_n = win.lb_head["old"].cget("text"), win.lb_head["new"].cget("text")
        chk("旧版栏标题按色分桶（删减）", "删减" in t_o, t_o)
        chk("新版栏标题按色分桶（新增）", "新增" in t_n, t_n)
        chk("状态行按删/增统计",
            "删减" in win.status.cget("text") and "新增" in win.status.cget("text"),
            win.status.cget("text")[:70])

        # —— 位图**不许被 Tk 裁掉**（2026-10-01 用户报「依旧没有显示完全」）——
        #    根因：Tk 9.0 的 `PhotoImage.configure(width=, height=)` 是**裁切**不是
        #    缩放，上一版拿它当「超采样后缩回逻辑尺寸」用 → 每页只剩左上角 1/4。
        #    ⚠️ 只查 `img.width()` 查不出来：数据被截断之后宽高**正好**等于逻辑
        #       尺寸，看着完全正常。所以下面要**查内容**（右下角那块黑方块）。
        win.set_dpi(DEFAULT_DPI)          # 上面缩放测试把 dpi 顶到了上限，先复位
        _orig_dpi = win.dpi
        base = win._scale()
        chk("探测到的屏幕倍率在允许区间内", HIDPI_MIN <= base <= HIDPI_MAX, str(base))
        _can = photo_can_scale()
        chk("这台 Tk 的 configure 到底是缩放还是裁切 —— 已探明（布尔结论）",
            isinstance(_can, bool), str(_can))
        chk("Tk 缩不了位图时**绝不能**按倍率出图（否则被裁掉 3/4）",
            _can or (win._render_dpi(2) == win._render_dpi(1) == DEFAULT_DPI),
            "能缩放=%s，2 倍请求 → %d dpi" % (_can, win._render_dpi(2)))
        chk("渲染 dpi 封顶（超采样不吃爆内存）",
            win._render_dpi(HIDPI_MAX) <= RENDER_DPI_MAX, str(win._render_dpi(3)))

        # 内容级探针：造一页「整页空白、只在**右下角**画个黑方块」——
        #   画全了 → 位图右下角是黑的；被裁成左上角 1/4 → 那里其实是页面中部（白的）。
        _probe = fitz.open()
        try:
            _pg = _probe.new_page(width=200, height=200)
            _pg.draw_rect(fitz.Rect(168, 168, 196, 196), color=(0, 0, 0),
                          fill=(0, 0, 0))
            for _k in (1, 2, HIDPI_MAX):
                win._bscale = _k
                win._cache.pop(("probe", 0, win.dpi), None)
                _im = win._cached_pixmap("probe", _pg)
                _x, _y = int(_im.width() * 0.91), int(_im.height() * 0.91)
                _px = _rgb(_im.get(_x, _y)) if _x < _im.width() and _y < _im.height() else ()
                chk("倍率 %d：位图把整页画全了（右下角的黑方块在图里）" % _k,
                    bool(_px) and max(_px) < 120, str(_px))
                chk("倍率 %d：位图逻辑尺寸 = 整页（200pt @ %d dpi）" % (_k, win.dpi),
                    abs(_im.width() - round(200 / 72.0 * win.dpi)) <= 2
                    and abs(_im.height() - round(200 / 72.0 * win.dpi)) <= 2,
                    "%d×%d" % (_im.width(), _im.height()))
            win._cache.pop(("probe", 0, win.dpi), None)
        finally:
            _probe.close()

        # 兜底：**别信 `photo_can_scale()` 自己说了什么，独立复核一遍 Tk 的行为。**
        #   8×8 全红、右下 4×4 涂绿，缩成 4×4 再读 (3,3)：绿=缩放、红=裁切。
        _chk = tk.PhotoImage(width=8, height=8)
        _chk.put("#ff0000", to=(0, 0, 8, 8))
        _chk.put("#00ff00", to=(4, 4, 8, 8))
        _chk.configure(width=4, height=4)
        _truly = _rgb(_chk.get(3, 3)) == (0, 255, 0)
        chk("能力探测与 Tk 实测行为一致（configure 是缩放=%s）" % _truly,
            _can == _truly, "探测说 %s，实测 %s" % (_can, _truly))
        _pw = float(win.docs["old"][0].rect.width)
        _ph = float(win.docs["old"][0].rect.height)
        win._bscale = base
        win._cache.clear()
        win.render()
        root.update()
        _im = win._imgs["old"]
        chk("正常路径下位图仍是**整页**尺寸（%.0f×%.0fpt @ %d dpi）"
            % (_pw, _ph, _orig_dpi),
            abs(_im.width() - round(_pw / 72.0 * _orig_dpi)) <= 2
            and abs(_im.height() - round(_ph / 72.0 * _orig_dpi)) <= 2,
            "%d×%d（应 %d×%d）" % (_im.width(), _im.height(),
                                  round(_pw / 72.0 * _orig_dpi),
                                  round(_ph / 72.0 * _orig_dpi)))

        # —— 「铺满整栏」：**用户 2026-10-01 报「没有全局显示 / 只截取了一小部分」** ——
        #    真实 WI 是 842×595 的横版 A4，写死档位出图必然比画布宽得多。
        #    现在：开窗前先估（`_boot_dpi`）→ 首帧按实测画布宽反算（`_fit_first_frame`）
        #    → 之后窗口一变形就重算（`_on_canvas_resize`，用户没手动选过档位时）。
        #    「看得全」永远优先于「看得清」：宁可让用户点 ＋ 放大，也不能一开窗就少半页。
        _mapped, _cvw = win.winfo_ismapped, win.canv["new"].winfo_width
        _cvh = win.canv["new"].winfo_height
        try:
            win.winfo_ismapped = lambda: True          # 自检里窗口是 withdraw 的
            win.canv["new"].winfo_width = lambda: 668  # ≈两栏并排的真实可用宽
            win.canv["new"].winfo_height = lambda: 700
            win.set_dpi(FIT_DPI_MAX)
            win._fit_first_frame()
            # `_fit_dpi` 里留了 8px 边距（`cw = winfo_width() - 8`）
            _want = int(round((668 - 8) / 420.0 * 72.0))
            chk("首帧按画布宽把缩放调成「铺满整栏」（不再留一个被裁的局部）",
                win.dpi == _want, "%s ≠ %s" % (win.dpi, _want))
            chk("首帧结果让整页宽度真的塞得下",
                420 / 72.0 * win.dpi <= 668, str(420 / 72.0 * win.dpi))
            # 「整页」按钮：横竖都要塞得下 —— 画布矮的时候按高度收紧
            win.canv["new"].winfo_height = lambda: 300
            win.fit_page()
            chk("「整页」在画布矮时按高度收紧（竖版 A4 看全篇用）",
                win.dpi == int(round((300 - 8) / 300.0 * 72.0)), str(win.dpi))
            win.canv["new"].winfo_height = lambda: 700
            # 画布还没布局好（winfo_width 给 1）时不许乱算
            win.set_dpi(200)
            win.canv["new"].winfo_width = lambda: 1
            win._fit_first_frame()
            chk("量不到画布宽时保持原档、不瞎猜", win.dpi == 200, str(win.dpi))

            # —— 「窗口变形后自动重新铺满」（2026-10-01 补）——
            #    首帧那次测量可能落在布局完成前（实测由此掉到过下限 40dpi、
            #    页面小得离谱），所以 <Configure> 里要能自己纠正回来。
            win.canv["new"].winfo_width = lambda: 668
            win._auto_fit = True
            win._apply_dpi(FIT_DPI_MAX)
            win._on_canvas_resize()
            _job = win._fit_job
            win._fit_job = None
            if _job:
                win.after_cancel(_job)         # 自检里不等 after，直接跑一次
            win._refit()
            chk("窗口变形后自动重新铺满整栏（首帧量偏了也能纠回来）",
                win.dpi == _want, "%s ≠ %s" % (win.dpi, _want))
            win.set_dpi(FIT_DPI_MIN)           # 用户手动选档
            win._on_canvas_resize()
            _job = win._fit_job
            win._fit_job = None
            if _job:
                win.after_cancel(_job)
            win._refit()
            chk("用户手动选过档位后，变形不再自动改（尊重用户选择）",
                win.dpi == FIT_DPI_MIN, str(win.dpi))
        finally:
            win.winfo_ismapped, win.canv["new"].winfo_width = _mapped, _cvw
            win.canv["new"].winfo_height = _cvh
        # 开窗前就得估个八九不离十，否则首帧会「先裁后缩」闪一下
        _est = (win._win_w - 48.0) / 2.0
        chk("开窗前的预估档位就不会把整页裁掉（消除首帧闪烁）",
            420 / 72.0 * win._boot_dpi() <= _est * 1.02,
            "%s dpi → %.0fpx，预估栏宽 %.0f" % (win._boot_dpi(),
                                                420 / 72.0 * win._boot_dpi(), _est))
        win.set_dpi(DEFAULT_DPI)
        win.render()
        root.update()

        # 缩放：改 dpi → 位图尺寸变
        w0 = win._imgs["old"].width()
        win.set_dpi(200)
        root.update()
        chk("放大后位图变宽", win._imgs["old"].width() > w0,
            f"{w0} → {win._imgs['old'].width()}")
        win.set_dpi(60)
        root.update()
        chk("缩小后位图变窄", win._imgs["old"].width() < w0)
        win.dpi = DEFAULT_DPI
        win.render()
        chk("缩放档位有上下限",
            (win.set_dpi(10) or win.dpi) >= FIT_DPI_MIN
            and (win.set_dpi(9999) or win.dpi) <= FIT_DPI_MAX, str(win.dpi))

        # （「显示标注」开关已移除 —— 标注永远显示，无开关可测。）

        # 翻页：单页文档时两侧按钮都该禁用，goto 不越界
        win.goto(-1)
        chk("单页时不会往前翻出界", win.page == 0)
        win.goto(1)
        chk("单页时不会往后翻出界", win.page == 0)
        chk("单页时翻页按钮禁用",
            str(win.btn_prev["state"]) == "disabled" and str(win.btn_next["state"]) == "disabled")

        # 联动滚动：把一侧拖到中间，另一侧跟着走
        win.canv["old"].configure(scrollregion=(0, 0, 400, 4000))
        win.canv["new"].configure(scrollregion=(0, 0, 400, 4000))
        win.canv["old"].yview_moveto(0.5)
        win._sync_scrollbars("old")
        chk("左右联动滚动（纵向）",
            abs(win.canv["new"].yview()[0] - 0.5) < 0.05,
            f"{win.canv['new'].yview()[0]:.3f}")

        # —— 横向联动（2026-10-01 用户：放大后左右各滑一次很麻烦）——
        #   放大后图片横向超宽，主要靠横向滚动 + 拖拽平移；
        #   早先只同步纵向，横向被漏掉 → 这里钉住横向也要跟。
        win.canv["old"].configure(scrollregion=(0, 0, 4000, 400))
        win.canv["new"].configure(scrollregion=(0, 0, 4000, 400))
        win.canv["old"].xview_moveto(0.3)
        win.canv["old"].yview_moveto(0.0)
        win._sync_scrollbars("old")
        chk("左右联动滚动（横向也跟）",
            abs(win.canv["new"].xview()[0] - 0.3) < 0.05,
            f"{win.canv['new'].xview()[0]:.3f}")
        # 横向滚动条也在同步后更新到同一位置
        chk("横向滚动条跟着更新",
            abs(win._hbars["new"].get()[0] - win.canv["new"].xview()[0]) < 0.05)

        # —— 滚轮方向（2026-10-01 用户「上下滑动失效、只能左右」）——
        #   macOS 触控板/滚轮纵向滚动走 <MouseWheel>，必须真的驱动 yview；
        #   横向走 <Shift-MouseWheel> 或 <Button-6/7>，驱动 xview。
        #   ⚠️ 自检窗口是 withdraw 的，`yview_scroll` 在未映射画布上不产生实际位移，
        #      所以这里 monkeypatch 掉 canvas 的滚动方法，验证 `_wheel`/`_wheel_h`
        #      **调对了方向与轴**（横向事件绝不碰 yview、纵向事件绝不碰 xview）。
        class _Ev:
            delta = 120
            num = 0
        _calls = []
        _yc = win.canv["old"].yview_scroll
        _xc = win.canv["old"].xview_scroll
        win.canv["old"].yview_scroll = lambda n, u: _calls.append(("y", n))
        win.canv["old"].xview_scroll = lambda n, u: _calls.append(("x", n))
        try:
            win._wheel("old", _Ev())
            chk("滚轮纵向事件走 yview（不再「只能左右」）",
                ("y", -3) in _calls, str(_calls))
            chk("滚轮纵向事件绝不碰 xview",
                not any(a == "x" for a, _ in _calls), str(_calls))
            _calls.clear()
            win._wheel_h("old", _Ev())
            chk("横向滚轮事件走 xview",
                ("x", -3) in _calls, str(_calls))
            chk("横向滚轮事件绝不碰 yview",
                not any(a == "y" for a, _ in _calls), str(_calls))
            _calls.clear()
            class _Ev6:
                delta = 0
                num = 6
            win._wheel_h("old", _Ev6())
            chk("<Button-6>（无 delta）按 num 定方向横向滚动",
                _calls and _calls[0][0] == "x", str(_calls))
        finally:
            win.canv["old"].yview_scroll = _yc
            win.canv["old"].xview_scroll = _xc

        # 关窗释放
        win.close()
        win = None
        chk("关窗后文档句柄已释放", True)

        after = snapshot()
        chk("没有在源目录里产生/改动任何文件", before == after,
            f"{set(after) - set(before)} / 指纹变了 "
            f"{[k for k in before if k in after and before[k] != after[k]]}")
        chk("原文件指纹前后一致（大小 + mtime）",
            all(fingerprint(p) == before[os.path.basename(p)] for p in srcs))
    finally:
        try:
            if win is not None:
                win.close()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    print(f"预览模块自检：{ok} 项通过，{fail} 项失败")
    return 1 if fail else 0


if __name__ == "__main__":
    import sys
    sys.exit(_self_test())
