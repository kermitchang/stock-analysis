"""資料收集：全部使用免費公開來源。

- 公司清單 / 產業別、月營收、季損益表：證交所 & 櫃買中心 OpenAPI
- 每日收盤行情：證交所 STOCK_DAY_ALL、櫃買 mainboard_daily_close_quotes
- 三大法人：證交所 T86、櫃買 dailyTrade
- 歷史行情與國際指數：yfinance
- 新聞：鉅亨網 API（附股票代號）、Google News RSS
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Iterable, Optional

import pandas as pd
import requests

from . import config, db, llm
from .util import TPE, is_common_stock, now_tpe, roc_to_iso, roc_ym_to_iso, to_float

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
})
_last_twse_call = 0.0


def _get_json(url: str, params: dict = None, throttle: float = 0.0, timeout: int = 40):
    """GET JSON；throttle 秒數用來避免證交所 www 站封鎖（建議 >= 2.5 秒）。"""
    global _last_twse_call
    if throttle:
        wait = throttle - (time.time() - _last_twse_call)
        if wait > 0:
            time.sleep(wait)
    for attempt in range(3):
        try:
            resp = _session.get(url, params=params, timeout=timeout)
            _last_twse_call = time.time()
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("抓取失敗 %s（第 %d 次）：%s", url, attempt + 1, e)
            time.sleep(3 * (attempt + 1))
    return None


def yf_symbol(stock_id: str, market: str) -> str:
    if market == "INDEX":
        return stock_id
    return f"{stock_id}.TWO" if market == "TPEX" else f"{stock_id}.TW"


# ════════════════════════════════════════
#  公司清單與主題歸類
# ════════════════════════════════════════

def refresh_universe(conn: sqlite3.Connection, use_llm: bool = True) -> int:
    """更新候選池公司清單；新公司用 LLM 歸類到七大主題。"""
    rows = []
    twse = _get_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L") or []
    for r in twse:
        rows.append(("TWSE", r.get("公司代號", ""), r.get("公司簡稱", ""), r.get("產業別", "")))
    tpex = _get_json("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O") or []
    for r in tpex:
        rows.append(("TPEX", r.get("SecuritiesCompanyCode", ""), r.get("CompanyAbbreviation", ""),
                     r.get("SecuritiesIndustryCode", "")))

    core = {sid: theme for theme, ids in config.CORE_WATCHLIST.items() for sid in ids}
    existing = {r["stock_id"]: dict(r) for r in conn.execute("SELECT * FROM stocks")}
    now = now_tpe().isoformat(timespec="seconds")
    upserts = []
    for market, code, name, ind in rows:
        code, ind = str(code).strip(), str(ind).strip().zfill(2)
        if not is_common_stock(code) or (ind not in config.CANDIDATE_INDUSTRY_CODES and code not in core):
            continue
        old = existing.get(code, {})
        upserts.append({
            "stock_id": code, "name": str(name).strip(), "market": market,
            "industry_code": ind, "industry_name": config.CANDIDATE_INDUSTRY_CODES.get(ind, ind),
            "theme": old.get("theme"), "theme_source": old.get("theme_source"),
            "theme_note": old.get("theme_note"), "updated_at": now,
        })
    db.upsert(conn, "stocks", upserts)
    conn.commit()
    log.info("候選池公司：%d 檔", len(upserts))
    # 固定名單的主題以設定檔為準
    for sid, theme in core.items():
        conn.execute("UPDATE stocks SET theme=?, theme_source='manual' WHERE stock_id=?", (theme, sid))
    conn.commit()
    missing = set(core) - {u["stock_id"] for u in upserts}
    if missing:
        log.warning("固定名單中找不到（可能已下市或改代號）：%s", sorted(missing))
    tag_themes(conn, use_llm=use_llm)
    return len(upserts)


def tag_themes(conn: sqlite3.Connection, use_llm: bool = True, batch: int = 60) -> None:
    pending = [dict(r) for r in conn.execute(
        "SELECT stock_id,name,industry_code,industry_name FROM stocks "
        "WHERE market!='INDEX' AND (theme IS NULL OR theme_source='industry')")]
    if not pending:
        return
    if use_llm and llm.available():
        system = (
            "你是熟悉台灣上市櫃公司的產業研究員。把每家公司歸類到下列主題之一："
            + "、".join(config.THEMES)
            + "；若主要業務與這些主題都無關，填「無」。IC設計（無晶圓廠的晶片設計公司）與"
            "半導體（晶圓代工、記憶體、封測、設備材料、功率元件）要分開。"
            "AI相關指 AI 伺服器、AI 網通、AI 邊緣運算等；系統廠指 PC/NB/主機板/工業電腦等組裝與品牌。"
            "散熱相關指風扇、散熱模組、均熱片、水冷；衛星相關含低軌衛星與通訊網路設備；"
            "能源相關含太陽能、儲能、電網、重電、電池。"
            '輸出格式：{"items":[{"id":"代號","theme":"主題","note":"10字內主要業務"}]}'
        )
        for i in range(0, len(pending), batch):
            chunk = pending[i:i + batch]
            lines = "\n".join(f"{p['stock_id']} {p['name']}（{p['industry_name']}）" for p in chunk)
            try:
                res = llm.ask_json(conn, "theme_tagging", system, lines, max_tokens=4000)
            except llm.BudgetExceeded:
                break
            for item in (res or {}).get("items", []):
                theme = item.get("theme")
                if theme not in config.THEMES + ["無"]:
                    continue
                conn.execute("UPDATE stocks SET theme=?, theme_source='llm', theme_note=? WHERE stock_id=?",
                             (theme, str(item.get("note", ""))[:40], str(item.get("id"))))
            conn.commit()
    # 沒有 LLM（或 LLM 沒回答到）的，用產業別粗分
    for p in conn.execute("SELECT stock_id, industry_code FROM stocks WHERE theme IS NULL AND market!='INDEX'").fetchall():
        theme = config.INDUSTRY_FALLBACK_THEME.get(p["industry_code"])
        if theme:
            conn.execute("UPDATE stocks SET theme=?, theme_source='industry' WHERE stock_id=?",
                         (theme, p["stock_id"]))
    conn.commit()


# ════════════════════════════════════════
#  行情
# ════════════════════════════════════════

def fetch_daily_quotes(conn: sqlite3.Connection) -> Optional[str]:
    """抓今天（最近一個交易日）全市場收盤行情。回傳交易日 ISO 日期。"""
    known = {r[0] for r in conn.execute("SELECT stock_id FROM stocks WHERE market!='INDEX'")}
    rows, dates = [], set()
    for r in _get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL") or []:
        code = str(r.get("Code", "")).strip()
        if code not in known:
            continue
        close = to_float(r.get("ClosingPrice"))
        if close is None:
            continue
        d = roc_to_iso(r["Date"])
        dates.add(d)
        rows.append({"stock_id": code, "date": d, "open": to_float(r.get("OpeningPrice")),
                     "high": to_float(r.get("HighestPrice")), "low": to_float(r.get("LowestPrice")),
                     "close": close, "volume": to_float(r.get("TradeVolume"))})
    for r in _get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes") or []:
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if code not in known:
            continue
        close = to_float(r.get("Close"))
        if close is None:
            continue
        d = roc_to_iso(r["Date"])
        dates.add(d)
        rows.append({"stock_id": code, "date": d, "open": to_float(r.get("Open")),
                     "high": to_float(r.get("High")), "low": to_float(r.get("Low")),
                     "close": close, "volume": to_float(r.get("TradingShares"))})
    if not rows:
        return None
    # 加權指數（官方收盤；yfinance 常延遲一天）
    for r in _get_json("https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX") or []:
        if r.get("指數") == "發行量加權股價指數" and to_float(r.get("收盤指數")):
            rows.append({"stock_id": "^TWII", "date": roc_to_iso(r["日期"]), "open": None, "high": None,
                         "low": None, "close": to_float(r["收盤指數"]), "volume": None})
            break
    db.upsert(conn, "prices", rows)
    conn.commit()
    trade_date = max(dates)
    log.info("收盤行情 %s：%d 檔", trade_date, len(rows))
    return trade_date


def _yf_download(symbols: list, start: str) -> pd.DataFrame:
    import yfinance as yf
    return yf.download(symbols, start=start, auto_adjust=False, threads=False,
                       group_by="ticker", progress=False)


def fetch_price_history(conn: sqlite3.Connection, stocks: Iterable[tuple], start: str,
                        chunk: int = 40) -> int:
    """用 yfinance 補歷史行情。stocks: [(stock_id, market), ...]"""
    stocks = list(stocks)
    total = 0
    for i in range(0, len(stocks), chunk):
        part = stocks[i:i + chunk]
        sym2id = {yf_symbol(s, m): s for s, m in part}
        try:
            df = _yf_download(list(sym2id), start)
        except Exception as e:  # yfinance 偶爾整批失敗
            log.warning("yfinance 失敗：%s", e)
            continue
        rows = []
        for sym, sid in sym2id.items():
            if isinstance(df.columns, pd.MultiIndex):
                if sym not in df.columns.get_level_values(0):
                    continue
                sub = df[sym]
            else:
                sub = df
            sub = sub.dropna(subset=["Close"])
            for idx, r in sub.iterrows():
                rows.append({"stock_id": sid, "date": idx.strftime("%Y-%m-%d"),
                             "open": float(r["Open"]), "high": float(r["High"]),
                             "low": float(r["Low"]), "close": float(r["Close"]),
                             "volume": float(r["Volume"]) if pd.notna(r["Volume"]) else None})
        # 官方資料優先：不覆蓋已存在的日期
        conn.executemany(
            "INSERT OR IGNORE INTO prices (stock_id,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
            [(r["stock_id"], r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows])
        conn.commit()
        total += len(rows)
        log.info("歷史行情 %d/%d 檔，累計 %d 筆", min(i + chunk, len(stocks)), len(stocks), total)
    return total


def fetch_market(conn: sqlite3.Connection, start: str) -> None:
    """國際與大盤指標（yfinance）。指數的 date 是「該市場自己的交易日」。"""
    db.upsert(conn, "stocks", [{"stock_id": t, "name": n, "market": "INDEX", "industry_code": None,
                                "industry_name": None, "theme": None, "theme_source": None,
                                "theme_note": None, "updated_at": now_tpe().isoformat(timespec="seconds")}
                               for t, n in config.MARKET_TICKERS.items()])
    df = _yf_download(list(config.MARKET_TICKERS), start)
    rows = []
    for sym in config.MARKET_TICKERS:
        if sym not in df.columns.get_level_values(0):
            continue
        for idx, r in df[sym].dropna(subset=["Close"]).iterrows():
            rows.append({"stock_id": sym, "date": idx.strftime("%Y-%m-%d"), "open": float(r["Open"]),
                         "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"]),
                         "volume": float(r["Volume"]) if pd.notna(r["Volume"]) else None})
    # yfinance 不覆蓋官方已寫入的同日資料（官方只有收盤價時，補上開高低）
    conn.executemany(
        "INSERT INTO prices (stock_id,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(stock_id,date) DO UPDATE SET open=COALESCE(prices.open,excluded.open), "
        "high=COALESCE(prices.high,excluded.high), low=COALESCE(prices.low,excluded.low), "
        "volume=COALESCE(prices.volume,excluded.volume)",
        [(r["stock_id"], r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows])
    conn.commit()


# ════════════════════════════════════════
#  三大法人
# ════════════════════════════════════════

def fetch_chips(conn: sqlite3.Connection, trade_date: str) -> int:
    ymd = trade_date.replace("-", "")
    known = {r[0] for r in conn.execute("SELECT stock_id FROM stocks WHERE market!='INDEX'")}
    rows = []
    d = _get_json("https://www.twse.com.tw/rwd/zh/fund/T86",
                  params={"date": ymd, "selectType": "ALLBUT0999", "response": "json"}, throttle=3)
    if d and d.get("stat") == "OK":
        for r in d.get("data", []):
            code = r[0].strip()
            if code in known:
                rows.append({"stock_id": code, "date": trade_date,
                             "foreign_net": (to_float(r[4]) or 0) + (to_float(r[7]) or 0),
                             "trust_net": to_float(r[10]), "dealer_net": to_float(r[11]),
                             "total_net": to_float(r[18])})
    y, m, dd = trade_date.split("-")
    d = _get_json("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                  params={"type": "Daily", "sect": "EW", "date": f"{y}/{m}/{dd}", "response": "json"},
                  throttle=3)
    for table in (d or {}).get("tables", []):
        for r in table.get("data", []):
            code = str(r[0]).strip()
            if code in known and len(r) >= 24:
                rows.append({"stock_id": code, "date": trade_date, "foreign_net": to_float(r[10]),
                             "trust_net": to_float(r[13]), "dealer_net": to_float(r[22]),
                             "total_net": to_float(r[23])})
    db.upsert(conn, "chips", rows)
    conn.commit()
    log.info("三大法人 %s：%d 檔", trade_date, len(rows))
    return len(rows)


# ════════════════════════════════════════
#  月營收 / 季財報
# ════════════════════════════════════════

def fetch_fundamentals(conn: sqlite3.Connection) -> None:
    known = {r[0] for r in conn.execute("SELECT stock_id FROM stocks WHERE market!='INDEX'")}
    now = now_tpe().isoformat(timespec="seconds")

    rev_rows = []
    for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
                "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"):
        for r in _get_json(url) or []:
            code = str(r.get("公司代號", "")).strip()
            if code not in known or not r.get("資料年月"):
                continue
            pct = lambda k: (to_float(r.get(k)) / 100) if to_float(r.get(k)) is not None else None
            rev_rows.append({"stock_id": code, "ym": roc_ym_to_iso(r["資料年月"]),
                             "revenue": to_float(r.get("營業收入-當月營收")),
                             "mom": pct("營業收入-上月比較增減(%)"), "yoy": pct("營業收入-去年同月增減(%)"),
                             "cum_yoy": pct("累計營業收入-前期比較增減(%)"), "fetched_at": now})
    db.upsert(conn, "revenue", rev_rows)

    fin_rows = []
    for url in ("https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci",
                "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci"):
        for r in _get_json(url) or []:
            code = str(r.get("公司代號") or r.get("SecuritiesCompanyCode") or "").strip()
            year, season = r.get("年度") or r.get("Year"), r.get("季別") or r.get("Season")
            if code not in known or not year or not season:
                continue
            eps_key = next((k for k in r if "每股盈餘" in k), None)
            fin_rows.append({
                "stock_id": code, "year": int(year) + 1911, "quarter": int(season),
                "revenue": to_float(r.get("營業收入")),
                "gross_profit": to_float(r.get("營業毛利（毛損）淨額")) or to_float(r.get("營業毛利（毛損）")),
                "op_income": to_float(r.get("營業利益（損失）")),
                "net_income": to_float(r.get("淨利（淨損）歸屬於母公司業主")) or to_float(r.get("本期淨利（淨損）")),
                "eps": to_float(r.get(eps_key)) if eps_key else None,
                "fetched_at": now})
    db.upsert(conn, "financials", fin_rows)
    conn.commit()
    log.info("月營收 %d 筆、季財報 %d 筆", len(rev_rows), len(fin_rows))


# ════════════════════════════════════════
#  新聞
# ════════════════════════════════════════

def _clean(text: str) -> str:
    text = html.unescape(html.unescape(text or ""))
    return re.sub(r"<[^>]+>", "", text).replace("\xa0", " ").strip()


def fetch_news_cnyes(conn: sqlite3.Connection, since: datetime, max_pages: int = 15) -> int:
    known = {r[0] for r in conn.execute("SELECT stock_id FROM stocks WHERE market!='INDEX'")}
    n = 0
    for page in range(1, max_pages + 1):
        d = _get_json("https://api.cnyes.com/media/api/v1/newslist/category/tw_stock",
                      params={"limit": 100, "page": page})
        items = ((d or {}).get("items") or {}).get("data") or []
        if not items:
            break
        oldest = None
        for it in items:
            ts = datetime.fromtimestamp(int(it.get("publishAt", 0)), TPE)
            oldest = ts if oldest is None else min(oldest, ts)
            codes = [c for c in (it.get("stock") or []) if c in known]
            if ts < since or not codes:
                continue
            nid = f"cnyes:{it['newsId']}"
            conn.execute("INSERT OR IGNORE INTO news (news_id,published_at,title,summary,source,url) "
                         "VALUES (?,?,?,?,?,?)",
                         (nid, ts.isoformat(timespec="minutes"), _clean(it.get("title")),
                          _clean(it.get("summary") or it.get("content"))[:400], "鉅亨網",
                          f"https://news.cnyes.com/news/id/{it['newsId']}"))
            for c in codes:
                conn.execute("INSERT OR IGNORE INTO news_stock (news_id, stock_id) VALUES (?,?)", (nid, c))
            n += 1
        conn.commit()
        if oldest and oldest < since:
            break
    log.info("鉅亨網新聞：%d 則", n)
    return n


def fetch_news_google(conn: sqlite3.Connection, stock_id: str, name: str, since: datetime) -> int:
    import xml.etree.ElementTree as ET
    try:
        resp = _session.get("https://news.google.com/rss/search", timeout=20, params={
            "q": f"{name} {stock_id}", "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"})
        root = ET.fromstring(resp.content)
    except Exception as e:
        log.warning("Google News 失敗 %s：%s", stock_id, e)
        return 0
    n = 0
    for item in root.findall(".//item")[:15]:
        title = _clean(item.findtext("title", ""))
        if name not in title and stock_id not in title:
            continue
        try:
            ts = email.utils.parsedate_to_datetime(item.findtext("pubDate", "")).astimezone(TPE)
        except (TypeError, ValueError):
            continue
        if ts < since:
            continue
        nid = "gnews:" + hashlib.md5(title.encode("utf-8")).hexdigest()[:16]
        conn.execute("INSERT OR IGNORE INTO news (news_id,published_at,title,summary,source,url) VALUES (?,?,?,?,?,?)",
                     (nid, ts.isoformat(timespec="minutes"), title, "", item.findtext("source", "Google News"),
                      item.findtext("link", "")))
        conn.execute("INSERT OR IGNORE INTO news_stock (news_id, stock_id) VALUES (?,?)", (nid, stock_id))
        n += 1
    conn.commit()
    time.sleep(1)
    return n


def label_news(conn: sqlite3.Connection, batch: int = 40) -> int:
    """LLM 把新聞標成結構化欄位（情緒、類別、重要性），之後可當模型特徵。"""
    if not llm.available():
        return 0
    pending = conn.execute(
        "SELECT ns.news_id, ns.stock_id, s.name, n.title, n.summary FROM news_stock ns "
        "JOIN news n ON n.news_id=ns.news_id JOIN stocks s ON s.stock_id=ns.stock_id "
        "WHERE ns.labeled=0 ORDER BY n.published_at DESC LIMIT 400").fetchall()
    system = (
        "你是台股新聞分析師。針對每則新聞，判斷它對指定個股的影響。"
        "sentiment：-2(重大利空) -1(利空) 0(中性) 1(利多) 2(重大利多)；"
        "category：營收、財報、訂單客戶、產能擴產、法說展望、產業趨勢、政策法規、籌碼股權、處分資產、其他；"
        "importance：0~1，對股價可能影響的大小，例行性報導給低分。"
        '輸出：{"items":[{"k":編號,"sentiment":0,"category":"其他","importance":0.3}]}'
    )
    done = 0
    for i in range(0, len(pending), batch):
        chunk = pending[i:i + batch]
        lines = "\n".join(f"{k}. [{r['stock_id']} {r['name']}] {r['title']}｜{(r['summary'] or '')[:120]}"
                          for k, r in enumerate(chunk))
        try:
            res = llm.ask_json(conn, "news_label", system, lines, max_tokens=3000, temperature=0.1)
        except llm.BudgetExceeded:
            break
        for item in (res or {}).get("items", []):
            try:
                r = chunk[int(item["k"])]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            conn.execute("UPDATE news_stock SET sentiment=?, category=?, importance=?, labeled=1 "
                         "WHERE news_id=? AND stock_id=?",
                         (to_float(item.get("sentiment")), str(item.get("category", ""))[:10],
                          to_float(item.get("importance")), r["news_id"], r["stock_id"]))
            done += 1
        conn.commit()
    return done
