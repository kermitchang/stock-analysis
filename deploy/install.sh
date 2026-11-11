#!/usr/bin/env bash
# 安裝 / 更新 stockbot（樹莓派 4，Raspberry Pi OS）。可重複執行。
#   第一次：bash deploy/install.sh && systemctl --user start stockbot-bootstrap
#   之後更新：git pull && bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"

echo "📦 安裝 Python 套件…"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ -f .env ]; then
  chmod 600 .env
  grep -q '^DEEPSEEK_API_KEY=.\+' .env && echo "🔑 已找到 DeepSeek 金鑰（.env）" || echo "⚠️  .env 裡沒有 DEEPSEEK_API_KEY，將只跑量化分析"
else
  echo "⚠️  找不到 .env（可參考 .env.example）；將只跑量化分析"
fi

echo "⏱  安裝排程（systemd --user）…"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
for f in deploy/systemd/*.service deploy/systemd/*.timer; do
  sed "s|__DIR__|$DIR|g" "$f" > "$UNIT_DIR/$(basename "$f")"
done
systemctl --user daemon-reload
systemctl --user enable --now stockbot-daily.timer stockbot-morning.timer stockbot-weekly.timer
systemctl --user enable stockbot-web.service
systemctl --user restart stockbot-web.service

if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes; then
  echo "ℹ️  要讓排程在沒有登入時也執行，請執行一次：sudo loginctl enable-linger $USER"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "✅ 完成。網頁：http://${IP:-樹莓派IP}:5000"
echo "   排程：systemctl --user list-timers 'stockbot*'"
if [ ! -f data/stockbot.db ] && [ -d memory/prices ]; then
  echo "   偵測到 memory/ 但沒有資料庫 → 可執行 venv/bin/python -m stockbot restore 從 git 還原"
fi
