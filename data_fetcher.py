"""
資料抓取模組 - DataFetcher
===========================
從免費公開來源取得台股大盤與個股資料。
資料來源：TWSE 官方 API、yfinance、Goodinfo
"""
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from config import DATA_DIR

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(hours=2)  # 快取有效期


class DataFetcher:
    """台股資料抓取器（零成本，全免費 API）"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
        })

    # ════════════════════════════════════════
    #  大盤資料
    # ════════════════════════════════════════

    def fetch_market_index(self) -> dict:
        """取得台股加權指數收盤行情（TWSE 官方 API，免費）"""
        cache = self._load_cache("market_index")
        if cache:
            return cache

        today = datetime.now()
        # 嘗試今天，若失敗則嘗試前一交易日
        for days_ago in range(0, 5):
            date = today - timedelta(days=days_ago)
            date_str = date.strftime("%Y%m%d")
            # 跳過週末
            if date.weekday() >= 5:
                continue

            url = (
                "https://www.twse.com.tw/exchangeReport/"
                f"MI_INDEX?response=json&date={date_str}&type=IND"
            )
            try:
                resp = self.session.get(url, timeout=15)
                data = resp.json()
                if data.get("stat") != "OK":
                    continue

                # 解析加權指數
                raw = data.get("data1", [])
                result = {"date": date_str}
                for row in raw:
                    if "加權指數" in str(row):
                        # 格式: ["加權指數", "收盤價", "漲跌點", "漲跌%", ...]
                        result["index"] = float(str(row[1]).replace(",", ""))
                        change = str(row[2]).replace(",", "").replace("+", "").replace("▲", "").replace("▼", "-").strip()
                        result["change"] = float(change) if change else 0
                        pct = str(row[3]).replace("+", "").replace("%", "").strip()
                        result["change_pct"] = float(pct) if pct else 0
                        result["volume"] = self._parse_volume(row[4]) if len(row) > 4 else 0
                        self._save_cache("market_index", result)
                        return result
            except Exception as e:
                logger.warning(f"TWSE 大盤資料抓取失敗 ({date_str}): {e}")
                continue

        logger.error("無法取得大盤資料（連續 5 日嘗試失敗）")
        return {"error": "無法取得大盤資料", "date": today.strftime("%Y%m%d")}

    def fetch_institutional_trading(self) -> dict:
        """取得三大法人買賣超（TWSE API，免費）"""
        cache = self._load_cache("institutional_trading")
        if cache:
            return cache

        today = datetime.now()
        for days_ago in range(0, 5):
            date = today - timedelta(days=days_ago)
            date_str = date.strftime("%Y%m%d")
            if date.weekday() >= 5:
                continue

            url = (
                "https://www.twse.com.tw/fund/"
                f"MI_QFIIS?response=json&date={date_str}&selectType=ALL"
            )
            try:
                resp = self.session.get(url, timeout=15)
                data = resp.json()
                if data.get("stat") != "OK":
                    continue

                result = {"date": date_str, "details": {}}
                for row in data.get("data", []):
                    if "合計" in str(row):
                        result["foreign"] = self._parse_number(row[1]) if len(row) > 1 else 0
                        result["sitc"] = self._parse_number(row[5]) if len(row) > 5 else 0  # 投信
                        result["dealer"] = self._parse_number(row[9]) if len(row) > 9 else 0  # 自營商
                        result["total"] = self._parse_number(row[13]) if len(row) > 13 else 0
                        self._save_cache("institutional_trading", result)
                        return result
            except Exception as e:
                logger.warning(f"法人買賣超抓取失敗 ({date_str}): {e}")
                continue

        return {"error": "無法取得法人資料", "date": today.strftime("%Y%m%d")}

    # ════════════════════════════════════════
    #  個股資料
    # ════════════════════════════════════════

    def fetch_stock_current(self, stock_id: str) -> Optional[dict]:
        """取得個股即時行情（TWSE 最佳五檔 API，免費）"""
        cache_key = f"stock_current_{stock_id}"
        cache = self._load_cache(cache_key, ttl=timedelta(minutes=5))
        if cache:
            return cache

        today = datetime.now().strftime("%Y%m%d")
        url = (
            "https://www.twse.com.tw/exchangeReport/"
            f"STOCK_DAY?response=json&date={today}&stockNo={stock_id}"
        )
        try:
            resp = self.session.get(url, timeout=15)
            data = resp.json()
            if data.get("stat") != "OK":
                return None

            rows = data.get("data", [])
            if not rows:
                return None

            # 最後一筆為最近交易日資料
            latest = rows[-1]
            result = {
                "stock_id": stock_id,
                "date": latest[0],
                "volume": self._parse_volume(latest[1]),
                "volume_value": self._parse_number(latest[2]),
                "open": float(latest[3].replace(",", "")),
                "high": float(latest[4].replace(",", "")),
                "low": float(latest[5].replace(",", "")),
                "close": float(latest[6].replace(",", "")),
                "change": latest[7].strip(),
                "change_raw": latest[7].replace("+", "").replace("−", "-").replace(" ", "").strip(),
            }

            # 還原漲跌幅
            try:
                result["change_pct"] = (
                    (result["close"] - result["open"]) / result["open"] * 100
                )
            except ZeroDivisionError:
                result["change_pct"] = 0

            self._save_cache(cache_key, result, ttl=timedelta(minutes=5))
            return result
        except Exception as e:
            logger.warning(f"個股行情抓取失敗 ({stock_id}): {e}")
            return None

    def fetch_stock_history(self, stock_id: str, days: int = 120) -> Optional[pd.DataFrame]:
        """取得個股歷史資料（yfinance，免費）
        回傳包含 Open/High/Low/Close/Volume 的 DataFrame
        """
        try:
            import yfinance as yf
            ticker = yf.Ticker(f"{stock_id}.TW")
            df = ticker.history(period=f"{days}d")
            if df.empty:
                return None
            return df
        except ImportError:
            logger.error("yfinance 未安裝，請執行 pip install yfinance")
            return None
        except Exception as e:
            logger.warning(f"yfinance 抓取失敗 ({stock_id}): {e}")
            return None

    def fetch_news(self, stock_id: str, stock_name: str = "") -> list:
        """取得個股相關新聞摘要（使用 Google News RSS，免費）"""
        query = f"{stock_id} {stock_name} 台股".strip()
        url = "https://news.google.com/rss/search"
        params = {"q": query, "hl": "zh-TW", "gl": "TW", "ceid": "TW:zh-Hant"}
        try:
            resp = self.session.get(url, params=params, timeout=10)
            import xml.etree.ElementTree as ET
            root = ET.fromstring(resp.content)
            items = []
            for item in root.findall(".//item")[:5]:
                title = item.findtext("title", "")
                pub_date = item.findtext("pubDate", "")[:16]
                items.append({"title": title, "date": pub_date})
            return items
        except Exception as e:
            logger.warning(f"新聞抓取失敗 ({stock_id}): {e}")
            return []

    # ════════════════════════════════════════
    #  快取機制
    # ════════════════════════════════════════

    def _load_cache(self, key: str, ttl: timedelta = CACHE_TTL) -> Optional[dict]:
        path = DATA_DIR / f"{key}.json"
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            cached_time = datetime.fromisoformat(data["_cached_at"])
            if datetime.now() - cached_time < ttl:
                return data["payload"]
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
        return None

    def _save_cache(self, key: str, payload: dict, ttl: timedelta = CACHE_TTL):
        path = DATA_DIR / f"{key}.json"
        try:
            with open(path, "w") as f:
                json.dump({
                    "_cached_at": datetime.now().isoformat(),
                    "_ttl_seconds": int(ttl.total_seconds()),
                    "payload": payload,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"快取寫入失敗 ({key}): {e}")

    # ════════════════════════════════════════
    #  工具方法
    # ════════════════════════════════════════

    @staticmethod
    def _parse_number(text: str) -> float:
        """將 '1,234.56' 或 '+1,234' 轉為 float"""
        text = str(text).replace(",", "").replace("+", "").replace(" ", "")
        text = text.replace("▲", "").replace("▼", "").replace("−", "-").strip()
        try:
            return float(text) if text else 0.0
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_volume(text: str) -> float:
        """解析成交量（股數），將 '10,000,000' 轉為 float"""
        text = str(text).replace(",", "").strip()
        try:
            return float(text) if text else 0.0
        except ValueError:
            return 0.0


if __name__ == "__main__":
    # 測試
    logging.basicConfig(level=logging.INFO)
    fetcher = DataFetcher()
    print("=== 大盤 ===")
    print(json.dumps(fetcher.fetch_market_index(), ensure_ascii=False, indent=2))
    print("\n=== 法人買賣超 ===")
    print(json.dumps(fetcher.fetch_institutional_trading(), ensure_ascii=False, indent=2))
    print("\n=== 台揚(2314) 即時行情 ===")
    print(json.dumps(fetcher.fetch_stock_current("2314"), ensure_ascii=False, indent=2))
