"""量化模型：
1. 區間：收盤 ± k × ATR%，k 依歷史資料校準到目標覆蓋率（預設 70%）。
2. 方向：梯度提升樹，輸出隔日上漲機率；每週用 walk-forward 重新訓練與驗證。
3. 早盤修正（quant_am）：用前一晚費半漲跌 × 個股對費半的敏感度，平移區間中心。
"""
from __future__ import annotations

import json
import logging
import pickle
import sqlite3
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import brier_score_loss, roc_auc_score

from . import config
from .features import MODEL_FEATURES
from .util import now_tpe

log = logging.getLogger(__name__)
MODEL_PATH = config.MODEL_DIR / "direction.pkl"
CALIB_PATH = config.MODEL_DIR / "calibration.json"


def _eligible(feats: pd.DataFrame) -> pd.DataFrame:
    """訓練用樣本：七大主題、流動性足夠、有隔日報酬、排除漲跌停鎖死（無法交易的樣本）。"""
    f = feats[feats["theme"].isin(config.THEMES) & feats["next_ret"].notna() & feats["atr_pct"].notna()]
    f = f[f["avg_value_20"] >= config.MIN_AVG_TRADE_VALUE * 0.5]
    return f[f["next_ret"].abs() < 0.105]


def _new_model() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(max_iter=250, learning_rate=0.04, max_leaf_nodes=15,
                                          min_samples_leaf=200, l2_regularization=1.0, random_state=7)


def train(feats: pd.DataFrame, test_days: int = 60) -> dict:
    """walk-forward：用較早資料訓練、最後 test_days 個交易日測試，再用全部資料訓練正式模型。"""
    data = _eligible(feats)
    dates = sorted(data["date"].unique())
    if len(dates) < test_days + 120:
        raise ValueError(f"資料只有 {len(dates)} 個交易日，至少需要 {test_days + 120}")
    split = dates[-test_days]
    # 只用有足夠資料的特徵（例如新聞、籌碼剛開始累積時幾乎全是空值）
    data = data.copy()
    data[MODEL_FEATURES] = data[MODEL_FEATURES].astype(float).replace([np.inf, -np.inf], np.nan)
    early = data[data["date"] < split]
    cols = [c for c in MODEL_FEATURES if early[c].notna().sum() >= 200 and early[c].nunique() > 1]
    tr, te = data[data["date"] < split], data[data["date"] >= split]
    y_tr, y_te = (tr["next_ret"] > 0).astype(int), (te["next_ret"] > 0).astype(int)

    m = _new_model().fit(tr[cols], y_tr)
    p = m.predict_proba(te[cols])[:, 1]
    base_rate = float(y_tr.mean())
    bias = np.where(p > config.BIAS_UP_PROB, 1, np.where(p < config.BIAS_DOWN_PROB, -1, 0))
    actual = np.sign(te["next_ret"].values)
    decided = bias != 0
    metrics = {
        "trained_at": now_tpe().isoformat(timespec="seconds"),
        "train_rows": int(len(tr)), "test_rows": int(len(te)), "test_from": split,
        "auc": float(roc_auc_score(y_te, p)) if y_te.nunique() > 1 else None,
        "brier": float(brier_score_loss(y_te, p)),
        "brier_baseline": float(brier_score_loss(y_te, np.full(len(y_te), base_rate))),
        "dir_hit_when_decided": float((bias[decided] == actual[decided]).mean()) if decided.any() else None,
        "decided_share": float(decided.mean()),
        "up_rate_test": float(y_te.mean()),
    }
    # 前 10 名最重要特徵（排列重要度太慢，用與預測值的相關度近似）
    corr = te[cols].apply(lambda c: pd.Series(p, index=te.index).corr(c)).abs().sort_values(ascending=False)
    metrics["top_features"] = {k: round(float(v), 3) for k, v in corr.head(10).items() if pd.notna(v)}

    metrics["features_used"] = len(cols)
    final = _new_model().fit(data[cols], (data["next_ret"] > 0).astype(int))
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"model": final, "features": cols, "metrics": metrics}, f)
    log.info("方向模型：AUC=%s Brier=%.4f（基準 %.4f）", metrics["auc"], metrics["brier"], metrics["brier_baseline"])
    return metrics


def calibrate(feats: pd.DataFrame, recent_days: int = 120) -> dict:
    """找出 k，使 |隔日報酬| <= k × ATR% 的比例 = 目標覆蓋率。依波動度分三組各自校準。"""
    data = _eligible(feats)
    dates = sorted(data["date"].unique())[-recent_days:]
    d = data[data["date"].isin(dates)].copy()
    z = (d["next_ret"].abs() / d["atr_pct"]).replace([np.inf, -np.inf], np.nan).dropna()
    d = d.loc[z.index]
    d["z"] = z
    q = config.RANGE_COVERAGE_TARGET
    edges = d["atr_pct"].quantile([0.33, 0.67]).tolist()
    d["bucket"] = np.digitize(d["atr_pct"], edges)
    calib = {"target": q, "k_all": float(d["z"].quantile(q)), "atr_edges": edges,
             "k_bucket": {str(b): float(g["z"].quantile(q)) for b, g in d.groupby("bucket")},
             "updated_at": now_tpe().isoformat(timespec="seconds"), "rows": int(len(d))}
    # 早盤修正係數：x = beta_sox × 當晚費半報酬
    #   隔日開盤跳空 ≈ gap_slope × x；隔日上漲機率 ≈ 原機率 + prob_slope × x
    g = data[data["date"].isin(sorted(data["date"].unique())[-250:])]
    x = (g["beta_sox"].clip(-0.5, 1.5) * g["sox_on"]).rename("x")
    gg = pd.concat([x, g["next_open_gap"], (g["next_ret"] > 0).astype(float).rename("up")], axis=1).dropna()
    if len(gg) > 500 and gg["x"].var() > 0:
        calib["gap_slope"] = float(gg["x"].cov(gg["next_open_gap"]) / gg["x"].var())
        calib["prob_slope"] = float(gg["x"].cov(gg["up"]) / gg["x"].var())
    CALIB_PATH.write_text(json.dumps(calib, ensure_ascii=False, indent=2))
    log.info("區間校準：k=%.2f（%d 筆）", calib["k_all"], len(d))
    return calib


