# 📊 台股盤後四維度分析系統

**免付費 · 自架自用 · AI 驅動的台股盤後分析儀表板**

---

## 🎯 功能

- ✅ **自動盤後分析**：每日台股收盤後，自動抓取大盤與個股資料
- ✅ **四維度深度解構**：消息題材、技術資金、財務底氣、戰略護城河
- ✅ **30元以下潛力股篩選**：鎖定 AI / 衛星 / 散熱 / 能源 四大產業
- ✅ **網頁儀表板**：用瀏覽器即可查看，支援手機
- ✅ **零成本**：全部使用免費公開 API，無需付費訂閱

---

## 🚀 快速啟動

### 第一步：安裝依賴

```bash
cd stock-analysis
chmod +x run.sh
./run.sh install
```

### 第二步：啟動網頁

```bash
./run.sh
```

打開瀏覽器前往：**http://localhost:5000** 或 **http://你的樹莓派IP:5000**

### 第三步：設定自動排程（可選）

```bash
# 編輯 crontab
crontab -e

# 加入此行（每日 14:30 自動分析，週一至週五）
30 14 * * 1-5 cd /home/kermitpi4/stock-analysis && ./run.sh cron
```

---

## ⚙️ 設定指南

編輯 `config.py` 即可調整：

| 參數 | 說明 | 預設值 |
|------|------|--------|
| `STOCK_WATCHLIST` | 四大產業的觀察名單（可增刪） | 各類 5~6 檔 |
| `MAX_STOCK_PRICE` | 股價篩選上限 | 30.0 元 |
| `MAX_STOCKS_PER_CATEGORY` | 每類最多分析幾檔 | 2 檔 |
| `CRON_SCHEDULE` | 排程時間 | 14:30 週一至週五 |

### 加入新股票到觀察名單

```python
STOCK_WATCHLIST = {
    "AI相關": ["2356", "3706", "2352", "2324"],  # 在清單中新增代號
    "衛星相關": ["2314", "3491", "6285"],
    "散熱相關": ["3071", "6275"],
    "能源相關": ["6806", "6170", "1513"],
    # 也可以自行新增分類...
    "你的新分類": ["0000", "0000"],
}
```

---

## 🌐 在外網觀看（免費方案）

### 方案一：Tailscale（推薦，最簡單）

```bash
# 在你的樹莓派安裝 Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# 在你的手機/筆電也安裝 Tailscale，登入同帳號
# 打開瀏覽器：http://樹莓派的TailscaleIP:5000
```

### 方案二：Cloudflare Tunnel（需要自己的 domain）

```bash
# 安裝 cloudflared
sudo apt install cloudflared

# 啟動 tunnel
cloudflared tunnel --url http://localhost:5000
```

兩種方案都 **完全免費**。

---

## 📁 專案結構

```
stock-analysis/
├── main.py              # Flask 網頁伺服器＋分析入口
├── data_fetcher.py      # 免費 API 資料抓取
├── analyzer.py          # 四維度分析引擎
├── config.py            # 設定檔（你在這改）
├── templates/
│   └── dashboard.html   # 網頁儀表板（深色主題）
├── data/                # 快取目錄（自動建立）
├── requirements.txt     # Python 依賴
├── run.sh              # 快速啟動腳本
└── README.md           # 本文件
```

---

## 📝 注意事項

- 此系統 **非投資建議**，分析結果僅供參考
- 資料來源為 TWSE 官方 API 與 Yahoo Finance，均為免費公開資料
- 盤後分析需在 **14:00 之後** 執行，以確保收盤資料完整
- 若 TWSE API 在非交易時段無資料，系統會自動沿用上次快取

---

【本報告已同步準時回報給 Kermit】
