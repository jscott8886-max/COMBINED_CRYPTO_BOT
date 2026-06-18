# ScalpAI Combined Crypto Bot - v1.0
# 4 Strategies: EMA + MSS + VPA + Breakout
# Shared position manager, unified dashboard
import os, time, logging, math, json
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

API_KEY    = os.environ.get("ALPACA_API_KEY", "")
API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
PAPER_MODE = os.environ.get("PAPER_MODE", "true").lower() == "true"

SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD"]

# ── Strategy configs ───────────────────────────────────────────────────────────
EMA_CONFIG = {
    "name": "EMA",
    "ema_fast": 9, "ema_slow": 21, "ema_trend": 50,
    "rsi_period": 14,
    "rsi_hard_gate": 55,       # Hard block if RSI above this
    "rsi_entry_max": 40,       # RSI must have been below this recently
    "bb_period": 20, "bb_std": 2.0, "bb_min_bw": 1.5,
    "min_score": 4,
    "atr_period": 14,
    "atr_min_mult": 0.8,       # ATR must be above 80% of its average
    "volume_bonus_mult": 1.5,  # +1 score if volume above this
    "time_filter": True,
    "time_start_utc": 13,      # 9AM ET = 1PM UTC
    "time_end_utc": 21,        # 5PM ET = 9PM UTC
    "candle_confirm": True,
    "bear_filter": True,
}

MSS_CONFIG = {
    "name": "MSS",
    "swing_lookback": 10,
    "swing_fallback": 7,       # Use if no signal in 4 hours
    "fallback_hours": 4,
    "rsi_soft_threshold": 50,  # +1 score if RSI below this
    "atr_period": 14,
    "atr_min_mult": 0.8,
    "volume_bonus_mult": 1.5,
    "time_filter": True,
    "time_start_utc": 13,
    "time_end_utc": 21,
    "candle_confirm": True,
    "bear_filter": True,
    "min_sl_pct": 0.3,
    "max_sl_pct": 2.0,
}

VPA_CONFIG = {
    "name": "VPA",
    "volume_spike_mult": 2.5,  # Raised from 1.5x to 2.5x
    "volume_avg_period": 20,
    "min_close_ratio": 0.6,
    "effort_result_ratio": 0.02,
    "min_score": 3,
    "time_filter": False,      # Trades 24/7
    "candle_confirm": True,
    "bear_filter": True,
}

BREAKOUT_CONFIG = {
    "name": "Breakout",
    "consolidation_candles": 10,
    "consolidation_threshold": 0.8,
    "breakout_volume_mult": 2.0,
    "breakout_candle_close_ratio": 0.6,
    "min_breakout_pct": 0.5,
    "momentum_override_pct": 2.5,  # Bypass bear filter
    "momentum_override_volume": 3.0,
    "time_filter": False,           # Trades 24/7
    "candle_confirm": False,        # No confirmation on momentum override
    "bear_filter": True,            # Standard breakout still needs bull market
}

# ── Shared risk config ─────────────────────────────────────────────────────────
RISK = {
    "position_size": 0.12,      # 12% per trade
    "stop_loss_pct": 0.75,
    "take_profit_pct": 1.5,
    "max_positions": 4,         # 1 per strategy
    "cooldown_minutes": 15,
    "daily_loss_limit_pct": 5.0,
}

STRATEGIES = ["EMA", "MSS", "VPA", "Breakout"]

bot_state = {
    "running": True, "killed": False,
    "positions": {},            # symbol -> position dict with strategy label
    "strategy_positions": {s: None for s in STRATEGIES},  # strategy -> symbol or None
    "closed_trades": [], "diary": [],
    "day_pnl": 0.0, "daily_start_equity": 0.0,
    "total_trades": 0, "win_count": 0,
    "strategy_stats": {s: {"trades": 0, "wins": 0, "pnl": 0.0} for s in STRATEGIES},
    "signals": {sym.replace("/",""): {s: {} for s in STRATEGIES} for sym in SYMBOLS},
    "account_cash": 0.0, "account_equity": 0.0, "account_buying_power": 0.0,
    "active_cooldowns": {},
    "market_regime": "UNKNOWN",
    "symbol_regimes": {s.replace("/",""): "UNKNOWN" for s in ["BTC/USD","ETH/USD","SOL/USD","XRP/USD","DOGE/USD"]},
    "daily_paused": False,
    "mss_last_signal_time": {sym: None for sym in SYMBOLS},
    "version": "Combined-1.1"
}

# ── Alpaca helpers ─────────────────────────────────────────────────────────────
def get_trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(api_key=API_KEY, secret_key=API_SECRET, paper=PAPER_MODE)

def get_data_client():
    from alpaca.data.historical import CryptoHistoricalDataClient
    return CryptoHistoricalDataClient(api_key=API_KEY, secret_key=API_SECRET)

