"""
stockbot 設定檔
================
大部分參數可以直接改這裡；機密（API key）放在專案根目錄的 `.env`，不要進 git。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("STOCKBOT_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "stockbot.db"
MODEL_DIR = DATA_DIR / "models"
REPORT_DIR = BASE_DIR / "reports" / "stockbot"


def _load_dotenv(path: Path) -> None:
    """極簡 .env 讀取（KEY=VALUE，# 開頭為註解），不覆蓋已存在的環境變數。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")   # 金鑰只放在 Pi 的 ~/stock-analysis/.env（已被 .gitignore 排除，權限 600）
if (BASE_DIR / ".env").exists() and (BASE_DIR / ".env").stat().st_mode & 0o077:
    import warnings
    warnings.warn(".env 權限過寬，建議執行：chmod 600 .env")

# ─── LLM（DeepSeek，OpenAI 相容 API）───
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("DEEPSEEK_MODEL") or os.environ.get("STOCKBOT_LLM_MODEL", "deepseek-chat")
# 每百萬 token 的美元價格（僅用於估算花費，依官網調整）
LLM_PRICE_INPUT_PER_M = float(os.environ.get("STOCKBOT_PRICE_IN", "0.28"))
LLM_PRICE_OUTPUT_PER_M = float(os.environ.get("STOCKBOT_PRICE_OUT", "0.42"))
LLM_DAILY_BUDGET_USD = float(os.environ.get("STOCKBOT_DAILY_BUDGET_USD", "0.5"))
LLM_REASONING_HEADROOM = int(os.environ.get("STOCKBOT_REASONING_HEADROOM", "2000"))  # 推理模型思考用的額外 token
LLM_LOW_BALANCE_USD = float(os.environ.get("STOCKBOT_LOW_BALANCE_USD", "2"))   # 低於此金額就在報告提醒

# ─── 分析範圍 ───
# 七大主題：由 LLM 依公司名稱與產業別歸類，無 LLM 時用 INDUSTRY_FALLBACK_THEME
THEMES = ["AI相關", "半導體", "IC設計", "系統廠", "衛星相關", "散熱相關", "能源相關"]

# 候選池：只從這些官方產業別裡挑（證交所 / 櫃買產業代碼）
CANDIDATE_INDUSTRY_CODES = {
    "05": "電機機械", "06": "電器電纜", "23": "油電燃氣", "24": "半導體業",
    "25": "電腦及週邊設備業", "26": "光電業", "27": "通信網路業", "28": "電子零組件業",
    "31": "其他電子業", "35": "綠能環保", "36": "數位雲端",
}
INDUSTRY_FALLBACK_THEME = {
    "24": "半導體", "25": "系統廠", "27": "衛星相關", "35": "能源相關", "23": "能源相關",
}

# 固定觀察名單（原本的 29 檔）：每天都分析，累積最完整的經驗；主題以這裡為準
CORE_WATCHLIST = {
    "AI相關": {"2356": "英業達", "3706": "神達", "2352": "佳世達", "2324": "仁寶", "3062": "建漢"},
    "半導體": {"2344": "華邦電", "2337": "旺宏", "2408": "南亞科", "3707": "漢磊"},
    "IC設計": {"2401": "凌陽", "4919": "新唐", "2436": "偉詮電", "6202": "盛群"},
    "系統廠": {"2353": "宏碁", "2362": "藍天", "2331": "精英", "2376": "技嘉"},
    "衛星相關": {"2314": "台揚", "3491": "昇達科", "6285": "啟碁", "4906": "正文"},
    "散熱相關": {"3071": "協禧", "6275": "元山", "3323": "加百裕", "2421": "建準"},
    "能源相關": {"6806": "森崴能源", "6170": "統振", "1513": "中興電", "3714": "富采"},  # 6806 已不在上市櫃清單，會自動略過
}
CORE_APPLY_PRICE_LIMIT = False        # 固定名單是否也套用股價上限（False = 一律分析）

MAX_PRICE = float(os.environ.get("STOCKBOT_MAX_PRICE", "50"))   # 動態挑選的股價上限
MIN_AVG_TRADE_VALUE = 20_000_000      # 近 20 日平均成交值（元）下限，過濾冷門股
DYNAMIC_PICKS = 21                    # 每天從候選池動態挑幾檔
MAX_UNIVERSE = 50                     # 每日分析總數上限（固定 + 動態）
DYNAMIC_MIN_HOLD_DAYS = 5             # 動態挑入的股票至少保留幾個交易日（才能累積經驗）
FOCUS_STOCKS_PER_DAY = 10             # 每天 LLM 深度分析幾檔
ATTRIBUTION_PER_DAY = 20              # 每天 LLM 做漲跌歸因幾檔（挑個股因素最大的）
HISTORY_YEARS = 3                     # bootstrap 抓幾年歷史

# 市場指標（yfinance 代號）
MARKET_TICKERS = {
    "^TWII": "加權指數", "^SOX": "費城半導體",
    "^IXIC": "那斯達克", "TWD=X": "美元兌台幣",
}

# ─── 預測 ───
RANGE_COVERAGE_TARGET = 0.70          # 區間覆蓋率目標（校準 k 用）
BIAS_UP_PROB = 0.55                   # prob_up 高於此 → 偏多
BIAS_DOWN_PROB = 0.45                 # prob_up 低於此 → 偏空

# ─── 規則學習 ───
RULE_MIN_SAMPLES = 50
RULE_MIN_LIFT = 0.03                  # 命中率至少比基準高 3 個百分點
RULE_MAX_P_VALUE = 0.05
RULE_RECENT_DAYS = 120                # 近期驗證視窗（交易日）
MAX_ACTIVE_RULES = 15

# ─── 發佈 ───
GIT_PUBLISH = os.environ.get("STOCKBOT_GIT_PUBLISH", "0") == "1"
WEB_PORT = int(os.environ.get("STOCKBOT_WEB_PORT", "5000"))

for d in (DATA_DIR, MODEL_DIR, REPORT_DIR / "daily", REPORT_DIR / "weekly"):
    d.mkdir(parents=True, exist_ok=True)
