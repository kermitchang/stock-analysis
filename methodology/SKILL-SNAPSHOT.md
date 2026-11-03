---
name: taiwan-stock-analysis
title: 台股盤後四維度分析系統
description: 自架零成本台股分析儀表板 — 四維度評估法、50元以下潛力股篩選、GitHub Pages + Actions 免費部署
triggers:
  - 'stock analysis taiwan'
  - '台股 分析 程式'
  - '股票 分析 儀表板'
  - 'Taiwan stock screener'
category: daily-life
---

# 台股盤後四維度分析系統

## Overview

Self-hosted, zero-cost Taiwan stock analysis dashboard built with Flask + free public APIs (TWSE + yfinance). Generates structured daily reports using a proprietary **4-dimension evaluation framework**.

**Reference files**:
- `references/4-dimension-framework.md` — full detailed framework with checklists, KD trading rules, institutional flow analysis, and real examples
- `references/cron-delivery-debugging.md` — diagnosing cron job delivery failures (origin null bug, detection steps, safe delivery targets)
- `references/daily-cron-prompt.md` — exact prompt template used in the 22:00 daily Hermes cron job (6-8 stocks, 4-dimension + prediction + error review)
- `references/weekly-cron-prompt.md` — exact prompt template used in the Saturday 06:00 weekly summary cron job
- `references/stock-universe.md` — full stock watchlist with 29 stocks across 7 categories
- `references/github-sync-topology.md` — **兩套系統的辨別**（停更 Flask repo vs 現行 Hermes cron 報告）+ **AI 報告 GitHub 同步機制完整說明**（A1 方案：reports/ 目錄、sync-stock-reports.sh、cron job、實作陷阱、診斷指令）

## User Preferences / Stock Selection Rules

These are user preferences — do not override:

1. **Price**: must be **under NT$50** (closing price that day)
2. **Sectors** (seven categories, dynamically filtered by price):
   - **AI-related**: AI server supply chain, AI PC/NB ODM, edge computing, AI sensors
   - **半導體** (Semiconductor): memory, wafer foundry, power semiconductor
   - **IC設計** (IC Design): MCU, power management, multimedia IC
   - **系統廠** (System Integrators): PC brand, ODM, motherboard/server
   - **Satellite-related**: LEO satellites, ground stations, satellite communication components
   - **Thermal-related**: cooling modules, fans, liquid/immersion cooling, vapor chambers
   - **Energy-related**: solar, wind, energy storage, grid, EV chargers, green energy
3. **Quantity**: pick **6–8 stocks** dynamically each day (different stocks based on volume/performance, not a fixed list), try to cover different sectors
4. **Do NOT hardcode** TSMC (2330) or Innolux (3481) as core analysis targets — those were examples only
5. **Config**: stocks live in `config.py` under `STOCK_WATCHLIST` dict, grouped by category — currently 29 stocks across 7 categories
6. **All data sources MUST be free** — no paid APIs or services (Yahoo Finance, TWSE, Google News RSS only)

## The 4-Dimension Evaluation Framework

Every stock is analyzed against these four dimensions:

### D1: News & Re-rating (消息與題材面)
- Is a legacy/low-margin stock being revalued by entering a new supply chain?
- Global AI infrastructure / satellite / energy trends affecting this stock
- Asset disposal, factory sales, financial engineering (e.g. selling factory for cash)

### D2: Technical & Smart Money (技術與資金面)
- Moving average structure: 5MA / 20MA / 60MA — bullish alignment?
- Volume-price momentum: volume surge on breakout? Institutional accumulation?
- KD Stochastic: golden cross near 20 (buy signal) vs death cross above 80 (overheated)
- Three major institutional flows: foreign investors, investment trusts, dealers

### D3: Fundamental Backing (財務底氣面)
- Monthly revenue (MoM / YoY), gross margin, operating margin
- CAPEX growth — expanding factory/R&D validates future orders

### D4: Strategic Moat (戰略與管理面)
- Pricing power: monopoly position in supply chain?
- Insider trading: board/director/large shareholder position changes?

### D5: Tomorrow's Price Prediction (免費技術面推估)
Predict next-day price range (low～high) using only free technical indicators:
- Recent 5/20 day moving average trend (short-term momentum direction)
- KD Stochastic position: oversold (<20 → possible bounce) vs overbought (>80 → possible pullback)
- Support/resistance levels: previous swing highs/lows, round numbers, MA positions
- Volume analysis: whether volume supports breakout or reversal
- Format per stock: predicted range, short-term direction (bullish/bearish/sideways), brief rationale

