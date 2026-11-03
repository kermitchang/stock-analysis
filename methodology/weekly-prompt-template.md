# 台股週度預測準確度檢討 Cron Prompt 模板

This is the prompt used in the weekly `04e4b23f0e8c` Hermes cron job (Saturday 06:00).

## Prompt Structure

```
你現在是台股分析回顧分析師。今天星期六，請回顧本週（週一至週五）所有的台股盤後覆盤報告，撰寫一份**週度預測準確度檢討報告**，並存檔到指定位置。

## Step 1：讀取本週所有報告
從 ~/.hermes/cron/output/2b7d1c0ba906/ 找出本週一到五的 .md 檔案。

## Step 2：統整分析
### A. 預測準確度統計
- 總預測次數、準確次數、方向正確、方向錯誤、準確率

### B. 錯誤類型統計
- 🏭產業面 / 📊技術面 / 🌍大盤干擾 / 📰突發事件 / 🎯其他

### C. 最佳/最差預測
- 本週最準的個股（檔名、次數、準確率）
- 本週最不準的個股（檔名、次數、準確率）

### D. 趨勢歸納
- 哪些產業較準確？哪些較差？
- 有無重複出現的錯誤模式？
- 學到的教訓與改進建議

## Step 3：存檔
存到 ~/Documents/StockAnalysis/{年份}/Week-{週數}-{起始日期}/週回顧報告.md

## 報告格式（Markdown）
1. 封面資訊：報告期間、涵蓋交易日數、總分析檔次
2. 準確度統計表
3. 錯誤類型分布
4. 最佳/最差預測分析
5. 錯誤案例深度檢討（選2~3個最有意義的）
6. 下週改進方向
```

## Cron Job Config

```yaml
job_id: 04e4b23f0e8c
name: 台股週報：預測準確度檢討
schedule: "0 6 * * 6"
deliver: "telegram:8815160325"
enabled_toolsets: [terminal, file]
repeat: forever
```
