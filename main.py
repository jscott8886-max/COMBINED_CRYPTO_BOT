# ScalpAI Combined Crypto Bot - v2.0
# 4 Strategies: EMA + MSS + VPA + Breakout
# 10 Coins: BTC ETH SOL XRP DOGE AVAX LINK LTC ADA UNI
# Confirmation candles, 2 pos/strategy, 10min cooldown
# VPA+Breakout no bear filter, momentum override 1.5%
import os, time, logging, math
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

SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD",
           "LINK/USD", "LTC/USD", "ADA/USD"]  # AVAX + UNI removed — 0% win rate across 2 weeks
STRATEGIES = ["EMA", "MSS", "VPA", "Breakout", "Gap"]

EMA_CONFIG = {
    "name": "EMA", "ema_fast": 9, "ema_slow": 21, "ema_trend": 50,
    "rsi_period": 14, "rsi_hard_gate": 55, "rsi_entry_max": 40,
    "bb_period": 20, "bb_std": 2.0, "bb_min_bw": 1.5,
    "min_score": 4, "min_score_confirmed": 3,
    "atr_min_mult": 0.8, "volume_bonus_mult": 1.5,
    "time_filter": True, "time_start_utc": 13, "time_end_utc": 21,
    "bear_filter": True,
}
MSS_CONFIG = {
    "name": "MSS", "swing_lookback": 10, "swing_fallback": 7, "fallback_hours": 4,
    "rsi_soft_threshold": 50, "atr_min_mult": 0.8, "volume_bonus_mult": 1.5,
    "time_filter": True, "time_start_utc": 13, "time_end_utc": 21,
    "bear_filter": True, "min_score": 4, "min_score_confirmed": 3,
}
VPA_CONFIG = {
    "name": "VPA", "volume_spike_mult": 2.0, "volume_avg_period": 20,
    "min_close_ratio": 0.6, "effort_result_ratio": 0.02,
    "min_score": 4, "min_score_confirmed": 4,  # FIX: raised to 4 — score 3 still lost 80%+
    "bear_score_cap": 4,  # FIX: cap score at 4 in bear regime — score 5 = distribution not accumulation
    "time_filter": False, "bear_filter": False,
}
BREAKOUT_CONFIG = {
    "name": "Breakout", "consolidation_candles": 10, "consolidation_threshold": 0.8,
    "breakout_volume_mult": 2.0, "breakout_candle_close_ratio": 0.6,
    "min_breakout_pct": 0.5,
    "momentum_override_pct": 1.5, "momentum_override_volume": 2.5,
    "min_score": 4, "min_score_confirmed": 3,
    "time_filter": False, "bear_filter": False,
}

GAP_CONFIG = {
    "name": "WeekendGap", "min_gap_pct": 1.0, "max_gap_pct": 8.0,
    "volume_confirm_mult": 1.3, "min_score": 4,
}

# Session open windows (UTC) for momentum boost — not fake gaps, real volume windows
SESSION_WINDOWS_UTC = [
    (0, 0.5),    # Tokyo open ~00:00 UTC (8PM ET)
    (7, 7.5),    # London open ~07:00 UTC (3AM ET)
    (13.5, 14),  # NY open ~13:30 UTC (9:30AM ET)
]

RISK = {
    "position_size": 0.12, "stop_loss_pct": 0.75, "take_profit_pct": 1.5,
    "max_positions_per_strategy": 2, "max_total_positions": 6,
    "cooldown_minutes": 10, "vpa_cooldown_minutes": 120,  # FIX: VPA gets 2-hour cooldown to prevent loss loops
    "daily_loss_limit_pct": 5.0,
    "time_exit_minutes": 60,  # FIX: extended 30→60 — crypto wins avg 45min-4hrs, 30 was too aggressive
}

bot_state = {
    "running": True, "killed": False, "positions": {},
    "strategy_positions": {s: [] for s in STRATEGIES},
    "closed_trades": [], "diary": [],
    "day_pnl": 0.0, "daily_start_equity": 0.0,
    "total_trades": 0, "win_count": 0,
    "strategy_stats": {s: {"trades": 0, "wins": 0, "pnl": 0.0} for s in STRATEGIES},
    "signals": {sym.replace("/",""): {s: {} for s in STRATEGIES} for sym in SYMBOLS},
    "account_cash": 0.0, "account_equity": 0.0, "account_buying_power": 0.0,
    "market_regime": "UNKNOWN",
    "symbol_regimes": {sym.replace("/",""): "UNKNOWN" for sym in SYMBOLS},
    "active_cooldowns": {}, "daily_paused": False,
    "mss_last_signal_time": {sym: None for sym in SYMBOLS},
    "pending_confirmation": {},
    "consecutive_losses": {},  # symbol -> count of consecutive losses
    "symbol_lockouts": {},  # symbol -> lockout expiry ISO timestamp
    "prev_week_closes": {},  # For weekend gap detection
    "gap_fired_this_week": {},
    "version": "Combined-4.1"
}

