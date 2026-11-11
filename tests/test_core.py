"""離線單元測試（不連網、不用 LLM）：python -m pytest -q"""
import json
import os
import tempfile

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("STOCKBOT_DATA_DIR", tempfile.mkdtemp())

from stockbot import config, db, evaluate, features, llm, rules  # noqa: E402
from stockbot.util import roc_to_iso, roc_ym_to_iso, to_float  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    yield c
    c.close()


def _seed_prices(conn, n=120, seed=1):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-01-05", periods=n).strftime("%Y-%m-%d").tolist()
    db.upsert(conn, "stocks", [
        {"stock_id": "1111", "name": "測試一", "market": "TWSE", "industry_code": "24", "industry_name": "半導體業",
         "theme": "半導體", "theme_source": "manual", "theme_note": None, "updated_at": "2026-01-01"},
        {"stock_id": "^TWII", "name": "加權", "market": "INDEX", "industry_code": None, "industry_name": None,
         "theme": None, "theme_source": None, "theme_note": None, "updated_at": "2026-01-01"},
        {"stock_id": "^SOX", "name": "費半", "market": "INDEX", "industry_code": None, "industry_name": None,
         "theme": None, "theme_source": None, "theme_note": None, "updated_at": "2026-01-01"},
    ])
    rows = []
    for sid, base in (("1111", 30.0), ("^TWII", 20000.0), ("^SOX", 5000.0)):
        c = base * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
        for d, v in zip(dates, c):
            rows.append({"stock_id": sid, "date": d, "open": v * 0.99, "high": v * 1.02, "low": v * 0.98,
                         "close": float(v), "volume": 1e6})
    db.upsert(conn, "prices", rows)
    conn.commit()
    return dates


def test_util_parsers():
    assert roc_to_iso("1151110") == "2026-11-10"
    assert roc_to_iso("115/11/10") == "2026-11-10"
    assert roc_ym_to_iso("11509") == "2026-09"
    assert to_float("1,234.5") == 1234.5 and to_float("+0.42") == 0.42 and to_float("--") is None


def test_features_no_lookahead(conn):
    dates = _seed_prices(conn)
    f = features.build_features(conn)
    s = f[f.stock_id == "1111"].set_index("date")
    # sox_ret_1（收盤時已知）必須是「前一個」美股交易日的報酬
    sox = pd.Series({r["date"]: r["close"] for r in conn.execute(
        "SELECT date, close FROM prices WHERE stock_id='^SOX'")}).sort_index().pct_change()
    d = dates[50]
    assert s.loc[d, "sox_ret_1"] == pytest.approx(sox.loc[dates[49]])
    assert s.loc[d, "sox_on"] == pytest.approx(sox.loc[d])          # 當晚（隔天早上才知道）
    # next_ret 是下一列的報酬
    c = s["close"]
    assert s.loc[d, "next_ret"] == pytest.approx(c.loc[dates[51]] / c.loc[d] - 1)
    assert pd.isna(s.loc[dates[-1], "next_ret"])


def test_rule_validation_and_backtest(conn):
    _seed_prices(conn, n=200)
    f = features.build_features(conn)
    assert rules.validate_condition({"all": [{"f": "kd_k", "op": ">", "v": 80}]}) is None
    assert rules.validate_condition({"all": [{"f": "__import__", "op": ">", "v": 1}]})
    assert rules.validate_condition({"all": [{"f": "kd_k", "op": "in", "v": 1}]})
    res = rules.backtest(f, {"all": [{"f": "ret_1", "op": ">", "v": -1}]}, "up")
    assert res["n"] > 100 and abs(res["lift"]) < 0.02   # 幾乎全選 → 與基準相同


def test_evaluate_definitions(conn):
    db.upsert(conn, "prices", [
        {"stock_id": "1111", "date": "2026-03-02", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
        {"stock_id": "1111", "date": "2026-03-03", "open": 11.5, "high": 11.6, "low": 10.4, "close": 10.5, "volume": 1}])
    preds = [
        {"stock_id": "1111", "date": "2026-03-02", "variant": "quant", "base_close": 10, "low": 9.5, "high": 10.8,
         "prob_up": 0.7, "bias": "up", "rationale": "", "features_json": None, "created_at": ""},
        {"stock_id": "1111", "date": "2026-03-02", "variant": "llm", "base_close": 10, "low": 10.6, "high": 11,
         "prob_up": 0.3, "bias": "down", "rationale": "", "features_json": None, "created_at": ""},
    ]
    db.upsert(conn, "predictions", preds)
    assert evaluate.evaluate_pending(conn) == 2
    ev = {r["variant"]: r for r in conn.execute(
        "SELECT p.variant, e.* FROM evaluations e JOIN predictions p ON p.id=e.prediction_id")}
    assert ev["quant"]["in_range"] == 1 and ev["quant"]["dir_hit"] == 1 and ev["quant"]["open_outside"] == 1
    assert ev["llm"]["in_range"] == 0 and ev["llm"]["dir_hit"] == 0
    assert ev["quant"]["brier"] == pytest.approx(0.09)
    assert evaluate.evaluate_pending(conn) == 0   # 不重複對帳


def test_llm_disabled_without_key(conn, monkeypatch):
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "")
    assert not llm.available()
    assert llm.ask_json(conn, "x", "s", "u") is None
    assert "未設定" in llm.disabled_reason()


def test_llm_stops_on_insufficient_balance(conn, monkeypatch):
    class R:
        status_code = 402
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(llm, "_disabled_reason", None)
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: R())
    assert llm.ask_json(conn, "x", "s", "u") is None
    assert not llm.available() and "餘額不足" in llm.disabled_reason()