### D6: Previous Day Comparison & Error Review
When the previous day's report exists (in `~/.hermes/cron/output/<job_id>/`), compare:
1. Yesterday's prediction vs today's actual (open/high/low/close)
2. Analyze reasons for divergence:
   - Market-wide impact (US markets, global events)
   - Sector rotation (money flowing out/in)
   - Stock-specific news (conference calls, news, institutional moves)
   - Technical signal changes (MA break, KD cross, etc.)
3. **Error classification per stock**:
   - 🏭 Sector misjudgment
   - 📊 Technical misread
   - 🌍 Macro/international interference
   - 📰 Unexpected event
   - 🎯 Other
4. **Learning summary**: 1-2 sentences per mispredicted stock explaining what was learned and how to adjust next time

### Scoring Formula
| Signal | Adjustment |
|--------|-----------|
| Base | 50 (neutral) |
| Bullish MA alignment | +15 |
| Bearish MA alignment | -15 |
| Volume surge (+1.5x avg) | +10 |
| Volume shrink (<0.7x avg) | -10 |
| KD golden cross / oversold | +10 |
| KD death cross / overbought | -10 |
| Matched themes/news | +10 |
| **Final** | 70+ = Bullish, 45-69 = Neutral, <45 = Bearish |

## Architecture (Flask Web Server)

```
stock-analysis/
├── main.py              # Flask web server (routes: /, /api/latest, /api/run)
├── data_fetcher.py      # Free API fetcher (TWSE + yfinance + Google News RSS)
├── analyzer.py          # 4-dimension engine (KD, MA, volume, scoring)
├── config.py            # STOCK_WATCHLIST + MAX_STOCK_PRICE + params
├── templates/
│   └── dashboard.html   # Dark-themed responsive dashboard
├── data/                # Cache dir (auto-created)
├── requirements.txt     # flask, pandas, numpy, yfinance, requests
├── run.sh               # One-click launcher (install / cron / server)
└── README.md            # Full docs
```

### Free Data Sources
- **TWSE API** (臺灣證券交易所): market index, institutional trading, daily stock prices — no API key
- **yfinance**: historical OHLCV data for technical analysis
- **Google News RSS**: stock news headlines

### Free Remote Access
- **Tailscale**: zero-config VPN, completely free (recommended for Pi)
- **Cloudflare Tunnel**: free tier (requires a domain)

## GitHub Actions + Pages 自動部署（完全免費版）

An alternative to the Flask server — deploy the analysis as a **static site** on GitHub Pages, zero hosting cost.

### 轉換方式

Instead of `main.py` (Flask), use `generate_site.py` which:
1. Runs the same analysis engine (via `data_fetcher.py` + `analyzer.py`)
2. Renders `templates/dashboard.html` with Jinja2 (same template, no Flask needed)
3. Outputs `_site/index.html` + `_site/data/analysis.json`

### GitHub Actions Workflow

```yaml
# .github/workflows/stock-analysis.yml
on:
  schedule:
    - cron: '30 6 * * 1-5'  # 台灣 14:30（收盤後1hr）= UTC 06:30
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

jobs:
  analyze-and-deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11', cache: 'pip' }
      - run: pip install -r requirements.txt
      - run: python generate_site.py
      - uses: actions/upload-pages-artifact@v3
        with: { path: '_site' }
      - uses: actions/deploy-pages@v4
```

### 啟用步驟
1. Push code to GitHub repo
2. Repo Settings → Pages → Source 選「GitHub Actions」
3. Workflow 會自動跑，網站出現在 `https://<user>.github.io/<repo>/`
4. 之後每天收盤後自動更新

### 費用試算（GitHub Free 方案）

| 項目 | 用量（估） | 免費額度 |
|------|-----------|---------|
| Actions 分鐘 | ~44分/月（2分×22天） | 2,000分/月 |
| Pages 儲存 | < 1 MB | 1 GB |
| Pages 頻寬 | ~50 MB/月 | 100 GB |
| **總計** | **$0** | — |

## Hermes Cron Job 整合（Telegram 推播）

Two cron jobs work together:

### Daily Report (22:00 Mon-Fri)

```bash
hermes cron create \
  --schedule "0 22 * * 1-5" \
  --prompt "台股盤後分析 prompt" \
  --deliver "telegram:8815160325" \
  --enabled-toolsets "web,terminal,file"
```

The daily prompt must be self-contained and include:
- 6-8 stocks to analyze across 7 sectors
- 4-dimension framework (D1-D4)
- Tomorrow's price prediction (D5) — free technical indicators only
- Previous day comparison & error review (D6) — reads yesterday's output from `~/.hermes/cron/output/<job_id>/`
- Error classification and learning summary per stock
- Full report format: 大盤觀測 → 個股深度解構 → 預測錯誤檢討 → 明日大盤研判 → 綜合結論
- **Free data only** mandate (Yahoo Finance, TWSE, Google News RSS — no paid services)

