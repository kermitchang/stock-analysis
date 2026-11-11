# Hermes 操作指引：在樹莓派 4 安裝與維運 stockbot

> 給 Hermes 讀的文件。專案目錄：`~/stock-analysis`。所有指令都在這個目錄下執行。
> 金鑰在 `~/stock-analysis/.env`（不在 git 裡，權限 600），**不要印出、不要 commit、不要傳到 Telegram**。

## A. 第一次安裝

1. **停掉舊系統**（舊的 `main.py` / `run.sh` 已從 repo 移除，且會佔用 5000 埠）
   ```bash
   pm2 delete stock-analysis 2>/dev/null; pm2 save 2>/dev/null
   crontab -l | grep -v 'run.sh cron' | crontab -     # 移除舊的 14:30 排程（若有）
   pkill -f 'python3 main.py' 2>/dev/null || true
   ```
2. **更新程式並安裝**
   ```bash
   cd ~/stock-analysis && git pull
   ls -l .env                      # 應為 -rw-------；確認有 DEEPSEEK_API_KEY / DEEPSEEK_MODEL
   grep -q '^STOCKBOT_GIT_PUBLISH=' .env || echo 'STOCKBOT_GIT_PUBLISH=1' >> .env
   bash deploy/install.sh
   sudo loginctl enable-linger $USER   # 讓排程在沒有登入時也會執行（只需一次）
   ```
3. **建立歷史資料與模型**（約 30–60 分鐘，背景執行；避開 17:00–18:30）
   ```bash
   systemctl --user start --no-block stockbot-bootstrap
   journalctl --user -u stockbot-bootstrap -f      # 看進度，出現「bootstrap 完成」即可
   ```
4. **驗證**
   ```bash
   venv/bin/python -m stockbot status     # 應顯示 DeepSeek 餘額與資料筆數
   curl -s localhost:5000/healthz         # 應回 ok
   systemctl --user list-timers 'stockbot*'
   ```
   把 `status` 的輸出與網址 `http://<樹莓派IP>:5000` 回報給 Kermit。

## B. 設定 Hermes 的 Telegram 回報（cron job）

stockbot 的排程（systemd）負責計算，Hermes 只負責把結果傳給 Kermit：

| 時間 | 指令 | 傳給 Kermit |
|---|---|---|
| 週一至五 18:30 | `cd ~/stock-analysis && venv/bin/python -m stockbot summary` | 輸出原文 |
| 週六 08:30 | 讀 `reports/stockbot/weekly/` 最新一份 | 摘要：本週成績、規則庫變化、學習曲線 |

- `summary` 第二行若出現 **❌**（DeepSeek 餘額不足／金鑰無效）或 **⚠️**（餘額偏低），請在訊息最前面醒目提醒 Kermit 儲值。
- 若 `summary` 回「尚無日報」，或 `systemctl --user status stockbot-daily` 顯示失敗，請附上
  `journalctl --user -u stockbot-daily -n 40 --no-pager` 的最後幾行。

## C. Kermit 說「更新 stockbot」時

```bash
cd ~/stock-analysis && git pull && bash deploy/install.sh
```

## D. 常見問題

| 狀況 | 處理 |
|---|---|
| 網頁打不開 | `systemctl --user restart stockbot-web`；`journalctl --user -u stockbot-web -n 50` |
| 某天沒跑 | `venv/bin/python -m stockbot daily`（同一天已完成會自動略過；`--force` 強制重跑） |
| git push 失敗 | 報告與 memory/ 仍在本機，下次成功時會一起推；確認 `git status` 沒有衝突 |
| SD 卡損壞、重灌 | `git clone` 後放回 `.env`，`bash deploy/install.sh`，`venv/bin/python -m stockbot restore`，再 `weekly` 重新訓練 |
| DeepSeek 餘額用完 | 系統自動停用 LLM、量化分析照常；儲值後隔天自動恢復，不需重啟 |

## E. 舊的 Hermes 22:00 盤後分析

可以先保留 2–4 週，與 stockbot 的成績互相對照（stockbot 的網頁「報告」頁也看得到 Hermes 的日報）。
是否停用由 Kermit 決定。
