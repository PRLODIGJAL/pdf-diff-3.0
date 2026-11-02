#!/bin/bash
cd "$(dirname "$0")" || exit 1

# 环境未就绪时自动安装（已就绪则秒过）
if [ ! -x ".venv/bin/python" ]; then
  echo "首次运行，正在准备环境…"
  python3 setup_env.py || exit 1
fi

# 3.0：单窗口直显版（老引擎识别 + 单窗口并排标注，无文字清单）
exec .venv/bin/python -B diff_app3.py
