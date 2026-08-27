#!/usr/bin/env bash
# 开发/测试环境一键脚本:在 venv 中跑冒烟测试(不依赖 astrbot)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "[dev] 创建 venv 并安装依赖..."
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
fi

echo "[dev] 运行冒烟测试..."
OCVERSE_SMOKE_OUT="${OCVERSE_SMOKE_OUT:-}" .venv/bin/python tests/smoke_test.py
echo "[dev] 完成。"
