# ScalpAI Crypto Breakout Bot - v1.0
# Strategy: Detects consolidation + explosive volume breakout
# Momentum override: catches strong individual moves even in bear market
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
 
STRATEGY = {
    # Consolidation detection
    "consolidation_candles": 10,       # Look back N candles for tight range
    "consolidation_threshold": 0.8,    # Max % range during consolidation
    # Breakout detection
    "breakout_volume_mult": 2.0,       # Volume must be 2x average to confirm
    "breakout_candle_close_ratio": 0.6,# Candle must close in top 60% of range
    "min_breakout_pct": 0.5,           # Minimum % move to qualify as breakout
    # Momentum override (catches explosive moves even in bear market)
    "momentum_override_pct": 2.5,      # If single candle moves 2.5%+ override bear filter
    "momentum_override_volume": 3.0,   # AND volume must be 3x average
    # Risk management
    "stop_loss_pct": 0.75,
    "take_profit_pct": 1.5,
    "position_size": 0.20,             # 20% of cash per trade
    "cooldown_minutes": 20,
    "max_positions": 3,
}
 
bot_state = {
    "running": True, "killed": False, "positions": {},
    "closed_trades": [], "diary": [], "day_pnl": 0.0,
    "total_trades": 0, "win_count": 0,
    "signals": {s.replace("/", ""): {} for s in SYMBOLS},
    "account_cash": 0.0, "account_equity": 0.0,
    "account_buying_power": 0.0,
    "active_cooldowns": {},
    "market_regime": "UNKNOWN",
    "version": "CryptoBreakout-1.0"
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
        bot_state["account_cash"] = float(acct.cash)
        bot_state["account_equity"] = float(acct.equity)
        bot_state["account_buying_power"] = float(acct.buying_power)
    except Exception as e:
        log.error(f"Account refresh error: {e}")
 
def sync_positions():
    try:
        tc = get_trading_client()
        positions = tc.get_all_positions()
        synced = {}
        for p in positions:
            sym = p.symbol
            if "/" not in sym and len(sym) > 3:
                sym = sym[:-3] + "/" + sym[-3:]
            synced[sym] = {
                "symbol": sym,
                "entry": float(p.avg_entry_price),
                "qty": float(p.qty),
                "current_price": float(p.current_price),
                "unrealized_pnl": float(p.unrealized_pl),
                "open_time": datetime.now(timezone.utc).isoformat()
            }
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
        log.error(f"Close position error {symbol}: {e}")
        return False
 
def add_diary(symbol, text, entry_type="info"):
    entry = {
        "time": datetime.now(timezone.utc).strftime("%H:%M"),
        "symbol": symbol, "text": text, "type": entry_type
    }
    bot_state["diary"].insert(0, entry)
    if len(bot_state["diary"]) > 200:
        bot_state["diary"] = bot_state["diary"][:200]
 
# ── Indicators ─────────────────────────────────────────────────────────────────
def calc_ema(prices, period):
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema
 
def check_market_regime():
    try:
        bars = get_bars("BTC/USD", "1Day", 210)
        if len(bars) < 200:
            return "UNKNOWN"
        closes = [b["close"] for b in bars]
        ema200 = calc_ema(closes, 200)
        if not ema200:
            return "UNKNOWN"
        regime = "BULL" if closes[-1] > ema200[-1] else "BEAR"
        log.info(f"Market regime: {regime} | BTC={closes[-1]:.0f} | 200EMA={ema200[-1]:.0f}")
        return regime
    except Exception as e:
        log.error(f"Regime check error: {e}")
        return "UNKNOWN"
 
# ── Core breakout detection ─────────────────────────────────────────────────────
def detect_breakout(symbol):
    try:
        bars = get_bars(symbol, "5Min", 40)
        if len(bars) < 15:
            return {}
 
        closes  = [b["close"]  for b in bars]
        highs   = [b["high"]   for b in bars]
        lows    = [b["low"]    for b in bars]
        volumes = [b["volume"] for b in bars]
        opens   = [b["open"]   for b in bars]
 
        # Stale data check
        if all(v == 0 for v in volumes[-5:]):
            log.warning(f"{symbol} stale volume — skipping")
            return {}
 
        price = closes[-1]
        curr_open  = opens[-1]
        curr_high  = highs[-1]
        curr_low   = lows[-1]
        curr_close = closes[-1]
        curr_vol   = volumes[-1]
 
        avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes)
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0
 
        # ── Step 1: Check for momentum override FIRST ──────────────────────────
        # If a single candle moves 2.5%+ on 3x volume, override the bear filter
        candle_pct = abs(curr_close - curr_open) / curr_open * 100 if curr_open > 0 else 0
        momentum_override = (
            candle_pct >= STRATEGY["momentum_override_pct"] and
            vol_ratio >= STRATEGY["momentum_override_volume"] and
            curr_close > curr_open  # Must be bullish candle
        )
 
        if momentum_override:
            log.info(f"{symbol} MOMENTUM OVERRIDE | candle={round(candle_pct,2)}% vol={round(vol_ratio,1)}x")
 
        # ── Step 2: Consolidation detection ────────────────────────────────────
        lookback = STRATEGY["consolidation_candles"]
        if len(bars) < lookback + 2:
            return {}
 
        consol_bars = bars[-(lookback+2):-2]  # Exclude last 2 candles
        consol_highs = [b["high"] for b in consol_bars]
        consol_lows  = [b["low"]  for b in consol_bars]
        consol_range = max(consol_highs) - min(consol_lows)
        consol_pct   = consol_range / price * 100
 
        in_consolidation = consol_pct <= STRATEGY["consolidation_threshold"]
 
        # ── Step 3: Breakout detection ──────────────────────────────────────────
        consol_high = max(consol_highs)
        bar_range   = curr_high - curr_low
        close_ratio = (curr_close - curr_low) / bar_range if bar_range > 0 else 0
        breakout_pct = (curr_close - consol_high) / consol_high * 100 if consol_high > 0 else 0
 
        is_breakout = (
            in_consolidation and
            curr_close > consol_high and
            breakout_pct >= STRATEGY["min_breakout_pct"] and
            vol_ratio >= STRATEGY["breakout_volume_mult"] and
            close_ratio >= STRATEGY["breakout_candle_close_ratio"]
        )
 
        sig_key = symbol.replace("/", "")
        signal = {
            "price": price,
            "vol_ratio": round(vol_ratio, 2),
            "candle_pct": round(candle_pct, 2),
            "consol_pct": round(consol_pct, 2),
            "breakout_pct": round(breakout_pct, 2),
            "close_ratio": round(close_ratio, 2),
            "in_consolidation": in_consolidation,
            "is_breakout": is_breakout,
            "momentum_override": momentum_override,
            "consol_high": round(consol_high, 6),
            "buy_signal": is_breakout or momentum_override
        }
        bot_state["signals"][sig_key] = signal
 
        log.info(f"{symbol} | price={price} vol={round(vol_ratio,1)}x candle={round(candle_pct,2)}% "
                 f"consol={round(consol_pct,2)}% breakout={is_breakout} momentum={momentum_override}")
 
        return signal
 
    except Exception as e:
        log.error(f"Breakout detection error {symbol}: {e}")
        return {}
 
