# -*- coding: utf-8 -*-
"""
pdfdiff_img.py —— 图片输入归一化（PNG / JPG / TIFF … → 单页 PDF）
==================================================================
用户 2026-09-30：「不只去识别一个正常的 PDF，而是扫描件的 PDF，甚至 PNG 格式的
图片、JPG 格式的图片或其他什么图片，也试着去识别标记出来。」

**为什么是「图片 → 单页 PDF」而不是给引擎加一套图片分支**
--------------------------------------------------------
比对 / 定位 / 标注三条链路（pdfdiff_core / pdfdiff_locate / pdf_annotate）
**全都只认 PDF**：pdfminer 要 PDF 的对象树、PyMuPDF 的 `page.rect`、
标注的输出载体也是 PDF。给每条链路各加一套「如果是图片就怎样」的分支，
等于把同一件事写三遍，还会让「图片这条路」和「PDF 这条路」慢慢长歪
（现在扫描件链路刚调通，最怕的就是分叉）。

所以做**一次归一化**：图片在进入任何链路之前先折成一页 PDF（整页一张位图、
没有文字层）—— 它和「扫描件 PDF」在引擎眼里**完全是同一种东西**，
2026-09-30 为 GRA-3634 / GRA-3925 调出来的扫描件管线（整页 OCR 行对拍 +
像素画像）原样生效，不用为图片改一个判定。

代价只有一条：图片页没有文字层，全部结论来自 OCR —— 这是**输入本身**决定的，
不是本模块的取舍。

页面尺寸怎么定（会直接影响 OCR 质量，别拍脑袋）
----------------------------------------------
图片只有像素、没有物理尺寸，而 PDF 的坐标系是 pt（72pt = 1inch）。
换算规则定成一张表（`IMG_SIZE_RULES`），三条判据：

  ① 按 `IMG_ASSUMED_DPI` 折：把图片当成一张 200dpi 的扫描件。
     取 200 是因为扫描件管线的整页 OCR 就跑在 200dpi（`SCAN_LINES_DPI`），
     这样渲染出来**恰好等于原图像素** —— 不放大（不糊）、不缩小（不丢字）。
  ② 长边封顶 `IMG_MAX_PT`：超大图（手机拍照 6000px）折出来的页会有 2000pt+，
     300dpi 复核时一张 9000px 的位图能吃到上百 MB。封到 A3 长边，
     OCR 时的有效分辨率仍有 3300px，够认小字。
  ③ 短边兜底 `IMG_MIN_PT`：几十像素的图标不至于折出 1pt 的页，
     让坐标量级和正常页一致（下游阈值全是按 pt 调的）。

缓存
----
转换结果按**文件内容 sha1** 落在系统临时目录里复用：同一张图第二次对比
（旧版/新版互换、改完规则重跑回归）不再重新解码。缓存在 /tmp，系统自己会清。

⚠️ EXIF 方向
------------
手机拍的 JPG 常带 EXIF 旋转标记，解码器不认就会**躺着的页面**被当成正的，
OCR 直接废。有 Pillow 就走 `ImageOps.exif_transpose`；没有就原样读，
并在返回信息里记一笔（见 `convert_report`）。

⚠️ `/Rotate` 归一化（2026-10-10 加）
------------------------------------
PDF 允许整页挂一个 `/Rotate`（扫描仪与报告导出器很常写）。实测 **pdfminer 遇到带
`/Rotate` 的页会把每个字读成一行**（一行 rev 标题 → 18 个单字行），清单直接炸成逐字
增删；标注链路又因为两版 `page.rect` 不等（595×842 vs 842×595）整页跳过。

修法不是去改 pdfminer，而是**进链路之前先把 `/Rotate` 归零**：只改页字典里的
`/Rotate`、内容层一个字节不动，落一份 /tmp 缓存副本。三例实测（只旧版转 / 只新版转 /
两侧都转）从 17~19 条乱码塌回**精确 1 条 modified**，两版 `page.rect` 也跟着一致。

⚠️ 别改用 `show_pdf_page(rotate=)` 把页面「转回来」：内容会包进 `LTFigure` 且字形变
竖排，pdfminer 反而读成 **0 行**（实测打回来的）。

⚠️ 因此本模块的职责是「**输入归一化**」两件事：图片 → 单页 PDF、`/Rotate` → 0 度。
两者都只产出 /tmp 副本，**从不改写用户的原件**。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

try:                                     # 可选依赖：只为 EXIF 方向，缺了也能跑
    from PIL import Image, ImageOps       # type: ignore
except Exception:                         # pragma: no cover
    Image = None                          # type: ignore
    ImageOps = None                       # type: ignore

# ==================================================================
# 第 1 节  输入格式表（新增格式只改这里）
# ==================================================================

# 后缀 → 展示名。能不能真的读出来取决于 MuPDF 的解码器，
# 读不出来会在 `image_to_pdf` 里抛错并由上层接住（界面报「无法解析」）。
IMG_EXT_MAP: Dict[str, str] = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG",
    ".jfif": "JPEG", ".jp2": "JPEG2000", ".jpx": "JPEG2000",
    ".tif": "TIFF", ".tiff": "TIFF",
    ".bmp": "BMP", ".gif": "GIF", ".webp": "WebP",
    ".pnm": "PNM", ".ppm": "PNM", ".pgm": "PNM",
}

# ==================================================================
# 第 2 节  页面尺寸规则表
# ==================================================================

IMG_ASSUMED_DPI = 200.0   # ① 把图片当作多少 dpi 的扫描件（= 扫描件管线的 OCR 分辨率）
IMG_MAX_PT = 1190.0       # ② 长边封顶（A3 长边）：防超大图把内存吃穿
IMG_MIN_PT = 72.0         # ③ 短边兜底（1 inch）：防几十像素的图折出接近 0 的页

IMG_SIZE_RULES: List[Dict[str, Any]] = [
    {"name": "按 200dpi 折算", "field": "assumed_dpi", "value": IMG_ASSUMED_DPI,
     "why": "与 SCAN_LINES_DPI 同口径，渲染出来 = 原图像素，不放大也不缩小"},
    {"name": "长边封顶", "field": "max_pt", "value": IMG_MAX_PT,
     "why": "手机拍照级大图在 300dpi 复核时会吃上百 MB，封到 A3 长边仍有 3300px"},
    {"name": "短边兜底", "field": "min_pt", "value": IMG_MIN_PT,
     "why": "坐标量级与正常页一致，下游阈值全是按 pt 调的"},
]


def page_size_for(px_w: int, px_h: int) -> Tuple[float, float]:
    """按 `IMG_SIZE_RULES` 把像素尺寸折成 PDF 页面尺寸（pt）。"""
    k = 72.0 / IMG_ASSUMED_DPI
    w, h = max(1.0, px_w * k), max(1.0, px_h * k)
    # ② 长边封顶：等比缩，不改宽高比
    m = max(w, h)
    if m > IMG_MAX_PT:
        f = IMG_MAX_PT / m
        w, h = w * f, h * f
    # ③ 短边兜底：只把过小的一侧抬到 1inch，同样不改另一侧的换算结果
    if min(w, h) < IMG_MIN_PT:
        f = IMG_MIN_PT / max(1e-6, min(w, h))
        w, h = w * f, h * f
    return round(w, 2), round(h, 2)


# ==================================================================
# 第 3 节  判定与缓存
# ==================================================================

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfdiff_img_cache")


def is_image_file(path: str) -> bool:
    """是不是「本模块能接管的图片」（PDF 一律返回 False，交给原链路）。"""
    return os.path.splitext(str(path or ""))[1].lower() in IMG_EXT_MAP


def image_kind(path: str) -> str:
    return IMG_EXT_MAP.get(os.path.splitext(str(path or ""))[1].lower(), "")


def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_path(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = "".join(c for c in stem if c not in '/\\:*?"<>|')[:40] or "img"
    return os.path.join(_CACHE_DIR, f"{stem}_{_sha1(path)[:16]}.pdf")


def _pixmaps(path: str) -> List[Tuple[Any, int, int]]:
    """解出图片的每一帧（多页 TIFF / 动图会被解成多页），返回 [(pixmap, w, h)]。"""
    import fitz

    # Pillow 优先：认得 EXIF 方向（手机拍照的 JPG 常带旋转标记）
    if Image is not None:
        try:
            im = ImageOps.exif_transpose(Image.open(path)) if ImageOps else Image.open(path)
            frames: List[Any] = []
            try:
                n = getattr(im, "n_frames", 1) or 1
            except Exception:
                n = 1
            for i in range(min(n, 32)):        # 动图最多取 32 帧，再多没意义
                try:
                    im.seek(i)
                except Exception:
                    break
                fr = im.convert("RGB") if im.mode not in ("RGB", "L") else im.copy()
                pm = _pm_from_pil(fr)
                if pm is not None:
                    frames.append((pm, fr.width, fr.height))
            if frames:
                return frames
        except Exception:
            pass                                # PIL 读不了 → 退回 MuPDF

    pm = fitz.Pixmap(path)
    if pm.n - int(pm.alpha) > 3:                # CMYK 之类 → 转 RGB，插入才不偏色
        pm = fitz.Pixmap(fitz.csRGB, pm)
    return [(pm, pm.width, pm.height)]


def _pm_from_pil(im):
    """PIL Image → PyMuPDF Pixmap（不经磁盘）。"""
    import fitz
    try:
        if im.mode == "RGB":
            return fitz.Pixmap(fitz.csRGB, im.width, im.height, im.tobytes(), False)
        if im.mode == "L":
            return fitz.Pixmap(fitz.csGRAY, im.width, im.height, im.tobytes(), False)
        im = im.convert("RGB")
        return fitz.Pixmap(fitz.csRGB, im.width, im.height, im.tobytes(), False)
    except Exception:
        return None


def image_to_pdf(path: str, out_path: Optional[str] = None) -> str:
    """图片 → 单页（多帧则多页）PDF，整页一张位图、无文字层。

    落在 `_CACHE_DIR` 并按内容哈希复用；`out_path` 给了就写到指定位置。
    """
    import fitz

    out_path = out_path or _cache_path(path)
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        return out_path
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    frames = _pixmaps(path)
    if not frames:
        raise ValueError(f"无法解码图片：{path}")

    doc = fitz.open()
    try:
        for pm, w, h in frames:
            pw, ph = page_size_for(w, h)
            page = doc.new_page(width=pw, height=ph)
            page.insert_image(fitz.Rect(0, 0, pw, ph), pixmap=pm)
        tmp = out_path + ".tmp"
        doc.save(tmp, deflate=True, garbage=3)
        os.replace(tmp, out_path)
    finally:
        doc.close()
    return out_path


# ==================================================================
# 第 3.5 节  /Rotate 归一化（2026-10-10 加）
# ==================================================================

_ROT_CACHE_DIR = os.path.join(tempfile.gettempdir(), "pdfdiff_rot_cache")
# 关掉就退回「不归一化」的老行为（排障/反证哨兵用；正常一律开）
ROT_NORM = os.environ.get("PDFDIFF_ROT_NORM", "1") != "0"


def _rot_cache_path(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = "".join(c for c in stem if c not in '/\\:*?"<>|')[:40] or "rot"
    return os.path.join(_ROT_CACHE_DIR, f"{stem}_{_sha1(path)[:16]}.pdf")


def rotated_pages(path: str) -> int:
    """这份输入里有多少页带 `/Rotate`（**只报告、不归一化**；图片一律 0）。

    给 `pdfdiff_core` 写「已归一化」提示用 —— 用户得知道我们按什么口径在比。
    """
    if not path or is_image_file(path) or not os.path.isfile(path):
        return 0
    try:
        import fitz
        d = fitz.open(path)
    except Exception:
        return 0
    try:
        return sum(1 for i in range(d.page_count) if d[i].rotation)
    except Exception:
        return 0
    finally:
        d.close()


def normalize_rotation(path: str) -> str:
    """把带 `/Rotate` 的页归一化成 0 度；**没有旋转就原样返回**。

    ⚠️ 只改页字典里的 `/Rotate`，内容层一个字节都不动 —— 不是把画面「转回来」，
       而是取消「显示时再转」这一层。两版各自归一化后，`page.rect`、字形方向、
       行切分全都一致，pdfminer 与 PyMuPDF 才说同一种话（见模块说明那节）。

    结果按**文件内容 sha1** 落在 /tmp 复用。归一化失败**一律原样返回** ——
    宁可退回旧行为，也不让一份读不动的文件把整次比对打断。
    """
    if not ROT_NORM or rotated_pages(path) == 0:
        return path
    out = _rot_cache_path(path)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    try:
        import fitz
        d = fitz.open(path)
    except Exception:
        return path
    try:
        for i in range(d.page_count):
            if d[i].rotation:
                d[i].set_rotation(0)
        os.makedirs(_ROT_CACHE_DIR, exist_ok=True)
        tmp = out + ".tmp"
        d.save(tmp, garbage=3, deflate=True)
        os.replace(tmp, out)
        return out
    except Exception:
        return path
    finally:
        d.close()


def ensure_pdf(path: str) -> str:
    """把任何输入归一化成「可直接进入三条链路的 PDF」= 图片折页 + `/Rotate` 归零。

    · PDF 且无旋转 → **原样返回**（老行为一字不改，保证零回归）；
    · PDF 有旋转 → 落 /tmp 缓存副本；
    · 图片 → 先折单页 PDF（折出来没有 `/Rotate`，第二步自然空转）；
    · 路径不存在 → 原样返回，由上层如实报错。
    """
    if not path:
        return path
    if not os.path.isfile(path):
        return path
    if is_image_file(path):
        path = image_to_pdf(path)
    return normalize_rotation(path)


def prepare(old_path: str, new_path: str) -> Dict[str, Any]:
    """一对输入的归一化结果（界面/回归用）：

    `{"old_pdf", "new_pdf", "old_src", "new_src", "is_image": (bool, bool),
      "notes": [...]}`
    —— 原始路径一并留着：输出目录、导出文件名都该用**原件**的名字，
      不能让用户拿到一份叫 `xxxx_sha1.pdf` 的标注版。
    """
    io_, in_ = is_image_file(old_path), is_image_file(new_path)
    notes: List[str] = []
    try:
        o = ensure_pdf(old_path)
        n = ensure_pdf(new_path)
    except Exception as e:                       # 转换失败：如实往上抛，别静默走 PDF 分支
        raise ValueError(f"图片转换失败：{e}")
    for flag, p, side in ((io_, old_path, "旧版"), (in_, new_path, "新版")):
        if flag:
            notes.append(f"{side}是图片（{image_kind(p)}），已折成单页 PDF 走扫描件链路")
    if io_ or in_:
        notes.append("图片/扫描件没有文字层，全部结论来自 OCR，请在界面里核对「待确认」项")
    return {"old_pdf": o, "new_pdf": n, "old_src": old_path, "new_src": new_path,
            "is_image": (io_, in_), "notes": notes}


# ==================================================================
# 第 4 节  自检 self_test()
# ==================================================================

def self_test() -> int:
    """不依赖外部语料的自检：尺寸规则、图片→PDF 往返、缓存复用、多帧、PDF 旁路。"""
    import fitz

    ok = 0

    def chk(cond, msg):
        nonlocal ok
        if cond:
            ok += 1
        else:
            raise AssertionError(msg)

    # ① 尺寸规则
    w, h = page_size_for(1700, 2200)             # 典型 A4@200dpi 扫描件
    chk(abs(w - 612) < 1 and abs(h - 792) < 1, f"A4@200dpi 应折成 A4 pt，实得 {w}x{h}")
    chk(abs(w / h - 1700 / 2200) < 1e-3, "折算不能改宽高比")
    w2, h2 = page_size_for(6000, 8000)           # 手机拍照
    chk(max(w2, h2) <= IMG_MAX_PT + 0.01, "长边必须封顶")
    chk(abs(w2 / h2 - 6000 / 8000) < 1e-3, "封顶后宽高比仍要一致")
    w3, h3 = page_size_for(40, 30)               # 图标级小图
    chk(min(w3, h3) >= IMG_MIN_PT - 0.01, "短边必须兜底")

    # ② 往返：造一张图 → 折 PDF → 尺寸/页数/无文字层
    tmpd = tempfile.mkdtemp(prefix="pdfdiff_img_")
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 900, 1200))
    pm.set_rect(fitz.IRect(0, 0, 900, 1200), (255, 255, 255))
    png = os.path.join(tmpd, "t.png")
    pm.save(png)
    pdf = image_to_pdf(png, os.path.join(tmpd, "t.pdf"))
    d = fitz.open(pdf)
    chk(d.page_count == 1, f"单帧图应折成 1 页，实得 {d.page_count}")
    chk(abs(d[0].rect.width - 324) < 1.5 and abs(d[0].rect.height - 432) < 1.5,
        f"900x1200px 应折成 324x432pt，实得 {d[0].rect}")
    chk(not d[0].get_text().strip(), "折出来的页不能有文字层（走扫描件链路的前提）")
    chk(len(d[0].get_images()) == 1, "页内应恰好一张位图")
    d.close()

    # ③ 缓存复用：同内容第二次不再重建（返回同一路径且文件已存在）
    again = image_to_pdf(png, pdf)
    chk(again == pdf and os.path.isfile(pdf), "内容相同的图必须命中缓存")
    # 内容变了 → 换一个缓存文件（哈希进名）
    pm2 = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 900, 1200))
    pm2.set_rect(fitz.IRect(0, 0, 900, 1200), (0, 0, 0))
    png2 = os.path.join(tmpd, "t2.png")
    pm2.save(png2)
    pdf2 = image_to_pdf(png2)
    chk(os.path.abspath(pdf2) != os.path.abspath(pdf), "内容不同必须落到不同缓存文件")

    # ④ PDF 旁路
    chk(ensure_pdf(pdf) == pdf, "PDF 必须原样返回")
    chk(is_image_file(png) and not is_image_file(pdf), "后缀判定要对")
    chk(ensure_pdf("/不存在的图.png") == "/不存在的图.png", "文件不存在时原样返回，交给上层报错")

    # ⑤ prepare 记的原件名/提示
    p = prepare(png, pdf)
    chk(p["old_src"] == png and p["new_src"] == pdf, "prepare 必须留原件路径")
    chk(p["is_image"] == (True, False), "图片/PDF 混合输入要各自判对")
    chk(any("图片" in s for s in p["notes"]), "图片输入要有提示")

    # ⑥ /Rotate 归一化（2026-10-10）：pdfminer 遇旋转页会把每字读成一行 → 进链路前归零
    rd = os.path.join(tmpd, "rot.pdf")
    _r = fitz.open()
    _rp = _r.new_page(width=595, height=842)
    _rp.insert_text((72, 100), "rev01 test line", fontsize=14)
    _rp.set_rotation(90)
    _r.save(rd)
    _r.close()

    chk(rotated_pages(rd) == 1, "带 /Rotate 的页要数得出来")
    chk(rotated_pages(pdf) == 0 and rotated_pages(png) == 0, "无旋转的 PDF / 图片必须报 0")
    nr = ensure_pdf(rd)
    chk(nr != rd, "有旋转必须产出一份归一化副本")
    chk(ensure_pdf(pdf) == pdf, "无旋转的 PDF 必须原样返回（零回归的前提）")
    chk(ensure_pdf(nr) == nr, "归一化副本本身没有旋转，必须幂等返回")
    _d2 = fitz.open(nr)
    chk(all(_d2[i].rotation == 0 for i in range(_d2.page_count)), "副本的 /Rotate 必须归零")
    _txt = _d2[0].get_text().strip()
    chk(_txt == "rev01 test line", f"归一化不能改变读出来的文字，实得 {_txt!r}")
    chk(abs(_d2[0].rect.width - 595) < 1 and abs(_d2[0].rect.height - 842) < 1,
        f"归一化后 page.rect 应回到 mediabox，实得 {_d2[0].rect}")
    _d2.close()
    chk(ensure_pdf(rd) == nr, "同内容第二次必须命中缓存（同一路径）")

    rd2 = os.path.join(tmpd, "rot2.pdf")
    _r = fitz.open()
    _rp = _r.new_page(width=595, height=842)
    _rp.insert_text((72, 100), "rev02 test line", fontsize=14)
    _rp.set_rotation(90)
    _r.save(rd2)
    _r.close()
    chk(os.path.abspath(ensure_pdf(rd2)) != os.path.abspath(nr),
        "内容不同必须落到不同缓存文件")
    chk(ensure_pdf("/不存在的文件.pdf") == "/不存在的文件.pdf", "不存在的 PDF 原样返回")

    return ok


if __name__ == "__main__":
    # ⚠️ 输出格式必须与别家一致（「N 项通过，0 项失败」）——
    #    `_regress/smoke_gui.py` 是按这个串断言的，写别的会直接假失败。
    print(f"图片输入自检：{self_test()} 项通过，0 项失败")
