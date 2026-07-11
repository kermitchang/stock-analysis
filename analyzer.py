"""
四維度分析引擎 - Analyzer
==========================
依據台股四維度評估法，對個股進行深度技術與基本面分析。
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from config import *

logger = logging.getLogger(__name__)


class StockAnalyzer:
    """個股四維度分析引擎"""

    def analyze(self, stock_data: dict, history: Optional[pd.DataFrame] = None,
                news: list = None) -> dict:
        """
        對單一個股執行完整四維度分析

        Args:
            stock_data: fetch_stock_current() 回傳的即時行情
            history: fetch_stock_history() 回傳的歷史 DataFrame
            news: fetch_news() 回傳的新聞列表

        Returns:
            四維度分析結果字典
        """
        stock_id = stock_data.get("stock_id", "???")
        close = stock_data.get("close", 0)

        result = {
            "stock_id": stock_id,
            "stock_name": self._get_stock_name(stock_id),
            "price": close,
            "change": stock_data.get("change", "0"),
            "change_pct": stock_data.get("change_pct", 0),
            "volume": stock_data.get("volume", 0),
            "analysis_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

        # 維度一：消息與題材面
        result["dimension_1"] = self._analyze_sentiment(stock_id, news)

        # 維度二：技術與資金面
        result["dimension_2"] = self._analyze_technical(history, close)

        # 維度三：財務底氣面
        result["dimension_3"] = self._analyze_fundamental(stock_id, history)

        # 維度四：戰略與管理面
        result["dimension_4"] = self._analyze_strategic(stock_id)

        # 綜合評分
        result["overall_score"] = self._calculate_score(result)
        result["verdict"] = self._make_verdict(result)

        return result

    # ════════════════════════════════════════
    #  維度一：消息與題材面
    # ════════════════════════════════════════

    def _analyze_sentiment(self, stock_id: str, news: list = None) -> dict:
        """分析消息面題材與產業鏈重新定義可能性"""
        # 產業題材關鍵字對照
        themes = {
            "AI": ["AI", "人工智慧", "伺服器", "邊緣運算", "HPC", "GPU", "NVIDIA輝達",
                   "AI PC", "AI手機", "AI伺服器", "CoWoS", "FOPLP"],
            "衛星": ["衛星", "低軌", "LEO", "星鏈", "Starlink", "地面站",
                     "衛星通訊", "太空", "通訊"],
            "散熱": ["散熱", "水冷", "液冷", "浸沒式", "熱管理", "均熱片",
                     "VC", "熱導管", "風扇", "冷卻"],
            "能源": ["能源", "太陽能", "風電", "儲能", "電網", "充電樁",
                     "綠能", "碳權", "淨零", "RE100", "氫能"],
        }

        # 資產活化關鍵字
        asset_keywords = ["處分", "賣廠", "活化", "資產", "轉讓", "子公司", "分拆"]

        matched_themes = {}
        if news:
            news_text = " ".join([n["title"] for n in news])
            for category, keywords in themes.items():
                matches = [kw for kw in keywords if kw.lower() in news_text.lower()]
                if matches:
                    matched_themes[category] = matches

        # Re-rating 可能性判斷（股價 + 轉型題材）
        re_rating_signals = []
        if news:
            re_rating_keywords = ["轉型", "切入", "新訂單", "跨入", "轉機",
                                  "重新評價", "新領域", "轉單", "通過認證"]
            for kw in re_rating_keywords:
                if kw in news_text:
                    re_rating_signals.append(kw)

        return {
            "matched_themes": matched_themes or {"note": "暫無明顯題材匹配"},
            "re_rating_signals": re_rating_signals or ["待觀察"],
            "asset_activation_signals": ["待確認"],
            "summary": self._generate_sentiment_summary(matched_themes, re_rating_signals)
        }

    # ════════════════════════════════════════
    #  維度二：技術與資金面
    # ════════════════════════════════════════

    def _analyze_technical(self, history: Optional[pd.DataFrame],
                           current_price: float) -> dict:
        """技術面與籌碼面分析"""
        result = {
            "ma_status": "資料不足",
            "volume_analysis": "資料不足",
            "kd": {},
            "summary": "資料不足，無法進行技術分析"
        }

        if history is None or len(history) < 60:
            return result

        close = history["Close"]

        # ── 均線結構 ──
        mas = {}
        for period in MA_PERIODS:
            if len(close) >= period:
                mas[f"MA{period}"] = round(close.tail(period).mean(), 2)
            else:
                mas[f"MA{period}"] = None

        # 判斷是否多頭排列（MA5 > MA20 > MA60）
        has_all_mas = all(mas.get(f"MA{p}") is not None for p in [5, 20, 60])
        bullish = has_all_mas and (mas["MA5"] > mas["MA20"] > mas["MA60"])
        bearish = has_all_mas and (mas["MA5"] < mas["MA20"] < mas["MA60"])

        if bullish:
            ma_status = "✅ 多頭排列（MA5 > MA20 > MA60），趨勢偏多"
        elif bearish:
            ma_status = "⚠️ 空頭排列（MA5 < MA20 < MA60），趨勢偏空"
        else:
            ma_status = "🔄 均線糾結/混亂，方向不明"
        result["ma_status"] = ma_status
        result["mas"] = mas

        # ── 量價動能 ──
        volume = history["Volume"]
        avg_5_volume = volume.tail(5).mean()
        latest_volume = volume.iloc[-1] if len(volume) > 0 else 0

        if avg_5_volume > 0 and latest_volume > avg_5_volume * VOLUME_SURGE_RATIO:
            volume_status = f"📈 成交量放大（當日 {latest_volume/1000:.0f}張 > 5日均 {avg_5_volume/1000:.0f}張），動能充足"
        elif avg_5_volume > 0 and latest_volume < avg_5_volume * 0.7:
            volume_status = f"📉 量縮（當日 {latest_volume/1000:.0f}張 < 5日均 {avg_5_volume/1000:.0f}張），動能不足"
        else:
            volume_status = f"🔸 量能平穩（當日 {latest_volume/1000:.0f}張，5日均 {avg_5_volume/1000:.0f}張）"
        result["volume_analysis"] = volume_status

        # ── KD 指標 ──
        kd = self._compute_kd(close)
        if kd:
            k_val, d_val = kd["K"], kd["D"]
            result["kd"] = kd

            if k_val > KD_OVERBOUGHT and d_val > KD_OVERBOUGHT:
                kd_status = f"🔴 高檔鈍化（K={k_val:.1f}, D={d_val:.1f}），短線過熱，不宜追高"
            elif k_val < KD_OVERSOLD and d_val < KD_OVERSOLD:
                kd_status = f"🟢 低檔超賣（K={k_val:.1f}, D={d_val:.1f}），可能出現反彈/黃金交叉"
            elif kd.get("golden_cross"):
                kd_status = f"🟢 黃金交叉（K={k_val:.1f}, D={d_val:.1f}），右側買點訊號"
            elif kd.get("death_cross"):
                kd_status = f"🔴 死亡交叉（K={k_val:.1f}, D={d_val:.1f}），出場警訊"
            else:
                kd_status = f"🔸 KD 中性（K={k_val:.1f}, D={d_val:.1f}），無明顯訊號"
            result["kd_status"] = kd_status
        else:
            result["kd_status"] = "KD 計算失敗"

        # ── 價格位置 ──
        high_20d = close.tail(20).max()
        low_20d = close.tail(20).min()
        if high_20d > low_20d:
            position_pct = (current_price - low_20d) / (high_20d - low_20d) * 100
            result["price_position"] = f"20日區間位置：{position_pct:.0f}%（高={high_20d:.2f}, 低={low_20d:.2f}）"
            if position_pct > 80:
                result["price_position_status"] = "⚠️ 近20日高檔"
            elif position_pct < 20:
                result["price_position_status"] = "✅ 近20日低檔"
            else:
                result["price_position_status"] = "🔸 區間中間"

        # 綜合摘要
        summaries = [ma_status, volume_status, result.get("kd_status", "")]
        result["summary"] = " | ".join(summaries)

        return result

    # ════════════════════════════════════════
    #  維度三：財務底氣面
    # ════════════════════════════════════════

    def _analyze_fundamental(self, stock_id: str,
                             history: Optional[pd.DataFrame]) -> dict:
        """財務基本面分析（月營收趨勢、獲利能力）"""
        result = {
            "revenue_trend": "待補（需月營收資料）",
            "profitability": "待補（需財報資料）",
            "summary": "財務資料因免費 API 限制，建議搭配 Goodinfo/公開資訊站查閱"
        }

        # 營收趨勢：從股價歷史反推（近60日 vs 近20日均價）
        if history is not None and len(history) > 60:
            close = history["Close"]
            recent_avg = close.tail(20).mean()
            mid_avg = close.iloc[-60:-20].mean() if len(close) > 60 else recent_avg
            if mid_avg > 0:
                trend_pct = (recent_avg / mid_avg - 1) * 100
                if trend_pct > 10:
                    result["price_trend"] = f"📈 近期股價均價上揚 {trend_pct:.1f}%，反映市場對營收預期轉佳"
                elif trend_pct < -10:
                    result["price_trend"] = f"📉 近期股價均價下滑 {trend_pct:.1f}%，市場預期偏弱"
                else:
                    result["price_trend"] = f"🔸 股價均價持平（{trend_pct:.1f}%），無明顯趨勢變化"

        return result

    # ════════════════════════════════════════
    #  維度四：戰略與管理面
    # ════════════════════════════════════════

    def _analyze_strategic(self, stock_id: str) -> dict:
        """產業護城河與內部人持股分析"""
        # 此維度需深入財報與公開資訊，免費 API 較難取得
        # 提供初步框架與產業鏈地位判斷

        # 依股票代號提供不同產業鏈定位提示
        industry_hints = {
            "2356": "AI伺服器代工廠，議價能力中等，受惠於全球AI資本支出擴張",
            "3706": "AI伺服器/邊緣運算，具備品牌與代工雙軌策略",
            "2352": "AIoT/智慧解決方案整合商，涵蓋醫療、教育、智慧城市",
            "2324": "全球前三大NB代工廠，AI PC換機潮主要受惠者",
            "3062": "網通設備大廠，5G FWA與LEO衛星地面站為成長動能",
            "2314": "低軌衛星接收站關鍵供應商，LEO產業爆發直接受惠",
            "3491": "微波/毫米波通訊元件，衛星與5G基建雙題材",
            "6285": "網通設備大廠，衛星通訊+WiFi 7雙引擎",
            "4906": "網通設備ODM，低軌衛星用戶終端出貨成長",
            "3071": "散熱風扇專業廠，AI伺服器散熱需求帶動規格升級",
            "6275": "散熱模組廠，布局水冷與浸沒式散熱方案",
            "3323": "散熱/電池模組雙軌並進，儲能為新成長動能",
            "2421": "散熱風扇龍頭之一，伺服器散熱需求持續增溫",
            "6806": "太陽能電站/風電開發商，綠電憑證與RE100需求受益",
            "6170": "儲能系統整合商，台電電網韌性計畫受惠",
            "1513": "電力設備龍頭，智慧電網/氫能/充電樁多元布局",
            "3714": "LED/感測元件，切入能源管理與車用市場",
            # 半導體
            "2344": "記憶體/晶圓代工廠，受惠AI帶動HBM與DDR5需求",
            "2337": "NOR Flash全球領導廠商，車用與工業規格需求穩定",
            "2408": "台灣DRAM大廠，AI邊緣運算帶動記憶體需求回升",
            "3707": "功率半導體磊晶廠，電動車與綠能逆變器需求受惠",
            # IC設計
            "2401": "多媒體IC設計，車用影音與AI邊緣運算晶片布局",
            "4919": "MCU微控制器大廠，ARM架構與BMC晶片雙軌成長",
            "2436": "電源管理IC、馬達驅動IC，伺服器與車用需求成長",
            "6202": "MCU設計廠，觸控與健康量測晶片利基市場布局",
            # 系統廠
            "2353": "全球品牌PC大廠，AI PC換機潮直接受惠",
            "2362": "NB專業代工廠，商用換機需求帶動營運回溫",
            "2331": "主機板/系統廠，IPC工業電腦轉型布局",
            "2376": "主機板/顯卡/伺服器三引擎，AI伺服器出貨成長",
        }

        return {
            "industry_position": industry_hints.get(stock_id, "待補：請查閱公開資訊站補充"),
            "pricing_power": "需參考該公司在供應鏈中的地位與毛利率趨勢",
            "insider_trading": "待補（需櫃買中心/公開資訊觀測站資料）",
            "summary": f"產業護城河分析：{industry_hints.get(stock_id, '請自行補充')}"
        }

    # ════════════════════════════════════════
    #  評分與結論
    # ════════════════════════════════════════

    def _calculate_score(self, analysis: dict) -> float:
        """綜合評分（0~100）"""
        score = 50  # 起始中立

        d2 = analysis.get("dimension_2", {})
        # 多頭排列加分
        if "多頭排列" in d2.get("ma_status", ""):
            score += 15
        elif "空頭排列" in d2.get("ma_status", ""):
            score -= 15

        # 量能充足加分
        if "放大" in d2.get("volume_analysis", ""):
            score += 10
        elif "量縮" in d2.get("volume_analysis", ""):
            score -= 10

        # KD 訊號
        kd_status = d2.get("kd_status", "")
        if "黃金交叉" in kd_status or "低檔超賣" in kd_status:
            score += 10
        elif "死亡交叉" in kd_status or "高檔鈍化" in kd_status:
            score -= 10

        # 題材加分
        d1 = analysis.get("dimension_1", {})
        if d1.get("matched_themes"):
            score += 10

        return max(0, min(100, round(score)))

    def _make_verdict(self, analysis: dict) -> dict:
        """產出最終判讀"""
        score = analysis.get("overall_score", 50)

        if score >= 70:
            trend = "📈 偏多看"
            suggestion = "若能站穩均線支撐且量能配合，可列為觀察標的"
        elif score >= 45:
            trend = "🔸 中立觀察"
            suggestion = "等待技術面進一步表態或題材發酵後再評估"
        else:
            trend = "📉 偏空看待"
            suggestion = "技術面與籌碼面均偏弱，建議觀望"

        return {
            "trend": trend,
            "score": score,
            "suggestion": suggestion,
        }

    # ════════════════════════════════════════
    #  工具方法
    # ════════════════════════════════════════

    @staticmethod
    def _compute_kd(close: pd.Series, n: int = 9) -> Optional[dict]:
        """計算 KD 指標（隨機指標）"""
        if len(close) < n + 3:
            return None
        try:
            high_n = close.rolling(n).max()
            low_n = close.rolling(n).min()
            rsv = (close - low_n) / (high_n - low_n) * 100

            k = [50.0]
            d = [50.0]
            for val in rsv.dropna().values:
                k.append(2/3 * k[-1] + 1/3 * val)
                d.append(2/3 * d[-1] + 1/3 * k[-1])

            prev_k, prev_d = k[-3], d[-3]
            curr_k, curr_d = k[-2], d[-2]

            return {
                "K": round(curr_k, 2),
                "D": round(curr_d, 2),
                "golden_cross": bool(prev_k < prev_d and curr_k > curr_d),
                "death_cross": bool(prev_k > prev_d and curr_k < curr_d),
            }
        except Exception as e:
            logger.warning(f"KD 計算錯誤: {e}")
            return None

    @staticmethod
    def _get_stock_name(stock_id: str) -> str:
        """取得股票名稱"""
        names = {
            "2356": "英業達", "3706": "神達", "2352": "佳世達",
            "2324": "仁寶", "3062": "建漢", "2314": "台揚",
            "3491": "昇達科", "6285": "啟碁", "4906": "正文",
            "3071": "協禧", "6275": "元山", "3323": "加百裕",
            "2421": "建準", "6806": "森崴能源", "6170": "統振",
            "1513": "中興電", "3714": "富采",
            # 半導體
            "2344": "華邦電", "2337": "旺宏", "2408": "南亞科", "3707": "漢磊",
            # IC設計
            "2401": "凌陽", "4919": "新唐", "2436": "偉詮電", "6202": "盛群",
            # 系統廠
            "2353": "宏碁", "2362": "藍天", "2331": "精英", "2376": "技嘉",
        }
        return names.get(stock_id, stock_id)

    @staticmethod
    def _generate_sentiment_summary(themes: dict, signals: list) -> str:
        """產出題材面摘要"""
        parts = []
        if themes:
            cats = list(themes.keys())
            parts.append(f"匹配題材：{'、'.join(cats)}")
        if signals and signals[0] != "待觀察":
            parts.append(f"Re-rating訊號：{'、'.join(signals)}")
        if not parts:
            parts.append("暫無明顯題材催化劑，需持續追蹤")
        return " | ".join(parts)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # 測試用假資料
    import pandas as pd
    import numpy as np

    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=120, freq="D")
    mock_data = {
        "Close": 25 + np.cumsum(np.random.randn(120) * 0.5),
        "Volume": np.random.randint(5000000, 20000000, 120),
    }
    mock_df = pd.DataFrame(mock_data, index=dates)

    analyzer = StockAnalyzer()
    result = analyzer.analyze(
        stock_data={"stock_id": "2314", "close": 26.5, "change": "+0.5", "change_pct": 1.92, "volume": 8500000},
        history=mock_df,
        news=[{"title": "台揚打入Starlink供應鏈 低軌衛星商機爆發", "date": "2026-01-15"}]
    )
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