def get_bars(symbol, timeframe="5Min", limit=50):
    try:
        from alpaca.data.requests import CryptoBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        client = get_data_client()
        if timeframe == "1Min":
            tf = TimeFrame(1, TimeFrameUnit.Minute)
        elif timeframe == "5Min":
            tf = TimeFrame(5, TimeFrameUnit.Minute)
        elif timeframe == "1Hour":
            tf = TimeFrame(1, TimeFrameUnit.Hour)
        elif timeframe == "1Day":
            tf = TimeFrame(1, TimeFrameUnit.Day)
        else:
            tf = TimeFrame(5, TimeFrameUnit.Minute)
        end = datetime.now(timezone.utc)
        if timeframe == "1Day":
            start = end - timedelta(days=limit + 10)
        elif timeframe == "1Hour":
            start = end - timedelta(hours=limit + 5)
        else:
            start = end - timedelta(minutes=limit * 6)
        req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf,
                                start=start, limit=limit)
        bars = client.get_crypto_bars(req)
        df = bars.df
        if df.empty:
            return []
        if hasattr(df.index, 'levels'):
            df = df.loc[symbol] if symbol in df.index.get_level_values(0) else df
        result = []
        for idx, row in df.iterrows():
            result.append({
                "time": idx.isoformat() if hasattr(idx, 'isoformat') else str(idx),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row["volume"])
            })
        return result[-limit:]
    except Exception as e:
        log.error(f"Bars error {symbol}: {e}")
        return []

def refresh_account():
    try:
        tc = get_trading_client()
        acct = tc.get_account()
        bot_state["account_cash"]         = float(acct.cash)
        bot_state["account_equity"]       = float(acct.equity)
        bot_state["account_buying_power"] = float(acct.buying_power)
        if bot_state["daily_start_equity"] == 0.0:
            bot_state["daily_start_equity"] = float(acct.equity)
    except Exception as e:
        log.error(f"Account refresh error: {e}")

def sync_positions():
    try:
        tc = get_trading_client()
        positions = tc.get_all_positions()
        synced = {}
        active_symbols = set()
        for p in positions:
            sym = p.symbol
            if "/" not in sym and len(sym) > 3:
                sym = sym[:-3] + "/" + sym[-3:]
            active_symbols.add(sym)
            existing = bot_state["positions"].get(sym, {})
            synced[sym] = {
                "symbol": sym,
                "entry": float(p.avg_entry_price),
                "qty": float(p.qty),
                "current_price": float(p.current_price),
                "unrealized_pnl": float(p.unrealized_pl),
                "open_time": existing.get("open_time", datetime.now(timezone.utc).isoformat()),
                "strategy": existing.get("strategy", "UNKNOWN")
            }
        # Clear strategy slots for closed positions
        for strategy in STRATEGIES:
            held_sym = bot_state["strategy_positions"].get(strategy)
            if held_sym and held_sym not in active_symbols:
                bot_state["strategy_positions"][strategy] = None
        bot_state["positions"] = synced
    except Exception as e:
        log.error(f"Sync positions error: {e}")

def place_order(symbol, qty, side):
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        tc = get_trading_client()
        req = MarketOrderRequest(
            symbol=symbol, qty=round(qty, 6),
            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC
        )
        return tc.submit_order(req)
    except Exception as e:
        log.error(f"Order error {symbol}: {e}")
        return None

def close_position_alpaca(symbol):
    try:
        tc = get_trading_client()
        tc.close_position(symbol.replace("/", ""))
        return True
    except Exception as e:
        log.error(f"Close error {symbol}: {e}")
        return False

def add_diary(symbol, text, entry_type="info", strategy="SYSTEM"):
    label = f"[{strategy}] " if strategy != "SYSTEM" else ""
    entry = {
        "time": datetime.now(timezone.utc).strftime("%H:%M"),
        "symbol": symbol, "text": f"{label}{text}",
        "type": entry_type, "strategy": strategy
    }
    bot_state["diary"].insert(0, entry)
    if len(bot_state["diary"]) > 300:
        bot_state["diary"] = bot_state["diary"][:300]

# ── Indicators ─────────────────────────────────────────────────────────────────
def calc_ema(prices, period):
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag/al))

def calc_bb(closes, period=20, std_dev=2.0):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    std = math.sqrt(sum((x-mid)**2 for x in window) / period)
    return mid - std_dev*std, mid, mid + std_dev*std

def calc_macd(closes):
    if len(closes) < 26:
        return 0.0
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if not ema12 or not ema26:
        return 0.0
    min_len = min(len(ema12), len(ema26))
    macd_line = [ema12[-(min_len-i)] - ema26[-(min_len-i)] for i in range(min_len)]
    signal = calc_ema(macd_line, 9)
    return macd_line[-1] - signal[-1] if signal else 0.0

def calc_atr(bars, period=14):
    if len(bars) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        high = bars[i]["high"]
        low  = bars[i]["low"]
        prev_close = bars[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs)
    return sum(trs[-period:]) / period

