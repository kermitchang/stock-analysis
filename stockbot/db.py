"""SQLite 資料庫：所有資料、預測、對帳、歸因、規則都存在這裡，是系統的「記憶」。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    stock_id TEXT PRIMARY KEY,
    name TEXT,
    market TEXT,                -- TWSE / TPEX / INDEX
    industry_code TEXT,
    industry_name TEXT,
    theme TEXT,                 -- 七大主題之一，或 '無'
    theme_source TEXT,          -- llm / industry / manual
    theme_note TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    stock_id TEXT, date TEXT,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (stock_id, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

CREATE TABLE IF NOT EXISTS chips (
    stock_id TEXT, date TEXT,
    foreign_net REAL, trust_net REAL, dealer_net REAL, total_net REAL,
    PRIMARY KEY (stock_id, date)
);

CREATE TABLE IF NOT EXISTS revenue (
    stock_id TEXT, ym TEXT,     -- 西元 YYYY-MM
    revenue REAL, mom REAL, yoy REAL, cum_yoy REAL,
    fetched_at TEXT,
    PRIMARY KEY (stock_id, ym)
);

CREATE TABLE IF NOT EXISTS financials (
    stock_id TEXT, year INTEGER, quarter INTEGER,
    revenue REAL, gross_profit REAL, op_income REAL, net_income REAL, eps REAL,
    fetched_at TEXT,
    PRIMARY KEY (stock_id, year, quarter)
);

CREATE TABLE IF NOT EXISTS news (
    news_id TEXT PRIMARY KEY,   -- source:id
    published_at TEXT,          -- ISO 台北時間
    title TEXT, summary TEXT, source TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS news_stock (
    news_id TEXT, stock_id TEXT,
    sentiment REAL,             -- -2 ~ +2（LLM 標註）
    category TEXT,              -- 營收 / 財報 / 訂單 / ...
    importance REAL,            -- 0 ~ 1
    labeled INTEGER DEFAULT 0,
    PRIMARY KEY (news_id, stock_id)
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id TEXT, date TEXT,   -- 以哪一天收盤為基準
    variant TEXT,               -- baseline / quant / quant_am / llm
    base_close REAL, low REAL, high REAL,
    prob_up REAL, bias TEXT,    -- up / down / flat
    rationale TEXT,
    features_json TEXT,
    created_at TEXT,
    UNIQUE (stock_id, date, variant)
);

CREATE TABLE IF NOT EXISTS evaluations (
    prediction_id INTEGER PRIMARY KEY,
    target_date TEXT,
    actual_open REAL, actual_close REAL, actual_ret REAL,
    in_range INTEGER, dir_hit INTEGER, open_outside INTEGER, brier REAL,
    evaluated_at TEXT
);

CREATE TABLE IF NOT EXISTS attributions (
    stock_id TEXT, date TEXT,
    ret REAL, market_part REAL, industry_part REAL, idio_part REAL,
    primary_driver TEXT, explanation TEXT, evidence_json TEXT, source TEXT,
    PRIMARY KEY (stock_id, date)
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT,
    condition_json TEXT,
    direction TEXT,             -- up / down
    status TEXT,                -- candidate / active / retired / rejected
    source TEXT,                -- seed / llm / manual
    n INTEGER, hit_rate REAL, base_rate REAL, lift REAL, p_value REAL,
    recent_n INTEGER, recent_lift REAL,
    created_at TEXT, updated_at TEXT, note TEXT
);

CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT, kind TEXT, metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS llm_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, purpose TEXT, model TEXT,
    prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL, ok INTEGER
);

CREATE TABLE IF NOT EXISTS watchlist (
    date TEXT, stock_id TEXT,
    source TEXT,                -- core / dynamic
    reason TEXT,
    PRIMARY KEY (date, stock_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, run_date TEXT, started_at TEXT, finished_at TEXT,
    status TEXT, message TEXT
);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # 減少 SD 卡寫入、允許網頁同時讀
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Optional[Path] = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert(conn: sqlite3.Connection, table: str, rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = (f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
           f"VALUES ({','.join('?' * len(cols))})")
    conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
    return len(rows)


def query_df(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def get_meta_json(value: Optional[str], default=None):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