# ── Alpaca helpers ─────────────────────────────────────────────────────
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
        tf_map = {"1Min": TimeFrame(1, TimeFrameUnit.Minute), "5Min": TimeFrame(5, TimeFrameUnit.Minute),
                  "15Min": TimeFrame(15, TimeFrameUnit.Minute),
                  "1Hour": TimeFrame(1, TimeFrameUnit.Hour), "1Day": TimeFrame(1, TimeFrameUnit.Day)}
        tf = tf_map.get(timeframe, TimeFrame(5, TimeFrameUnit.Minute))
        end = datetime.now(timezone.utc)
        if timeframe == "1Day": start = end - timedelta(days=limit + 10)
        elif timeframe == "1Hour": start = end - timedelta(hours=limit + 5)
        else: start = end - timedelta(minutes=limit * 6)
        req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, limit=limit)
        bars = client.get_crypto_bars(req)
        df = bars.df
        if df.empty: return []
        if hasattr(df.index, 'levels'):
            df = df.loc[symbol] if symbol in df.index.get_level_values(0) else df
        result = []
        for idx, row in df.iterrows():
            result.append({"time": idx.isoformat() if hasattr(idx, 'isoformat') else str(idx),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row["volume"])})
        return result[-limit:]
    except Exception as e:
        log.error(f"Bars error {symbol}: {e}"); return []

def refresh_account():
    try:
        tc = get_trading_client(); acct = tc.get_account()
        bot_state["account_cash"] = float(acct.cash)
        bot_state["account_equity"] = float(acct.equity)
        bot_state["account_buying_power"] = float(acct.buying_power)
        if bot_state["daily_start_equity"] == 0.0:
            bot_state["daily_start_equity"] = float(acct.equity)
    except Exception as e: log.error(f"Account error: {e}")

def sync_positions():
    try:
        tc = get_trading_client(); positions = tc.get_all_positions()
        synced = {}; active = set()
        for p in positions:
            sym = p.symbol
            if "/" not in sym and len(sym) > 3: sym = sym[:-3] + "/" + sym[-3:]
            active.add(sym)
            existing = bot_state["positions"].get(sym, {})
            synced[sym] = {"symbol": sym, "entry": float(p.avg_entry_price),
                "qty": float(p.qty), "current_price": float(p.current_price),
                "unrealized_pnl": float(p.unrealized_pl),
                "open_time": existing.get("open_time", datetime.now(timezone.utc).isoformat()),
                "strategy": existing.get("strategy", "UNKNOWN")}
        for strat in STRATEGIES:
            bot_state["strategy_positions"][strat] = [s for s in bot_state["strategy_positions"][strat] if s in active]
        bot_state["positions"] = synced
    except Exception as e: log.error(f"Sync error: {e}")

def place_order(symbol, qty, side):
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        tc = get_trading_client()
        req = MarketOrderRequest(symbol=symbol, qty=round(qty, 6),
            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL, time_in_force=TimeInForce.GTC)
        return tc.submit_order(req)
    except Exception as e: log.error(f"Order error {symbol}: {e}"); return None

def close_position_alpaca(symbol):
    try:
        tc = get_trading_client(); tc.close_position(symbol.replace("/", "")); return True
    except Exception as e: log.error(f"Close error {symbol}: {e}"); return False

def add_diary(symbol, text, entry_type="info", strategy="SYSTEM"):
    label = f"[{strategy}] " if strategy != "SYSTEM" else ""
    entry = {"time": datetime.now(timezone.utc).strftime("%H:%M"), "symbol": symbol,
             "text": f"{label}{text}", "type": entry_type, "strategy": strategy}
    bot_state["diary"].insert(0, entry)
    if len(bot_state["diary"]) > 300: bot_state["diary"] = bot_state["diary"][:300]

# ── Indicators ─────────────────────────────────────────────────────────
def calc_ema(prices, period):
    if len(prices) < period: return []
    k = 2 / (period + 1); ema = [sum(prices[:period]) / period]
    for p in prices[period:]: ema.append(p * k + ema[-1] * (1 - k))
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]; gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period; al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    return 100 - (100 / (1 + ag/al))

def calc_bb(closes, period=20, std_dev=2.0):
    if len(closes) < period: return None, None, None
    window = closes[-period:]; mid = sum(window) / period
    std = math.sqrt(sum((x-mid)**2 for x in window) / period)
    return mid - std_dev*std, mid, mid + std_dev*std

def calc_atr(bars, period=14):
    if len(bars) < period + 1: return 0.0
    trs = []
    for i in range(1, len(bars)):
        trs.append(max(bars[i]["high"]-bars[i]["low"], abs(bars[i]["high"]-bars[i-1]["close"]), abs(bars[i]["low"]-bars[i-1]["close"])))
    return sum(trs[-period:]) / period if len(trs) >= period else sum(trs)/len(trs)

def check_market_regime():
    try:
        bars = get_bars("BTC/USD", "1Day", 210)
        if len(bars) < 200: return "UNKNOWN"
        closes = [b["close"] for b in bars]; ema200 = calc_ema(closes, 200)
        if not ema200: return "UNKNOWN"
        regime = "BULL" if closes[-1] > ema200[-1] else "BEAR"
        log.info(f"Global: {regime} | BTC={closes[-1]:.0f} | 200EMA={ema200[-1]:.0f}")
        return regime
    except Exception as e: log.error(f"Regime error: {e}"); return "UNKNOWN"