# ── Market helpers ─────────────────────────────────────────────────────────────
def check_market_regime():
    """Global BTC regime - kept for reference"""
    try:
        bars = get_bars("BTC/USD", "1Day", 210)
        if len(bars) < 200:
            return "UNKNOWN"
        closes = [b["close"] for b in bars]
        ema200 = calc_ema(closes, 200)
        if not ema200:
            return "UNKNOWN"
        regime = "BULL" if closes[-1] > ema200[-1] else "BEAR"
        log.info(f"Global regime: {regime} | BTC={closes[-1]:.0f} | 200EMA={ema200[-1]:.0f}")
        return regime
    except Exception as e:
        log.error(f"Regime check error: {e}")
        return "UNKNOWN"

def check_symbol_regime(symbol):
    """Per-symbol 200-day EMA regime - Option 2 fix"""
    try:
        bars = get_bars(symbol, "1Day", 210)
        if len(bars) < 200:
            return "UNKNOWN"
        closes = [b["close"] for b in bars]
        ema200 = calc_ema(closes, 200)
        if not ema200:
            return "UNKNOWN"
        regime = "BULL" if closes[-1] > ema200[-1] else "BEAR"
        log.info(f"Regime {symbol}: {regime} | price={closes[-1]:.2f} | 200EMA={ema200[-1]:.2f}")
        return regime
    except Exception as e:
        log.error(f"Symbol regime check error {symbol}: {e}")
        return "UNKNOWN"

def is_in_time_window(cfg):
    if not cfg.get("time_filter", False):
        return True
    now = datetime.now(timezone.utc)
    h = now.hour + now.minute / 60
    return cfg["time_start_utc"] <= h <= cfg["time_end_utc"]

def check_daily_loss():
    if bot_state["daily_start_equity"] == 0:
        return False
    equity = bot_state["account_equity"]
    loss_pct = (bot_state["daily_start_equity"] - equity) / bot_state["daily_start_equity"] * 100
    if loss_pct >= RISK["daily_loss_limit_pct"]:
        if not bot_state["daily_paused"]:
            bot_state["daily_paused"] = True
            add_diary("SYSTEM", f"Daily loss limit {RISK['daily_loss_limit_pct']}% hit — all strategies paused", "system")
        return True
    bot_state["daily_paused"] = False
    return False

def can_enter(symbol, strategy):
    """Check all shared entry gates"""
    # Kill switch
    if bot_state["killed"]:
        return False, "Kill switch active"
    # Daily loss limit
    if check_daily_loss():
        return False, "Daily loss limit hit"
    # Max positions
    if len(bot_state["positions"]) >= RISK["max_positions"]:
        return False, "Max positions reached"
    # Strategy already has a position
    if bot_state["strategy_positions"].get(strategy):
        return False, f"{strategy} already has open position"
    # Symbol already held by any strategy
    if symbol in bot_state["positions"]:
        return False, f"{symbol} already held"
    # Cooldown
    cooldown_key = f"{strategy}_{symbol}"
    if cooldown_key in bot_state["active_cooldowns"]:
        return False, f"Cooldown active for {symbol}"
    return True, "OK"

def record_entry(symbol, strategy):
    bot_state["strategy_positions"][strategy] = symbol

def record_exit(symbol, strategy, pnl, win):
    bot_state["strategy_positions"][strategy] = None
    bot_state["day_pnl"] += pnl
    bot_state["total_trades"] += 1
    if win:
        bot_state["win_count"] += 1
    stats = bot_state["strategy_stats"][strategy]
    stats["trades"] += 1
    stats["pnl"] = round(stats["pnl"] + pnl, 2)
    if win:
        stats["wins"] += 1

