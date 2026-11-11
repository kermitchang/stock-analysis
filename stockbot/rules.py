"""規則庫：系統「經驗」的核心。

流程：假設（種子規則 / LLM 從錯誤中提出）→ 用多年歷史資料回測 → 通過統計門檻才啟用
→ 每週用近期資料複驗，失效就退役。只有「已驗證」的規則會被放進每日 LLM 分析的提示裡。

條件格式（JSON，只能引用 features.FEATURE_DOCS 裡的欄位，不執行任意程式碼）：
  {"all": [{"f": "kd_k", "op": ">", "v": 80}, {"f": "kd_cross", "op": "==", "v": -1}]}
"""
from __future__ import annotations

import json
import logging
import math
import operator
import sqlite3
from typing import Optional

import numpy as np
import pandas as pd

from . import config, db, llm
from .features import FEATURE_DOCS
from .util import now_tpe

log = logging.getLogger(__name__)
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq}

# 來自 methodology/4-dimension-framework.md 的既有交易直覺 —— 先拿去回測，看哪些真的有效
SEED_RULES = [
    ("均線多頭排列且量增 → 隔日偏多", "up",
     {"all": [{"f": "ma_bull", "op": "==", "v": 1}, {"f": "vol_ratio", "op": ">", "v": 1.5}]}),
    ("KD 低檔（K<25）黃金交叉 → 隔日偏多", "up",
     {"all": [{"f": "kd_k", "op": "<", "v": 25}, {"f": "kd_cross", "op": "==", "v": 1}]}),
    ("KD 高檔（K>80）死亡交叉 → 隔日偏空", "down",
     {"all": [{"f": "kd_k", "op": ">", "v": 80}, {"f": "kd_cross", "op": "==", "v": -1}]}),
    ("均線空頭排列 → 隔日偏空", "down", {"all": [{"f": "ma_bull", "op": "==", "v": -1}]}),
    ("漲停 → 隔日續漲", "up", {"all": [{"f": "limit_up", "op": "==", "v": 1}]}),
    ("跌停 → 隔日反彈", "up", {"all": [{"f": "limit_down", "op": "==", "v": 1}]}),
    ("前一晚費半跌超過 3% → 隔日偏空", "down", {"all": [{"f": "sox_ret_1", "op": "<", "v": -0.03}]}),
    ("短線乖離過大（5日乖離>8%）→ 隔日回檔", "down", {"all": [{"f": "ma5_gap", "op": ">", "v": 0.08}]}),
    ("投信近5日明顯買超 → 隔日偏多", "up", {"all": [{"f": "chip_trust_5", "op": ">", "v": 0.03}]}),
    ("RSI 超賣（<25）→ 隔日反彈", "up", {"all": [{"f": "rsi14", "op": "<", "v": 25}]}),
]


def validate_condition(cond: dict) -> Optional[str]:
    if not isinstance(cond, dict) or not isinstance(cond.get("all"), list) or not cond["all"]:
        return "條件必須是 {'all': [...]}"
    for c in cond["all"]:
        if not isinstance(c, dict) or c.get("f") not in FEATURE_DOCS:
            return f"未知特徵：{c}"
        if c.get("op") not in OPS:
            return f"未知運算子：{c.get('op')}"
        if not isinstance(c.get("v"), (int, float)):
            return f"門檻必須是數字：{c}"
    return None


def mask(df: pd.DataFrame, cond: dict) -> pd.Series:
    m = pd.Series(True, index=df.index)
    for c in cond["all"]:
        col = df[c["f"]]
        m &= col.notna() & OPS[c["op"]](col, c["v"])
    return m


def describe_condition(cond: dict) -> str:
    return " 且 ".join(f"{c['f']} {c['op']} {c['v']}" for c in cond["all"])


def backtest(feats: pd.DataFrame, cond: dict, direction: str) -> dict:
    """命中＝隔日方向與規則一致。基準＝同一批樣本裡所有股票日的同方向比例。"""
    data = feats[feats["theme"].isin(config.THEMES) & feats["next_ret"].notna()]
    hit_all = (data["next_ret"] > 0) if direction == "up" else (data["next_ret"] < 0)
    m = mask(data, cond)
    n = int(m.sum())
    base = float(hit_all.mean()) if len(data) else float("nan")
    res = {"n": n, "base_rate": base, "hit_rate": None, "lift": None, "p_value": None,
           "recent_n": 0, "recent_lift": None, "avg_next_ret": None}
    if n == 0:
        return res
    hit = float(hit_all[m].mean())
    se = math.sqrt(base * (1 - base) / n)
    z = (hit - base) / se if se > 0 else 0.0
    res.update(hit_rate=hit, lift=hit - base, p_value=0.5 * math.erfc(z / math.sqrt(2)),
               avg_next_ret=float(data.loc[m, "next_ret"].mean()))
    recent_dates = sorted(data["date"].unique())[-config.RULE_RECENT_DAYS:]
    rd = data["date"].isin(recent_dates)
    rm = m & rd
    if rm.sum() > 0:
        res["recent_n"] = int(rm.sum())
        res["recent_lift"] = float(hit_all[rm].mean() - hit_all[rd].mean())
    return res


def decide_status(stats: dict) -> tuple[str, str]:
    if stats["n"] < config.RULE_MIN_SAMPLES:
        return "candidate", f"樣本不足（{stats['n']} < {config.RULE_MIN_SAMPLES}）"
    if stats["lift"] is None or stats["lift"] < config.RULE_MIN_LIFT or stats["p_value"] > config.RULE_MAX_P_VALUE:
        return "rejected", "勝率沒有顯著高於基準"
    if stats["recent_n"] >= 15 and stats["recent_lift"] is not None and stats["recent_lift"] < 0:
        return "retired", "近期失效（近期勝率低於基準）"
    return "active", "通過驗證"


