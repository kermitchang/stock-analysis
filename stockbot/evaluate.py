"""對帳：預測 vs 實際。由程式計算，不讓 LLM 自評。

定義（與 Hermes 週報的嚴格定義一致）：
- in_range：隔日收盤落在預測區間內
- dir_hit：bias=up 且收漲、bias=down 且收跌；bias=flat 時以 in_range 代替
- open_outside：隔日開盤就在區間外（跳空貫穿）
- brier：(prob_up - 實際是否上漲)^2，越小越好
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from . import db
from .util import now_tpe


def evaluate_pending(conn: sqlite3.Connection) -> int:
    pending = db.query_df(conn,
        "SELECT p.* FROM predictions p LEFT JOIN evaluations e ON e.prediction_id=p.id "
        "WHERE e.prediction_id IS NULL")
    if pending.empty:
        return 0
    rows = []
    for _, p in pending.iterrows():
        nxt = conn.execute(
            "SELECT date, open, close FROM prices WHERE stock_id=? AND date>? ORDER BY date LIMIT 1",
            (p["stock_id"], p["date"])).fetchone()
        if not nxt:
            continue
        ret = nxt["close"] / p["base_close"] - 1
        in_range = int(p["low"] <= nxt["close"] <= p["high"])
        if p["bias"] == "up":
            hit = int(ret > 0)
        elif p["bias"] == "down":
            hit = int(ret < 0)
        else:
            hit = in_range
        brier = None if pd.isna(p["prob_up"]) else (p["prob_up"] - (1.0 if ret > 0 else 0.0)) ** 2
        open_out = None if nxt["open"] is None else int(not (p["low"] <= nxt["open"] <= p["high"]))
        rows.append({"prediction_id": int(p["id"]), "target_date": nxt["date"], "actual_open": nxt["open"],
                     "actual_close": nxt["close"], "actual_ret": ret, "in_range": in_range, "dir_hit": hit,
                     "open_outside": open_out, "brier": brier,
                     "evaluated_at": now_tpe().isoformat(timespec="seconds")})
    db.upsert(conn, "evaluations", rows)
    conn.commit()
    return len(rows)


def scoreboard(conn: sqlite3.Connection, since: str = None, until: str = None,
               focus_only: bool = False) -> pd.DataFrame:
    """各預測版本的成績。focus_only=True 時只看有 LLM 預測的那幾檔（公平比較）。"""
    cond, params = ["1=1"], []
    if since:
        cond.append("e.target_date >= ?")
        params.append(since)
    if until:
        cond.append("e.target_date <= ?")
        params.append(until)
    if focus_only:
        cond.append("EXISTS (SELECT 1 FROM predictions l WHERE l.variant='llm' "
                    "AND l.stock_id=p.stock_id AND l.date=p.date)")
    df = db.query_df(conn,
        "SELECT p.variant, COUNT(*) n, AVG(e.in_range) range_hit, AVG(e.dir_hit) dir_hit, "
        "AVG(CASE WHEN p.bias!='flat' THEN e.dir_hit END) dir_hit_decided, "
        "SUM(CASE WHEN p.bias!='flat' THEN 1 ELSE 0 END) n_decided, "
        "AVG(e.open_outside) open_outside, AVG(e.brier) brier, "
        "AVG((p.high-p.low)/p.base_close) avg_width "
        "FROM predictions p JOIN evaluations e ON e.prediction_id=p.id "
        f"WHERE {' AND '.join(cond)} GROUP BY p.variant ORDER BY p.variant", tuple(params))
    return df


def daily_curve(conn: sqlite3.Connection, window: int = 20) -> pd.DataFrame:
    """學習曲線：每個版本的滾動命中率（給週報與網頁畫圖）。"""
    df = db.query_df(conn,
        "SELECT e.target_date d, p.variant, AVG(e.in_range) range_hit, "
        "AVG(CASE WHEN p.bias!='flat' THEN e.dir_hit END) dir_hit_decided, AVG(e.brier) brier, COUNT(*) n "
        "FROM predictions p JOIN evaluations e ON e.prediction_id=p.id GROUP BY e.target_date, p.variant")
    if df.empty:
        return df
    df = df.sort_values("d")
    out = []
    for v, g in df.groupby("variant"):
        g = g.copy()
        for c in ("range_hit", "dir_hit_decided", "brier"):
            g[c + f"_{window}d"] = g[c].rolling(window, min_periods=3).mean()
        out.append(g)
    return pd.concat(out)