def load_calibration() -> dict:
    if CALIB_PATH.exists():
        return json.loads(CALIB_PATH.read_text())
    return {"k_all": 0.9, "atr_edges": [], "k_bucket": {}}


def _k_for(atr: float, calib: dict) -> float:
    if calib.get("atr_edges") and calib.get("k_bucket"):
        b = str(int(np.digitize([atr], calib["atr_edges"])[0]))
        return calib["k_bucket"].get(b, calib["k_all"])
    return calib["k_all"]


def load_model() -> Optional[dict]:
    if MODEL_PATH.exists():
        with open(MODEL_PATH, "rb") as f:
            return pickle.load(f)
    return None


def _clip_limits(close: float, low: float, high: float) -> tuple[float, float]:
    return max(low, close * 0.9), min(high, close * 1.1)


def bias_from_prob(p: Optional[float]) -> str:
    if p is None:
        return "flat"
    return "up" if p > config.BIAS_UP_PROB else "down" if p < config.BIAS_DOWN_PROB else "flat"


def predict(day: pd.DataFrame) -> list[dict]:
    """對當天的股票產生 baseline 與 quant 兩種預測。"""
    calib = load_calibration()
    bundle = load_model()
    probs = None
    if bundle is not None and not day.empty:
        probs = bundle["model"].predict_proba(
            day[bundle["features"]].astype(float).replace([np.inf, -np.inf], np.nan))[:, 1]
    out = []
    for i, (_, r) in enumerate(day.iterrows()):
        close, atr = float(r["close"]), float(r["atr_pct"]) if pd.notna(r["atr_pct"]) else 0.03
        feat = {k: (None if pd.isna(r.get(k)) else round(float(r[k]), 5)) for k in MODEL_FEATURES}
        # baseline：固定 1 × ATR、不判斷方向
        lo, hi = _clip_limits(close, close * (1 - atr), close * (1 + atr))
        out.append({"stock_id": r["stock_id"], "date": r["date"], "variant": "baseline", "base_close": close,
                    "low": round(lo, 2), "high": round(hi, 2), "prob_up": None, "bias": "flat",
                    "rationale": "收盤 ± 1×ATR", "features_json": None})
        k = _k_for(atr, calib)
        lo, hi = _clip_limits(close, close * (1 - k * atr), close * (1 + k * atr))
        p = float(probs[i]) if probs is not None else None
        out.append({"stock_id": r["stock_id"], "date": r["date"], "variant": "quant", "base_close": close,
                    "low": round(lo, 2), "high": round(hi, 2), "prob_up": p, "bias": bias_from_prob(p),
                    "rationale": f"k={k:.2f}×ATR；上漲機率 {p:.0%}" if p is not None else f"k={k:.2f}×ATR",
                    "features_json": json.dumps(feat)})
    return out


def morning_adjust(quant_preds: pd.DataFrame, sox_overnight: float) -> list[dict]:
    """早盤：前一晚費半漲跌已知 → 依個股 beta_sox 平移區間中心（係數由 calibrate 估計）。"""
    calib = load_calibration()
    out = []
    for _, r in quant_preds.iterrows():
        feat = json.loads(r["features_json"] or "{}")
        beta = feat.get("beta_sox") or 0.0
        beta = float(np.clip(beta, -0.5, 1.5))
        x = beta * sox_overnight
        shift = calib.get("gap_slope", 1.0) * x
        close = r["base_close"]
        lo, hi = _clip_limits(close, r["low"] * (1 + shift), r["high"] * (1 + shift))
        p = r["prob_up"]
        if p is not None and not pd.isna(p):
            p = float(np.clip(p + np.clip(calib.get("prob_slope", 0.0) * x, -0.2, 0.2), 0.02, 0.98))
        else:
            p = None
        out.append({"stock_id": r["stock_id"], "date": r["date"], "variant": "quant_am", "base_close": close,
                    "low": round(lo, 2), "high": round(hi, 2), "prob_up": p, "bias": bias_from_prob(p),
                    "rationale": f"費半前一晚 {sox_overnight:+.2%} × beta {beta:.2f} → 平移 {shift:+.2%}",
                    "features_json": r["features_json"]})
    return out


def record_run(conn: sqlite3.Connection, kind: str, metrics: dict) -> None:
    conn.execute("INSERT INTO model_runs (created_at, kind, metrics_json) VALUES (?,?,?)",
                 (now_tpe().isoformat(timespec="seconds"), kind, json.dumps(metrics, ensure_ascii=False)))
    conn.commit()
