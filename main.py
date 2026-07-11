"""
台股分析網頁伺服器 - main.py
==============================
Flask 網頁伺服器，提供：
1. 首頁儀表板：展示最新大盤與個股分析
2. 手動更新按鈕：立即執行最新分析
3. 自動排程：每日收盤後自動更新

啟動方式：python main.py
瀏覽網址：http://localhost:5000
"""
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from config import DATA_DIR, STOCK_WATCHLIST, MAX_STOCK_PRICE, MAX_STOCKS_PER_CATEGORY
from data_fetcher import DataFetcher
from analyzer import StockAnalyzer

# ─── 初始化 ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("stock-app")

app = Flask(__name__)
fetcher = DataFetcher()
analyzer = StockAnalyzer()

ANALYSIS_RESULT_FILE = DATA_DIR / "latest_analysis.json"
LAST_RUN_FILE = DATA_DIR / "last_run.txt"


# ════════════════════════════════════════
#  路由：網頁頁面
# ════════════════════════════════════════

@app.route("/")
def index():
    """首頁：顯示最新分析儀表板"""
    analysis = load_latest_analysis()
    last_run = get_last_run_time()
    is_stale = is_data_stale(last_run)

    return render_template(
        "dashboard.html",
        analysis=analysis,
        last_run=last_run,
        is_stale=is_stale,
        now=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_stocks=sum(len(v) for v in STOCK_WATCHLIST.values()),
        categories=len(STOCK_WATCHLIST),
    )


@app.route("/api/latest")
def api_latest():
    """API：取最新分析結果（JSON）"""
    analysis = load_latest_analysis()
    return jsonify(analysis)


@app.route("/api/run", methods=["POST"])
def api_run():
    """API：觸發立即分析（手動更新）"""
    try:
        result = run_full_analysis()
        save_analysis(result)
        return jsonify({"status": "ok", "message": "分析完成", "time": datetime.now().isoformat()})
    except Exception as e:
        logger.exception("分析執行失敗")
        return jsonify({"status": "error", "message": str(e)}), 500


# ════════════════════════════════════════
#  核心分析引擎
# ════════════════════════════════════════

def run_full_analysis() -> dict:
    """執行完整台股分析"""
    logger.info("=== 開始台股盤後分析 ===")

    # 1. 大盤資料
    logger.info("→ 抓取大盤資料...")
    market = fetcher.fetch_market_index()
    institutional = fetcher.fetch_institutional_trading()

    # 2. 篩選個股（股價 < 30 元）
    logger.info("→ 篩選符合條件個股...")
    qualified_stocks = []

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
                qualified_stocks.append({
                    "category": category,
                    "data": cur,
                })
                hits += 1
                logger.info(f"  ✓ {category}: {sid} ({price:.2f}元)")

    # 3. 逐一分析個股
    logger.info(f"→ 分析 {len(qualified_stocks)} 檔個股...")
    stock_analyses = []
    for item in qualified_stocks:
        sd = item["data"]
        sid = sd["stock_id"]
        history = fetcher.fetch_stock_history(sid, days=120)
        news = fetcher.fetch_news(sid)
        analysis = analyzer.analyze(sd, history, news)
        analysis["category"] = item["category"]
        stock_analyses.append(analysis)
        logger.info(f"  ✓ {sid} 分析完成")

    # 4. 大盤技術摘要
    market_technical = summarize_market_technical(market)

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": market,
        "institutional": institutional,
        "market_technical": market_technical,
        "stocks": stock_analyses,
        "stats": {
            "total_watchlist": sum(len(v) for v in STOCK_WATCHLIST.values()),
            "qualified": len(stock_analyses),
            "price_limit": MAX_STOCK_PRICE,
        },
    }

    logger.info(f"=== 分析完成：{len(stock_analyses)} 檔個股 ===")
    return result


def summarize_market_technical(market: dict) -> dict:
    """大盤技術面摘要"""
    change = market.get("change_pct", 0)
    if abs(change) < 0.3:
        direction = "🔸 平盤震盪"
    elif change > 0:
        direction = "📈 資金偏多"
    else:
        direction = "📉 賣壓浮現"

    return {
        "direction": direction,
        "summary": f"今日大盤{'上漲' if change >= 0 else '下跌'} {abs(change):.2f}%"
    }


# ════════════════════════════════════════
#  資料持久化
# ════════════════════════════════════════

def save_analysis(data: dict):
    """儲存分析結果到 JSON 檔"""
    with open(ANALYSIS_RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(LAST_RUN_FILE, "w") as f:
        f.write(datetime.now().isoformat())
    logger.info(f"分析結果已儲存至 {ANALYSIS_RESULT_FILE}")


def load_latest_analysis() -> dict:
    """載入最新分析結果"""
    if ANALYSIS_RESULT_FILE.exists():
        try:
            with open(ANALYSIS_RESULT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return None


def get_last_run_time() -> str:
    """取得最後執行時間"""
    if LAST_RUN_FILE.exists():
        return LAST_RUN_FILE.read_text().strip()
    return "尚未執行"


def is_data_stale(last_run: str) -> bool:
    """判斷資料是否為今天的"""
    if not last_run or last_run == "尚未執行":
        return True
    try:
        run_date = datetime.fromisoformat(last_run).date()
        return run_date != datetime.now().date()
    except (ValueError, TypeError):
        return True


# ════════════════════════════════════════
#  入口
# ════════════════════════════════════════

def get_local_ip() -> str:
    """取得區域網路 IP"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "你的IP"


if __name__ == "__main__":
    # 如果有 --run 參數，執行分析後結束（給 cron 用）
    if "--run" in sys.argv:
        result = run_full_analysis()
        save_analysis(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0)

    # 啟動前先跑一次分析（如果還沒有資料）
    if not ANALYSIS_RESULT_FILE.exists():
        logger.info("首次執行，自動進行分析...")
        try:
            result = run_full_analysis()
            save_analysis(result)
        except Exception as e:
            logger.error(f"首次分析失敗: {e}")

    # 啟動網頁伺服器
    ip = get_local_ip()
    print(f"""
╔══════════════════════════════════════════╗
║     📊 台股盤後分析儀表板                ║
║                                          ║
║   ✓ 區域網路：http://{ip}:5000  ║
║   ✓ 本機：     http://localhost:5000      ║
║                                          ║
║   在 config.py 修改觀察名單              ║
║   用 --run 參數給 cron 排程自動執行      ║
╚══════════════════════════════════════════╝
    """)
    app.run(host="0.0.0.0", port=5000, debug=False)