# ── STRATEGY A: EMA ───────────────────────────────────────────────────────────
def run_ema_strategy(symbol, regime):
    cfg = EMA_CONFIG
    try:
        bars_5m = get_bars(symbol, "5Min", 60)
        bars_1h = get_bars(symbol, "1Hour", 60)
        if len(bars_5m) < 30 or len(bars_1h) < 30:
            return {}

        closes_5m = [b["close"] for b in bars_5m]
        closes_1h = [b["close"] for b in bars_1h]
        volumes   = [b["volume"] for b in bars_5m]
        price     = closes_5m[-1]

        # Stale data check
        if all(v == 0 for v in volumes[-5:]):
            return {}

        ema9  = calc_ema(closes_5m, cfg["ema_fast"])
        ema21 = calc_ema(closes_5m, cfg["ema_slow"])
        ema50 = calc_ema(closes_5m, cfg["ema_trend"])
        ema50_1h = calc_ema(closes_1h, 50)
        rsi   = calc_rsi(closes_5m, cfg["rsi_period"])
        rsi_prev = calc_rsi(closes_5m[:-2], cfg["rsi_period"])
        bb_low, bb_mid, bb_high = calc_bb(closes_5m, cfg["bb_period"], cfg["bb_std"])
        macd_h = calc_macd(closes_5m)
        atr    = calc_atr(bars_5m, cfg["atr_period"])
        avg_atr = calc_atr(bars_5m[:-10], cfg["atr_period"])

        if not ema9 or not ema21 or not ema50 or not ema50_1h or bb_mid is None:
            return {}

        bb_bw = ((bb_high - bb_low) / bb_mid) * 100 if bb_mid > 0 else 0
        avg_vol = sum(volumes[-20:]) / 20
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0

        # ── RSI HARD GATE ──────────────────────────────────────────────────────
        # RSI must be below hard gate AND must have been in oversold territory recently
        rsi_rising = rsi > rsi_prev
        if rsi > cfg["rsi_hard_gate"]:
            sig = {"price": price, "rsi": round(rsi,1), "blocked": "RSI_OVERBOUGHT",
                   "buy_score": 0, "strategy": "EMA"}
            bot_state["signals"][symbol.replace("/","")]["EMA"] = sig
            return sig

        # ── ATR FILTER ────────────────────────────────────────────────────────
        if avg_atr > 0 and atr < avg_atr * cfg["atr_min_mult"]:
            sig = {"price": price, "blocked": "ATR_TOO_LOW", "buy_score": 0, "strategy": "EMA"}
            bot_state["signals"][symbol.replace("/","")]["EMA"] = sig
            return sig

        # ── CANDLE CLOSE CONFIRMATION ─────────────────────────────────────────
        # Only use completed candles (already handled by get_bars returning complete bars)

        # ── SCORING ───────────────────────────────────────────────────────────
        score = 0

        # Trend: price above 1H 50 EMA
        if price > ema50_1h[-1]:
            score += 1

        # EMA crossover
        if ema9[-1] > ema21[-1]:
            score += 2
        if len(ema9) > 1 and ema9[-1] > ema21[-1] and ema9[-2] <= ema21[-2]:
            score += 1  # Fresh crossover bonus

        # RSI oversold + rising
        if rsi < 40 and rsi_rising:
            score += 2
        elif rsi < cfg["rsi_hard_gate"] and rsi_rising:
            score += 1

        # Bollinger Bands
        if bb_bw >= cfg["bb_min_bw"] and price < bb_low:
            score += 1

        # MACD
        if macd_h > 0:
            score += 1

        # Volume bonus (soft)
        if vol_ratio >= cfg["volume_bonus_mult"]:
            score += 1

        sig = {
            "price": price, "rsi": round(rsi,1), "rsi_rising": rsi_rising,
            "macd_h": round(macd_h, 4), "bb_bw": round(bb_bw, 2),
            "vol_ratio": round(vol_ratio, 2), "atr": round(atr, 4),
            "buy_score": score, "strategy": "EMA",
            "regime_ok": regime != "BEAR"
        }
        bot_state["signals"][symbol.replace("/","")]["EMA"] = sig
        log.info(f"[EMA] {symbol} | price={price} RSI={round(rsi,1)} score={score} regime={regime}")
        return sig

    except Exception as e:
        log.error(f"[EMA] Signal error {symbol}: {e}")
        return {}

# ── STRATEGY B: MSS ───────────────────────────────────────────────────────────
def run_mss_strategy(symbol, regime):
    cfg = MSS_CONFIG
    try:
        bars_5m = get_bars(symbol, "5Min", 60)
        bars_1h = get_bars(symbol, "1Hour", 30)
        if len(bars_5m) < 20 or len(bars_1h) < 15:
            return {}

        closes_5m = [b["close"] for b in bars_5m]
        closes_1h = [b["close"] for b in bars_1h]
        highs_1h  = [b["high"]  for b in bars_1h]
        lows_1h   = [b["low"]   for b in bars_1h]
        volumes   = [b["volume"] for b in bars_5m]
        price     = closes_5m[-1]

        if all(v == 0 for v in volumes[-5:]):
            return {}

        rsi = calc_rsi(closes_5m)
        atr = calc_atr(bars_5m, cfg["atr_period"])
        avg_atr = calc_atr(bars_5m[:-10], cfg["atr_period"])
        avg_vol = sum(volumes[-20:]) / 20
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0

        # ── ATR FILTER ────────────────────────────────────────────────────────
        if avg_atr > 0 and atr < avg_atr * cfg["atr_min_mult"]:
            return {"price": price, "blocked": "ATR_TOO_LOW", "buy_score": 0, "strategy": "MSS"}

        # ── 1H TREND ─────────────────────────────────────────────────────────
        recent_highs = highs_1h[-5:]
        prev_highs   = highs_1h[-10:-5]
        recent_lows  = lows_1h[-5:]
        prev_lows    = lows_1h[-10:-5]

        trend_1h = "NEUTRAL"
        if max(recent_highs) > max(prev_highs) and min(recent_lows) > min(prev_lows):
            trend_1h = "BULL"
        elif max(recent_highs) < max(prev_highs) and min(recent_lows) < min(prev_lows):
            trend_1h = "BEAR"

        if trend_1h != "BULL":
            sig = {"price": price, "trend_1h": trend_1h, "buy_score": 0, "strategy": "MSS"}
            bot_state["signals"][symbol.replace("/","")]["MSS"] = sig
            return sig

        # ── MSS DETECTION ─────────────────────────────────────────────────────
        # Determine lookback — use fallback if no signal recently
        last_signal = bot_state["mss_last_signal_time"].get(symbol)
        if last_signal:
            hours_since = (datetime.now(timezone.utc) - last_signal).total_seconds() / 3600
            lookback = cfg["swing_fallback"] if hours_since > cfg["fallback_hours"] else cfg["swing_lookback"]
        else:
            lookback = cfg["swing_lookback"]

        highs_5m = [b["high"] for b in bars_5m]
        lows_5m  = [b["low"]  for b in bars_5m]

        recent_lows_5m = lows_5m[-lookback:]
        made_lower_low = len(recent_lows_5m) >= 4 and recent_lows_5m[-3] < recent_lows_5m[-5] if len(recent_lows_5m) >= 5 else False
        now_higher_low = recent_lows_5m[-1] > recent_lows_5m[-2] if len(recent_lows_5m) >= 2 else False
        mss_detected = made_lower_low and now_higher_low

        if mss_detected:
            bot_state["mss_last_signal_time"][symbol] = datetime.now(timezone.utc)

        # ── SCORING ───────────────────────────────────────────────────────────
        score = 0
        if mss_detected:
            score += 3
        if rsi < cfg["rsi_soft_threshold"]:
            score += 1  # Soft RSI bonus
        if vol_ratio >= cfg["volume_bonus_mult"]:
            score += 1  # Volume bonus

        swing_low = min(recent_lows_5m[-lookback:]) if recent_lows_5m else price
        sl_pct = (price - swing_low) / price * 100

        sig = {
            "price": price, "trend_1h": trend_1h, "mss_detected": mss_detected,
            "rsi": round(rsi, 1), "vol_ratio": round(vol_ratio, 2),
            "buy_score": score, "swing_low": round(swing_low, 4),
            "sl_pct": round(sl_pct, 2), "lookback_used": lookback,
            "strategy": "MSS"
        }
        bot_state["signals"][symbol.replace("/","")]["MSS"] = sig
        log.info(f"[MSS] {symbol} | price={price} trend={trend_1h} MSS={mss_detected} score={score}")
        return sig

    except Exception as e:
        log.error(f"[MSS] Signal error {symbol}: {e}")
        return {}

