"""DeepSeek LLM 客戶端（OpenAI 相容的 chat/completions），只回傳 JSON。

安全機制：
- 沒有設定 DEEPSEEK_API_KEY → 跳過所有 LLM 步驟，只跑量化部分。
- 餘額不足（HTTP 402）或金鑰無效（HTTP 401）→ 本次執行立刻停用 LLM，不重試，
  並把原因寫進日報與 Telegram 摘要。
- 每日花費上限（STOCKBOT_DAILY_BUDGET_USD）→ 達到就停止呼叫。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

import requests

from . import config

log = logging.getLogger(__name__)

# 本次執行中被停用的原因（例如餘額不足）；None 表示正常
_disabled_reason: Optional[str] = None


class BudgetExceeded(RuntimeError):
    pass


def available() -> bool:
    return bool(config.DEEPSEEK_API_KEY) and _disabled_reason is None


def disabled_reason() -> Optional[str]:
    if not config.DEEPSEEK_API_KEY:
        return "未設定 DEEPSEEK_API_KEY"
    return _disabled_reason


def _disable(reason: str) -> None:
    global _disabled_reason
    _disabled_reason = reason
    log.error("LLM 已停用：%s", reason)


def check_balance() -> dict:
    """查詢 DeepSeek 餘額。回傳 {ok, balance, currency, low, message}。"""
    if not config.DEEPSEEK_API_KEY:
        return {"ok": False, "balance": None, "currency": None, "low": False, "message": "未設定 DEEPSEEK_API_KEY"}
    try:
        resp = requests.get(config.DEEPSEEK_BASE_URL.rstrip("/") + "/user/balance",
                            headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"}, timeout=20)
        if resp.status_code == 401:
            _disable("DeepSeek API 金鑰無效（401）")
            return {"ok": False, "balance": None, "currency": None, "low": False, "message": _disabled_reason}
        resp.raise_for_status()
        data = resp.json()
        infos = data.get("balance_infos") or [{}]
        info = next((i for i in infos if i.get("currency") == "USD"), infos[0])
        bal = float(info.get("total_balance") or 0)
        cur = info.get("currency", "")
        threshold = config.LLM_LOW_BALANCE_USD * (7.2 if cur == "CNY" else 1.0)
        if not data.get("is_available", True) or bal <= 0:
            _disable(f"DeepSeek 餘額不足（剩 {bal} {cur}），請儲值")
            return {"ok": False, "balance": bal, "currency": cur, "low": True, "message": _disabled_reason}
        return {"ok": True, "balance": bal, "currency": cur, "low": bal < threshold,
                "message": f"DeepSeek 餘額 {bal:.2f} {cur}" + ("（偏低，請儲值）" if bal < threshold else "")}
    except (requests.RequestException, ValueError) as e:
        return {"ok": True, "balance": None, "currency": None, "low": False, "message": f"無法查詢餘額：{e}"}


def spent_today(conn: sqlite3.Connection) -> float:
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) FROM llm_log WHERE ts LIKE ?", (today + "%",)
    ).fetchone()
    return float(row[0])


def _extract_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def ask_json(conn: sqlite3.Connection, purpose: str, system: str, user: str,
             max_tokens: int = 1500, temperature: float = 0.3) -> Optional[dict]:
    """呼叫 LLM 並解析 JSON；失敗回傳 None（呼叫端要能處理）。"""
    if not available():
        return None
    if spent_today(conn) >= config.LLM_DAILY_BUDGET_USD:
        raise BudgetExceeded(f"今日 LLM 花費已達上限 ${config.LLM_DAILY_BUDGET_USD}")

    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system + "\n\n一律使用繁體中文（台灣用語）。只輸出一個 JSON 物件，不要任何其他文字。"},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        # 推理型模型（如 deepseek-flash）的「思考」token 也算在 max_tokens 裡，要預留空間
        "max_tokens": max_tokens + config.LLM_REASONING_HEADROOM,
        "temperature": temperature,
    }
    headers = {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
               "Content-Type": "application/json"}
    url = config.DEEPSEEK_BASE_URL.rstrip("/") + "/chat/completions"

    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=180)
            if resp.status_code == 402:
                _disable("DeepSeek 餘額不足（402 Insufficient Balance），已停止 LLM 分析，請儲值")
                return None
            if resp.status_code == 401:
                _disable("DeepSeek API 金鑰無效（401）")
                return None
            if resp.status_code in (429, 500, 502, 503):
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage", {})
            pt, ct = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            cost = (pt * config.LLM_PRICE_INPUT_PER_M + ct * config.LLM_PRICE_OUTPUT_PER_M) / 1e6
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
            try:
                parsed = _extract_json(content)
                ok = 1
            except (json.JSONDecodeError, TypeError):
                log.warning("LLM 回傳非 JSON（%s，finish=%s）：%s", purpose, choice.get("finish_reason"), content[:200])
                parsed, ok = None, 0
            conn.execute(
                "INSERT INTO llm_log (ts,purpose,model,prompt_tokens,completion_tokens,cost_usd,ok) "
                "VALUES (?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), purpose, config.LLM_MODEL, pt, ct, cost, ok),
            )
            conn.commit()
            if parsed is None and choice.get("finish_reason") == "length" and attempt == 0:
                payload["max_tokens"] *= 2          # 被思考過程用完 → 加倍空間重試一次
                continue
            return parsed
        except requests.RequestException as e:
            log.warning("LLM 呼叫失敗（%s，第 %d 次）：%s", purpose, attempt + 1, e)
            time.sleep(5 * (attempt + 1))
    return None
