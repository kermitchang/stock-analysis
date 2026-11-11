"""特徵工程：把行情、籌碼、營收、財報、新聞、國際市場整理成每檔股票每天一列。

重要原則：每一列只能用「當天收盤時已知」的資訊（避免偷看未來）。
- 美股（費半）的 date 是美國交易日；台股 d 日收盤時，已知的是 d 之前最近一個美股收盤。
- 月營收在次月 10 日前公布，這裡保守假設次月 10 日才可用。
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import numpy as np
import pandas as pd

from . import config, db

# 規則引擎與 LLM 可以引用的特徵（名稱 → 說明）
FEATURE_DOCS = {
    "ret_1": "當日報酬率", "ret_5": "近5日報酬率", "ret_20": "近20日報酬率",
    "gap_open": "當日開盤跳空幅度（開盤/前收-1）",
    "ma5_gap": "收盤相對5日均線乖離", "ma20_gap": "收盤相對20日均線乖離", "ma60_gap": "收盤相對60日均線乖離",
    "ma_bull": "均線多頭排列(5>20>60)=1，空頭排列=-1，其他=0",
    "kd_k": "KD 的 K 值(0~100)", "kd_d": "KD 的 D 值", "kd_diff": "K-D（正=K在D上）",
    "kd_cross": "今日黃金交叉=1、死亡交叉=-1、無=0",
    "rsi14": "RSI(14)", "atr_pct": "ATR(14)/收盤，日波動度",
    "vol_ratio": "當日成交量/20日均量", "range_pos20": "收盤在20日高低區間的位置(0~1)",
    "high20_gap": "收盤相對20日最高價", "limit_up": "當日漲停(>=9.5%)=1", "limit_down": "當日跌停=1",
    "chip_total_1": "當日三大法人買賣超/成交量", "chip_foreign_5": "近5日外資買賣超/成交量",
    "chip_trust_5": "近5日投信買賣超/成交量", "chip_streak": "三大法人連續買超(+)或賣超(-)天數",
    "rev_yoy": "最新已公布月營收年增率", "rev_mom": "最新已公布月營收月增率",
    "gross_margin": "最新季毛利率", "op_margin": "最新季營益率",
    "news_n_3": "近3日新聞則數", "news_sent_3": "近3日新聞情緒加權和",
    "mkt_ret_1": "加權指數當日報酬", "mkt_ret_5": "加權指數近5日報酬", "mkt_ma20_gap": "加權指數相對20日均線",
    "sox_ret_1": "前一晚費半報酬", "sox_ret_5": "費半近5日報酬",
    "ind_ret_1": "同主題股票當日平均報酬", "rel_ret_5": "近5日報酬減同主題平均",
    "beta_mkt": "對加權指數的 60 日 beta", "beta_sox": "對前一晚費半的 60 日敏感度",
    "price": "收盤價",
}
MODEL_FEATURES = [f for f in FEATURE_DOCS if f not in ("price",)]


def _kd(df: pd.DataFrame, n: int = 9) -> tuple[pd.Series, pd.Series]:
    low_n = df["low"].rolling(n, min_periods=n).min()
    high_n = df["high"].rolling(n, min_periods=n).max()
    rsv = ((df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100).fillna(50)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    return k, d


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def _streak(s: pd.Series) -> pd.Series:
    out, cur = [], 0
    for v in s.fillna(0):
        if v > 0:
            cur = cur + 1 if cur > 0 else 1
        elif v < 0:
            cur = cur - 1 if cur < 0 else -1
        else:
            cur = 0
        out.append(cur)
    return pd.Series(out, index=s.index)


def _per_stock(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("date").copy()
    c = g["close"]
    prev = c.shift(1)
    g["ret_1"] = c / prev - 1
    g["ret_5"] = c / c.shift(5) - 1
    g["ret_20"] = c / c.shift(20) - 1
    g["gap_open"] = g["open"] / prev - 1
    ma5, ma20, ma60 = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(60).mean()
    g["ma5_gap"], g["ma20_gap"], g["ma60_gap"] = c / ma5 - 1, c / ma20 - 1, c / ma60 - 1
    g["ma_bull"] = np.where((ma5 > ma20) & (ma20 > ma60), 1, np.where((ma5 < ma20) & (ma20 < ma60), -1, 0))
    g.loc[ma60.isna(), "ma_bull"] = np.nan
    k, d = _kd(g)
    g["kd_k"], g["kd_d"], g["kd_diff"] = k, d, k - d
    diff_prev = (k - d).shift(1)
    g["kd_cross"] = np.where((k > d) & (diff_prev <= 0), 1, np.where((k < d) & (diff_prev >= 0), -1, 0))
    g["rsi14"] = _rsi(c)
    tr = pd.concat([g["high"] - g["low"], (g["high"] - prev).abs(), (g["low"] - prev).abs()], axis=1).max(axis=1)
    g["atr_pct"] = tr.rolling(14).mean() / c
    vol = g["volume"].replace(0, np.nan)
    g["vol_ratio"] = vol / vol.rolling(20, min_periods=10).mean()
    hi20, lo20 = g["high"].rolling(20).max(), g["low"].rolling(20).min()
    g["range_pos20"] = (c - lo20) / (hi20 - lo20).replace(0, np.nan)
    g["high20_gap"] = c / hi20 - 1
    g["limit_up"] = (g["ret_1"] >= 0.095).astype(float)
    g["limit_down"] = (g["ret_1"] <= -0.095).astype(float)
    g["avg_value_20"] = (c * g["volume"]).rolling(20, min_periods=10).mean()

    # 籌碼（沒有資料時為 NaN；模型可處理）
    v = g["volume"].replace(0, np.nan)
    g["chip_total_1"] = g["total_net"] / v
    g["chip_foreign_5"] = g["foreign_net"].rolling(5, min_periods=3).sum() / v.rolling(5, min_periods=3).sum()
    g["chip_trust_5"] = g["trust_net"].rolling(5, min_periods=3).sum() / v.rolling(5, min_periods=3).sum()
    g["chip_streak"] = _streak(g["total_net"])
    g.loc[g["total_net"].isna(), "chip_streak"] = np.nan

    # 目標：隔日報酬（訓練與對帳用，預測時這欄是 NaN）
    g["next_ret"] = c.shift(-1) / c - 1
    g["next_open_gap"] = g["open"].shift(-1) / c - 1
    g["next_date"] = g["date"].shift(-1)
    return g


def _rolling_beta(y: pd.Series, x: pd.Series, n: int = 60) -> pd.Series:
    cov = y.rolling(n, min_periods=40).cov(x)
    var = x.rolling(n, min_periods=40).var()
    return cov / var.replace(0, np.nan)


def build_features(conn: sqlite3.Connection, start: Optional[str] = None,
                   stock_ids: Optional[list] = None) -> pd.DataFrame:
    """回傳所有股票的特徵 DataFrame（index 無意義；欄位 stock_id, date, 特徵..., next_ret）。"""
    where, params = ["s.market!='INDEX'"], []
    if start:
        where.append("p.date >= ?")
        params.append(start)
    if stock_ids:
        where.append(f"p.stock_id IN ({','.join('?' * len(stock_ids))})")
        params += list(stock_ids)
    px = db.query_df(conn,
        "SELECT p.*, s.theme, c.foreign_net, c.trust_net, c.dealer_net, c.total_net "
        "FROM prices p JOIN stocks s ON s.stock_id=p.stock_id "
        "LEFT JOIN chips c ON c.stock_id=p.stock_id AND c.date=p.date "
        f"WHERE {' AND '.join(where)} ORDER BY p.stock_id, p.date", tuple(params))
    if px.empty:
        return px
    px["price"] = px["close"]
    feats = pd.concat([_per_stock(g) for _, g in px.groupby("stock_id", sort=False)], ignore_index=True)

    # ── 大盤與國際 ──
    idx = db.query_df(conn, "SELECT stock_id, date, close FROM prices WHERE stock_id IN ('^TWII','^SOX')")
    tw = idx[idx.stock_id == "^TWII"].set_index("date")["close"].sort_index()
    if not tw.empty:
        mk = pd.DataFrame({"mkt_ret_1": tw.pct_change(), "mkt_ret_5": tw.pct_change(5),
                           "mkt_ma20_gap": tw / tw.rolling(20).mean() - 1})
        feats = feats.merge(mk, left_on="date", right_index=True, how="left")
    else:
        feats["mkt_ret_1"] = feats["mkt_ret_5"] = feats["mkt_ma20_gap"] = np.nan
    sox = idx[idx.stock_id == "^SOX"].set_index("date")["close"].sort_index()
    feats = feats.sort_values("date")
    if not sox.empty:
        sx = pd.DataFrame({"us_date": sox.index, "sox_ret_1": sox.pct_change().values,
                           "sox_ret_5": sox.pct_change(5).values})
        sx["us_date"] = pd.to_datetime(sx["us_date"])
        feats["_d"] = pd.to_datetime(feats["date"])
        # 台股 d 日已知：美國交易日 < d 的最近一筆
        feats = pd.merge_asof(feats.sort_values("_d"), sx.sort_values("us_date"), left_on="_d",
                              right_on="us_date", allow_exact_matches=False, direction="backward")
        feats = feats.drop(columns=["us_date"])
        # sox_on：d 日台股收盤「之後」的那一晚費半報酬（隔天早上才知道）。
        # 不能當收盤特徵，只用於校準早盤修正（quant_am）。
        on = sx[["us_date", "sox_ret_1"]].rename(columns={"sox_ret_1": "sox_on"})
        feats = pd.merge_asof(feats.sort_values("_d"), on.sort_values("us_date"), left_on="_d",
                              right_on="us_date", allow_exact_matches=True, direction="forward",
                              tolerance=pd.Timedelta(days=4))
        feats = feats.drop(columns=["_d", "us_date"])
    else:
        feats["sox_ret_1"] = feats["sox_ret_5"] = feats["sox_on"] = np.nan

    # ── 同主題平均（產業連動）──
    grp = feats.groupby(["theme", "date"])
    feats["ind_ret_1"] = grp["ret_1"].transform("mean")
    feats["rel_ret_5"] = feats["ret_5"] - grp["ret_5"].transform("mean")

    # ── beta ──
    feats = feats.sort_values(["stock_id", "date"]).reset_index(drop=True)
    betas = []
    for _, g in feats.groupby("stock_id", sort=False):
        betas.append(pd.DataFrame({"beta_mkt": _rolling_beta(g["ret_1"], g["mkt_ret_1"]),
                                   "beta_sox": _rolling_beta(g["ret_1"], g["sox_ret_1"])}, index=g.index))
    feats = feats.join(pd.concat(betas))

    feats = _add_fundamentals(conn, feats)
    feats = _add_news(conn, feats)
    return feats.reset_index(drop=True)


def _add_fundamentals(conn: sqlite3.Connection, feats: pd.DataFrame) -> pd.DataFrame:
    rev = db.query_df(conn, "SELECT stock_id, ym, yoy AS rev_yoy, mom AS rev_mom FROM revenue")
    if not rev.empty:
        # 次月 10 日起才算已知
        rev["avail"] = pd.to_datetime(rev["ym"] + "-01") + pd.offsets.MonthBegin(1) + pd.Timedelta(days=9)
        feats["_d"] = pd.to_datetime(feats["date"])
        feats = pd.merge_asof(feats.sort_values("_d"), rev.sort_values("avail")[["stock_id", "avail", "rev_yoy", "rev_mom"]],
                              left_on="_d", right_on="avail", by="stock_id", direction="backward")
        feats = feats.drop(columns=["_d", "avail"])
    else:
        feats["rev_yoy"] = feats["rev_mom"] = np.nan

    fin = db.query_df(conn, "SELECT stock_id, year, quarter, revenue, gross_profit, op_income, fetched_at FROM financials")
    if not fin.empty:
        fin["gross_margin"] = fin["gross_profit"] / fin["revenue"]
        fin["op_margin"] = fin["op_income"] / fin["revenue"]
        # 法定公告期限：Q1 5/15、Q2 8/14、Q3 11/14、Q4 次年 3/31
        deadline = {1: "-05-15", 2: "-08-14", 3: "-11-14", 4: "-03-31"}
        fin["avail"] = pd.to_datetime([
            f"{y + (1 if q == 4 else 0)}{deadline[q]}" for y, q in zip(fin["year"], fin["quarter"])])
        fin["avail"] = fin[["avail"]].assign(f=pd.to_datetime(fin["fetched_at"].str[:10])).min(axis=1)
        feats["_d"] = pd.to_datetime(feats["date"])
        feats = pd.merge_asof(feats.sort_values("_d"), fin.sort_values("avail")[["stock_id", "avail", "gross_margin", "op_margin"]],
                              left_on="_d", right_on="avail", by="stock_id", direction="backward")
        feats = feats.drop(columns=["_d", "avail"])
    else:
        feats["gross_margin"] = feats["op_margin"] = np.nan
    return feats


def _add_news(conn: sqlite3.Connection, feats: pd.DataFrame) -> pd.DataFrame:
    news = db.query_df(conn,
        "SELECT ns.stock_id, substr(n.published_at,1,10) AS d, n.published_at, "
        "COALESCE(ns.sentiment,0)*COALESCE(ns.importance,0.5) AS w FROM news_stock ns JOIN news n USING(news_id)")
    feats["news_n_3"] = np.nan
    feats["news_sent_3"] = np.nan
    if news.empty:
        return feats
    # 收盤後（13:30 以後）的新聞算到下一個交易日
    t = pd.to_datetime(news["published_at"].str[:16])
    news["d"] = np.where(t.dt.hour * 60 + t.dt.minute > 13 * 60 + 30,
                         (t + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d"), news["d"])
    daily = news.groupby(["stock_id", "d"]).agg(n=("w", "size"), s=("w", "sum")).reset_index()
    first_news = daily["d"].min()
    out = []
    for sid, g in feats.groupby("stock_id"):
        dn = daily[daily.stock_id == sid].set_index("d")
        if dn.empty:
            g = g.copy()
            g.loc[g["date"] >= first_news, ["news_n_3", "news_sent_3"]] = 0.0
            out.append(g)
            continue
        dates = pd.to_datetime(g["date"])
        idx = pd.date_range(min(dates.min(), pd.to_datetime(dn.index.min())), dates.max())
        cal = dn.reindex(idx.strftime("%Y-%m-%d")).fillna(0)
        roll = cal.rolling(3, min_periods=1).sum()
        g = g.copy()
        g["news_n_3"] = roll["n"].reindex(g["date"]).values
        g["news_sent_3"] = roll["s"].reindex(g["date"]).values
        g.loc[g["date"] < first_news, ["news_n_3", "news_sent_3"]] = np.nan
        out.append(g)
    return pd.concat(out)


def candidate_pool(feats: pd.DataFrame, date: str) -> pd.DataFrame:
    """動態挑選的候選池：七大主題、股價 < 上限、流動性足夠（不含固定名單）。"""
    core = {sid for ids in config.CORE_WATCHLIST.values() for sid in ids}
    day = feats[(feats["date"] == date) & (feats["theme"].isin(config.THEMES)) & ~feats["stock_id"].isin(core)]
    return day[(day["close"] < config.MAX_PRICE) & (day["avg_value_20"] >= config.MIN_AVG_TRADE_VALUE)]


def core_today(feats: pd.DataFrame, date: str) -> pd.DataFrame:
    core = {sid for ids in config.CORE_WATCHLIST.values() for sid in ids}
    day = feats[(feats["date"] == date) & feats["stock_id"].isin(core)]
    if config.CORE_APPLY_PRICE_LIMIT:
        day = day[day["close"] < config.MAX_PRICE]
    return day


def select_universe(conn: sqlite3.Connection, feats: pd.DataFrame, date: str,
                    probs: dict) -> pd.DataFrame:
    """今日分析名單 = 固定名單 + 動態挑選（合計最多 MAX_UNIVERSE 檔）。

    動態挑選分數：量能異常、新聞熱度、近期動能、模型把握度；
    並讓前幾天挑進來的股票至少留 DYNAMIC_MIN_HOLD_DAYS 個交易日，才能累積對它的經驗。
    """
    core = core_today(feats, date)
    pool = candidate_pool(feats, date).copy()
    n_dyn = max(0, min(config.DYNAMIC_PICKS, config.MAX_UNIVERSE - len(core)))

    pool["conf"] = (pool["stock_id"].map(probs).fillna(0.5) - 0.5).abs() * 2
    pool["score"] = (
        (pool["vol_ratio"].fillna(1).clip(0, 5) - 1).clip(lower=0) * 0.25     # 爆量
        + pool["news_n_3"].fillna(0).clip(0, 5) * 0.08                          # 新聞熱度
        + pool["ret_5"].abs().fillna(0).clip(0, 0.3) * 2                        # 近期動能（漲跌都算）
        + pool["conf"] * 1.0                                                    # 模型把握度
    )

    recent_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM watchlist WHERE date < ? ORDER BY date DESC LIMIT ?",
        (date, config.DYNAMIC_MIN_HOLD_DAYS - 1))]
    held = set()
    if recent_dates:
        q = f"SELECT DISTINCT stock_id FROM watchlist WHERE source='dynamic' AND date IN ({','.join('?' * len(recent_dates))})"
        held = {r[0] for r in conn.execute(q, recent_dates)}

    pool = pool.sort_values("score", ascending=False)
    keep = pool[pool["stock_id"].isin(held)].head(n_dyn)
    fresh = pool[~pool["stock_id"].isin(keep["stock_id"])].head(n_dyn - len(keep))
    dyn = pd.concat([keep, fresh])

    def reason(r):
        parts = []
        if r["vol_ratio"] > 2:
            parts.append(f"量比{r['vol_ratio']:.1f}")
        if r["news_n_3"] and r["news_n_3"] >= 2:
            parts.append(f"新聞{int(r['news_n_3'])}則")
        if abs(r["ret_5"]) > 0.08:
            parts.append(f"5日{r['ret_5']:+.0%}")
        if r["conf"] > 0.2:
            parts.append("模型把握度高")
        if r["stock_id"] in held:
            parts.append("續留觀察")
        return "、".join(parts) or "綜合分數"

    rows = [{"date": date, "stock_id": sid, "source": "core", "reason": "固定名單"} for sid in core["stock_id"]]
    rows += [{"date": date, "stock_id": r["stock_id"], "source": "dynamic", "reason": reason(r)} for _, r in dyn.iterrows()]
    conn.execute("DELETE FROM watchlist WHERE date=?", (date,))
    db.upsert(conn, "watchlist", rows)
    conn.commit()
    out = pd.concat([core.assign(source="core"), dyn.drop(columns=["conf", "score"]).assign(source="dynamic")])
    return out.reset_index(drop=True)