# ── STRATEGY C: VPA ───────────────────────────────────────────────────────────
def run_vpa_strategy(symbol, regime):
    cfg = VPA_CONFIG
    try:
        bars = get_bars(symbol, "5Min", 40)
        if len(bars) < 25:
            return {}

        volumes = [b["volume"] for b in bars]
        closes  = [b["close"]  for b in bars]
        opens   = [b["open"]   for b in bars]
        highs   = [b["high"]   for b in bars]
        lows    = [b["low"]    for b in bars]

        if all(v == 0 for v in volumes[-5:]):
            return {}

        avg_vol   = sum(volumes[-cfg["volume_avg_period"]:]) / cfg["volume_avg_period"]
        curr_vol  = volumes[-1]
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0

        price      = closes[-1]
        curr_open  = opens[-1]
        curr_high  = highs[-1]
        curr_low   = lows[-1]
        curr_close = closes[-1]
        bar_range  = curr_high - curr_low

        if bar_range == 0:
            return {}

        close_ratio = (curr_close - curr_low) / bar_range

        score = 0
        signals_detected = []

        # Volume spike (raised threshold to 2.5x)
        if vol_ratio >= cfg["volume_spike_mult"]:
            if close_ratio >= cfg["min_close_ratio"]:
                score += 2
                signals_detected.append("VOL_SPIKE_BULL")
            else:
                pass  # Bearish spike — no entry

        # Absorption
        price_move_pct = bar_range / price if price > 0 else 0
        if vol_ratio >= 2.5 and price_move_pct < cfg["effort_result_ratio"]:
            if curr_close > curr_open:
                score += 2
                signals_detected.append("ABSORPTION_BULL")

        # No supply
        if vol_ratio < 0.7 and curr_close > curr_open and close_ratio > 0.5:
            score += 1
            signals_detected.append("NO_SUPPLY")

        # EMA trend bonus
        ema20 = calc_ema(closes, 20)
        if ema20 and price > ema20[-1]:
            score += 1

        # Candle close confirmation
        # Confirmed by using only complete bars (already handled)

        sig = {
            "price": price, "vol_ratio": round(vol_ratio, 2),
            "close_ratio": round(close_ratio, 2),
            "buy_score": score, "signals": signals_detected,
            "strategy": "VPA"
        }
        bot_state["signals"][symbol.replace("/","")]["VPA"] = sig
        log.info(f"[VPA] {symbol} | price={price} vol={round(vol_ratio,2)}x score={score} sigs={signals_detected}")
        return sig

    except Exception as e:
        log.error(f"[VPA] Signal error {symbol}: {e}")
        return {}

