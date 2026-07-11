#!/bin/bash
# 台股分析系統 - 快速啟動腳本
# 用法：
#   ./run.sh          → 啟動網頁伺服器（前台）
#   ./run.sh install  → 安裝依賴套件
#   ./run.sh cron     → 排程一次分析（給 cron 用）
#   ./run.sh pm2      → 用 pm2 背景執行（建議正式使用）

set -e
cd "$(dirname "$0")"

VENV_DIR="venv"

install_deps() {
    echo "📦 安裝 Python 套件..."
    if [ ! -d "$VENV_DIR" ]; then
        python3 -m venv "$VENV_DIR"
        echo "✅ 虛擬環境已建立"
    fi
    source "$VENV_DIR/bin/activate"
    pip install --quiet -r requirements.txt
    echo "✅ 套件安裝完成"
}

case "${1:-}" in
    install)
        install_deps
        ;;
    cron)
        # 排程模式：執行一次分析後結束
        source "$VENV_DIR/bin/activate" 2>/dev/null || true
        python3 main.py --run
        ;;
    pm2)
        # 用 pm2 背景執行（需要先 npm install -g pm2）
        source "$VENV_DIR/bin/activate" 2>/dev/null || true
        pm2 start main.py --name "stock-analysis" --interpreter python3 -- --port 5000
        pm2 save
        echo "✅ pm2 已啟動，執行 pm2 logs stock-analysis 查看日誌"
        ;;
    *)
        # 預設：啟動網頁伺服器
        if [ ! -d "$VENV_DIR" ]; then
            install_deps
        fi
        source "$VENV_DIR/bin/activate"
        echo "🚀 啟動台股分析網頁伺服器..."
        python3 main.py
        ;;
esac