def check_symbol_regime(symbol):
    try:
        bars = get_bars(symbol, "1Day", 210)
        if len(bars) < 200: return "UNKNOWN"
        closes = [b["close"] for b in bars]; ema200 = calc_ema(closes, 200)
        if not ema200: return "UNKNOWN"
        regime = "BULL" if closes[-1] > ema200[-1] else "BEAR"
        log.info(f"Regime {symbol}: {regime} | price={closes[-1]:.4f} | 200EMA={ema200[-1]:.4f}")
        return regime
    except Exception as e: log.error(f"Symbol regime error {symbol}: {e}"); return "UNKNOWN"

def is_in_time_window(cfg):
    if not cfg.get("time_filter", False): return True
    now = datetime.now(timezone.utc)
    return cfg["time_start_utc"] <= now.hour + now.minute/60 <= cfg["time_end_utc"]

# ── Confirmation system ────────────────────────────────────────────────
def check_confirmation(symbol, strategy, current_bar):
    key = f"{symbol}_{strategy}_BUY"
    pending = bot_state["pending_confirmation"].get(key)
    if not pending: return False
    confirmed = current_bar["close"] > current_bar["open"]
    if confirmed: del bot_state["pending_confirmation"][key]; return True
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(pending["time"])).total_seconds()
    if elapsed > 900: del bot_state["pending_confirmation"][key]
    return False

def set_pending(symbol, strategy, signal):
    key = f"{symbol}_{strategy}_BUY"
    bot_state["pending_confirmation"][key] = {"signal": signal, "time": datetime.now(timezone.utc).isoformat()}

def can_enter(symbol, strategy):
    if bot_state["killed"] or bot_state["daily_paused"]: return False
    if len(bot_state["positions"]) >= RISK["max_total_positions"]: return False
    if len(bot_state["strategy_positions"][strategy]) >= RISK["max_positions_per_strategy"]: return False
    if symbol in bot_state["positions"]: return False

    # FIX 6: Check consecutive loss lockout (3 losses → 6 hour ban)
    lockout = bot_state["symbol_lockouts"].get(symbol)
    if lockout:
        if datetime.now(timezone.utc) < datetime.fromisoformat(lockout):
            return False
        else:
            del bot_state["symbol_lockouts"][symbol]
            bot_state["consecutive_losses"][symbol] = 0

    # FIX 4: VPA gets 2-hour cooldown, others get 10 minutes
    ck = f"{strategy}_{symbol}"
    if ck in bot_state["active_cooldowns"]:
        cooldown = RISK["vpa_cooldown_minutes"] if strategy == "VPA" else RISK["cooldown_minutes"]
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(bot_state["active_cooldowns"][ck])).total_seconds() / 60
        if elapsed < cooldown: return False
        del bot_state["active_cooldowns"][ck]
    return True

def record_exit(symbol, strategy, pnl, win):
    # Handle legacy positions with "UNKNOWN" strategy from pre-redeploy
    if strategy in bot_state["strategy_positions"]:
        bot_state["strategy_positions"][strategy] = [s for s in bot_state["strategy_positions"][strategy] if s != symbol]
    bot_state["day_pnl"] += pnl; bot_state["total_trades"] += 1
    if win: bot_state["win_count"] += 1
    s = bot_state["strategy_stats"][strategy]
    s["trades"] += 1; s["pnl"] = round(s["pnl"] + pnl, 2)
    if win:
        s["wins"] += 1
        bot_state["consecutive_losses"][symbol] = 0  # Reset on win
    else:
        # FIX 6: Track consecutive losses — 3 in a row = 6 hour ban
        count = bot_state["consecutive_losses"].get(symbol, 0) + 1
        bot_state["consecutive_losses"][symbol] = count
        if count >= 3:
            lockout_until = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
            bot_state["symbol_lockouts"][symbol] = lockout_until
            log.info(f"[LOCKOUT] {symbol} locked out for 6 hours after {count} consecutive losses")