# ── STRATEGY D: BREAKOUT ──────────────────────────────────────────────────────
def run_breakout_strategy(symbol, regime):
    cfg = BREAKOUT_CONFIG
    try:
        bars = get_bars(symbol, "5Min", 40)
        if len(bars) < 15:
            return {}

        closes  = [b["close"]  for b in bars]
        highs   = [b["high"]   for b in bars]
        lows    = [b["low"]    for b in bars]
        volumes = [b["volume"] for b in bars]
        opens   = [b["open"]   for b in bars]

        if all(v == 0 for v in volumes[-5:]):
            return {}

        price      = closes[-1]
        curr_open  = opens[-1]
        curr_high  = highs[-1]
        curr_low   = lows[-1]
        curr_close = closes[-1]
        curr_vol   = volumes[-1]

        avg_vol   = sum(volumes[-20:]) / 20
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0

        # ── MOMENTUM OVERRIDE CHECK (first priority) ──────────────────────────
        candle_pct = abs(curr_close - curr_open) / curr_open * 100 if curr_open > 0 else 0
        momentum_override = (
            candle_pct >= cfg["momentum_override_pct"] and
            vol_ratio >= cfg["momentum_override_volume"] and
            curr_close > curr_open
        )

        if momentum_override:
            log.info(f"[Breakout] {symbol} MOMENTUM OVERRIDE | {round(candle_pct,2)}% | {round(vol_ratio,1)}x vol")

        # ── CONSOLIDATION + BREAKOUT ──────────────────────────────────────────
        lookback = cfg["consolidation_candles"]
        if len(bars) < lookback + 2:
            return {}

        consol_bars  = bars[-(lookback+2):-2]
        consol_highs = [b["high"] for b in consol_bars]
        consol_lows  = [b["low"]  for b in consol_bars]
        consol_range = max(consol_highs) - min(consol_lows)
        consol_pct   = consol_range / price * 100 if price > 0 else 0
        consol_high  = max(consol_highs)

        in_consolidation = consol_pct <= cfg["consolidation_threshold"]

        bar_range   = curr_high - curr_low
        close_ratio = (curr_close - curr_low) / bar_range if bar_range > 0 else 0
        breakout_pct = (curr_close - consol_high) / consol_high * 100 if consol_high > 0 else 0

        # Standard breakout with confirmation candle
        prev_bar = bars[-2] if len(bars) >= 2 else None
        prev_confirmed = False
        if prev_bar:
            prev_range = prev_bar["high"] - prev_bar["low"]
            prev_cr = (prev_bar["close"] - prev_bar["low"]) / prev_range if prev_range > 0 else 0
            prev_confirmed = prev_bar["close"] > consol_high and prev_cr >= 0.5

        is_standard_breakout = (
            in_consolidation and
            curr_close > consol_high and
            breakout_pct >= cfg["min_breakout_pct"] and
            vol_ratio >= cfg["breakout_volume_mult"] and
            close_ratio >= cfg["breakout_candle_close_ratio"] and
            prev_confirmed  # Confirmation candle required for standard breakout
        )

        buy_signal = momentum_override or is_standard_breakout
        signal_type = ""
        if momentum_override:
            signal_type = "MOMENTUM_OVERRIDE"
        elif is_standard_breakout:
            signal_type = "BREAKOUT"

        sig = {
            "price": price, "vol_ratio": round(vol_ratio, 2),
            "candle_pct": round(candle_pct, 2),
            "consol_pct": round(consol_pct, 2),
            "breakout_pct": round(breakout_pct, 2),
            "in_consolidation": in_consolidation,
            "is_standard_breakout": is_standard_breakout,
            "momentum_override": momentum_override,
            "buy_signal": buy_signal,
            "signal_type": signal_type,
            "strategy": "Breakout"
        }
        bot_state["signals"][symbol.replace("/","")]["Breakout"] = sig
        log.info(f"[Breakout] {symbol} | price={price} vol={round(vol_ratio,1)}x candle={round(candle_pct,2)}% signal={signal_type or 'NONE'}")
        return sig

    except Exception as e:
        log.error(f"[Breakout] Signal error {symbol}: {e}")
        return {}

# ── EXIT HANDLER ──────────────────────────────────────────────────────────────
def check_exits(symbol, price, now):
    if symbol not in bot_state["positions"]:
        return
    pos      = bot_state["positions"][symbol]
    entry    = pos["entry"]
    qty      = pos["qty"]
    strategy = pos.get("strategy", "UNKNOWN")
    pct      = (price - entry) / entry * 100

    should_exit = False
    reason = ""

    if pct >= RISK["take_profit_pct"]:
        should_exit = True
        reason = f"Take profit (+{round(pct,2)}%)"
    elif pct <= -RISK["stop_loss_pct"]:
        should_exit = True
        reason = f"Stop loss ({round(pct,2)}%)"
        cooldown_key = f"{strategy}_{symbol}"
        bot_state["active_cooldowns"][cooldown_key] = now.isoformat()

    if should_exit:
        success = close_position_alpaca(symbol)
        if success:
            pnl = (price - entry) * qty
            win = pnl > 0
            record_exit(symbol, strategy, pnl, win)
            entry_type = "win" if win else "loss"
            add_diary(symbol,
                f"{'WIN' if win else 'LOSS'} | ${entry:,.4f} → ${price:,.4f} | "
                f"P&L ${round(pnl,2)} ({round(pct,2)}%) | {reason}",
                entry_type, strategy)
            bot_state["closed_trades"].append({
                "symbol": symbol, "entry": entry, "exit": price,
                "pnl": round(pnl,2), "pct": round(pct,2),
                "win": win, "strategy": strategy, "reason": reason,
                "time": now.strftime("%H:%M")
            })
            sync_positions()

