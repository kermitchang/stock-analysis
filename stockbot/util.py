from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))


def now_tpe() -> datetime:
    return datetime.now(TPE)


def today_tpe() -> str:
    return now_tpe().strftime("%Y-%m-%d")


def roc_to_iso(s: str) -> str:
    """民國日期 '1151110' 或 '115/11/10' → '2026-11-10'。"""
    s = str(s).strip()
    if "/" in s:
        y, m, d = s.split("/")
    else:
        y, m, d = s[:-4], s[-4:-2], s[-2:]
    return date(int(y) + 1911, int(m), int(d)).isoformat()


def roc_ym_to_iso(s: str) -> str:
    """'11509' → '2026-09'。"""
    s = str(s).strip()
    return f"{int(s[:-2]) + 1911:04d}-{int(s[-2:]):02d}"


def to_float(x):
    """'1,234.5' / '+0.42' / '--' / '' → float 或 None。"""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return None if (isinstance(x, float) and math.isnan(x)) else float(x)
    s = str(x).replace(",", "").replace("+", "").replace("−", "-").strip()
    if s in ("", "-", "--", "---", "X", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def is_common_stock(code: str) -> bool:
    return bool(re.fullmatch(r"\d{4}", str(code).strip()))


def fmt_pct(x, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x * 100:+.{digits}f}%"