# ── STRATEGIES ─────────────────────────────────────────────────────────
def run_ema(symbol, regime, tf="5Min"):
    cfg = EMA_CONFIG
    try:
        bars_5m = get_bars(symbol, tf, 60); bars_1h = get_bars(symbol, "1Hour", 60)
        if len(bars_5m) < 30 or len(bars_1h) < 30: return {}
        closes = [b["close"] for b in bars_5m]; closes_1h = [b["close"] for b in bars_1h]
        volumes = [b["volume"] for b in bars_5m]; price = closes[-1]
        if all(v == 0 for v in volumes[-5:]): return {}

        ema9 = calc_ema(closes, 9); ema21 = calc_ema(closes, 21)
        ema50_1h = calc_ema(closes_1h, 50)
        rsi = calc_rsi(closes); rsi_prev = calc_rsi(closes[:-2]); rsi_rising = rsi > rsi_prev
        bb_low, bb_mid, bb_high = calc_bb(closes)
        atr = calc_atr(bars_5m); avg_atr = calc_atr(bars_5m[:-10]) if len(bars_5m) > 15 else atr
        if not ema9 or not ema21 or not ema50_1h or bb_mid is None: return {}

        bb_bw = ((bb_high - bb_low) / bb_mid) * 100 if bb_mid > 0 else 0
        avg_vol = sum(volumes[-20:]) / 20; vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        atr_ok = avg_atr == 0 or atr >= avg_atr * cfg["atr_min_mult"]
        sk = symbol.replace("/","")

        if rsi > cfg["rsi_hard_gate"]:
            bot_state["signals"][sk]["EMA" if tf=="5Min" else "EMA_15m"] = {"price": price, "rsi": round(rsi,1), "blocked": "RSI_HIGH", "buy_score": 0, "strategy": "EMA"}
            return bot_state["signals"][sk]["EMA"]
        if not atr_ok:
            bot_state["signals"][sk]["EMA" if tf=="5Min" else "EMA_15m"] = {"price": price, "blocked": "ATR_LOW", "buy_score": 0, "strategy": "EMA"}
            return bot_state["signals"][sk]["EMA"]

        confirmed = check_confirmation(symbol, "EMA", bars_5m[-1])
        score = 0
        if price > ema50_1h[-1]: score += 1
        if ema9[-1] > ema21[-1]: score += 2
        if len(ema9) > 1 and ema9[-1] > ema21[-1] and ema9[-2] <= ema21[-2]: score += 1
        if rsi < 40 and rsi_rising: score += 2
        elif rsi < cfg["rsi_hard_gate"] and rsi_rising: score += 1
        if bb_bw >= cfg["bb_min_bw"] and price < bb_low: score += 1
        if vol_ratio >= cfg["volume_bonus_mult"]: score += 1

        if score >= cfg["min_score"]:
            confirmed = True
        elif score >= cfg["min_score_confirmed"] and not confirmed:
            set_pending(symbol, "EMA", {"score": score})

        sig = {"price": price, "rsi": round(rsi,1), "rsi_rising": rsi_rising,
               "vol_ratio": round(vol_ratio,2), "buy_score": score, "confirmed": confirmed, "strategy": "EMA"}
        bot_state["signals"][sk]["EMA" if tf=="5Min" else "EMA_15m"] = sig
        log.info(f"[EMA] {symbol} | price={price} RSI={round(rsi,1)} score={score} conf={confirmed}")
        return sig
    except Exception as e: log.error(f"[EMA] error {symbol}: {e}"); return {}

def run_mss(symbol, regime, tf="5Min"):
    cfg = MSS_CONFIG
    try:
        bars_5m = get_bars(symbol, tf, 60); bars_1h = get_bars(symbol, "1Hour", 30)
        if len(bars_5m) < 20 or len(bars_1h) < 15: return {}
        closes = [b["close"] for b in bars_5m]; highs_1h = [b["high"] for b in bars_1h]
        lows_1h = [b["low"] for b in bars_1h]; lows_5m = [b["low"] for b in bars_5m]
        volumes = [b["volume"] for b in bars_5m]; price = closes[-1]
        if all(v == 0 for v in volumes[-5:]): return {}

        rsi = calc_rsi(closes); rsi_prev = calc_rsi(closes[:-2]); rsi_rising = rsi > rsi_prev
        atr = calc_atr(bars_5m); avg_atr = calc_atr(bars_5m[:-10]) if len(bars_5m) > 15 else atr
        avg_vol = sum(volumes[-20:]) / 20; vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        sk = symbol.replace("/","")

        rh = highs_1h[-5:]; ph = highs_1h[-10:-5]; rl = lows_1h[-5:]; pl = lows_1h[-10:-5]
        trend_1h = "NEUTRAL"
        if rh and ph and rl and pl:
            if max(rh) > max(ph) and min(rl) > min(pl): trend_1h = "BULL"
            elif max(rh) < max(ph) and min(rl) < min(pl): trend_1h = "BEAR"

        if trend_1h != "BULL":
            bot_state["signals"][sk]["MSS" if tf=="5Min" else "MSS_15m"] = {"price": price, "trend_1h": trend_1h, "buy_score": 0, "strategy": "MSS"}
            return bot_state["signals"][sk]["MSS"]

        last_sig = bot_state["mss_last_signal_time"].get(symbol)
        lookback = cfg["swing_lookback"]
        if last_sig:
            hrs = (datetime.now(timezone.utc) - last_sig).total_seconds() / 3600
            if hrs > cfg["fallback_hours"]: lookback = cfg["swing_fallback"]

        recent_lows = lows_5m[-lookback:]
        mss = len(recent_lows) >= 5 and recent_lows[-3] < recent_lows[-5] and recent_lows[-1] > recent_lows[-2]
        if mss: bot_state["mss_last_signal_time"][symbol] = datetime.now(timezone.utc)

        confirmed = check_confirmation(symbol, "MSS", bars_5m[-1])
        score = 0
        if mss: score += 3
        if rsi < cfg["rsi_soft_threshold"] and rsi_rising: score += 2
        elif rsi < cfg["rsi_soft_threshold"]: score += 1
        if vol_ratio >= cfg["volume_bonus_mult"]: score += 1

        if score >= cfg["min_score"]:
            confirmed = True
        elif score >= cfg["min_score_confirmed"] and not confirmed:
            set_pending(symbol, "MSS", {"score": score})

        sig = {"price": price, "trend_1h": trend_1h, "mss_detected": mss, "rsi": round(rsi,1),
               "rsi_rising": rsi_rising, "vol_ratio": round(vol_ratio,2),
               "buy_score": score, "confirmed": confirmed, "strategy": "MSS"}
        bot_state["signals"][sk]["MSS" if tf=="5Min" else "MSS_15m"] = sig
        log.info(f"[MSS] {symbol} | trend={trend_1h} MSS={mss} score={score} conf={confirmed}")
        return sig
    except Exception as e: log.error(f"[MSS] error {symbol}: {e}"); return {}

