# memory/ — stockbot 的資料與經驗（自動產生，請勿手動編輯）

樹莓派每天分析完把 SQLite 資料庫匯出成文字檔並推到 GitHub，目的：
1. 人和 AI 都能直接讀，回顧系統哪裡做錯、該怎麼修正
2. git diff 看得出每天新增了什麼
3. SD 卡壞掉時可還原：`python -m stockbot restore`

| 檔案 | 內容 |
|---|---|
| `predictions/YYYY-MM.csv` | 每筆預測（variant：baseline / quant / quant_am / llm）＋ 對帳結果（`e_` 開頭欄位）。`rationale` 是 LLM 的完整判斷 JSON，`features_json` 是當時的特徵 |
| `attributions/YYYY-MM.csv` | 每檔每天的漲跌拆解（大盤/產業/個股）與主因、說明、證據、教訓 |
| `rules.json` | 規則庫：條件、回測樣本數、勝率、基準、lift、狀態（active/candidate/retired/rejected） |
| `model_runs.csv` | 每次訓練／校準的指標（AUC、Brier、方向命中、重要特徵） |
| `calibration.json` | 目前的區間校準係數 |
| `watchlist/YYYY-MM.csv` | 每天分析了哪些股票、為什麼被挑中 |
| `news/YYYY-MM.csv` | 新聞與 LLM 標註（情緒、類別、重要性） |
| `prices/` `chips/` `revenue.csv` `financials.csv` `stocks.csv` | 原始資料 |
| `llm_usage.csv` | 每日 LLM 呼叫次數與花費 |
