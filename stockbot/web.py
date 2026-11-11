"""區網儀表板：python -m stockbot web → http://樹莓派IP:5000

只讀資料庫，不會觸發分析；分析由排程負責。
"""
from __future__ import annotations

import json
from pathlib import Path

import markdown as md_lib
import pandas as pd
from flask import Flask, abort, jsonify, render_template, request
from markupsafe import Markup

from . import charts, config, db, evaluate
from .report import BIAS, VARIANT_NAMES
from .util import fmt_pct

app = Flask(__name__, template_folder=str(Path(__file__).parent / "templates"))
VARIANT_CLS = {"baseline": "s-base", "quant": "s-quant", "quant_am": "s-am", "llm": "s-llm"}


def _conn():
    return db.connect()


def _recs(df: pd.DataFrame) -> list[dict]:
    """DataFrame → records，NaN 轉 None（模板才判斷得出「沒有值」）。"""
    return df.astype(object).where(df.notna(), None).to_dict("records")


def _f(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else v


@app.template_filter("pct")
def pct_filter(v, digits=1):
    return fmt_pct(_f(v), digits)


@app.template_filter("pct0")
def pct0_filter(v):
    v = _f(v)
    return "—" if v is None else f"{v:.0%}"


@app.template_filter("sign")
def sign_filter(v):
    v = _f(v)
    return "" if v is None else "up" if v > 0 else "down" if v < 0 else ""


# 用 globals（而非 context_processor），import 進來的 macro 也看得到
app.jinja_env.globals.update(VARIANT_NAMES=VARIANT_NAMES, BIAS=BIAS, VARIANT_CLS=VARIANT_CLS)


def _latest_date(conn) -> str | None:
    r = conn.execute("SELECT MAX(date) FROM predictions WHERE variant='quant'").fetchone()
    return r[0] if r else None


def _status(conn) -> dict:
    run = conn.execute("SELECT * FROM runs WHERE kind='daily' ORDER BY id DESC LIMIT 1").fetchone()
    spent = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_log WHERE ts >= date('now','-30 day')").fetchone()[0]
    msg = (run["message"] or "") if run else ""
    level = "bad" if msg.startswith("❌") or (run and run["status"] == "fail") else "warn" if msg.startswith("⚠️") else "ok"
    return {"run": dict(run) if run else None, "message": msg, "level": level, "spent30": spent}


@app.route("/")
def index():
    conn = _conn()
    date = _latest_date(conn)
    ctx = {"date": date, "status": _status(conn)}
    if not date:
        return render_template("index.html", **ctx, rows=[], sb=pd.DataFrame(), atts=[], themes=[])

    tw = db.query_df(conn, "SELECT date, close FROM prices WHERE stock_id='^TWII' AND date<=? ORDER BY date DESC LIMIT 6", (date,))
    sox = db.query_df(conn, "SELECT date, close FROM prices WHERE stock_id='^SOX' ORDER BY date DESC LIMIT 2")
    ctx["market"] = {
        "twii": tw.iloc[0]["close"] if len(tw) else None,
        "twii_ret": tw.iloc[0]["close"] / tw.iloc[1]["close"] - 1 if len(tw) > 1 else None,
        "twii_ret5": tw.iloc[0]["close"] / tw.iloc[5]["close"] - 1 if len(tw) > 5 else None,
        "sox": sox.iloc[0]["close"] if len(sox) else None, "sox_date": sox.iloc[0]["date"] if len(sox) else None,
        "sox_ret": sox.iloc[0]["close"] / sox.iloc[1]["close"] - 1 if len(sox) > 1 else None,
    }

    preds = db.query_df(conn, "SELECT * FROM predictions WHERE date=?", (date,))
    wl = db.query_df(conn, "SELECT w.stock_id, w.source, w.reason, s.name, s.theme FROM watchlist w "
                           "JOIN stocks s USING(stock_id) WHERE w.date=?", (date,))
    px = db.query_df(conn, "SELECT p.stock_id, p.close, p.close / (SELECT close FROM prices q WHERE q.stock_id=p.stock_id "
                           "AND q.date<p.date ORDER BY q.date DESC LIMIT 1) - 1 AS ret FROM prices p WHERE p.date=?", (date,))
    att = db.query_df(conn, "SELECT * FROM attributions WHERE date=?", (date,))
    pxm, attm = px.set_index("stock_id"), att.set_index("stock_id")
    rows = []
    for _, w in wl.iterrows():
        p = preds[preds.stock_id == w["stock_id"]].set_index("variant")
        row = {"id": w["stock_id"], "name": w["name"], "theme": w["theme"], "source": w["source"], "reason": w["reason"],
               "close": _f(pxm["close"].get(w["stock_id"])), "ret": _f(pxm["ret"].get(w["stock_id"])),
               "driver": attm["primary_driver"].get(w["stock_id"]) if not att.empty else None}
        for v in ("quant", "quant_am", "llm"):
            if v in p.index:
                row[v] = {"low": p.loc[v, "low"], "high": p.loc[v, "high"], "prob": _f(p.loc[v, "prob_up"]),
                          "bias": p.loc[v, "bias"]}
                if v == "llm":
                    row["thesis"] = json.loads(p.loc[v, "rationale"] or "{}").get("thesis")
        rows.append(row)
    rows.sort(key=lambda r: (r.get("llm") is None, r["source"] != "core", -(r.get("quant", {}).get("prob") or 0)))
    themes = sorted({r["theme"] for r in rows if r["theme"]})

    since = db.query_df(conn, "SELECT DISTINCT date FROM prices WHERE stock_id='^TWII' AND date<=? ORDER BY date DESC LIMIT 20", (date,))
    sb = evaluate.scoreboard(conn, since=since["date"].min() if not since.empty else None)
    names = dict(conn.execute("SELECT stock_id, name FROM stocks").fetchall())
    atts = []
    for _, a in att.reindex(att["idio_part"].abs().sort_values(ascending=False).index).head(10).iterrows():
        atts.append({**a.to_dict(), "name": names.get(a["stock_id"], "")})
    conn.close()
    return render_template("index.html", **ctx, rows=rows, themes=themes, sb=sb, atts=atts)


@app.route("/stock/<sid>")
def stock(sid: str):
    conn = _conn()
    s = conn.execute("SELECT * FROM stocks WHERE stock_id=?", (sid,)).fetchone()
    if not s:
        abort(404)
    variant = request.args.get("v", "quant")
    px = db.query_df(conn, "SELECT * FROM (SELECT * FROM prices WHERE stock_id=? ORDER BY date DESC LIMIT 120) ORDER BY date", (sid,))
    preds = db.query_df(conn,
        "SELECT p.*, e.target_date, e.actual_close, e.actual_ret, e.in_range, e.dir_hit FROM predictions p "
        "LEFT JOIN evaluations e ON e.prediction_id=p.id WHERE p.stock_id=? ORDER BY p.date DESC, p.variant", (sid,))
    dates = px["date"].tolist()
    idx = {d: i for i, d in enumerate(dates)}
    bands = []
    for _, p in preds[preds.variant == variant].iterrows():
        if pd.notna(p["target_date"]) and p["target_date"] in idx:
            i, hit = idx[p["target_date"]], bool(p["in_range"])
        elif p["date"] in idx and pd.isna(p["target_date"]):
            i, hit = len(dates), None   # 明日（尚未發生）
        else:
            continue
        bands.append({"i": i, "low": p["low"], "high": p["high"], "hit": hit,
                      "title": f"{p['date']} 預測 {p['low']}~{p['high']} {BIAS.get(p['bias'], '')}"})
    if any(b["i"] == len(dates) for b in bands):
        dates = dates + ["明日"]          # 多留一格放「明日預測區間」
    lst =lambda col: [None if pd.isna(v) else float(v) for v in px[col]] + [None] * (len(dates) - len(px))
    chart = charts.price_chart(dates, lst("open"), lst("high"), lst("low"), lst("close"), bands)

    llm_preds = []
    for p in _recs(preds[preds.variant == "llm"].head(15)):
        llm_preds.append({**p, "detail": json.loads(p["rationale"] or "{}")})
    rec = preds.dropna(subset=["target_date"]).groupby("variant").agg(
        n=("id", "size"), range_hit=("in_range", "mean"), dir_hit=("dir_hit", "mean")).reset_index()
    ctx = dict(
        s=dict(s), chart=Markup(chart), variant=variant, preds=_recs(preds.head(40)), llm_preds=llm_preds,
        rec=_recs(rec),
        news=_recs(db.query_df(conn, "SELECT n.*, ns.sentiment, ns.category, ns.importance FROM news_stock ns JOIN news n USING(news_id) "
                               "WHERE ns.stock_id=? ORDER BY n.published_at DESC LIMIT 20", (sid,))),
        revenue=_recs(db.query_df(conn, "SELECT * FROM revenue WHERE stock_id=? ORDER BY ym DESC LIMIT 12", (sid,))),
        fin=_recs(db.query_df(conn, "SELECT * FROM financials WHERE stock_id=? ORDER BY year DESC, quarter DESC LIMIT 6", (sid,))),
        atts=_recs(db.query_df(conn, "SELECT * FROM attributions WHERE stock_id=? ORDER BY date DESC LIMIT 15", (sid,))),
        chips=_recs(db.query_df(conn, "SELECT * FROM chips WHERE stock_id=? ORDER BY date DESC LIMIT 10", (sid,))),
        last=px.dropna(subset=["close"]).iloc[-1].to_dict() if not px.dropna(subset=["close"]).empty else None,
    )
    conn.close()
    return render_template("stock.html", **ctx)


@app.route("/learning")
def learning():
    conn = _conn()
    curve = evaluate.daily_curve(conn)
    dir_chart = range_chart = Markup('<div class="empty">資料累積中</div>')
    if not curve.empty:
        xs = sorted(curve["d"].unique())
        mk = lambda col: {VARIANT_NAMES.get(v, v): (VARIANT_CLS.get(v, ""), [
            _f(g.set_index("d")[col].get(d)) for d in xs]) for v, g in curve.groupby("variant")}
        dir_chart = Markup(charts.line_chart(xs, {k: v for k, v in mk("dir_hit_decided_20d").items()
                                                  if "基準" not in k}, ref=0.5))
        range_chart = Markup(charts.line_chart(xs, mk("range_hit_20d"), ref=config.RANGE_COVERAGE_TARGET))
    rules = _recs(db.query_df(conn, "SELECT * FROM rules ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 "
                              "WHEN 'retired' THEN 2 ELSE 3 END, lift DESC"))
    for r in rules:
        r["cond"] = " 且 ".join(f"{c['f']} {c['op']} {c['v']}" for c in json.loads(r["condition_json"])["all"])
    runs = db.query_df(conn, "SELECT * FROM model_runs WHERE kind='train' ORDER BY id DESC LIMIT 12")
    models = [{"at": r["created_at"], **json.loads(r["metrics_json"])} for _, r in runs.iterrows()]
    cost = db.query_df(conn, "SELECT substr(ts,1,10) d, SUM(cost_usd) c FROM llm_log GROUP BY 1 ORDER BY 1 DESC LIMIT 30")
    cost = cost.iloc[::-1]
    cost_chart = Markup(charts.bar_chart(cost["d"].tolist(), cost["c"].tolist()))
    sb_all = evaluate.scoreboard(conn)
    sb_focus = evaluate.scoreboard(conn, focus_only=True)
    drivers = db.query_df(conn, "SELECT primary_driver d, COUNT(*) n FROM attributions WHERE source='llm' "
                                "AND date >= date('now','-30 day') GROUP BY 1 ORDER BY 2 DESC").to_dict("records")
    conn.close()
    return render_template("learning.html", dir_chart=dir_chart, range_chart=range_chart, rules=rules, models=models,
                           cost_chart=cost_chart, sb_all=sb_all, sb_focus=sb_focus, drivers=drivers,
                           total_cost=float(cost["c"].sum()) if not cost.empty else 0.0)


def _report_files():
    groups = {
        "stockbot 日報": sorted((config.REPORT_DIR / "daily").glob("*.md"), reverse=True),
        "stockbot 週報": sorted((config.REPORT_DIR / "weekly").glob("*.md"), reverse=True),
        "Hermes 日報": sorted((config.BASE_DIR / "reports" / "daily").glob("*.md"), reverse=True),
        "Hermes 週報": sorted((config.BASE_DIR / "reports" / "weekly").glob("*.md"), reverse=True),
    }
    return {k: [p.relative_to(config.BASE_DIR / "reports").as_posix() for p in v] for k, v in groups.items()}


@app.route("/reports")
@app.route("/reports/<path:rel>")
def reports(rel: str | None = None):
    groups = _report_files()
    html = None
    if rel is None:
        rel = next((v[0] for v in groups.values() if v), None)
    if rel:
        p = (config.BASE_DIR / "reports" / rel).resolve()
        if not str(p).startswith(str((config.BASE_DIR / "reports").resolve())) or not p.exists():
            abort(404)
        html = Markup(md_lib.markdown(p.read_text(encoding="utf-8"), extensions=["tables", "fenced_code", "sane_lists"]))
    return render_template("reports.html", groups=groups, current=rel, html=html)


@app.route("/api/latest")
def api_latest():
    conn = _conn()
    date = _latest_date(conn)
    preds = db.query_df(conn, "SELECT stock_id, variant, low, high, prob_up, bias FROM predictions WHERE date=?", (date,))
    conn.close()
    return jsonify({"date": date, "predictions": _recs(preds)})


@app.route("/healthz")
def healthz():
    return "ok"