def run_vpa(symbol, regime, tf="5Min"):
    cfg = VPA_CONFIG
    try:
        bars = get_bars(symbol, tf, 40)
        if len(bars) < 25: return {}
        volumes = [b["volume"] for b in bars]; closes = [b["close"] for b in bars]
        opens = [b["open"] for b in bars]; highs = [b["high"] for b in bars]; lows = [b["low"] for b in bars]
        if all(v == 0 for v in volumes[-5:]): return {}

        avg_vol = sum(volumes[-cfg["volume_avg_period"]:]) / cfg["volume_avg_period"]
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        price = closes[-1]; bar_range = highs[-1] - lows[-1]
        if bar_range == 0: return {}
        close_ratio = (closes[-1] - lows[-1]) / bar_range
        price_move = bar_range / price if price > 0 else 0
        sk = symbol.replace("/","")

        confirmed = check_confirmation(symbol, "VPA", bars[-1])
        score = 0; signals_detected = []
        if vol_ratio >= cfg["volume_spike_mult"]:
            if close_ratio >= cfg["min_close_ratio"]: score += 2; signals_detected.append("VOL_SPIKE_BULL")
        if vol_ratio >= 2.5 and price_move < cfg["effort_result_ratio"]:
            if closes[-1] > opens[-1]: score += 2; signals_detected.append("ABSORPTION_BULL")
        if vol_ratio < 0.7 and closes[-1] > opens[-1] and close_ratio > 0.5:
            # FIX: NO_SUPPLY only counts when combined with a real volume signal
            # By itself it's noise — fires constantly on every coin
            if len(signals_detected) > 0:  # Only add if VOL_SPIKE or ABSORPTION already detected
                score += 1; signals_detected.append("NO_SUPPLY")
        ema20 = calc_ema(closes, 20)
        if ema20 and price > ema20[-1]: score += 1

        # FIX: score 5 in bear regime = institutional distribution, not accumulation — cap it
        raw_score = score
        if regime == "BEAR" and score > cfg["bear_score_cap"]:
            score = cfg["bear_score_cap"]
            signals_detected.append("BEAR_CAPPED")

        # FIX: score 4+ enters immediately, skip confirmation requirement entirely
        if score >= cfg["min_score"]:
            confirmed = True  # treat as confirmed — high score doesn't need candle confirmation
        elif score >= cfg["min_score_confirmed"] and not confirmed:
            set_pending(symbol, "VPA", {"score": score})

        sig = {"price": price, "vol_ratio": round(vol_ratio,2), "close_ratio": round(close_ratio,2),
               "buy_score": score, "raw_score": raw_score, "signals": signals_detected, "confirmed": confirmed, "strategy": "VPA"}
        bot_state["signals"][sk]["VPA" if tf=="5Min" else "VPA_15m"] = sig
        log.info(f"[VPA] {symbol} | vol={round(vol_ratio,2)}x score={score} sigs={signals_detected} conf={confirmed}")
        return sig
    except Exception as e: log.error(f"[VPA] error {symbol}: {e}"); return {}

def is_session_window():
    """Returns True if within 30min of Tokyo/London/NY open — real volume windows"""
    now = datetime.now(timezone.utc)
    h = now.hour + now.minute/60
    for start, end in SESSION_WINDOWS_UTC:
        if start <= h < end:
            return True
    return False

def is_weekend_gap_window():
    """Sunday 00:00-01:00 UTC — right after the weekly low-liquidity weekend period,
    comparing Friday 21:00 UTC close to current price"""
    now = datetime.now(timezone.utc)
    return now.weekday() == 6 and now.hour < 1  # Sunday, first hour

def run_weekend_gap(symbol, regime):
    """5th strategy — crypto's equivalent of ETF's Gap detector.
    Compares Friday 9PM UTC (5PM ET, when TradFi closes) price to Sunday open price."""
    cfg = GAP_CONFIG
    try:
        if not is_weekend_gap_window():
            return {}
        sk = symbol.replace("/","")
        week_key = datetime.now(timezone.utc).strftime("%Y-W%U")
        if bot_state["gap_fired_this_week"].get(sk) == week_key:
            return {}

        bars = get_bars(symbol, "1Hour", 72)  # 3 days back to find Friday close
        if len(bars) < 10: return {}

        # Find the bar closest to Friday 21:00 UTC
        friday_close = None
        for b in bars:
            try:
                bt = datetime.fromisoformat(b["time"].replace("Z","+00:00"))
                if bt.weekday() == 4 and bt.hour >= 20:
                    friday_close = b["close"]
            except: continue
        if not friday_close:
            friday_close = bars[-24]["close"] if len(bars) >= 24 else bars[0]["close"]

        current_price = bars[-1]["close"]
        volumes = [b["volume"] for b in bars[-24:]]
        avg_vol = sum(volumes) / len(volumes) if volumes else 1
        curr_vol = bars[-1]["volume"]
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0

        gap_pct = (current_price - friday_close) / friday_close * 100
        is_gap_up = cfg["min_gap_pct"] <= gap_pct <= cfg["max_gap_pct"]
        vol_ok = vol_ratio >= cfg["volume_confirm_mult"]

        score = 5 if (is_gap_up and vol_ok) else 0
        sig = {"price": current_price, "friday_close": round(friday_close,4),
               "gap_pct": round(gap_pct,2), "vol_ratio": round(vol_ratio,2),
               "is_gap_up": is_gap_up, "buy_score": score, "confirmed": True,
               "strategy": "Gap"}
        bot_state["signals"][sk]["Gap"] = sig
        if score > 0:
            log.info(f"[Gap] {symbol} | weekend gap {round(gap_pct,2)}% vol={round(vol_ratio,1)}x SCORE={score}")
        return sig
    except Exception as e:
        log.error(f"[Gap] error {symbol}: {e}"); return {}