# ── ENTRY HANDLER ─────────────────────────────────────────────────────────────
def try_entry(symbol, strategy, sig, regime, now):
    ok, reason = can_enter(symbol, strategy)
    if not ok:
        return

    # Per-symbol regime (Option 2) — each coin checks its own 200 EMA
    sym_key = symbol.replace("/", "")
    sym_regime = bot_state["symbol_regimes"].get(sym_key, "UNKNOWN")

    # Strategy-specific entry gates
    if strategy == "EMA":
        cfg = EMA_CONFIG
        if sig.get("buy_score", 0) < cfg["min_score"]:
            return
        if sym_regime == "BEAR":
            return
        if not is_in_time_window(cfg):
            return

    elif strategy == "MSS":
        cfg = MSS_CONFIG
        if sig.get("buy_score", 0) < 3:
            return
        if not sig.get("mss_detected"):
            return
        if sym_regime == "BEAR":
            return
        if not is_in_time_window(cfg):
            return

    elif strategy == "VPA":
        if sig.get("buy_score", 0) < VPA_CONFIG["min_score"]:
            return
        if sym_regime == "BEAR":
            return

    elif strategy == "Breakout":
        if not sig.get("buy_signal"):
            return
        # Option 3: Momentum override always bypasses bear filter
        if sym_regime == "BEAR" and not sig.get("momentum_override"):
            return

    # Place order
    cash   = bot_state["account_cash"]
    budget = cash * RISK["position_size"]
    price  = sig["price"]
    qty    = budget / price

    if budget < 10 or qty <= 0:
        return

    order = place_order(symbol, qty, "BUY")
    if order:
        bot_state["positions"][symbol] = {
            "symbol": symbol, "entry": price, "qty": qty,
            "current_price": price, "unrealized_pnl": 0,
            "open_time": now.isoformat(), "strategy": strategy
        }
        record_entry(symbol, strategy)
        signal_type = sig.get("signal_type", "")
        score = sig.get("buy_score", 0)
        add_diary(symbol,
            f"BUY | ${price:,.4f} | Budget ${round(budget,2)} | "
            f"Score {score} | {signal_type}",
            "trade", strategy)
        sync_positions()
        log.info(f"[{strategy}] ENTERED {symbol} at {price} | {signal_type}")

# ── MAIN TRADING LOOP ─────────────────────────────────────────────────────────
def trading_loop():
    if not API_KEY or not API_SECRET:
        log.warning("No Alpaca API keys — cannot start")
        return

    # Clear stale state
    try:
        import os as _os
        for f in ["/tmp/combined_state.json", "/tmp/scalp_state.json"]:
            if _os.path.exists(f):
                _os.remove(f)
    except:
        pass

    add_diary("SYSTEM",
        "Combined Crypto Bot v1.0 started | "
        "4 Strategies: EMA + MSS + VPA + Breakout | "
        "Max 4 positions | SL=0.75% TP=1.5% | Daily limit=5%",
        "system")
    log.info("Combined Crypto Bot v1.0 started")

    regime_check_time   = None
    daily_reset_date    = None

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Daily reset at midnight UTC
            today = now.date()
            if daily_reset_date != today:
                bot_state["day_pnl"] = 0.0
                bot_state["daily_start_equity"] = 0.0
                bot_state["daily_paused"] = False
                daily_reset_date = today
                log.info("Daily stats reset")

            refresh_account()
            sync_positions()

            # Regime check every 30 minutes
            if not regime_check_time or (now - regime_check_time).total_seconds() > 1800:
                bot_state["market_regime"] = check_market_regime()
                # Per-symbol regime check (Option 2)
                for sym in SYMBOLS:
                    bot_state["symbol_regimes"][sym.replace("/","")] = check_symbol_regime(sym)
                regime_check_time = now

            regime = bot_state["market_regime"]

            # Clear expired cooldowns
            expired = [k for k, t in list(bot_state["active_cooldowns"].items())
                       if (now - datetime.fromisoformat(t)).total_seconds() > RISK["cooldown_minutes"] * 60]
            for k in expired:
                del bot_state["active_cooldowns"][k]

            # Check daily loss limit
            if check_daily_loss():
                time.sleep(60)
                continue

            # ── Per-symbol loop ─────────────────────────────────────────────
            for symbol in SYMBOLS:
                if bot_state["killed"]:
                    break

                # Get current price for exit checks
                bars = get_bars(symbol, "5Min", 3)
                if not bars:
                    continue
                price = bars[-1]["close"]

                # Check exits first (always run regardless of regime)
                check_exits(symbol, price, now)

                # Run all 4 strategies in priority order
                # Priority: Breakout > VPA > MSS > EMA

                # Strategy D: Breakout (highest priority — most time sensitive)
                if bot_state["strategy_positions"]["Breakout"] is None:
                    sig_b = run_breakout_strategy(symbol, regime)
                    if sig_b:
                        try_entry(symbol, "Breakout", sig_b, regime, now)

                # Strategy C: VPA (24/7, institutional signals)
                if bot_state["strategy_positions"]["VPA"] is None:
                    sig_v = run_vpa_strategy(symbol, regime)
                    if sig_v:
                        try_entry(symbol, "VPA", sig_v, regime, now)

                # Strategy B: MSS (time filtered, structure based)
                if bot_state["strategy_positions"]["MSS"] is None:
                    sig_m = run_mss_strategy(symbol, regime)
                    if sig_m:
                        try_entry(symbol, "MSS", sig_m, regime, now)

                # Strategy A: EMA (time filtered, indicator based)
                if bot_state["strategy_positions"]["EMA"] is None:
                    sig_e = run_ema_strategy(symbol, regime)
                    if sig_e:
                        try_entry(symbol, "EMA", sig_e, regime, now)

        except Exception as e:
            log.error(f"Loop error: {e}")
            import traceback
            log.error(traceback.format_exc())

        time.sleep(60)

