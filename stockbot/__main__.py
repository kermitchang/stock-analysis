"""指令列入口：python -m stockbot <指令>

  bootstrap   第一次安裝：下載公司清單、3 年歷史、回補法人、訓練模型、建立種子規則
  daily       每日收盤後：收資料 → 對帳 → 歸因 → 預測 → 日報 → 匯出 → (推送 git)
  morning     隔日早盤：用前一晚費半修正預測（quant_am）
  weekly      每週：重新訓練、校準、規則複驗與新假設、週報
  summary     印出最新日報的精簡摘要（給 Hermes 傳 Telegram）
  status      印出系統狀態（DeepSeek 餘額、最近執行、資料量）
  export      匯出 memory/（--full 全部月份）
  restore     從 memory/ 還原資料庫
  web         啟動網頁儀表板
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stockbot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["bootstrap", "daily", "morning", "weekly", "summary", "status",
                                        "export", "restore", "web"])
    ap.add_argument("--force", action="store_true", help="daily：同一天已完成也重跑")
    ap.add_argument("--full", action="store_true", help="export：匯出全部月份")
    ap.add_argument("--chips-days", type=int, default=60, help="bootstrap：回補幾天三大法人")
    ap.add_argument("--date", help="summary：指定日期（預設最新）")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("yfinance", "urllib3", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from . import db, llm, memory_export, pipeline, report

    if args.command == "bootstrap":
        pipeline.bootstrap(chips_days=args.chips_days)
    elif args.command == "daily":
        pipeline.daily(force=args.force)
    elif args.command == "morning":
        pipeline.morning()
    elif args.command == "weekly":
        pipeline.weekly()
    elif args.command == "summary":
        with db.session() as conn:
            date = args.date or (conn.execute(
                "SELECT MAX(run_date) FROM runs WHERE kind='daily' AND status='ok'").fetchone()[0])
            if not date:
                print("尚無日報")
                return 1
            print(report.summary(conn, date))
    elif args.command == "status":
        bal = llm.check_balance()
        with db.session() as conn:
            print(("❌ " if not bal["ok"] else "⚠️ " if bal["low"] else "💰 ") + bal["message"])
            print(f"今日 LLM 花費 ${llm.spent_today(conn):.3f}（每日上限 ${config.LLM_DAILY_BUDGET_USD}）")
            for r in conn.execute("SELECT kind, run_date, status, finished_at, message FROM runs "
                                  "ORDER BY id DESC LIMIT 8"):
                print(f"  {r['kind']:<9} {r['run_date']} {r['status']:<5} {r['finished_at'] or ''} {r['message'] or ''}")
            for t in ("stocks", "prices", "chips", "news", "predictions", "evaluations", "attributions", "rules"):
                print(f"  {t}: {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:,}")
    elif args.command == "export":
        with db.session() as conn:
            memory_export.export(conn, full=args.full)
    elif args.command == "restore":
        with db.session() as conn:
            memory_export.restore(conn)
    elif args.command == "web":
        from .web import app
        app.run(host="0.0.0.0", port=config.WEB_PORT, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