def run_breakout(symbol, regime, tf="5Min"):
    cfg = BREAKOUT_CONFIG
    try:
        bars = get_bars(symbol, tf, 40)
        if len(bars) < 15: return {}
        closes = [b["close"] for b in bars]; highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]; volumes = [b["volume"] for b in bars]
        opens = [b["open"] for b in bars]
        if all(v == 0 for v in volumes[-5:]): return {}

        price = closes[-1]; curr_close = closes[-1]; curr_open = opens[-1]
        avg_vol = sum(volumes[-20:]) / 20; vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 0
        candle_pct = abs(curr_close - curr_open) / curr_open * 100 if curr_open > 0 else 0
        sk = symbol.replace("/","")

        # Momentum override — lowered to 1.5%, further reduced during session opens (real volume windows)
        session_active = is_session_window()
        momentum_threshold = cfg["momentum_override_pct"] * 0.7 if session_active else cfg["momentum_override_pct"]
        momentum_override = (candle_pct >= momentum_threshold and
                            vol_ratio >= cfg["momentum_override_volume"] and curr_close > curr_open)

        lookback = cfg["consolidation_candles"]
        if len(bars) < lookback + 2: return {}
        consol = bars[-(lookback+2):-2]
        c_highs = [b["high"] for b in consol]; c_lows = [b["low"] for b in consol]
        c_range_pct = (max(c_highs) - min(c_lows)) / price * 100 if price > 0 else 0
        c_high = max(c_highs); in_consol = c_range_pct <= cfg["consolidation_threshold"]

        bar_range = highs[-1] - lows[-1]
        close_ratio = (closes[-1] - lows[-1]) / bar_range if bar_range > 0 else 0
        bo_pct = (closes[-1] - c_high) / c_high * 100 if c_high > 0 else 0

        prev = bars[-2]; prev_range = prev["high"] - prev["low"]
        prev_confirmed = False
        if prev_range > 0: prev_confirmed = prev["close"] > c_high and (prev["close"] - prev["low"]) / prev_range >= 0.5

        is_breakout = (in_consol and closes[-1] > c_high and bo_pct >= cfg["min_breakout_pct"] and
                      vol_ratio >= cfg["breakout_volume_mult"] and close_ratio >= cfg["breakout_candle_close_ratio"] and
                      prev_confirmed)

        confirmed = check_confirmation(symbol, "Breakout", bars[-1])
        buy_signal = momentum_override or is_breakout
        score = 5 if momentum_override else (4 if is_breakout else 0)

        sig = {"price": price, "vol_ratio": round(vol_ratio,2), "candle_pct": round(candle_pct,2),
               "consol_pct": round(c_range_pct,2), "is_breakout": is_breakout,
               "momentum_override": momentum_override, "buy_signal": buy_signal,
               "session_active": session_active, "buy_score": score,
               "confirmed": confirmed, "strategy": "Breakout"}
        bot_state["signals"][sk]["Breakout" if tf=="5Min" else "Breakout_15m"] = sig
        log.info(f"[Breakout] {symbol} | vol={round(vol_ratio,1)}x candle={round(candle_pct,2)}% breakout={is_breakout} momentum={momentum_override}")
        return sig
    except Exception as e: log.error(f"[Breakout] error {symbol}: {e}"); return {}

# ── EXIT / ENTRY ───────────────────────────────────────────────────────
def check_exits(symbol, price, now):
    pos = bot_state["positions"].get(symbol)
    if not pos: return
    entry = pos["entry"]; qty = pos["qty"]; strategy = pos.get("strategy", "UNKNOWN")
    pct = (price - entry) / entry * 100
    should_exit = False; reason = ""

    # FIX: 30-minute time exit — cut losers early, they rarely recover (data confirmed)
    open_time = pos.get("open_time")
    minutes_open = 0
    if open_time:
        minutes_open = (now - datetime.fromisoformat(open_time)).total_seconds() / 60

    if pct >= RISK["take_profit_pct"]: should_exit = True; reason = f"Take profit (+{round(pct,2)}%)"
    elif pct <= -RISK["stop_loss_pct"]:
        should_exit = True; reason = f"Stop loss ({round(pct,2)}%)"
        bot_state["active_cooldowns"][f"{strategy}_{symbol}"] = now.isoformat()
    elif minutes_open >= RISK["time_exit_minutes"] and pct < 0:
        should_exit = True; reason = f"30min time exit ({round(pct,2)}%)"
        bot_state["active_cooldowns"][f"{strategy}_{symbol}"] = now.isoformat()
    if should_exit:
        if close_position_alpaca(symbol):
            pnl = (price - entry) * qty; win = pnl > 0
            record_exit(symbol, strategy, pnl, win)
            add_diary(symbol, f"{'WIN' if win else 'LOSS'} | ${entry:,.4f}→${price:,.4f} | ${round(pnl,2)} ({round(pct,2)}%) | {reason}",
                "win" if win else "loss", strategy)
            bot_state["closed_trades"].append({"symbol": symbol, "entry": entry, "exit": price,
                "pnl": round(pnl,2), "pct": round(pct,2), "win": win, "strategy": strategy,
                "reason": reason, "time": now.strftime("%H:%M")})
            sync_positions()

