"""漲跌歸因與 LLM 分析師。

歸因：先用數字拆解「今天的報酬 = 大盤 beta 部分 + 同產業部分 + 個股自身部分」，
     個股自身部分大的，再請 LLM 讀新聞／營收／財報／籌碼，說明原因並引用證據。
分析師：每天挑幾檔重點股，把量化預測、財報、新聞、相似歷史案例、已驗證規則、
     這檔股票過去的預測成績一起交給 LLM，產出「llm」版本的預測。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

import numpy as np
import pandas as pd

from . import config, db, llm, rules
from .features import FEATURE_DOCS
from .quant import bias_from_prob
from .util import fmt_pct

log = logging.getLogger(__name__)

DRIVERS = ["大盤連動", "產業輪動", "個股消息", "營收財報", "法人籌碼", "技術面", "無明確原因"]
ANALOG_FEATURES = ["ret_1", "ret_5", "ret_20", "ma20_gap", "kd_k", "rsi14", "vol_ratio", "atr_pct",
                   "range_pos20", "mkt_ret_5", "sox_ret_1"]


# ════════════════════════════════════════
#  資料整理：給 LLM 的個股檔案
# ════════════════════════════════════════

def stock_dossier(conn: sqlite3.Connection, sid: str, date: str, row: pd.Series) -> str:
    s = conn.execute("SELECT * FROM stocks WHERE stock_id=?", (sid,)).fetchone()
    lines = [f"## {sid} {s['name']}｜主題：{s['theme']}（{s['theme_note'] or s['industry_name']}）"]

    px = db.query_df(conn, "SELECT date, open, high, low, close, volume FROM prices WHERE stock_id=? AND date<=? "
                           "ORDER BY date DESC LIMIT 10", (sid, date))
    lines.append("近10日行情（日期 開 高 低 收 量[張]）：")
    for _, p in px.iloc[::-1].iterrows():
        lines.append(f"  {p['date']} {p['open']} {p['high']} {p['low']} {p['close']} {int((p['volume'] or 0) / 1000)}")

    tech = {k: row.get(k) for k in ["ret_1", "ret_5", "ret_20", "ma5_gap", "ma20_gap", "ma60_gap", "ma_bull",
                                    "kd_k", "kd_d", "kd_cross", "rsi14", "atr_pct", "vol_ratio", "range_pos20",
                                    "beta_mkt", "beta_sox", "rel_ret_5"]}
    lines.append("技術指標：" + "，".join(f"{k}={_r(v)}" for k, v in tech.items()))

    ch = db.query_df(conn, "SELECT date, foreign_net, trust_net, dealer_net, total_net FROM chips "
                           "WHERE stock_id=? AND date<=? ORDER BY date DESC LIMIT 5", (sid, date))
    if not ch.empty:
        lines.append("三大法人（張，外資/投信/自營/合計）：" + "；".join(
            f"{c['date'][5:]} {_lots(c['foreign_net'])}/{_lots(c['trust_net'])}/{_lots(c['dealer_net'])}/{_lots(c['total_net'])}"
            for _, c in ch.iterrows()))

    rev = db.query_df(conn, "SELECT ym, revenue, mom, yoy, cum_yoy FROM revenue WHERE stock_id=? "
                            "ORDER BY ym DESC LIMIT 3", (sid,))
    if not rev.empty:
        lines.append("月營收（千元）：" + "；".join(
            f"{r['ym']} {r['revenue']:,.0f} 月增{fmt_pct(r['mom'])} 年增{fmt_pct(r['yoy'])} 累計年增{fmt_pct(r['cum_yoy'])}"
            for _, r in rev.iterrows()))
    fin = db.query_df(conn, "SELECT * FROM financials WHERE stock_id=? ORDER BY year DESC, quarter DESC LIMIT 4", (sid,))
    if not fin.empty:
        parts = []
        for _, f in fin.iterrows():
            gm = f["gross_profit"] / f["revenue"] if f["revenue"] else None
            om = f["op_income"] / f["revenue"] if f["revenue"] else None
            parts.append(f"{f['year']}Q{f['quarter']}(年初累計) 營收{(f['revenue'] or 0) / 1e3:,.0f}百萬 "
                         f"毛利率{fmt_pct(gm)} 營益率{fmt_pct(om)} EPS {f['eps']}")
        lines.append("財報：" + "；".join(parts))

    news = db.query_df(conn,
        "SELECT n.published_at, n.title, n.source, ns.sentiment, ns.category, ns.importance FROM news_stock ns "
        "JOIN news n USING(news_id) WHERE ns.stock_id=? AND n.published_at <= ? || 'T23:59' "
        "ORDER BY n.published_at DESC LIMIT 8", (sid, date))
    if not news.empty:
        lines.append("近期新聞：")
        for _, n in news.iterrows():
            lab = f"[{n['category']} 情緒{_r(n['sentiment'])} 重要{_r(n['importance'])}]" if pd.notna(n["sentiment"]) else ""
            lines.append(f"  {n['published_at'][:16]} {n['title']}（{n['source']}）{lab}")
    else:
        lines.append("近期新聞：無")
    return "\n".join(lines)


def _r(v, d: int = 3):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "NA"
    return round(float(v), d)


def _lots(v):
    return "NA" if v is None or pd.isna(v) else f"{v / 1000:+,.0f}"


# ════════════════════════════════════════
#  歸因
# ════════════════════════════════════════

def decompose(day: pd.DataFrame) -> pd.DataFrame:
    """報酬拆解：market = beta × 大盤報酬；industry = 同主題平均 − 大盤；idio = 剩下的。"""
    d = day.copy()
    beta = d["beta_mkt"].fillna(1.0).clip(-0.5, 2.5)
    d["market_part"] = beta * d["mkt_ret_1"].fillna(0)
    d["industry_part"] = (d["ind_ret_1"] - d["mkt_ret_1"]).fillna(0)
    d["idio_part"] = d["ret_1"] - d["market_part"] - d["industry_part"]
    return d


def rule_driver(r: pd.Series) -> str:
    parts = {"大盤連動": abs(r["market_part"]), "產業輪動": abs(r["industry_part"]), "個股": abs(r["idio_part"])}
    top = max(parts, key=parts.get)
    if top != "個股":
        return top
    if pd.notna(r.get("chip_total_1")) and abs(r["chip_total_1"]) > 0.15:
        return "法人籌碼"
    return "無明確原因"


def attribute_day(conn: sqlite3.Connection, day: pd.DataFrame, date: str) -> pd.DataFrame:
    d = decompose(day)
    rows = []
    for _, r in d.iterrows():
        rows.append({"stock_id": r["stock_id"], "date": date, "ret": r["ret_1"], "market_part": r["market_part"],
                     "industry_part": r["industry_part"], "idio_part": r["idio_part"],
                     "primary_driver": rule_driver(r), "explanation": None, "evidence_json": None, "source": "rule"})
    db.upsert(conn, "attributions", rows)
    conn.commit()

    if not llm.available():
        return d
    # 個股因素最大的幾檔請 LLM 解釋
    top = d.reindex(d["idio_part"].abs().sort_values(ascending=False).index).head(config.ATTRIBUTION_PER_DAY)
    system = ("你是台股盤後研究員，任務是解釋「這檔股票今天為什麼這樣漲跌」。"
              "已用數字拆出大盤與產業的貢獻，請專注解釋『個股自身部分』。"
              "只能根據提供的新聞、營收、財報、法人與技術資料推論；沒有證據就說「無明確原因」，不要編造。"
              f"primary_driver 從以下擇一：{'、'.join(DRIVERS)}。"
              '輸出：{"primary_driver":"…","explanation":"80字內","evidence":["引用的新聞標題或數據"],'
              '"confidence":"低/中/高","is_new_information":true,"lesson":"這種情況下次可以注意什麼（30字內）"}')
    for _, r in top.iterrows():
        user = (f"日期：{date}\n今日報酬 {fmt_pct(r['ret_1'], 2)} = 大盤部分 {fmt_pct(r['market_part'], 2)}"
                f" + 產業部分 {fmt_pct(r['industry_part'], 2)} + 個股部分 {fmt_pct(r['idio_part'], 2)}\n"
                f"加權指數 {fmt_pct(r['mkt_ret_1'], 2)}，同主題平均 {fmt_pct(r['ind_ret_1'], 2)}，"
                f"前一晚費半 {fmt_pct(r['sox_ret_1'], 2)}\n\n" + stock_dossier(conn, r["stock_id"], date, r))
        try:
            res = llm.ask_json(conn, "attribution", system, user, max_tokens=600)
        except llm.BudgetExceeded:
            break
        if not res:
            continue
        driver = res.get("primary_driver") if res.get("primary_driver") in DRIVERS else "無明確原因"
        evidence = {"evidence": res.get("evidence", []), "confidence": res.get("confidence"),
                    "is_new_information": res.get("is_new_information"), "lesson": res.get("lesson")}
        conn.execute("UPDATE attributions SET primary_driver=?, explanation=?, evidence_json=?, source='llm' "
                     "WHERE stock_id=? AND date=?",
                     (driver, str(res.get("explanation", ""))[:300], json.dumps(evidence, ensure_ascii=False),
                      r["stock_id"], date))
        conn.commit()
    return d


# ════════════════════════════════════════
#  經驗檢索
# ════════════════════════════════════════

def historical_analogs(feats: pd.DataFrame, row: pd.Series, k: int = 50) -> Optional[dict]:
    """在多年歷史中找技術型態最像的 k 個「股票日」，統計它們隔天的表現。"""
    hist = feats[(feats["date"] < row["date"]) & feats["next_ret"].notna()
                 & feats["theme"].isin(config.THEMES)].dropna(subset=ANALOG_FEATURES)
    if len(hist) < 500 or row[ANALOG_FEATURES].isna().any():
        return None
    X = hist[ANALOG_FEATURES].to_numpy(dtype=float)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1
    q = (row[ANALOG_FEATURES].to_numpy(dtype=float) - mu) / sd
    dist = np.sqrt((((X - mu) / sd - q) ** 2).sum(axis=1))
    idx = np.argsort(dist)[:k]
    nr = hist["next_ret"].to_numpy()[idx]
    return {"k": k, "up_ratio": float((nr > 0).mean()), "mean": float(nr.mean()),
            "p25": float(np.percentile(nr, 25)), "p75": float(np.percentile(nr, 75))}


def track_record(conn: sqlite3.Connection, sid: str, limit: int = 6) -> str:
    df = db.query_df(conn,
        "SELECT p.date, p.variant, p.low, p.high, p.bias, e.actual_close, e.actual_ret, e.in_range, e.dir_hit, "
        "a.primary_driver, a.explanation FROM predictions p JOIN evaluations e ON e.prediction_id=p.id "
        "LEFT JOIN attributions a ON a.stock_id=p.stock_id AND a.date=e.target_date "
        "WHERE p.stock_id=? AND p.variant='llm' ORDER BY p.date DESC LIMIT ?", (sid, limit))
    if df.empty:
        return "（尚無這檔股票的 LLM 預測紀錄）"
    return "\n".join(
        f"  {r['date']} 預測 {r['low']}~{r['high']} {r['bias']} → 實際 {r['actual_close']}（{fmt_pct(r['actual_ret'])}）"
        f" 區間{'✓' if r['in_range'] else '✗'} 方向{'✓' if r['dir_hit'] else '✗'}"
        + (f"；原因：{r['primary_driver']} {r['explanation'] or ''}" if r["primary_driver"] else "")
        for _, r in df.iterrows())


def self_assessment(conn: sqlite3.Connection) -> str:
    df = db.query_df(conn,
        "SELECT p.variant, COUNT(*) n, AVG(e.in_range) rh, AVG(CASE WHEN p.bias!='flat' THEN e.dir_hit END) dh "
        "FROM predictions p JOIN evaluations e ON e.prediction_id=p.id "
        "WHERE e.target_date >= date('now','-30 day') GROUP BY p.variant")
    if df.empty:
        return "（尚無對帳紀錄）"
    return "；".join(f"{r['variant']}：{int(r['n'])} 筆，區間命中 {fmt_pct(r['rh'], 0)}，"
                    f"方向命中 {fmt_pct(r['dh'], 0) if pd.notna(r['dh']) else '—'}" for _, r in df.iterrows())


# ════════════════════════════════════════
#  LLM 分析師
# ════════════════════════════════════════

def pick_focus(day: pd.DataFrame, quant_preds: pd.DataFrame, n: int) -> list[str]:
    """重點股：模型把握度高、量能異常、個股消息多；同時盡量涵蓋不同主題。"""
    q = quant_preds.set_index("stock_id")["prob_up"]
    d = day.copy()
    d["conf"] = (d["stock_id"].map(q).fillna(0.5) - 0.5).abs() * 2
    d["score"] = (d["conf"] + (d["vol_ratio"].fillna(1) > 2).astype(float) * 0.3
                  + d["news_n_3"].fillna(0).clip(0, 5) * 0.06 + d["ret_1"].abs().fillna(0) * 3)
    d = d.sort_values("score", ascending=False)
    picks, seen = [], set()
    for _, r in d.iterrows():  # 先每個主題挑一檔
        if r["theme"] not in seen:
            picks.append(r["stock_id"])
            seen.add(r["theme"])
    for sid in d["stock_id"]:
        if len(picks) >= n:
            break
        if sid not in picks:
            picks.append(sid)
    return picks[:n]


def analyst(conn: sqlite3.Connection, feats: pd.DataFrame, day: pd.DataFrame, date: str,
            focus: list[str], quant_preds: pd.DataFrame) -> list[dict]:
    if not llm.available():
        return []
    system = (
        "你是台股量化趨勢分析師，正在用『從經驗中學習』的方式工作。你會拿到：個股資料（行情、技術、法人、月營收、財報、新聞）、"
        "量化模型的預測、歷史上型態最像的案例統計、經過回測驗證的規則、這檔股票過去的預測成績、系統近期的整體成績。\n"
        "要求：1) 先判斷基本面與消息是否支持目前股價走勢；2) 再決定明日區間與方向；"
        "3) 量化模型與歷史案例是有統計依據的基準，若你要推翻它，必須說出具體理由；"
        "4) 隔日方向本質上接近隨機，沒有把握就給 flat，不要硬猜；5) 區間不可超過漲跌停（±10%）。\n"
        '輸出：{"bias":"up/down/flat","prob_up":0.5,"low":價格,"high":價格,'
        '"fundamental_view":"財報與營收怎麼看（60字內）","news_view":"消息面（60字內）",'
        '"thesis":"明日判斷的核心理由（100字內）","override_quant":"若與量化模型不同，理由；相同則空字串",'
        '"risks":["風險1","風險2"],"watch":"明天盤中要觀察什麼（40字內）"}')
    qp = quant_preds.set_index("stock_id")
    overall = self_assessment(conn)
    out = []
    for sid in focus:
        r = day[day["stock_id"] == sid].iloc[0]
        q = qp.loc[sid]
        analog = historical_analogs(feats, r)
        matched = rules.matching_rules(conn, r)
        att = conn.execute("SELECT * FROM attributions WHERE stock_id=? AND date=?", (sid, date)).fetchone()
        user = "\n\n".join([
            stock_dossier(conn, sid, date, r),
            (f"今日漲跌拆解：總 {fmt_pct(att['ret'], 2)} = 大盤 {fmt_pct(att['market_part'], 2)} + 產業 "
             f"{fmt_pct(att['industry_part'], 2)} + 個股 {fmt_pct(att['idio_part'], 2)}；主因：{att['primary_driver']} "
             f"{att['explanation'] or ''}") if att else "",
            f"量化模型：明日區間 {q['low']}~{q['high']}，上漲機率 {_r(q['prob_up'], 2)}（{q['bias']}）",
            (f"歷史最相似 {analog['k']} 個案例：隔日上漲比例 {analog['up_ratio']:.0%}，平均 {fmt_pct(analog['mean'], 2)}，"
             f"四分位 {fmt_pct(analog['p25'], 2)} ~ {fmt_pct(analog['p75'], 2)}") if analog else "歷史相似案例：資料不足",
            "符合的已驗證規則：\n" + ("\n".join(
                f"  - {m['text']}：歷史 {m['n']} 次，勝率 {m['hit_rate']:.0%}（基準 {m['base_rate']:.0%}）"
                for m in matched) if matched else "  無"),
            "這檔股票過去的預測紀錄（從錯誤中學習）：\n" + track_record(conn, sid),
            f"系統近 30 日整體成績：{overall}",
            f"大盤：加權指數今日 {fmt_pct(r['mkt_ret_1'], 2)}、近5日 {fmt_pct(r['mkt_ret_5'], 2)}；前一晚費半 {fmt_pct(r['sox_ret_1'], 2)}",
        ])
        try:
            res = llm.ask_json(conn, "analyst", system, user, max_tokens=900)
        except llm.BudgetExceeded:
            break
        if not res:
            continue
        close = float(r["close"])
        try:
            low = max(float(res["low"]), close * 0.9)
            high = min(float(res["high"]), close * 1.1)
            prob = float(np.clip(float(res.get("prob_up", 0.5)), 0.02, 0.98))
        except (KeyError, TypeError, ValueError):
            continue
        if low > high:
            low, high = high, low
        bias = res.get("bias") if res.get("bias") in ("up", "down", "flat") else bias_from_prob(prob)
        detail = {k: res.get(k) for k in ("fundamental_view", "news_view", "thesis", "override_quant", "risks", "watch")}
        detail["analog"] = analog
        detail["rules"] = [m["id"] for m in matched]
        out.append({"stock_id": sid, "date": date, "variant": "llm", "base_close": close, "low": round(low, 2),
                    "high": round(high, 2), "prob_up": prob, "bias": bias,
                    "rationale": json.dumps(detail, ensure_ascii=False), "features_json": q["features_json"]})
    return out


def weekly_mistakes(conn: sqlite3.Connection, since: str, limit: int = 25) -> str:
    """本週錯得最離譜的預測，連同當天歸因，給 LLM 找規律。"""
    df = db.query_df(conn,
        "SELECT p.stock_id, s.name, p.date, p.variant, p.bias, p.prob_up, p.low, p.high, e.actual_ret, "
        "e.open_outside, p.features_json, a.primary_driver, a.explanation "
        "FROM predictions p JOIN evaluations e ON e.prediction_id=p.id JOIN stocks s ON s.stock_id=p.stock_id "
        "LEFT JOIN attributions a ON a.stock_id=p.stock_id AND a.date=e.target_date "
        "WHERE e.target_date>=? AND p.variant IN ('quant','llm') AND p.bias!='flat' AND e.dir_hit=0 "
        "ORDER BY ABS(e.actual_ret) DESC LIMIT ?", (since, limit))
    lines = []
    for _, r in df.iterrows():
        f = json.loads(r["features_json"] or "{}")
        key = {k: f.get(k) for k in ("ret_5", "kd_k", "kd_cross", "vol_ratio", "ma_bull", "chip_foreign_5",
                                     "sox_ret_1", "mkt_ret_5", "rev_yoy") if k in FEATURE_DOCS}
        lines.append(f"- {r['stock_id']}{r['name']} {r['date']} [{r['variant']}] 預測 {r['bias']}"
                     f"（p={_r(r['prob_up'], 2)}）實際 {fmt_pct(r['actual_ret'])}"
                     f"{'，開盤即跳出區間' if r['open_outside'] else ''}；隔日主因：{r['primary_driver'] or '—'} "
                     f"{r['explanation'] or ''}；當時特徵 {json.dumps(key, ensure_ascii=False)}")
    return "\n".join(lines) or "（本週沒有方向錯誤的預測）"