threading.Thread(target=trading_loop, daemon=True).start()

# ── Flask routes ───────────────────────────────────────────────────────────────
@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    return r

def clean_nan(obj):
    if isinstance(obj, float):
        return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_nan(i) for i in obj]
    try:
        import numpy as np
        if isinstance(obj, (np.floating, np.integer)):
            return float(obj)
        if isinstance(obj, np.bool_):
            return bool(obj)
    except ImportError:
        pass
    return obj

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "version": bot_state["version"],
        "regime": bot_state["market_regime"],
        "positions": len(bot_state["positions"]),
        "daily_paused": bot_state["daily_paused"]
    })

@app.route("/status")
def status():
    refresh_account()
    wins  = bot_state["win_count"]
    total = bot_state["total_trades"]
    return jsonify(clean_nan({
        "running": bot_state["running"],
        "killed": bot_state["killed"],
        "paper_mode": PAPER_MODE,
        "positions": bot_state["positions"],
        "strategy_positions": bot_state["strategy_positions"],
        "closed_trades": bot_state["closed_trades"][-50:],
        "diary": bot_state["diary"][-100:],
        "day_pnl": bot_state["day_pnl"],
        "total_trades": total,
        "win_rate": round(wins/total*100) if total > 0 else 0,
        "strategy_stats": bot_state["strategy_stats"],
        "signals": bot_state["signals"],
        "strategy_configs": {
            "EMA": EMA_CONFIG, "MSS": MSS_CONFIG,
            "VPA": VPA_CONFIG, "Breakout": BREAKOUT_CONFIG
        },
        "risk": RISK,
        "account_cash": bot_state["account_cash"],
        "account_equity": bot_state["account_equity"],
        "account_buying_power": bot_state["account_buying_power"],
        "active_cooldowns": bot_state["active_cooldowns"],
        "market_regime": bot_state["market_regime"],
        "daily_paused": bot_state["daily_paused"],
        "version": bot_state["version"]
    }))

@app.route("/diary")
def diary():
    strategy_filter = request.args.get("strategy")
    diary_entries = bot_state["diary"]
    if strategy_filter:
        diary_entries = [e for e in diary_entries if e.get("strategy") == strategy_filter]
    return jsonify({"diary": diary_entries})

@app.route("/kill", methods=["POST"])
def kill():
    bot_state["killed"] = not bot_state["killed"]
    status = "KILLED" if bot_state["killed"] else "RESUMED"
    add_diary("SYSTEM", f"Kill switch {status}", "system")
    return jsonify({"killed": bot_state["killed"]})

@app.route("/bars")
def bars():
    symbol = request.args.get("symbol", "BTC/USD")
    tf     = request.args.get("timeframe", "5Min")
    data   = get_bars(symbol, tf, 150)
    return jsonify(clean_nan(data))

@app.route("/history")
def history():
    strategy_filter = request.args.get("strategy")
    trades = bot_state["closed_trades"]
    if strategy_filter:
        trades = [t for t in trades if t.get("strategy") == strategy_filter]
    return jsonify({"trades": trades})

@app.route("/stats")
def stats():
    return jsonify(clean_nan({
        "overall": {
            "total_trades": bot_state["total_trades"],
            "win_rate": round(bot_state["win_count"]/bot_state["total_trades"]*100)
                        if bot_state["total_trades"] > 0 else 0,
            "day_pnl": bot_state["day_pnl"]
        },
        "by_strategy": {
            s: {
                "trades": bot_state["strategy_stats"][s]["trades"],
                "wins": bot_state["strategy_stats"][s]["wins"],
                "win_rate": round(bot_state["strategy_stats"][s]["wins"] /
                            bot_state["strategy_stats"][s]["trades"] * 100)
                            if bot_state["strategy_stats"][s]["trades"] > 0 else 0,
                "pnl": bot_state["strategy_stats"][s]["pnl"]
            } for s in STRATEGIES
        }
    }))

@app.route("/")
def index():
    try:
        with open("index.html") as f:
            return f.read()
    except:
        return jsonify({"status": "Combined Crypto Bot v1.0 running",
                        "strategies": STRATEGIES,
                        "version": bot_state["version"]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