def try_entry(symbol, strategy, sig, regime, now):
    if not can_enter(symbol, strategy): return
    sk = symbol.replace("/","")
    sym_regime = bot_state["symbol_regimes"].get(sk, "UNKNOWN")
    confirmed = sig.get("confirmed", False)
    cfg_map = {"EMA": EMA_CONFIG, "MSS": MSS_CONFIG, "VPA": VPA_CONFIG, "Breakout": BREAKOUT_CONFIG}
    cfg = cfg_map.get(strategy, {})

    if strategy == "EMA":
        if sym_regime == "BEAR" and cfg.get("bear_filter"): return
        if not is_in_time_window(cfg): return
        if sig.get("blocked"): return
    elif strategy == "MSS":
        if not sig.get("mss_detected"): return
        if sym_regime == "BEAR" and cfg.get("bear_filter"): return
        if not is_in_time_window(cfg): return
    elif strategy == "VPA":
        # FIX: VPA DISABLED in bear regime — 0% win rate over 100+ trades in bear market
        # Volume signals are unreliable when the overall trend is down
        # VPA only activates when per-symbol regime is BULL or UNKNOWN
        if sym_regime == "BEAR":
            return
    elif strategy == "Breakout":
        if not sig.get("buy_signal") and not confirmed: return
        if sym_regime == "BEAR" and not sig.get("momentum_override"): return

    min_score = cfg.get("min_score_confirmed", 3) if confirmed else cfg.get("min_score", 4)
    if sig.get("buy_score", 0) < min_score: return

    cash = bot_state["account_cash"]; budget = cash * RISK["position_size"]
    price = sig["price"]; qty = budget / price
    if budget < 10 or qty <= 0: return

    order = place_order(symbol, qty, "BUY")
    if order:
        bot_state["positions"][symbol] = {"symbol": symbol, "entry": price, "qty": qty,
            "current_price": price, "unrealized_pnl": 0,
            "open_time": now.isoformat(), "strategy": strategy}
        bot_state["strategy_positions"][strategy].append(symbol)
        sync_positions()
        conf_label = " ✓CONF" if confirmed else ""
        add_diary(symbol, f"BUY | ${price:,.4f} | Score {sig.get('buy_score',0)}{conf_label}", "trade", strategy)
        log.info(f"[{strategy}] ENTERED {symbol} at {price}{conf_label}")

# ── TRADING LOOP ───────────────────────────────────────────────────────
def trading_loop():
    if not API_KEY or not API_SECRET:
        log.warning("No Alpaca credentials"); return

    add_diary("SYSTEM",
        "Combined Crypto v4.1 started | 5 Strategies | 8 Coins (-AVAX -UNI) | "
        "VPA DISABLED in bear regime | NO_SUPPLY standalone removed | "
        "VPA 2hr cooldown | 60min time exit | "
        "3-loss 6hr lockout | EMA+MSS priority over VPA", "system")
    log.info("Combined Crypto Bot v4.1 started")

    regime_check_time = None; daily_reset_date = None
    while True:
        try:
            now = datetime.now(timezone.utc)
            today = now.date()
            if daily_reset_date != today:
                bot_state["day_pnl"] = 0.0; bot_state["daily_start_equity"] = 0.0
                bot_state["daily_paused"] = False; daily_reset_date = today

            refresh_account(); sync_positions()

            if not regime_check_time or (now - regime_check_time).total_seconds() > 1800:
                bot_state["market_regime"] = check_market_regime()
                for sym in SYMBOLS:
                    bot_state["symbol_regimes"][sym.replace("/","")] = check_symbol_regime(sym)
                regime_check_time = now

            # Daily loss check
            if bot_state["daily_start_equity"] > 0:
                loss_pct = (bot_state["daily_start_equity"] - bot_state["account_equity"]) / bot_state["daily_start_equity"] * 100
                if loss_pct >= RISK["daily_loss_limit_pct"] and not bot_state["daily_paused"]:
                    bot_state["daily_paused"] = True
                    add_diary("SYSTEM", f"Daily loss limit {RISK['daily_loss_limit_pct']}% hit", "system")
            if bot_state["daily_paused"] or bot_state["killed"]: time.sleep(60); continue

            # Clear expired cooldowns
            expired = [k for k, t in list(bot_state["active_cooldowns"].items())
                       if (now - datetime.fromisoformat(t)).total_seconds() > RISK["cooldown_minutes"] * 60]
            for k in expired: del bot_state["active_cooldowns"][k]

            for symbol in SYMBOLS:
                if bot_state["killed"]: break
                bars = get_bars(symbol, "5Min", 3)
                if not bars: continue
                price = bars[-1]["close"]
                check_exits(symbol, price, now)
                regime = bot_state["symbol_regimes"].get(symbol.replace("/",""), "UNKNOWN")

                # Gap doesn't take a timeframe param
                if len(bot_state["strategy_positions"]["Gap"]) < RISK["max_positions_per_strategy"]:
                    sig = run_weekend_gap(symbol, regime)
                    if sig: try_entry(symbol, "Gap", sig, regime, now)

                # FIX 7: Reordered — EMA and MSS get priority over VPA
                # Gap > Breakout > EMA > MSS > VPA (VPA is last — fills remaining slots only)
                for strat, run_fn in [("Breakout", run_breakout), ("EMA", run_ema),
                                      ("MSS", run_mss), ("VPA", run_vpa)]:
                    if len(bot_state["strategy_positions"][strat]) < RISK["max_positions_per_strategy"]:
                        sig_5m = run_fn(symbol, regime, "5Min")
                        if sig_5m: try_entry(symbol, strat, sig_5m, regime, now)
                    if len(bot_state["strategy_positions"][strat]) < RISK["max_positions_per_strategy"]:
                        sig_15m = run_fn(symbol, regime, "15Min")
                        if sig_15m: try_entry(symbol, strat, sig_15m, regime, now)

        except Exception as e:
            log.error(f"Loop error: {e}"); import traceback; log.error(traceback.format_exc())
        time.sleep(60)