def add_rule(conn: sqlite3.Connection, text: str, direction: str, cond: dict, source: str,
             feats: pd.DataFrame) -> Optional[int]:
    err = validate_condition(cond)
    if err or direction not in ("up", "down"):
        log.info("規則格式不符，略過：%s（%s）", text, err)
        return None
    cj = json.dumps(cond, ensure_ascii=False, sort_keys=True)
    if conn.execute("SELECT 1 FROM rules WHERE condition_json=? AND direction=?", (cj, direction)).fetchone():
        return None
    stats = backtest(feats, cond, direction)
    status, note = decide_status(stats)
    now = now_tpe().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO rules (text, condition_json, direction, status, source, n, hit_rate, base_rate, lift, "
        "p_value, recent_n, recent_lift, created_at, updated_at, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (text, cj, direction, status, source, stats["n"], stats["hit_rate"], stats["base_rate"], stats["lift"],
         stats["p_value"], stats["recent_n"], stats["recent_lift"], now, now, note))
    conn.commit()
    # 顯著「反向有效」→ 自動驗證反向版本（例如「跌停→反彈」其實是「跌停→續跌」）
    if (status == "rejected" and stats["lift"] is not None and stats["lift"] <= -config.RULE_MIN_LIFT
            and stats["n"] >= config.RULE_MIN_SAMPLES and not text.startswith("（反向）")):
        flipped = "down" if direction == "up" else "up"
        add_rule(conn, f"（反向）{text.split('→')[0].strip()} → 隔日{'偏多' if flipped == 'up' else '偏空'}",
                 flipped, cond, source, feats)
    return cur.lastrowid


def seed(conn: sqlite3.Connection, feats: pd.DataFrame) -> None:
    for text, direction, cond in SEED_RULES:
        add_rule(conn, text, direction, cond, "seed", feats)


def review_all(conn: sqlite3.Connection, feats: pd.DataFrame) -> list[dict]:
    """每週複驗所有規則（被拒絕的不再複驗），回傳狀態有變化的清單。"""
    changes = []
    now = now_tpe().isoformat(timespec="seconds")
    for r in conn.execute("SELECT * FROM rules WHERE status IN ('candidate','active','retired')").fetchall():
        cond = json.loads(r["condition_json"])
        stats = backtest(feats, cond, r["direction"])
        status, note = decide_status(stats)
        if r["status"] == "retired" and status == "active":
            # 退役規則要近期重新有效才復活
            status = "active" if (stats["recent_lift"] or 0) > config.RULE_MIN_LIFT else "retired"
        conn.execute("UPDATE rules SET status=?, n=?, hit_rate=?, base_rate=?, lift=?, p_value=?, recent_n=?, "
                     "recent_lift=?, updated_at=?, note=? WHERE id=?",
                     (status, stats["n"], stats["hit_rate"], stats["base_rate"], stats["lift"], stats["p_value"],
                      stats["recent_n"], stats["recent_lift"], now, note, r["id"]))
        if status != r["status"]:
            changes.append({"id": r["id"], "text": r["text"], "from": r["status"], "to": status, "note": note})
    # 只保留 lift 最高的 MAX_ACTIVE_RULES 條
    active = conn.execute("SELECT id FROM rules WHERE status='active' ORDER BY lift DESC").fetchall()
    for r in active[config.MAX_ACTIVE_RULES:]:
        conn.execute("UPDATE rules SET status='candidate', note='超過啟用上限' WHERE id=?", (r["id"],))
    conn.commit()
    return changes


def matching_rules(conn: sqlite3.Connection, row: pd.Series) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM rules WHERE status='active'").fetchall():
        cond = json.loads(r["condition_json"])
        if bool(mask(row.to_frame().T.infer_objects(), cond).iloc[0]):
            out.append(dict(r))
    return out


def propose_from_mistakes(conn: sqlite3.Connection, feats: pd.DataFrame, mistakes: str) -> list[int]:
    """讓 LLM 根據本週的錯誤提出新假設；程式立刻回測決定要不要採用。"""
    existing = "\n".join(f"- [{r['status']}] {r['text']}（{describe_condition(json.loads(r['condition_json']))}，"
                         f"lift={r['lift'] if r['lift'] is None else round(r['lift'], 3)}）"
                         for r in conn.execute("SELECT * FROM rules ORDER BY id").fetchall())
    docs = "\n".join(f"- {k}：{v}" for k, v in FEATURE_DOCS.items())
    system = ("你是量化研究員。根據本週預測錯誤的案例，提出最多 5 條「可以用資料驗證」的隔日方向規則。"
              "每條規則只能使用提供的特徵欄位與數字門檻，條件 2~3 個為佳。不要重複既有規則。"
              '輸出：{"rules":[{"text":"中文說明","direction":"up或down",'
              '"condition":{"all":[{"f":"特徵","op":">","v":0.03}]},"why":"從哪個錯誤得到靈感"}]}')
    user = f"可用特徵：\n{docs}\n\n既有規則：\n{existing}\n\n本週錯誤摘要：\n{mistakes}"
    try:
        res = llm.ask_json(conn, "rule_proposal", system, user, max_tokens=2500, temperature=0.6)
    except llm.BudgetExceeded:
        return []
    ids = []
    for r in (res or {}).get("rules", [])[:5]:
        rid = add_rule(conn, str(r.get("text", ""))[:200], r.get("direction"), r.get("condition"), "llm", feats)
        if rid:
            ids.append(rid)
    return ids
