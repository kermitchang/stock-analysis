# 📊 stockbot · 會從經驗中學習的台股盤後分析

每天收盤後自動抓資料、預測明日股價、隔天對帳、解釋漲跌原因，並把經驗累積成「經過驗證的規則」，
讓分析一天一天變準（或誠實地告訴你：還沒有變準）。部署在樹莓派 4，區網用瀏覽器看。

> 僅供研究學習，非投資建議。

---

## 系統怎麼「學習」

```
每日 17:30 收盤後                                   每週六 07:00
─────────────────────────────────────────          ─────────────────────────────
1. 收集：行情、三大法人、月營收、季財報、新聞      6. 重新訓練量化模型（walk-forward）
2. 對帳：昨天的預測 vs 今天實際（程式計算）        7. 校準預測區間寬度（目標 70% 命中）
3. 歸因：今天為什麼漲跌？                          8. 規則庫複驗：失效的退役
   報酬 = 大盤 + 產業 + 個股；個股部分請 LLM        9. LLM 從本週錯誤提出新假設
   讀新聞／財報解釋，並引用證據                       → 立刻用多年歷史回測 → 通過才啟用
4. 預測明日（50 檔）：                             10. 週報：AI vs 量化 vs 基準的學習曲線
   - 基準：收盤 ± 1×ATR
   - 量化模型：校準區間 + 上漲機率                  每日 08:15
   - LLM 分析師（10 檔重點股）：讀財報、新聞、      ─────────────────────────────
     歷史相似案例、已驗證規則、自己過去的成績      11. 早盤修正：用前一晚費半漲跌
5. 日報 → 網頁 → memory/ → git push                    修正今日預測區間（quant_am）
```

**經驗存在哪裡**
- **規則庫**：每條規則都要用 3 年歷史回測，勝率顯著高於基準（lift ≥ 3%、p < 0.05）才啟用。
  例如回測發現你原本方法論裡「均線多頭＋量增 → 偏多」沒有效果，「漲停 → 隔日續漲」有效。
- **案例庫**：每次預測、對帳、歸因都存下來；LLM 分析時會看到「這檔股票過去的預測哪裡錯了」與「歷史上最相似的 50 個情況隔天怎麼走」。
- **量化模型**：每週用最新資料重新訓練。
- 成績一律由程式計算，不採用 LLM 自評。

## 分析範圍

- **固定 28 檔**：原本的觀察名單（`stockbot/config.py` 的 `CORE_WATCHLIST`；6806 森崴已不在上市櫃清單）
- **動態 21 檔**：每天從七大主題（AI、半導體、IC 設計、系統廠、衛星、散熱、能源）中股價 < 50 元的股票，
  依量能異常、新聞熱度、動能、模型把握度挑選；挑進來至少留 5 個交易日。合計最多 50 檔。

## 資料來源與費用

| 來源 | 用途 | 費用 |
|---|---|---|
| 證交所／櫃買中心 OpenAPI | 公司清單、每日行情、月營收、季財報 | 免費 |
| 證交所 T86、櫃買 dailyTrade | 三大法人 | 免費（程式已限速） |
| 鉅亨網、Google News RSS | 新聞 | 免費 |
| Yahoo Finance（yfinance） | 歷史行情、費半、匯率 | 免費 |
| **DeepSeek API** | 新聞標註、歸因、分析、規則提案 | **付費**，約每天 US$0.1–0.3 |

DeepSeek 保護機制：每日花費上限（預設 US$0.5）、餘額偏低在日報／Telegram／網頁顯示 ⚠️、
**餘額用完（HTTP 402）自動停止 LLM**，量化分析照常進行並顯示 ❌。

## 安裝（樹莓派 4）

```bash
cd ~/stock-analysis
git pull
cp .env.example .env    # 若還沒有 .env；填入 DEEPSEEK_API_KEY，權限 600，不會上傳 git
bash deploy/install.sh  # 建立 venv、安裝套件、安裝 systemd 排程與網頁服務
systemctl --user start stockbot-bootstrap   # 第一次：下載 3 年歷史、訓練模型（約 30–60 分鐘）
journalctl --user -u stockbot-bootstrap -f  # 看進度
```

網頁：`http://樹莓派IP:5000`（總覽 / 個股 / 學習進度 / 報告）

更新程式：`git pull && bash deploy/install.sh`

## 常用指令

```bash
venv/bin/python -m stockbot status    # DeepSeek 餘額、最近執行、資料量
venv/bin/python -m stockbot daily     # 手動跑每日分析（同一天只會跑一次，--force 重跑）
venv/bin/python -m stockbot summary   # 精簡摘要（給 Telegram）
venv/bin/python -m stockbot weekly    # 手動跑每週學習
venv/bin/python -m stockbot restore   # SD 卡壞掉時，從 git 的 memory/ 還原資料庫
systemctl --user list-timers 'stockbot*'
journalctl --user -u stockbot-daily -n 100
```

## 目錄

```
stockbot/            程式
  collectors.py      資料收集          features.py   特徵工程（防偷看未來）
  quant.py           量化模型/區間校準  rules.py      規則庫（回測、啟用、退役）
  analysis.py        歸因與 LLM 分析師  evaluate.py   對帳
  llm.py             DeepSeek（含餘額保護） report.py   日報/週報/摘要
  memory_export.py   匯出/還原 memory/  web.py        網頁
  pipeline.py        流程主控           config.py     設定（觀察名單、門檻）
deploy/              安裝腳本與 systemd 排程；HERMES_SETUP.md 給 Hermes 的操作指引
reports/stockbot/    stockbot 日報、週報（自動 push）
reports/daily|weekly Hermes 原本的 AI 報告（保留）
memory/              資料與經驗的文字匯出（自動 push，可還原資料庫）
methodology/         分析方法論
tests/               單元測試（python -m pytest -q）
```