threading.Thread(target=trading_loop, daemon=True).start()

# ── Flask routes ───────────────────────────────────────────────────────
@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"; return r

def clean_nan(obj):
    if isinstance(obj, float): return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict): return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list): return [clean_nan(i) for i in obj]
    return obj

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat(),
        "version": bot_state["version"], "regime": bot_state["market_regime"],
        "positions": len(bot_state["positions"]), "symbols": len(SYMBOLS)})

@app.route("/status")
def status():
    refresh_account(); wins = bot_state["win_count"]; total = bot_state["total_trades"]
    return jsonify(clean_nan({
        "running": bot_state["running"], "killed": bot_state["killed"],
        "positions": bot_state["positions"], "strategy_positions": bot_state["strategy_positions"],
        "closed_trades": bot_state["closed_trades"][-50:], "diary": bot_state["diary"][-100:],
        "day_pnl": bot_state["day_pnl"], "total_trades": total,
        "win_rate": round(wins/total*100) if total > 0 else 0,
        "strategy_stats": bot_state["strategy_stats"], "signals": bot_state["signals"],
        "account_cash": bot_state["account_cash"], "account_equity": bot_state["account_equity"],
        "market_regime": bot_state["market_regime"], "symbol_regimes": bot_state["symbol_regimes"],
        "active_cooldowns": bot_state["active_cooldowns"],
        "pending_confirmations": len(bot_state["pending_confirmation"]),
        "daily_paused": bot_state["daily_paused"], "version": bot_state["version"]}))

@app.route("/diary")
def diary():
    sf = request.args.get("strategy"); entries = bot_state["diary"]
    if sf: entries = [e for e in entries if e.get("strategy") == sf]
    return jsonify({"diary": entries})

@app.route("/kill", methods=["POST"])
def kill():
    bot_state["killed"] = not bot_state["killed"]
    add_diary("SYSTEM", f"Kill switch {'KILLED' if bot_state['killed'] else 'RESUMED'}", "system")
    return jsonify({"killed": bot_state["killed"]})

@app.route("/bars")
def bars():
    symbol = request.args.get("symbol", "BTC/USD"); tf = request.args.get("timeframe", "5Min")
    data = get_bars(symbol, tf, 150); return jsonify(clean_nan(data))

@app.route("/history")
def history():
    sf = request.args.get("strategy"); trades = bot_state["closed_trades"]
    if sf: trades = [t for t in trades if t.get("strategy") == sf]
    return jsonify({"trades": trades})

@app.route("/stats")
def stats():
    return jsonify(clean_nan({"overall": {"total_trades": bot_state["total_trades"],
        "win_rate": round(bot_state["win_count"]/bot_state["total_trades"]*100) if bot_state["total_trades"] > 0 else 0,
        "day_pnl": bot_state["day_pnl"]},
        "by_strategy": {s: {"trades": bot_state["strategy_stats"][s]["trades"],
            "wins": bot_state["strategy_stats"][s]["wins"],
            "win_rate": round(bot_state["strategy_stats"][s]["wins"]/bot_state["strategy_stats"][s]["trades"]*100) if bot_state["strategy_stats"][s]["trades"] > 0 else 0,
            "pnl": bot_state["strategy_stats"][s]["pnl"]} for s in STRATEGIES}}))

@app.route("/")
def index():
    try:
        with open("index.html") as f: return f.read()
    except: return jsonify({"status": "Combined Crypto v2.0", "symbols": [s for s in SYMBOLS]})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080)); app.run(host="0.0.0.0", port=port)
