"""產生 Markdown 日報／週報，以及給 Telegram 的精簡摘要。"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

import pandas as pd

from . import config, db, evaluate, llm
from .util import fmt_pct, now_tpe

VARIANT_NAMES = {"baseline": "基準(1×ATR)", "quant": "量化模型", "quant_am": "量化+早盤修正", "llm": "LLM 分析師"}
BIAS = {"up": "偏多", "down": "偏空", "flat": "盤整"}


def system_status(conn: sqlite3.Connection, balance: Optional[dict]) -> list[str]:
    lines = []
    reason = llm.disabled_reason()
    spent = llm.spent_today(conn)
    if reason:
        lines.append(f"> ❌ **LLM 已停止：{reason}**。今天只有量化分析，資料與經驗仍持續累積。")
    elif balance and balance.get("low"):
        lines.append(f"> ⚠️ **{balance['message']}**")
    if balance and balance.get("message") and not reason and not balance.get("low"):
        lines.append(f"> 💰 {balance['message']}")
    lines.append(f"> 今日 LLM 花費約 ${spent:.3f}（上限 ${config.LLM_DAILY_BUDGET_USD}）")
    return lines


def _table(df: pd.DataFrame) -> str:
    if df.empty:
        return "（無資料）"
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        out.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in r.values) + " |")
    return "\n".join(out)


def _score_table(sb: pd.DataFrame) -> str:
    if sb.empty:
        return "（尚無可對帳的預測）"
    t = pd.DataFrame({
        "版本": sb["variant"].map(VARIANT_NAMES).fillna(sb["variant"]),
        "筆數": sb["n"].astype(int),
        "區間命中": sb["range_hit"].map(lambda v: fmt_pct(v, 0).lstrip("+")),
        "方向命中(有表態)": [f"{fmt_pct(v, 0).lstrip('+')}（{int(n)}筆）" if pd.notna(v) else "—"
                       for v, n in zip(sb["dir_hit_decided"], sb["n_decided"])],
        "開盤即出區間": sb["open_outside"].map(lambda v: fmt_pct(v, 0).lstrip("+")),
        "Brier": sb["brier"].map(lambda v: f"{v:.3f}" if pd.notna(v) else "—"),
        "平均區間寬": sb["avg_width"].map(lambda v: fmt_pct(v, 1).lstrip("+")),
    })
    return _table(t)


def daily(conn: sqlite3.Connection, date: str, day: pd.DataFrame, focus: list[str],
          balance: Optional[dict]) -> str:
    names = dict(conn.execute("SELECT stock_id, name FROM stocks").fetchall())
    L = [f"# 📊 stockbot 台股日報 {date}", ""]
    L += system_status(conn, balance) + [""]

    # ── 一、大盤 ──
    L.append("## 一、大盤與主題")
    mk = day.iloc[0] if not day.empty else None
    idx = {r["stock_id"]: r for r in conn.execute(
        "SELECT stock_id, close FROM prices WHERE stock_id='^TWII' AND date=?", (date,))}
    if mk is not None:
        tw = idx.get("^TWII")
        L.append(f"- 加權指數 {tw['close']:,.2f}（{fmt_pct(mk['mkt_ret_1'], 2)}），近5日 {fmt_pct(mk['mkt_ret_5'], 2)}"
                 if tw else f"- 加權指數 {fmt_pct(mk['mkt_ret_1'], 2)}")
        L.append(f"- 前一晚費城半導體 {fmt_pct(mk['sox_ret_1'], 2)}")
    if "source" in day.columns:
        n_core, n_dyn = int((day["source"] == "core").sum()), int((day["source"] == "dynamic").sum())
        L.append(f"- 今日分析 **{len(day)}** 檔：固定名單 {n_core} 檔 ＋ 動態挑選 {n_dyn} 檔"
                 f"（七大主題、股價 < {config.MAX_PRICE:.0f} 元、流動性足夠）")
        wl = db.query_df(conn, "SELECT stock_id, reason FROM watchlist WHERE date=? AND source='dynamic'", (date,))
        if not wl.empty:
            L.append("- 動態挑選：" + "、".join(f"{s}{names.get(s, '')}（{r}）" for s, r in zip(wl["stock_id"], wl["reason"])))
    else:
        L.append(f"- 今日分析 **{len(day)}** 檔")
    if not day.empty:
        th = day.groupby("theme").agg(n=("stock_id", "size"), avg=("ret_1", "mean"),
                                      up=("ret_1", lambda s: (s > 0).mean())).reset_index()
        best = day.loc[day.groupby("theme")["ret_1"].idxmax(), ["theme", "stock_id", "ret_1"]].set_index("theme")
        worst = day.loc[day.groupby("theme")["ret_1"].idxmin(), ["theme", "stock_id", "ret_1"]].set_index("theme")
        L.append("")
        L.append(_table(pd.DataFrame({
            "主題": th["theme"], "檔數": th["n"], "平均漲跌": th["avg"].map(lambda v: fmt_pct(v, 2)),
            "上漲家數比": th["up"].map(lambda v: fmt_pct(v, 0).lstrip("+")),
            "最強": [f"{best.loc[t, 'stock_id']}{names.get(best.loc[t, 'stock_id'], '')} {fmt_pct(best.loc[t, 'ret_1'])}" for t in th["theme"]],
            "最弱": [f"{worst.loc[t, 'stock_id']}{names.get(worst.loc[t, 'stock_id'], '')} {fmt_pct(worst.loc[t, 'ret_1'])}" for t in th["theme"]],
        })))
    L.append("")

    # ── 二、昨日預測對帳 ──
    L.append("## 二、上一交易日預測對帳（程式自動計算）")
    L.append(_score_table(evaluate.scoreboard(conn, since=date, until=date)))
    det = db.query_df(conn,
        "SELECT p.stock_id, p.low, p.high, p.bias, e.actual_close, e.actual_ret, e.in_range, e.dir_hit, e.open_outside "
        "FROM predictions p JOIN evaluations e ON e.prediction_id=p.id WHERE p.variant='llm' AND e.target_date=?", (date,))
    if not det.empty:
        L.append("")
        L.append("LLM 重點股明細：")
        L.append(_table(pd.DataFrame({
            "股票": det["stock_id"].map(lambda s: f"{s} {names.get(s, '')}"),
            "預測": [f"{lo}~{hi} {BIAS.get(b, b)}" for lo, hi, b in zip(det["low"], det["high"], det["bias"])],
            "實際": [f"{c}（{fmt_pct(r)}）" for c, r in zip(det["actual_close"], det["actual_ret"])],
            "區間": det["in_range"].map({1: "✓", 0: "✗"}), "方向": det["dir_hit"].map({1: "✓", 0: "✗"}),
            "開盤跳出": det["open_outside"].map({1: "是", 0: ""}),
        })))
    L.append("")

    # ── 三、漲跌歸因 ──
    L.append("## 三、今日為什麼漲跌（個股因素最大的股票）")
    att = db.query_df(conn,
        "SELECT * FROM attributions WHERE date=? AND source='llm' ORDER BY ABS(idio_part) DESC", (date,))
    if att.empty:
        att = db.query_df(conn, "SELECT * FROM attributions WHERE date=? ORDER BY ABS(idio_part) DESC LIMIT 10", (date,))
    if not att.empty:
        L.append(_table(pd.DataFrame({
            "股票": att["stock_id"].map(lambda s: f"{s} {names.get(s, '')}"),
            "漲跌": att["ret"].map(lambda v: fmt_pct(v, 2)),
            "大盤/產業/個股": [f"{fmt_pct(a)}/{fmt_pct(b)}/{fmt_pct(c)}" for a, b, c in
                          zip(att["market_part"], att["industry_part"], att["idio_part"])],
            "主因": att["primary_driver"],
            "說明": att["explanation"].fillna("").str.replace("|", "／", regex=False),
        })))
    L.append("")
    drv = db.query_df(conn, "SELECT primary_driver d, COUNT(*) n FROM attributions WHERE date=? GROUP BY d ORDER BY n DESC", (date,))
    if not drv.empty:
        L.append("全體主因分布：" + "、".join(f"{r['d']} {r['n']}" for _, r in drv.iterrows()))
        L.append("")

    # ── 四、重點股 ──
    L.append("## 四、重點股分析與明日預測")
    preds = db.query_df(conn, "SELECT * FROM predictions WHERE date=?", (date,))
    for sid in focus:
        p = preds[preds.stock_id == sid].set_index("variant")
        if p.empty:
            continue
        L.append(f"### {sid} {names.get(sid, '')}")
        row = day[day.stock_id == sid]
        if not row.empty:
            r = row.iloc[0]
            L.append(f"收盤 {r['close']}（{fmt_pct(r['ret_1'], 2)}），量比 {r['vol_ratio']:.2f}，K {r['kd_k']:.0f}，"
                     f"RSI {r['rsi14']:.0f}，主題 {r['theme']}")
        for v in ("llm", "quant", "baseline"):
            if v in p.index:
                q = p.loc[v]
                prob = f"，上漲機率 {q['prob_up']:.0%}" if pd.notna(q["prob_up"]) else ""
                L.append(f"- **{VARIANT_NAMES[v]}**：{q['low']} ~ {q['high']}，{BIAS.get(q['bias'], q['bias'])}{prob}")
        if "llm" in p.index:
            d = json.loads(p.loc["llm", "rationale"] or "{}")
            for key, label in (("fundamental_view", "財報營收"), ("news_view", "消息面"), ("thesis", "判斷理由"),
                               ("override_quant", "與量化不同之處"), ("watch", "明日觀察")):
                if d.get(key):
                    L.append(f"- {label}：{d[key]}")
            if d.get("risks"):
                L.append(f"- 風險：{'；'.join(map(str, d['risks']))}")
            if d.get("analog"):
                a = d["analog"]
                L.append(f"- 歷史相似 {a['k']} 例：隔日上漲 {a['up_ratio']:.0%}，平均 {fmt_pct(a['mean'], 2)}")
        L.append("")

    # ── 五、全體量化排行 ──
    q = preds[preds.variant == "quant"].dropna(subset=["prob_up"]).sort_values("prob_up", ascending=False)
    if not q.empty:
        L.append("## 五、量化模型排行（全部分析範圍）")
        fmt = lambda df: ", ".join(f"{s}{names.get(s, '')} {p:.0%}" for s, p in zip(df["stock_id"], df["prob_up"]))
        L.append(f"- 上漲機率最高：{fmt(q.head(8))}")
        L.append(f"- 上漲機率最低：{fmt(q.tail(8).iloc[::-1])}")
        L.append("")

    # ── 六、學習進度 ──
    L.append("## 六、學習進度（近 20 個交易日）")
    since = db.query_df(conn, "SELECT DISTINCT date FROM prices WHERE stock_id='^TWII' AND date<=? "
                              "ORDER BY date DESC LIMIT 20", (date,))
    L.append(_score_table(evaluate.scoreboard(conn, since=since["date"].min() if not since.empty else None)))
    act = conn.execute("SELECT COUNT(*) FROM rules WHERE status='active'").fetchone()[0]
    L.append(f"\n已驗證啟用的規則：{act} 條（完整清單見週報與 memory/rules.json）")
    L.append("\n---\n*本報告由程式自動產生，僅供研究學習，非投資建議。*")
    return "\n".join(L)


def summary(conn: sqlite3.Connection, date: str) -> str:
    """給 Hermes 透過 Telegram 傳送的精簡版。"""
    names = dict(conn.execute("SELECT stock_id, name FROM stocks").fetchall())
    L = [f"📊 stockbot {date}"]
    status = conn.execute("SELECT message FROM runs WHERE kind='daily' AND run_date=? ORDER BY id DESC LIMIT 1",
                          (date,)).fetchone()
    if status and status["message"]:
        L.append(status["message"])
    sb = evaluate.scoreboard(conn, since=date, until=date)
    for _, r in sb.iterrows():
        dh = f"{r['dir_hit_decided']:.0%}" if pd.notna(r["dir_hit_decided"]) else "—"
        L.append(f"昨日對帳 {VARIANT_NAMES.get(r['variant'], r['variant'])}：區間 {r['range_hit']:.0%}，方向 {dh}")
    preds = db.query_df(conn, "SELECT * FROM predictions WHERE date=? AND variant='llm'", (date,))
    if not preds.empty:
        L.append("明日重點：")
        for _, p in preds.iterrows():
            d = json.loads(p["rationale"] or "{}")
            L.append(f"• {p['stock_id']}{names.get(p['stock_id'], '')} {p['low']}~{p['high']} "
                     f"{BIAS.get(p['bias'], p['bias'])}｜{(d.get('thesis') or '')[:50]}")
    L.append(f"完整報告：reports/stockbot/daily/{date}.md")
    return "\n".join(L)


def weekly(conn: sqlite3.Connection, week_start: str, week_end: str, rule_changes: list,
           new_rules: list, model_metrics: Optional[dict], calib: Optional[dict], balance: Optional[dict]) -> str:
    L = [f"# 📈 stockbot 週報 {week_start} ~ {week_end}", ""]
    L += system_status(conn, balance) + [""]

    L.append("## 一、本週成績（全部預測）")
    L.append(_score_table(evaluate.scoreboard(conn, since=week_start, until=week_end)))
    L.append("\n只看 LLM 有分析的重點股（公平比較 LLM vs 量化 vs 基準）：")
    L.append(_score_table(evaluate.scoreboard(conn, since=week_start, until=week_end, focus_only=True)))
    L.append("\n## 二、累積成績（上線至今）")
    L.append(_score_table(evaluate.scoreboard(conn)))

    curve = evaluate.daily_curve(conn)
    if not curve.empty:
        curve["week"] = pd.to_datetime(curve["d"]).dt.strftime("%G-W%V")
        wk = curve.groupby(["week", "variant"]).apply(
            lambda g: pd.Series({"range": (g["range_hit"] * g["n"]).sum() / g["n"].sum(),
                                 "dir": g["dir_hit_decided"].mean()}), include_groups=False).reset_index()
        piv = wk.pivot(index="week", columns="variant", values="dir").tail(12)
        L.append("\n## 三、學習曲線：每週方向命中率（有表態者）")
        piv = piv.map(lambda v: fmt_pct(v, 0).lstrip("+") if pd.notna(v) else "—")
        piv.columns = [VARIANT_NAMES.get(c, c) for c in piv.columns]
        L.append(_table(piv.reset_index().rename(columns={"week": "週"})))

    L.append("\n## 四、規則庫變化")
    if rule_changes:
        for c in rule_changes:
            L.append(f"- #{c['id']} {c['text']}：{c['from']} → **{c['to']}**（{c['note']}）")
    else:
        L.append("- 本週無狀態變化")
    if new_rules:
        L.append("\n本週 LLM 從錯誤中提出的新假設（已回測）：")
        for r in conn.execute(f"SELECT * FROM rules WHERE id IN ({','.join('?' * len(new_rules))})", new_rules):
            L.append(f"- #{r['id']} {r['text']} → **{r['status']}**（{r['n']} 樣本，勝率 "
                     f"{fmt_pct(r['hit_rate'], 1).lstrip('+') if r['hit_rate'] is not None else '—'}，"
                     f"lift {fmt_pct(r['lift'], 1) if r['lift'] is not None else '—'}；{r['note']}）")
    act = db.query_df(conn, "SELECT id, text, n, hit_rate, base_rate, lift, recent_lift FROM rules "
                            "WHERE status='active' ORDER BY lift DESC")
    L.append("\n目前啟用中的規則：")
    if act.empty:
        L.append("（沒有規則通過驗證——代表目前找不到比隨機明顯更好的隔日方向訊號，這本身也是重要的發現）")
    else:
        L.append(_table(pd.DataFrame({
            "#": act["id"], "規則": act["text"], "樣本": act["n"],
            "勝率": act["hit_rate"].map(lambda v: fmt_pct(v, 1).lstrip("+")),
            "基準": act["base_rate"].map(lambda v: fmt_pct(v, 1).lstrip("+")),
            "近期 lift": act["recent_lift"].map(lambda v: fmt_pct(v, 1)),
        })))

    L.append("\n## 五、量化模型")
    if model_metrics:
        L.append(f"- 驗證期（{model_metrics['test_from']} 起，{model_metrics['test_rows']} 筆）AUC "
                 f"{model_metrics['auc']:.3f}（0.5=隨機），Brier {model_metrics['brier']:.4f}（基準 {model_metrics['brier_baseline']:.4f}）")
        if model_metrics.get("dir_hit_when_decided") is not None:
            L.append(f"- 有表態時方向命中 {model_metrics['dir_hit_when_decided']:.1%}，表態比例 {model_metrics['decided_share']:.0%}")
        L.append("- 影響最大的特徵：" + "、".join(model_metrics.get("top_features", {}).keys()))
    if calib:
        L.append(f"- 區間校準：k = {calib['k_all']:.2f} × ATR（目標覆蓋率 {calib['target']:.0%}）"
                 + (f"；早盤跳空係數 {calib['gap_slope']:.2f}" if "gap_slope" in calib else ""))

    drv = db.query_df(conn, "SELECT primary_driver d, COUNT(*) n FROM attributions WHERE date BETWEEN ? AND ? "
                            "AND source='llm' GROUP BY d ORDER BY n DESC", (week_start, week_end))
    if not drv.empty:
        L.append("\n## 六、本週漲跌主因分布（LLM 歸因）")
        L.append("、".join(f"{r['d']} {r['n']}" for _, r in drv.iterrows()))
    lessons = db.query_df(conn, "SELECT stock_id, date, evidence_json FROM attributions WHERE date BETWEEN ? AND ? "
                                "AND source='llm'", (week_start, week_end))
    ls = [json.loads(e).get("lesson") for e in lessons["evidence_json"].dropna()] if not lessons.empty else []
    ls = [x for x in ls if x]
    if ls:
        L.append("\n歸因中提到的觀察（待轉成可驗證規則）：")
        L += [f"- {x}" for x in ls[:12]]

    cost = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_log WHERE ts >= ?", (week_start,)).fetchone()[0]
    L.append(f"\n本週 LLM 花費約 ${cost:.2f}")
    L.append("\n---\n*本報告由程式自動產生，僅供研究學習，非投資建議。*")
    return "\n".join(L)
