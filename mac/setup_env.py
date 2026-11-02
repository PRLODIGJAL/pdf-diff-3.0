# -*- coding: utf-8 -*-
"""
setup_env.py —— 环境自检与安装（零第三方依赖，只用标准库）
启动脚本会先跑它：.venv 已就绪就秒过，否则自动创建并安装依赖。
也可单独执行：python3 setup_env.py
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
# 3.0 像素级引擎只需 numpy + pymupdf + 拖放库；pdfminer/pdfplumber/openpyxl
# 是 2.0 老引擎的依赖，3.0 运行期用不到，已从清单移除（省安装时间与磁盘）。
PKGS = ["numpy", "pymupdf", "tkinterdnd2"]

IS_WIN = os.name == "nt"


def venv_python() -> str:
    p = os.path.join(VENV, "Scripts" if IS_WIN else "bin", "python.exe" if IS_WIN else "python")
    return p if os.path.exists(p) else ""


def run(cmd, **kw):
    print("  $", " ".join(cmd))
    return subprocess.run(cmd, **kw).returncode


def main() -> int:
    # --rebuild：删除旧 .venv 后重建（Windows「重装运行环境.bat」专用）。
    rebuild = "--rebuild" in sys.argv[1:]
    if rebuild and os.path.isdir(VENV):
        print("[·] 删除旧虚拟环境，准备重建…")
        shutil.rmtree(VENV, ignore_errors=True)

    py = venv_python()
    if py:
        print(f"[✓] 已存在虚拟环境：{py}")
    else:
        print("[·] 未找到 .venv，开始创建…")
        if run([sys.executable, "-m", "venv", VENV]) != 0:
            print("[✗] venv 创建失败，请确认已安装 Python 3.9+")
            return 1
        py = venv_python()
        if not py:
            print("[✗] venv 创建后仍找不到解释器")
            return 1

    need = []
    rc = subprocess.run([py, "-c", "import numpy, pymupdf, tkinterdnd2"],
                        capture_output=True)
    if rc.returncode != 0:
        need = PKGS

    if need:
        print(f"[·] 安装依赖：{' '.join(need)}")
        pip = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
        if run(pip + need) != 0:
            print("[·] 直连安装较慢，改用国内镜像重试…")
            if run(pip + ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"] + need) != 0:
                print("[✗] 依赖安装失败，请手动执行：")
                print(f"    {py} -m pip install {' '.join(need)}")
                return 1
    print("[✓] 环境就绪")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
