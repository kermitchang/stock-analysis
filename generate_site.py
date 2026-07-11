"""
靜態網站產生器 - generate_site.py
===================================
在 GitHub Actions 中執行：
1. 抓取台股資料
2. 執行四維度分析
3. 用 Jinja2 渲染靜態 HTML
4. 輸出到 _site/ 目錄（GitHub Pages 會自動發佈）
"""
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 確保可以 import 同目錄的模組
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config import STOCK_WATCHLIST, MAX_STOCK_PRICE, MAX_STOCKS_PER_CATEGORY
from data_fetcher import DataFetcher
from analyzer import StockAnalyzer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("generate-site")

OUTPUT_DIR = BASE_DIR / "_site"
DATA_DIR = BASE_DIR / "data"


def main():
    logger.info("=== 開始台股分析（靜態網站模式）===")
    fetcher = DataFetcher()
    analyzer = StockAnalyzer()

    # ── 1. 大盤資料 ──
    logger.info("→ 抓取大盤資料...")
    market = fetcher.fetch_market_index()
    institutional = fetcher.fetch_institutional_trading()

    # ── 2. 篩選 30 元以下個股 ──
    logger.info("→ 篩選符合條件個股...")
    qualified = []
    for category, stock_ids in STOCK_WATCHLIST.items():
        hits = 0
        for sid in stock_ids:
            if hits >= MAX_STOCKS_PER_CATEGORY:
                break
            cur = fetcher.fetch_stock_current(sid)
            if cur is None:
                continue
            price = cur.get("close", 999)
            if price < MAX_STOCK_PRICE:
                qualified.append({"category": category, "data": cur})
                hits += 1
                logger.info(f"  ✓ {category}: {sid} ({price:.2f}元)")

    # ── 3. 逐一分析 ──
    logger.info(f"→ 分析 {len(qualified)} 檔個股...")
    stock_analyses = []
    for item in qualified:
        sd = item["data"]
        sid = sd["stock_id"]
        history = fetcher.fetch_stock_history(sid, days=120)
        news = fetcher.fetch_news(sid)
        analysis = analyzer.analyze(sd, history, news)
        analysis["category"] = item["category"]
        stock_analyses.append(analysis)
        logger.info(f"  ✓ {sid} {analysis['stock_name']} 分析完成")

    # ── 4. 大盤技術摘要 ──
    change = market.get("change_pct", 0)
    if abs(change or 0) < 0.3:
        direction = "🔸 平盤震盪"
    elif (change or 0) > 0:
        direction = "📈 資金偏多"
    else:
        direction = "📉 賣壓浮現"

    # ── 5. 組合結果 ──
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "institutional": institutional,
        "market_technical": {
            "direction": direction,
            "summary": f"今日大盤{'上漲' if (change or 0) >= 0 else '下跌'} {abs(change or 0):.2f}%"
        },
        "stocks": stock_analyses,
        "stats": {
            "total_watchlist": sum(len(v) for v in STOCK_WATCHLIST.values()),
            "qualified": len(stock_analyses),
            "price_limit": MAX_STOCK_PRICE,
        },
    }

    # ── 6. 輸出靜態檔案 ──
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "data").mkdir(exist_ok=True)

    # 寫入 JSON
    json_path = OUTPUT_DIR / "data" / "analysis.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"✓ JSON 寫入 {json_path}")

    # 用 Jinja2 渲染 HTML
    try:
        from jinja2 import Environment, FileSystemLoader
        env = Environment(loader=FileSystemLoader(str(BASE_DIR / "templates")))
        template = env.get_template("dashboard.html")

        html = template.render(
            analysis=result,
            generated_at=result["generated_at"],
            now=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

        html_path = OUTPUT_DIR / "index.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"✓ HTML 寫入 {html_path}")

    except Exception as e:
        logger.error(f"HTML 渲染失敗: {e}")
        raise

    # ── 7. 複製靜態資源（如果有） ──
    static_src = BASE_DIR / "static"
    if static_src.exists():
        shutil.copytree(static_src, OUTPUT_DIR / "static", dirs_exist_ok=True)

    logger.info(f"=== 完成！{len(stock_analyses)} 檔個股已分析，網站輸出至 {OUTPUT_DIR} ===")


if __name__ == "__main__":
    main()
