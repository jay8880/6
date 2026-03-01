"""
╔══════════════════════════════════════════════════════════════════════════╗
║ 🌿 BLOOM PRO v5 — UNIFIED TRADING SYSTEM                               ║
║ Flask API + Angel One Bot + Live Prices + Dashboard Control             ║
║ GitHub → Railway Cloud · 24/7 Auto Trading                              ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import os, json, time, pyotp, pytz, threading, logging, schedule
import requests, pandas as pd, numpy as np
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()
IST = pytz.timezone("Asia/Kolkata")

# ─── Try Angel One SDK ────────────────────────────────────────────────────
try:
    from SmartApi import SmartConnect
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    ANGEL_SDK = True
except ImportError:
    ANGEL_SDK = False
    logging.warning("SmartAPI not installed — running in simulation mode")

# ═══════════════════════════════════════════════════════════════
# LOGGING   (FIX 1: missing closing ]) and `)` in basicConfig)
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(f"bloom_{datetime.now(IST).strftime('%Y%m%d')}.log")
    ]   # ← was missing ] and )
)

log = logging.getLogger("BloomV5")

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════
CONFIG_FILE = "bloom_config.json"

DEFAULT_CONFIG = {
    "api_key":     os.environ.get("ANGEL_API_KEY", ""),
    "client_id":   os.environ.get("ANGEL_CLIENT_ID", ""),
    "password":    os.environ.get("ANGEL_PASSWORD", ""),
    "totp_secret": os.environ.get("ANGEL_TOTP_SECRET", ""),
    "tg_token":    os.environ.get("TELEGRAM_TOKEN", ""),
    "tg_chat_id":  os.environ.get("TELEGRAM_CHAT_ID", ""),

    "total_capital":      float(os.environ.get("TOTAL_CAPITAL", 100000)),
    "max_deploy_pct":     float(os.environ.get("MAX_DEPLOY_PCT", 80)),
    "risk_per_trade_pct": float(os.environ.get("RISK_PER_TRADE_PCT", 1)),
    "max_daily_loss_pct": float(os.environ.get("MAX_DAILY_LOSS_PCT", 3)),
    "max_positions":      int(os.environ.get("MAX_POSITIONS", 5)),
    "max_trades_day":     int(os.environ.get("MAX_TRADES_DAY", 20)),
    "product_type":       os.environ.get("PRODUCT_TYPE", "INTRADAY"),

    "sl_pct":       float(os.environ.get("SL_PCT", 0.5)),
    "tp_pct":       float(os.environ.get("TP_PCT", 1.0)),
    "trail_pct":    float(os.environ.get("TRAIL_PCT", 0.3)),
    "min_score":    int(os.environ.get("MIN_SCORE", 4)),
    "candle_tf":    os.environ.get("CANDLE_TF", "ONE_MINUTE"),
    "cooldown_sec": int(os.environ.get("COOLDOWN_SEC", 60)),

    "use_rsi": True, "use_macd": True, "use_vwap": True, "use_bb": True,
    "use_supertrend": True, "use_ema": True, "use_volume": True, "use_momentum": True,

    "bot_running": False, "auto_squareoff": True,
    "send_telegram": True, "paper_trade": False,

    # FIX 2: watchlist dict was never closed (missing ] and })
    "watchlist": [
        {"symbol": "RELIANCE",  "token": "2885",  "exchange": "NSE"},
        {"symbol": "TCS",       "token": "11536", "exchange": "NSE"},
        {"symbol": "HDFCBANK",  "token": "1333",  "exchange": "NSE"},
        {"symbol": "INFY",      "token": "1594",  "exchange": "NSE"},
        {"symbol": "ICICIBANK", "token": "4963",  "exchange": "NSE"},
        {"symbol": "SBIN",      "token": "3045",  "exchange": "NSE"},
        {"symbol": "ITC",       "token": "1660",  "exchange": "NSE"},
        {"symbol": "AXISBANK",  "token": "5900",  "exchange": "NSE"},
        {"symbol": "WIPRO",     "token": "3787",  "exchange": "NSE"},
        {"symbol": "KOTAKBANK", "token": "1922",  "exchange": "NSE"},
    ]  # ← was missing ] and the outer } closing DEFAULT_CONFIG
}


def load_config() -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                saved = json.load(f)
            saved.pop("api_key", None); saved.pop("client_id", None)
            saved.pop("password", None); saved.pop("totp_secret", None)
            cfg.update(saved)
        except Exception as e:
            log.warning(f"Config load error: {e}")
    return cfg


def save_config(cfg: dict):
    safe = {k: v for k, v in cfg.items()
            if k not in ("api_key","client_id","password","totp_secret","tg_token","tg_chat_id")}
    with open(CONFIG_FILE, "w") as f:
        json.dump(safe, f, indent=2)
    log.info("✅ Config saved to bloom_config.json")


CFG = load_config()


# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════
def now_ist():   return datetime.now(IST).strftime("%H:%M:%S")
def today_ist(): return datetime.now(IST).strftime("%d/%m/%Y")

def is_market_open() -> bool:
    n = datetime.now(IST)
    if n.weekday() >= 5: return False
    o = n.replace(hour=9,  minute=15, second=0, microsecond=0)
    c = n.replace(hour=15, minute=30, second=0, microsecond=0)
    return o <= n <= c

def append_trade_csv(rec: dict):
    fname = f"trades_{datetime.now(IST).strftime('%Y%m%d')}.csv"
    pd.DataFrame([rec]).to_csv(fname, mode="a", header=not os.path.exists(fname), index=False)


# ═══════════════════════════════════════════════════════════════
# LIVE PRICE FEED
# ═══════════════════════════════════════════════════════════════
class PriceFeed:
    def __init__(self):
        self.prices  = {}
        self.ohlcv   = {}
        self.ws      = None
        self.ws_ready = False
        self._lk     = threading.Lock()

    def update(self, sym: str, price: float, vol: float = 0):
        with self._lk:
            self.prices[sym] = price

    def get(self, sym: str) -> float:
        return self.prices.get(sym, 0.0)

    def fetch_yahoo(self, sym: str) -> float:
        try:
            ticker = sym + ".NS"
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
            r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            data   = r.json()
            result = data["chart"]["result"][0]
            price  = result["meta"]["regularMarketPrice"]
            self.update(sym, price)
            return price
        except Exception:
            return 0.0

    def fetch_nse_index(self) -> dict:
        try:
            url = "https://www.nseindia.com/api/allIndices"
            headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                       "Referer": "https://www.nseindia.com/"}
            s = requests.Session()
            s.get("https://www.nseindia.com", headers=headers, timeout=5)
            r = s.get(url, headers=headers, timeout=8)
            indices = r.json().get("data", [])
            result  = {}
            for idx in indices:
                name = idx.get("indexSymbol", "")
                val  = idx.get("last", 0)
                if name in ("NIFTY 50", "NIFTY BANK", "NIFTY IT"):
                    result[name] = val
            return result
        except Exception:
            return {}

    def fetch_all_yahoo(self):
        syms = [s["symbol"] for s in CFG["watchlist"]]
        for sym in syms:
            try:
                p = self.fetch_yahoo(sym)
                if p > 0:
                    log.debug(f"Yahoo {sym}: ₹{p:.2f}")
                time.sleep(0.3)
            except Exception:
                pass

    def start_yahoo_loop(self):
        def loop():
            while True:
                if not self.ws_ready:
                    self.fetch_all_yahoo()
                time.sleep(30)
        threading.Thread(target=loop, daemon=True).start()
        log.info("📡 Yahoo Finance fallback price loop started")


FEED = PriceFeed()


# ═══════════════════════════════════════════════════════════════
# FUND MANAGER
# ═══════════════════════════════════════════════════════════════
class FundManager:
    def __init__(self): self.reset()

    def reset(self):
        self.total      = CFG["total_capital"]
        self.available  = CFG["total_capital"]
        self.deployed   = 0.0
        self.daily_pnl  = 0.0
        self.positions  = {}
        self.trade_log  = []
        self.trades     = 0
        self.wins       = 0
        self.losses     = 0
        self._lk        = threading.Lock()

    def reload_capital(self):
        with self._lk:
            old = self.total
            self.total = CFG["total_capital"]
            diff = self.total - old
            self.available += diff
            log.info(f"Capital reloaded: ₹{self.total:,.0f} (diff: {diff:+.0f})")

    @property
    def max_deploy(self): return self.total * CFG["max_deploy_pct"] / 100
    @property
    def free_capital(self): return max(0, min(self.available, self.max_deploy - self.deployed))
    @property
    def win_rate(self):
        c = self.wins + self.losses
        return round(self.wins / c * 100, 1) if c else 0.0

    def calc_qty(self, price: float) -> tuple:
        cap = self.free_capital
        if cap < price: return 0, "Insufficient capital"
        slots_free = max(1, CFG["max_positions"] - len(self.positions))
        slot_cap   = cap / slots_free
        risk_amt   = self.total * CFG["risk_per_trade_pct"] / 100
        sl_amt     = price * CFG["sl_pct"] / 100
        qty_risk   = max(1, int(risk_amt / sl_amt)) if sl_amt > 0 else 1
        qty_cap    = max(1, int(slot_cap / price))
        qty        = min(qty_risk, qty_cap)
        return qty, "OK"

    def can_trade(self, price: float) -> tuple:
        if len(self.positions) >= CFG["max_positions"]:          return False, 0, "Max positions"
        if self.trades >= CFG["max_trades_day"]:                 return False, 0, "Max trades"
        if self.daily_pnl <= -(self.total * CFG["max_daily_loss_pct"] / 100):
            return False, 0, "Daily loss limit"
        qty, msg = self.calc_qty(price)
        if qty < 1: return False, 0, msg
        return True, qty, "OK"

    def open_pos(self, sym, price, qty, sl, tp, trail, strat, tf, score, entry_time=None):
        with self._lk:
            val = price * qty
            self.positions[sym] = {
                "entry": price, "qty": qty, "val": val,
                "sl": sl, "tp": tp, "trail": trail, "peak": price,
                "strat": strat, "tf": tf, "score": score,
                "open_time": entry_time or now_ist(),
                "sym": sym
            }   # FIX 3: missing closing } for positions dict
            self.deployed  += val
            self.available -= val

    def close_pos(self, sym, exit_price, reason) -> dict:
        with self._lk:
            if sym not in self.positions: return {}
            p   = self.positions.pop(sym)
            pnl = (exit_price - p["entry"]) * p["qty"]
            self.deployed  -= p["val"]
            self.available += p["val"] + pnl
            self.daily_pnl += pnl
            self.trades    += 1
            if pnl > 0: self.wins   += 1
            else:        self.losses += 1
            rec = {
                "id":      f"{sym}_{int(time.time())}",
                "time":    now_ist(), "date": today_ist(),
                "sym":     sym, "action": "SELL",
                "entry":   round(p["entry"], 2),
                "exit":    round(exit_price, 2),
                "qty":     p["qty"],
                "value":   round(p["val"], 2),
                "pnl":     round(pnl, 2),
                "pnl_pct": round(pnl / p["val"] * 100, 2),
                "strat":   p["strat"], "tf": p["tf"],
                "score":   p["score"], "reason": reason,
                "duration": p["open_time"] + " → " + now_ist(),
                "status":  "FILLED"
            }   # FIX 4: missing closing } for rec dict
            self.trade_log.append(rec)
            append_trade_csv(rec)
            return rec

    def update_trail(self, sym, ltp):
        with self._lk:
            if sym not in self.positions: return
            p = self.positions[sym]
            if ltp > p["peak"]:
                p["peak"]  = ltp
                p["trail"] = ltp * (1 - CFG["trail_pct"] / 100)

    def summary(self) -> dict:
        return {
            "total":       round(self.total, 2),
            "available":   round(self.available, 2),
            "deployed":    round(self.deployed, 2),
            "daily_pnl":   round(self.daily_pnl, 2),
            "positions":   len(self.positions),
            "trades":      self.trades,
            "wins":        self.wins,
            "losses":      self.losses,
            "win_rate":    self.win_rate,
            "deploy_pct":  round(self.deployed  / self.total * 100, 1) if self.total else 0,
            "avail_pct":   round(self.available / self.total * 100, 1) if self.total else 0,
            "pnl_pct":     round(self.daily_pnl / self.total * 100, 2) if self.total else 0,
            "max_loss_lmt":round(self.total * CFG["max_daily_loss_pct"] / 100, 2),
        }   # FIX 5: missing closing } for summary dict

    def positions_list(self) -> list:
        result = []
        for sym, p in self.positions.items():
            ltp  = FEED.get(sym) or p["entry"]
            pnl  = (ltp - p["entry"]) * p["qty"]
            rng  = p["tp"] - p["sl"]
            prog = round(max(0, min(100, (ltp - p["sl"]) / rng * 100)), 1) if rng > 0 else 50
            result.append({**p, "sym": sym, "ltp": round(ltp, 2),
                           "pnl": round(pnl, 2), "pnl_pct": round(pnl / p["val"] * 100, 2),
                           "progress": prog})
        return result

    def reset_daily(self):
        with self._lk:
            self.daily_pnl = 0.0
            self.trades = self.wins = self.losses = 0
        log.info("🔄 Daily reset")


FUND = FundManager()


# ═══════════════════════════════════════════════════════════════
# INDICATOR ENGINE
# ═══════════════════════════════════════════════════════════════
def rsi(c, n=7):
    d = c.diff()
    g = d.where(d>0, 0.).ewm(com=n-1, min_periods=n).mean()
    l = (-d.where(d<0, 0.)).ewm(com=n-1, min_periods=n).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def macd(c, f=5, s=13, sig=4):
    ml = c.ewm(span=f, adjust=False).mean() - c.ewm(span=s, adjust=False).mean()
    return ml, ml.ewm(span=sig, adjust=False).mean()

def bollinger(c, n=15, std=2.0):
    m = c.rolling(n).mean()
    d = c.rolling(n).std()
    return m + std*d, m - std*d

def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum()

def supertrend(df, n=7, m=3.0):
    hl2 = (df["high"] + df["low"]) / 2
    atr  = (df["high"] - df["low"]).rolling(n).mean()
    ub   = hl2 + m * atr
    lb   = hl2 - m * atr
    di   = pd.Series(0.0, index=df.index)
    for i in range(1, len(df)):
        c = df["close"].iloc[i]
        di.iloc[i] = 1 if c > ub.iloc[i] else (-1 if c < lb.iloc[i] else di.iloc[i-1])
    return di

def volume_surge(df, n=20, mult=1.5):
    avg_vol = df["volume"].rolling(n).mean()
    return df["volume"] > avg_vol * mult

def momentum_breakout(df, n=10):
    hi = df["high"].rolling(n).max()
    lo = df["low"].rolling(n).min()
    c  = df["close"]
    return (c > hi.shift(1)), (c < lo.shift(1))

def get_signal(df: pd.DataFrame) -> tuple:
    if len(df) < 45: return "HOLD", 0, "", {}
    df = df.copy()
    df["rsi"]         = rsi(df["close"])
    df["macd"], df["ms"] = macd(df["close"])
    df["bbu"], df["bbl"] = bollinger(df["close"])
    df["vwap"]        = vwap(df)
    df["st"]          = supertrend(df)
    df["ema9"]        = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"]       = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"]       = df["close"].ewm(span=50, adjust=False).mean()
    df["vsurge"]      = volume_surge(df)
    df["bo_up"], df["bo_dn"] = momentum_breakout(df)
    df.dropna(inplace=True)
    if len(df) < 3: return "HOLD", 0, "", {}

    c, p = df.iloc[-1], df.iloc[-2]

    # FIX 6: buy dict was never closed (missing })
    buy = {
        "RSI Oversold":    CFG["use_rsi"]       and bool(c["rsi"] < 35),
        "MACD Crossover":  CFG["use_macd"]      and bool(c["macd"] > c["ms"] and p["macd"] <= p["ms"]),
        "BB Lower Touch":  CFG["use_bb"]        and bool(c["close"] <= c["bbl"] * 1.003),
        "Above VWAP":      CFG["use_vwap"]      and bool(c["close"] > c["vwap"]),
        "SuperTrend Up":   CFG["use_supertrend"]and bool(c["st"] == 1),
        "EMA Alignment":   CFG["use_ema"]       and bool(c["ema9"] > c["ema21"] > c["ema50"]),
        "Volume Surge":    CFG["use_volume"]    and bool(c["vsurge"]),
        "Momentum BO Up":  CFG["use_momentum"]  and bool(c["bo_up"]),
    }

    # FIX 7: sell dict was never closed (missing })
    sell = {
        "RSI Overbought":  CFG["use_rsi"]       and bool(c["rsi"] > 65),
        "MACD Crossdown":  CFG["use_macd"]      and bool(c["macd"] < c["ms"] and p["macd"] >= p["ms"]),
        "BB Upper Touch":  CFG["use_bb"]        and bool(c["close"] >= c["bbu"] * 0.997),
        "Below VWAP":      CFG["use_vwap"]      and bool(c["close"] < c["vwap"]),
        "SuperTrend Down": CFG["use_supertrend"]and bool(c["st"] == -1),
        "EMA Bear Cross":  CFG["use_ema"]       and bool(c["ema9"] < c["ema21"] < c["ema50"]),
        "Volume Surge":    CFG["use_volume"]    and bool(c["vsurge"]),
        "Momentum BO Dn":  CFG["use_momentum"]  and bool(c["bo_dn"]),
    }

    b_score = sum(buy.values())
    s_score = sum(sell.values())
    min_s   = CFG["min_score"]

    if b_score >= min_s and b_score > s_score:
        strat = " + ".join(k for k, v in buy.items() if v)[:60]
        return "BUY",  b_score, strat, {"buy": buy, "sell": sell, "rsi": round(float(c["rsi"]),1), "ltp": round(float(c["close"]),2)}
    if s_score >= min_s:
        strat = " + ".join(k for k, v in sell.items() if v)[:60]
        return "SELL", s_score, strat, {"buy": buy, "sell": sell, "rsi": round(float(c["rsi"]),1), "ltp": round(float(c["close"]),2)}

    return "HOLD", max(b_score, s_score), "", {"rsi": round(float(c["rsi"]),1), "ltp": round(float(c["close"]),2)}


# ═══════════════════════════════════════════════════════════════
# CANDLE STORE
# ═══════════════════════════════════════════════════════════════
class CandleStore:
    def __init__(self):
        self.candles = {}
        self.current = {}
        self._lk     = threading.Lock()

    def on_tick(self, sym: str, price: float, volume: float = 0):
        with self._lk:
            now = datetime.now(IST)
            mn  = now.replace(second=0, microsecond=0)
            FEED.update(sym, price, volume)
            if sym not in self.current:
                self.current[sym] = {"ts": mn, "open": price, "high": price,
                                     "low": price, "close": price, "volume": volume}
                return
            c = self.current[sym]
            if mn > c["ts"]:
                self._append(sym, c)
                self.current[sym] = {"ts": mn, "open": price, "high": price,
                                     "low": price, "close": price, "volume": volume}
            else:
                c["high"]   = max(c["high"], price)
                c["low"]    = min(c["low"],  price)
                c["close"]  = price
                c["volume"] += volume

    def _append(self, sym, c):
        row = pd.DataFrame([{"timestamp": c["ts"], "open": c["open"], "high": c["high"],
                              "low": c["low"], "close": c["close"], "volume": c["volume"]}])
        if sym not in self.candles:
            self.candles[sym] = row
        else:
            self.candles[sym] = pd.concat([self.candles[sym], row]).tail(500).reset_index(drop=True)

    def get_df(self, sym: str) -> pd.DataFrame:
        with self._lk:
            df = self.candles.get(sym, pd.DataFrame())
            if sym in self.current:
                c       = self.current[sym]
                cur_row = pd.DataFrame([{"timestamp": c["ts"], "open": c["open"],
                                          "high": c["high"], "low": c["low"],
                                          "close": c["close"], "volume": c["volume"]}])
                df = pd.concat([df, cur_row]).reset_index(drop=True)
            return df

    def load_historical(self, api, sym, token, exchange, days=5):
        if not api: return
        try:
            n = datetime.now(IST)
            r = api.getCandleData({
                "exchange": exchange, "symboltoken": token,
                "interval": CFG["candle_tf"],
                "fromdate": (n - timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
                "todate":   n.strftime("%Y-%m-%d %H:%M"),
            })
            if r.get("status") and r.get("data"):
                df = pd.DataFrame(r["data"], columns=["timestamp","open","high","low","close","volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"])
                df = df.sort_values("timestamp").reset_index(drop=True)
                self.candles[sym] = df.astype({"open": float,"high": float,"low": float,"close": float,"volume": float})
                lp = float(df.iloc[-1]["close"])
                FEED.update(sym, lp)
                log.info(f" ✅ {sym}: {len(df)} candles, LTP ₹{lp:.2f}")
        except Exception as e:
            log.warning(f" ⚠ {sym} history: {e}")


STORE = CandleStore()


# ═══════════════════════════════════════════════════════════════
# BOT ENGINE
# ═══════════════════════════════════════════════════════════════
class BotEngine:
    def __init__(self):
        self.api      = None
        self.auth_tok = None
        self.feed_tok = None
        self.ws       = None
        self.running  = False
        self.last_sig = {}
        self._lk      = threading.Lock()

    def login(self, retries=3) -> bool:
        if not ANGEL_SDK:
            log.info("⚠ Angel SDK not available — paper trading mode")
            return True
        for i in range(1, retries+1):
            try:
                log.info(f"🔐 Login {i}/{retries}...")
                self.api  = SmartConnect(api_key=CFG["api_key"])
                totp      = pyotp.TOTP(CFG["totp_secret"]).now()
                resp      = self.api.generateSession(CFG["client_id"], CFG["password"], totp)
                if resp.get("status"):
                    self.auth_tok = resp["data"]["jwtToken"]
                    self.feed_tok = self.api.getfeedToken()
                    log.info(f"✅ Login OK — {CFG['client_id']}")
                    fs = FUND.summary()
                    self._tg(
                        f"🌿 Bloom Pro v5 — LIVE\n"
                        f"Capital: ₹{fs['total']:,.0f}\n"
                        f"Mode: {'📄 Paper' if CFG['paper_trade'] else '💰 Live'}\n"
                        f"Stocks: {len(CFG['watchlist'])} | Score: {CFG['min_score']}/8"
                    )   # FIX 8: missing closing ) for self._tg(
                    return True
                log.warning(f"Login fail: {resp.get('message')}")
            except Exception as e:
                log.error(f"Login err: {e}")
            time.sleep(5)
        return False

    def load_data(self):
        log.info("📥 Loading historical candles...")
        for s in CFG["watchlist"]:
            STORE.load_historical(self.api, s["symbol"], s["token"], s["exchange"])
        FEED.start_yahoo_loop()

    def place_order(self, sym, token, exchange, txn, qty) -> str:
        if CFG["paper_trade"] or not self.api:
            oid = f"PAPER_{sym}_{int(time.time())}"
            log.info(f"📄 PAPER {txn} {qty}×{sym} → {oid}")
            return oid
        try:
            r = self.api.placeOrder({
                "variety": "NORMAL", "tradingsymbol": sym,
                "symboltoken": token, "transactiontype": txn,
                "exchange": exchange, "ordertype": "MARKET",
                "producttype": CFG["product_type"], "duration": "DAY",
                "price": "0", "squareoff": "0", "stoploss": "0",
                "quantity": str(qty),
            })
            oid = r.get("data", {}).get("orderid") if r else None
            if oid: log.info(f"📦 Order {txn} {qty}×{sym} ID:{oid}")
            else:   log.error(f"Order fail {sym}: {r}")
            return oid
        except Exception as e:
            log.error(f"Order exc {sym}: {e}"); return None

    def process(self, sym: str, ltp: float):
        if not self.running or not is_market_open(): return

        if sym in FUND.positions:
            p = FUND.positions[sym]
            FUND.update_trail(sym, ltp)
            reason = None
            if   ltp <= p["trail"]: reason = "TRAILING SL"
            elif ltp <= p["sl"]:    reason = "STOP LOSS"
            elif ltp >= p["tp"]:    reason = "TAKE PROFIT"
            if reason:
                stk = self._get_stock(sym)
                if stk:
                    oid = self.place_order(sym, stk["token"], stk["exchange"], "SELL", p["qty"])
                    if oid:
                        rec   = FUND.close_pos(sym, ltp, reason)
                        emoji = "✅" if rec.get("pnl", 0) > 0 else "❌"
                        fs    = FUND.summary()
                        self._tg(
                            f"{emoji} {reason} — {sym}\n"
                            f"Entry ₹{rec['entry']} → Exit ₹{rec['exit']}\n"
                            f"Qty: {rec['qty']} | P&L: ₹{rec['pnl']:+.2f} ({rec['pnl_pct']:+.2f}%)\n"
                            f"Daily: ₹{fs['daily_pnl']:+,.2f} | Win Rate: {fs['win_rate']}%"
                        )   # FIX 9: missing ) for self._tg(
                return

        last = self.last_sig.get(sym)
        if last and (datetime.now(IST) - last).total_seconds() < CFG["cooldown_sec"]: return

        df = STORE.get_df(sym)
        if len(df) < 45: return

        signal, score, strat, dets = get_signal(df)
        if signal != "BUY": return

        ok, qty, why = FUND.can_trade(ltp)
        if not ok: log.debug(f"No trade {sym}: {why}"); return

        stk = self._get_stock(sym)
        if not stk: return

        sl    = ltp * (1 - CFG["sl_pct"]    / 100)
        tp    = ltp * (1 + CFG["tp_pct"]    / 100)
        trail = ltp * (1 - CFG["trail_pct"] / 100)
        tf    = (CFG["candle_tf"].replace("ONE_MINUTE","1M")
                                 .replace("FIVE_MINUTE","5M")
                                 .replace("FIFTEEN_MINUTE","15M"))

        oid = self.place_order(sym, stk["token"], stk["exchange"], "BUY", qty)
        if not oid: return

        FUND.open_pos(sym, ltp, qty, sl, tp, trail, strat, tf, score)
        self.last_sig[sym] = datetime.now(IST)

        entry_rec = {
            "id":       f"{sym}_ENTRY_{int(time.time())}",
            "time":     now_ist(), "date": today_ist(),
            "sym":      sym, "action": "BUY",
            "entry":    round(ltp, 2), "exit": None,
            "qty":      qty, "value": round(ltp*qty, 2),
            "pnl":      None, "pnl_pct": None,
            "strat":    strat, "tf": tf, "score": score,
            "reason":   f"Signal {score}/8", "status": "OPEN"
        }   # FIX 10: missing closing } for entry_rec dict

        FUND.trade_log.append(entry_rec)
        append_trade_csv(entry_rec)

        fs = FUND.summary()
        self._tg(
            f"🟢 BUY — {sym} [{tf}]\n"
            f"Price: ₹{ltp:,.2f} | Qty: {qty}\n"
            f"Value: ₹{ltp*qty:,.0f}\n"
            f"Score: {score}/8 | SL: ₹{sl:,.2f} | TP: ₹{tp:,.2f}\n"
            f"Strategy: {strat[:50]}\n"
            f"Deployed: ₹{fs['deployed']:,.0f} | Free: ₹{fs['available']:,.0f}"
        )   # FIX 11: missing ) for self._tg(

    def on_tick(self, wsapp, msg):
        try:
            if isinstance(msg, str): msg = json.loads(msg)
            tok  = str(msg.get("token", ""))
            ltp  = msg.get("last_traded_price", 0) / 100
            vol  = msg.get("volume_trade_for_the_day", 0)
            if ltp <= 0: return
            sym  = next((s["symbol"] for s in CFG["watchlist"] if s["token"]==tok), None)
            if not sym: return
            STORE.on_tick(sym, ltp, float(vol or 0))
            self.process(sym, ltp)
        except Exception as e:
            log.debug(f"Tick err: {e}")

    def start_ws(self):
        if not self.api:
            log.info("No Angel API — using Yahoo prices only")
            return
        try:
            self.ws = SmartWebSocketV2(self.auth_tok, CFG["api_key"], CFG["client_id"], self.feed_tok)
            self.ws.on_open = lambda w: (
                self.ws.subscribe("bloom_v5", 1, [{"exchangeType":1,"tokens":[s["token"] for s in CFG["watchlist"]]}]),
                log.info(f"⚡ WS Live — {len(CFG['watchlist'])} stocks"),
                setattr(FEED, "ws_ready", True)
            )   # FIX 12: missing ) for on_open lambda tuple
            self.ws.on_data  = self.on_tick
            self.ws.on_error = lambda w, e: log.error(f"WS: {e}")
            self.ws.on_close = lambda w, c, m: (
                setattr(FEED, "ws_ready", False),
                log.warning("WS closed, reconnecting..."),
                time.sleep(8), self.running and self.start_ws()
            )   # FIX 13: missing ) for on_close lambda tuple
            threading.Thread(target=self.ws.connect, daemon=True).start()
        except Exception as e:
            log.error(f"WS start: {e}")

    def square_off_all(self, reason="AUTO SQUAREOFF"):
        for sym in list(FUND.positions.keys()):
            p   = FUND.positions.get(sym)
            stk = self._get_stock(sym)
            if not p or not stk: continue
            ltp = FEED.get(sym) or p["entry"]
            oid = self.place_order(sym, stk["token"], stk["exchange"], "SELL", p["qty"])
            if oid:
                rec = FUND.close_pos(sym, ltp, reason)
                log.info(f"SQ {sym}: ₹{rec.get('pnl',0):+.2f}")
        self._daily_summary()

    def _daily_summary(self):
        fs  = FUND.summary()
        msg = (f"📋 Daily Summary — {today_ist()}\n"
               f"P&L: ₹{fs['daily_pnl']:+,.2f} ({fs['pnl_pct']:+.2f}%)\n"
               f"Trades: {fs['trades']} | Win Rate: {fs['win_rate']}%\n"
               f"W:{fs['wins']} L:{fs['losses']}\n"
               f"Capital: ₹{fs['available']:,.0f}")
        log.info(msg)
        self._tg(msg)

    def start(self):
        if self.running: return
        log.info("═"*56)
        log.info(" 🌿 BLOOM PRO v5 — Starting Bot Engine")
        log.info(f" Capital : ₹{CFG['total_capital']:,.0f}")
        log.info(f" Mode    : {'PAPER' if CFG['paper_trade'] else 'LIVE'}")
        log.info("═"*56)
        if not all([CFG["api_key"], CFG["client_id"], CFG["password"], CFG["totp_secret"]]):
            log.error("❌ Missing credentials!")
            CFG["paper_trade"] = True
        if not self.login(): return
        self.load_data()
        self.start_ws()
        self.running = True
        CFG["bot_running"] = True
        save_config(CFG)
        schedule.every().day.at("09:00").do(FUND.reset_daily)
        schedule.every().day.at("15:25").do(lambda: self.square_off_all("AUTO SQUAREOFF"))
        schedule.every().day.at("15:32").do(self._daily_summary)
        schedule.every(30).minutes.do(self._heartbeat)
        log.info("✅ Bot running!")

    def stop(self):
        self.running = False
        CFG["bot_running"] = False
        save_config(CFG)
        self.square_off_all("MANUAL STOP")
        log.info("🛑 Bot stopped")

    def _heartbeat(self):
        fs = FUND.summary()
        log.info(f"💓 P&L:₹{fs['daily_pnl']:+,.0f} T:{fs['trades']} Pos:{fs['positions']}")

    def _get_stock(self, sym):
        return next((s for s in CFG["watchlist"] if s["symbol"]==sym), None)

    def _tg(self, msg):
        if not CFG.get("send_telegram") or not CFG["tg_token"]: return
        try:
            requests.post(
                f"https://api.telegram.org/bot{CFG['tg_token']}/sendMessage",
                data={"chat_id": CFG["tg_chat_id"], "text": msg, "parse_mode": "HTML"},
                timeout=5
            )   # FIX 14: missing ) for requests.post(
        except Exception:
            pass


BOT = BotEngine()


# ═══════════════════════════════════════════════════════════════
# FLASK REST API
# FIX 15: HTML was served from root; need static/ subfolder
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__, static_folder="static")
CORS(app)

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/status")
def api_status():
    fs = FUND.summary()
    return jsonify({
        "bot_running":  BOT.running,
        "market_open":  is_market_open(),
        "paper_trade":  CFG["paper_trade"],
        "time_ist":     now_ist(),
        "date_ist":     today_ist(),
        "fund":         fs,
        "positions":    FUND.positions_list(),
        "prices":       {s["symbol"]: round(FEED.get(s["symbol"]),2) for s in CFG["watchlist"]},
        "ws_live":      FEED.ws_ready,
    })

@app.route("/api/start", methods=["POST"])
def api_start():
    if BOT.running:
        return jsonify({"ok": False, "msg": "Bot already running"})
    threading.Thread(target=BOT.start, daemon=True).start()
    return jsonify({"ok": True, "msg": "Bot starting..."})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not BOT.running:
        return jsonify({"ok": False, "msg": "Bot not running"})
    threading.Thread(target=BOT.stop, daemon=True).start()
    return jsonify({"ok": True, "msg": "Bot stopping..."})

@app.route("/api/squareoff", methods=["POST"])
def api_squareoff():
    threading.Thread(target=lambda: BOT.square_off_all("DASHBOARD EXIT"), daemon=True).start()
    return jsonify({"ok": True, "msg": "Squaring off all positions..."})

@app.route("/api/exit_position", methods=["POST"])
def api_exit_pos():
    data = request.json or {}
    sym  = data.get("sym", "")
    if sym not in FUND.positions:
        return jsonify({"ok": False, "msg": f"{sym} not in positions"})
    stk = BOT._get_stock(sym)
    if stk:
        p   = FUND.positions[sym]
        ltp = FEED.get(sym) or p["entry"]
        oid = BOT.place_order(sym, stk["token"], stk["exchange"], "SELL", p["qty"])
        if oid:
            rec = FUND.close_pos(sym, ltp, "DASHBOARD EXIT")
            return jsonify({"ok": True, "pnl": rec.get("pnl", 0)})
    return jsonify({"ok": False, "msg": "Exit failed"})

@app.route("/api/config", methods=["GET"])
def api_config_get():
    safe = {k: v for k, v in CFG.items()
            if k not in ("api_key","client_id","password","totp_secret","tg_token","tg_chat_id")}
    return jsonify(safe)

@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.json or {}
    skip = {"api_key","client_id","password","totp_secret"}
    for k, v in data.items():
        if k not in skip and k in CFG:
            CFG[k] = v
    save_config(CFG)
    FUND.reload_capital()
    return jsonify({"ok": True, "msg": "Config saved & applied live!"})

@app.route("/api/trades")
def api_trades():
    return jsonify(FUND.trade_log[-500:])

@app.route("/api/trades/csv")
def api_trades_csv():
    fname = f"trades_{datetime.now(IST).strftime('%Y%m%d')}.csv"
    if os.path.exists(fname):
        return send_from_directory(".", fname, as_attachment=True)
    return jsonify({"error": "No trade file today"})

@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    data = []
    for s in CFG["watchlist"]:
        sym  = s["symbol"]
        df   = STORE.get_df(sym)
        sig, score, strat, dets = get_signal(df) if len(df) >= 45 else ("HOLD", 0, "", {})
        data.append({**s, "ltp": round(FEED.get(sym),2), "signal": sig,
                     "score": score, "rsi": dets.get("rsi",0)})
    return jsonify(data)

@app.route("/api/candles/<sym>")
def api_candles(sym):
    df = STORE.get_df(sym)
    if df.empty: return jsonify([])
    return jsonify(df.tail(100).to_dict("records"))

@app.route("/api/indices")
def api_indices():
    data = FEED.fetch_nse_index()
    return jsonify(data)

@app.route("/api/add_stock", methods=["POST"])
def api_add_stock():
    data = request.json or {}
    sym  = data.get("symbol","").upper()
    tok  = data.get("token","")
    exch = data.get("exchange","NSE")
    if not sym or not tok:
        return jsonify({"ok":False,"msg":"symbol and token required"})
    if any(s["symbol"]==sym for s in CFG["watchlist"]):
        return jsonify({"ok":False,"msg":f"{sym} already in watchlist"})
    CFG["watchlist"].append({"symbol":sym,"token":tok,"exchange":exch})
    save_config(CFG)
    return jsonify({"ok":True,"msg":f"{sym} added"})

@app.route("/api/remove_stock", methods=["POST"])
def api_remove_stock():
    sym = (request.json or {}).get("symbol","").upper()
    CFG["watchlist"] = [s for s in CFG["watchlist"] if s["symbol"] != sym]
    save_config(CFG)
    return jsonify({"ok":True,"msg":f"{sym} removed"})


# ═══════════════════════════════════════════════════════════════
# SCHEDULER LOOP
# ═══════════════════════════════════════════════════════════════
def sched_loop():
    while True:
        schedule.run_pending()
        time.sleep(1)

threading.Thread(target=sched_loop, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# FIX 16: app.run() was completely missing from __main__ block
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"🌿 Bloom Pro v5 API starting on port {port}")
    if CFG.get("bot_running"):
        log.info("🔄 Auto-restarting bot (was running before)")
        threading.Thread(target=BOT.start, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False)
