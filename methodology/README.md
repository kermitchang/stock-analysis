# 🧠 台股分析：方法論（Methodology）

本目錄收錄 **Hermes AI 台股分析系統**的完整分析方法與設定，供未來
改進分析邏輯、回測驗證、或由舊系統（`analyzer.py`）參考學習。

> 📌 分析**結果**在 [`../reports/`](../reports/)；本目錄是分析**方式**。

---

## 📁 檔案說明

| 檔案 | 內容 | 更新方式 |
|:-----|:------|:---------|
| **`4-dimension-framework.md`** | 四維度評估框架（消息/技術/財務/護城河）+ 評分公式 + KD 交易規則 | 手動（skill 更新時） |
| **`daily-prompt.md`** | **實際運行中**的每日分析 prompt（從 cron job 提取） | 🔄 **自動**（每次同步） |
| **`weekly-prompt.md`** | **實際運行中**的每週檢討 prompt（從 cron job 提取） | 🔄 **自動**（每次同步） |
| **`daily-prompt-template.md`** | 每日 prompt 的完整範本（含更多說明） | 手動 |
| **`weekly-prompt-template.md`** | 週報 prompt 的完整範本 | 手動 |
| **`stock-universe.md`** | 觀察名單（29 檔 × 7 類產業） | 手動 |
| **`SKILL-SNAPSHOT.md`** | Hermes skill 完整快照（含所有規則與陷阱） | 手動 |

---

## 🎯 分析系統架構

```
每日 22:00（週一至五）
    ↓
Hermes cron job 執行（DeepSeek AI）
    ↓
1. 抓取免費資料（Yahoo Finance / TWSE / Google News RSS）
2. 套用四維度框架分析 6-8 檔 50 元以下潛力股
3. 預測明日股價區間
4. 比對昨日預測 vs 今日實際 → 錯誤檢討
5. 產出報告 → Telegram + 存入本 repo 的 reports/daily/
    ↓
每週六 06:00
    ↓
彙整本週所有報告 → 準確度統計 → 錯誤模式分析 → reports/weekly/
```

---

## 📐 四維度框架摘要

| 維度 | 評估重點 |
|:-----|:---------|
| **D1 消息與題材** | 轉型題材、供應鏈地位變化、全球趨勢（AI/衛星/能源） |
| **D2 技術與資金** | 均線結構（5/20/60MA）、量價、KD、三大法人買賣超 |
| **D3 財務底氣** | 月營收 MoM/YoY、毛利率、營益率、CAPEX |
| **D4 戰略與管理** | 定價權、護城河、董監事持股變化 |
| **D5 明日預測** | 用免費技術指標推估隔日價格區間 |
| **D6 錯誤檢討** | 比對昨日預測 vs 今日實際，歸類錯誤類型 |

### 評分公式

| 訊號 | 加減分 |
|:-----|:------:|
| 基礎分 | 50 |
| 均線多頭排列 | +15 |
| 均線空頭排列 | -15 |
| 量增（>1.5倍均量） | +10 |
| 量縮（<0.7倍均量） | -10 |
| KD 黃金交叉/超賣 | +10 |
| KD 死亡交叉/超買 | -10 |
| 題材/消息吻合 | +10 |

**結論**：70+ 看多｜45-69 中立｜<45 看空

---

## 🔬 未來學習用途

### 1. 回測預測準確度
- `reports/daily/*.md` 含「明日預測區間」→ 與隔日實際 OHLC 比對
- 統計各產業、各技術型態的命中率
- 找出「哪些條件下預測特別不準」→ 調整評分權重

### 2. 錯誤模式學習
- 報告的「預測錯誤檢討學習區」已人工歸類 5 種錯誤類型：
  - 🏭 產業面誤判
  - 📊 技術面誤判
  - 🌍 大盤/國際面干擾
  - 📰 突發事件
  - 🎯 其他（籌碼、大戶）
- 可抽取為規則調整資料（例：KD 黃金交叉在 5MA 下彎時失效）

### 3. 與舊系統交叉驗證
- 對照 `analyzer.py` 的篩選結果 vs AI 選股
- 找出「AI 選到但舊系統沒選」的案例 → 兩邊邏輯互相校正

### 4. Prompt 演進追蹤
- `git log methodology/daily-prompt.md` 可看到分析方式如何隨時間調整
- 對照同時期報告的準確度，評估「改 prompt 到底有沒有效」

---

## ⚠️ 重要提醒

1. **`daily-prompt.md` / `weekly-prompt.md` 是自動產生的**
   - 由 `~/.hermes/scripts/sync-stock-reports.sh` 從 Hermes cron job 提取
   - 手動編輯會在下次同步時被覆蓋
   - 要改分析方式 → 改 Hermes cron job 的 prompt

2. **框架文件（4-dimension-framework.md 等）是手動維護的**
   - 來源：`~/.hermes/skills/daily-life/taiwan-stock-analysis/references/`
   - skill 更新後需手動複製到此目錄

3. **所有資料來源必須免費**
   - Yahoo Finance、TWSE 公開資料、Google News RSS
   - 禁止使用任何付費 API

---

*本目錄由 Hermes AI 系統維護，最後更新見 git log*
