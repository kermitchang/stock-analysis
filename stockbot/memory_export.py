"""把 SQLite 裡的資料與經驗匯出成 git 友善的文字檔（memory/），也能從 memory/ 還原資料庫。

為什麼不直接把 stockbot.db 放進 git：二進位檔每天整份變動、無法 diff、repo 會快速膨脹。
按月份分檔的 CSV 只有當月檔案會變，git diff 看得出每天新增了什麼，人和 AI 都能直接讀。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pandas as pd

from . import config, db
from .quant import CALIB_PATH
from .util import now_tpe

log = logging.getLogger(__name__)
MEM = config.BASE_DIR / "memory"

# 表格 → (日期欄位, 是否按月分檔)
MONTHLY = {
    "prices": "date", "chips": "date", "watchlist": "date", "attributions": "date",
}
WHOLE = ["stocks", "revenue", "financials", "rules", "model_runs"]


def _write(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, float_format="%.6g", lineterminator="\n")


def export(conn: sqlite3.Connection, full: bool = False) -> None:
    since = None if full else (now_tpe() - timedelta(days=40)).strftime("%Y-%m")
    for table, col in MONTHLY.items():
        df = db.query_df(conn, f"SELECT * FROM {table}")
        if df.empty:
            continue
        df["_m"] = df[col].str[:7]
        for m, g in df.groupby("_m"):
            if since and m < since:
                continue
            keys = [c for c in ("stock_id", "date") if c in g.columns]
            _write(g.drop(columns="_m").sort_values(keys), MEM / table / f"{m}.csv")

    # 預測 + 對帳結果合在同一列，方便閱讀
    pe = db.query_df(conn,
        "SELECT p.*, e.target_date AS e_target_date, e.actual_open AS e_actual_open, e.actual_close AS e_actual_close, "
        "e.actual_ret AS e_actual_ret, e.in_range AS e_in_range, e.dir_hit AS e_dir_hit, "
        "e.open_outside AS e_open_outside, e.brier AS e_brier, e.evaluated_at AS e_evaluated_at "
        "FROM predictions p LEFT JOIN evaluations e ON e.prediction_id=p.id")
    if not pe.empty:
        pe["_m"] = pe["date"].str[:7]
        for m, g in pe.groupby("_m"):
            if since and m < since:
                continue
            _write(g.drop(columns="_m").sort_values(["date", "stock_id", "variant"]), MEM / "predictions" / f"{m}.csv")

    news = db.query_df(conn,
        "SELECT n.*, ns.stock_id, ns.sentiment, ns.category, ns.importance, ns.labeled "
        "FROM news n JOIN news_stock ns USING(news_id)")
    if not news.empty:
        news["_m"] = news["published_at"].str[:7]
        for m, g in news.groupby("_m"):
            if since and m < since:
                continue
            _write(g.drop(columns="_m").sort_values(["published_at", "news_id", "stock_id"]), MEM / "news" / f"{m}.csv")

    for table in WHOLE:
        df = db.query_df(conn, f"SELECT * FROM {table}")
        if table == "rules" and not df.empty:
            recs = df.to_dict(orient="records")
            for r in recs:
                r["condition"] = json.loads(r.pop("condition_json"))
            (MEM / "rules.json").write_text(json.dumps(recs, ensure_ascii=False, indent=1, default=str) + "\n")
        elif not df.empty:
            _write(df, MEM / f"{table}.csv")

    usage = db.query_df(conn, "SELECT substr(ts,1,10) date, purpose, COUNT(*) calls, SUM(prompt_tokens) prompt_tokens, "
                              "SUM(completion_tokens) completion_tokens, SUM(cost_usd) cost_usd, SUM(ok) ok "
                              "FROM llm_log GROUP BY 1,2 ORDER BY 1,2")
    if not usage.empty:
        _write(usage, MEM / "llm_usage.csv")
    if CALIB_PATH.exists():
        (MEM / "calibration.json").write_text(CALIB_PATH.read_text())
    log.info("已匯出到 %s", MEM)


def restore(conn: sqlite3.Connection) -> None:
    """從 memory/ 重建資料庫（例如換 SD 卡後）。"""
    def load(path: Path) -> pd.DataFrame:
        return pd.read_csv(path, dtype={"stock_id": str, "industry_code": str})

    for table in MONTHLY:
        for f in sorted((MEM / table).glob("*.csv")):
            db.upsert(conn, table, load(f).astype(object).where(lambda d: d.notna(), None).to_dict("records"))
    for f in sorted((MEM / "predictions").glob("*.csv")):
        df = load(f)
        ecols = [c for c in df.columns if c.startswith("e_")]
        preds = df.drop(columns=ecols)
        db.upsert(conn, "predictions", preds.astype(object).where(preds.notna(), None).to_dict("records"))
        ev = df[df["e_target_date"].notna()][["id"] + ecols].rename(columns={"id": "prediction_id"})
        ev.columns = [c[2:] if c.startswith("e_") else c for c in ev.columns]
        db.upsert(conn, "evaluations", ev.astype(object).where(ev.notna(), None).to_dict("records"))
    for f in sorted((MEM / "news").glob("*.csv")):
        df = load(f)
        n = df[["news_id", "published_at", "title", "summary", "source", "url"]].drop_duplicates("news_id")
        db.upsert(conn, "news", n.astype(object).where(n.notna(), None).to_dict("records"))
        ns = df[["news_id", "stock_id", "sentiment", "category", "importance", "labeled"]]
        db.upsert(conn, "news_stock", ns.astype(object).where(ns.notna(), None).to_dict("records"))
    for table in WHOLE:
        if table == "rules":
            p = MEM / "rules.json"
            if p.exists():
                recs = json.loads(p.read_text())
                for r in recs:
                    r["condition_json"] = json.dumps(r.pop("condition"), ensure_ascii=False, sort_keys=True)
                db.upsert(conn, "rules", recs)
            continue
        p = MEM / f"{table}.csv"
        if p.exists():
            df = load(p)
            db.upsert(conn, table, df.astype(object).where(df.notna(), None).to_dict("records"))
    if (MEM / "calibration.json").exists():
        CALIB_PATH.write_text((MEM / "calibration.json").read_text())
    conn.commit()
    log.info("已從 memory/ 還原資料庫")