# ── Trading loop ────────────────────────────────────────────────────────────────
def trading_loop():
    if not API_KEY or not API_SECRET:
        log.warning("No Alpaca API keys — bot cannot start")
        return
 
    # Clear stale state
    try:
        import os as _os
        if _os.path.exists("/tmp/breakout_state.json"):
            _os.remove("/tmp/breakout_state.json")
    except:
        pass
 
    add_diary("SYSTEM",
        "Crypto Breakout Bot v1.0 started | "
        "Consolidation + Volume breakout | "
        "Momentum override: 2.5% + 3x vol | "
        "SL=0.75% TP=1.5%", "system")
    log.info("Crypto Breakout Bot v1.0 started")
 
    regime_check_time = None
 
    while True:
        try:
            now = datetime.now(timezone.utc)
 
            refresh_account()
            sync_positions()
 
            # Check regime every 30 minutes
            if not regime_check_time or (now - regime_check_time).total_seconds() > 1800:
                bot_state["market_regime"] = check_market_regime()
                regime_check_time = now
 
            regime = bot_state["market_regime"]
 
            # Clear expired cooldowns
            expired = [s for s, t in list(bot_state["active_cooldowns"].items())
                       if (now - datetime.fromisoformat(t)).total_seconds() > STRATEGY["cooldown_minutes"] * 60]
            for s in expired:
                del bot_state["active_cooldowns"][s]
                log.info(f"Cooldown expired: {s}")
 
            for symbol in SYMBOLS:
                if bot_state["killed"]:
                    break
 
                sig = detect_breakout(symbol)
                if not sig:
                    continue
 
                price = sig["price"]
 
                # ── Exit logic ──────────────────────────────────────────────────
                if symbol in bot_state["positions"]:
                    pos = bot_state["positions"][symbol]
                    entry = pos["entry"]
                    pct = (price - entry) / entry * 100
 
                    should_exit = False
                    reason = ""
 
                    if pct >= STRATEGY["take_profit_pct"]:
                        should_exit = True
                        reason = f"Take profit (+{round(pct,2)}%)"
                    elif pct <= -STRATEGY["stop_loss_pct"]:
                        should_exit = True
                        reason = f"Stop loss ({round(pct,2)}%)"
                        bot_state["active_cooldowns"][symbol] = now.isoformat()
                    elif not sig["buy_signal"] and pct > 0.3:
                        # Exit on loss of momentum if already profitable
                        should_exit = True
                        reason = "Momentum faded"
 
                    if should_exit:
                        success = close_position_alpaca(symbol)
                        if success:
                            qty = pos.get("qty", 0)
                            pnl = (price - entry) * qty
                            win = pnl > 0
                            bot_state["day_pnl"] += pnl
                            bot_state["total_trades"] += 1
                            if win:
                                bot_state["win_count"] += 1
                            entry_type = "win" if win else "loss"
                            add_diary(symbol,
                                f"{'WIN' if win else 'LOSS'} | "
                                f"${entry:,.4f} → ${price:,.4f} | "
                                f"P&L ${round(pnl,2)} ({round(pct,2)}%) | {reason}",
                                entry_type)
                            bot_state["closed_trades"].append({
                                "symbol": symbol, "entry": entry, "exit": price,
                                "pnl": round(pnl,2), "pct": round(pct,2),
                                "win": win, "reason": reason,
                                "time": now.strftime("%H:%M")
                            })
                            sync_positions()
 
                # ── Entry logic ─────────────────────────────────────────────────
                elif (symbol not in bot_state["active_cooldowns"]
                      and not bot_state["killed"]
                      and len(bot_state["positions"]) < STRATEGY["max_positions"]):
 
                    # Allow entry if:
                    # 1. Normal bull market breakout
                    # 2. Momentum override (strong individual move even in bear market)
                    can_enter = False
                    entry_reason = ""
 
                    if sig["is_breakout"] and regime != "BEAR":
                        can_enter = True
                        entry_reason = f"BREAKOUT | consol={sig['consol_pct']}% | vol={sig['vol_ratio']}x"
                    elif sig["momentum_override"]:
                        # Override bear filter for explosive moves
                        can_enter = True
                        entry_reason = f"MOMENTUM OVERRIDE | {sig['candle_pct']}% candle | {sig['vol_ratio']}x vol"
 
                    if can_enter:
                        cash = bot_state["account_cash"]
                        budget = cash * STRATEGY["position_size"]
                        qty = budget / price
 
                        if budget > 10 and qty > 0:
                            order = place_order(symbol, qty, "BUY")
                            if order:
                                add_diary(symbol,
                                    f"BUY | ${price:,.4f} | {entry_reason}",
                                    "trade")
                                sync_positions()
                                log.info(f"ENTERED {symbol} at {price} | {entry_reason}")
 
        except Exception as e:
            log.error(f"Loop error: {e}")
            import traceback
            log.error(traceback.format_exc())
 
        time.sleep(60)
 
threading.Thread(target=trading_loop, daemon=True).start()
 
# ── Flask routes ────────────────────────────────────────────────────────────────
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
    return jsonify({"status": "ok",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "version": bot_state["version"]})
 
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
        "closed_trades": bot_state["closed_trades"][-50:],
        "diary": bot_state["diary"][-100:],
        "day_pnl": bot_state["day_pnl"],
        "total_trades": total,
        "win_rate": round(wins/total*100) if total > 0 else 0,
        "signals": bot_state["signals"],
        "strategy": STRATEGY,
        "account_cash": bot_state["account_cash"],
        "account_equity": bot_state["account_equity"],
        "account_buying_power": bot_state["account_buying_power"],
        "active_cooldowns": bot_state["active_cooldowns"],
        "market_regime": bot_state["market_regime"],
        "version": bot_state["version"]
    }))
 
@app.route("/diary")
def diary():
    return jsonify({"diary": bot_state["diary"]})
 
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
    return jsonify({"trades": bot_state["closed_trades"]})
 
@app.route("/")
def index():
    try:
        with open("index.html") as f:
            return f.read()
    except:
        return jsonify({"status": "Crypto Breakout Bot v1.0 running"})
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