### Weekly Summary Report (Saturday 06:00)

```bash
hermes cron create \
  --schedule "0 6 * * 6" \
  --prompt "週度預測準確度檢討" \
  --deliver "telegram:8815160325" \
  --enabled-toolsets "terminal,file"
```

The weekly job:
1. Reads all Mon-Fri report files from `~/.hermes/cron/output/<daily_job_id>/`
2. Compiles statistics: total predictions, accuracy rate, error breakdown by type
3. Best/worst prediction stocks of the week
4. Chooses 2-3 most instructive errors for deep-dive review
5. Proposes improvement directions for next week
6. **Saves report to**: `~/Documents/StockAnalysis/{year}/Week-{weeknum}-{start_date}/週回顧報告.md`

### Delivery Rules

## Quick Start (Flask 版)

```bash
cd stock-analysis
./run.sh install   # create venv + install deps
./run.sh           # start web server on port 5000
# Open http://localhost:5000
```

### Cron Auto-Run (daily after market close)
```bash
crontab -e
30 14 * * 1-5 cd /home/kermitpi4/stock-analysis && ./run.sh cron
```

## Pitfalls
- ⚠️ **兩套系統極易混淆**：`~/stock-analysis`（Flask + GitHub Actions，`kermitchang/stock-analysis`，2026-07 停更）是舊的分析引擎；現行引擎是 **Hermes cron AI 分析**（22:00 日報 → Telegram + `~/.hermes/cron/output/`，週六週報 → `~/Documents/StockAnalysis/`）。**但同一個 repo 現在也用來存放 AI 報告**（`reports/daily|weekly/`，2026-09-18 起每天 23:00 自動同步）。被問到「股票分析有沒有上 GitHub」時，先確認問的是「分析引擎」還是「報告存放」。詳見 `references/github-sync-topology.md`。
- ✅ **AI 報告已同步 GitHub**（2026-09-18 起）：`sync-stock-reports.sh`（cron `6c9fbec84ce4`，每天 23:00，`no_agent` 零 token）把日報/週報複製到 `~/stock-analysis/reports/` 並 commit+push。目的：讓舊系統未來能讀取 AI 報告作為學習資料。改動此機制前先讀 `references/github-sync-topology.md` 的實作陷阱（變更判斷要交給 git，不可只比對檔案內容）。
- TWSE API returns empty data on weekends/holidays — cache falls back automatically
- yfinance requires `.TW` suffix internally (handled in `data_fetcher.py`)
- Market close is 13:30 TW time; wait until 14:00+ for complete data
- `config.py` uses a dict watchlist — add/remove stock IDs freely; price filter is applied at runtime
- `MAX_STOCKS_PER_CATEGORY` limits how many stocks per sector get analyzed each run
- Flask debug mode is OFF by default (set `debug=True` in `main.py` for development)
- Cache TTL: 2h for market data, 5 min for current prices (adjustable in `data_fetcher.py`)
- GitHub Pages deploy-pages step **fails silently** if Pages is not enabled in repo settings — must go to Settings → Pages → Source → GitHub Actions first
- SSH key auth for Pi → GitHub: `ssh-keygen -t ed25519` → add pub key to GitHub Settings → SSH and GPG keys
- For `generate_site.py`, `jinja2` must be in `requirements.txt` (not a Flask dependency, often missed)
- **Cron delivery `origin` null bug**: when `deliver` is set to `"origin"` and the cron job was created in a **different session**, the `origin` field is null and the output is saved to disk but never pushed. Always use explicit `"telegram:<chat_id>"` or `"line:<user_id>"` instead.
- **User explicitly forbids LINE delivery**: the user does NOT want stock reports pushed to LINE due to message quota limits. Never set `deliver: "all"` — it wastes LINE quota. Use Telegram only.
- **Cron delivery diagnostics**: if the user reports not receiving the report, check `cronjob action='list'` for `last_run_at`/`last_status`, verify the output file exists under `~/.hermes/cron/output/<job_id>/`, and confirm `deliver` is set to an explicit chat target. See `references/cron-delivery-debugging.md`.
- **Provider-unresponsive error**: `RuntimeError: Provider has been unresponsive (no response received) for 5 consecutive stale attempts` means the LLM provider (DeepSeek) was temporarily down — a **transient service issue**, not a config problem. Diagnose: verify provider health with a direct `curl` to the DeepSeek API (models + a tiny chat call), then re-run via `cronjob action='run'`. A ~5KB output file (vs normal 15-30KB) confirms a failed run. See `references/cron-delivery-debugging.md`.
