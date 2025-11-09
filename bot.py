# bot_v2.py
import os
import time
import threading
import traceback
from datetime import datetime, timezone

import requests
import websocket
from flask import Flask

# ======== إعدادات من Environment ========
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

def load_symbols():
    try:
        with open("symbols.txt", "r") as f:
            return [line.strip() for line in f if line.strip()]
    except Exception:
        return [s.strip() for s in os.environ.get("SYMBOLS", "ZEC/USDT,TAO/USDT").split(",") if s.strip()]

SYMBOLS = load_symbols()              # قائمة رموز: ["ZEC/USDT", "TAO/USDT", ...]
TIMEFRAME = os.environ.get("TIMEFRAME", "1m")
LOOKBACK_BARS = int(os.environ.get("LOOKBACK_BARS", "200"))
SLEEP_SECONDS = int(os.environ.get("SLEEP_SECONDS", "20"))
TP_STEPS_RAW = os.environ.get("TP_STEPS", "0.8,1.6,2.4")
SL_PCT = float(os.environ.get("SL_PCT", "0.8"))
MIN_VOL_USDT = float(os.environ.get("MIN_VOL_USDT", "200000"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

TP_LIST = [float(x) / 100.0 for x in TP_STEPS_RAW.split(",") if x.strip()]

# ======== Telegram helper ========
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] TELEGRAM env missing. Message:", text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# ======== مؤشرات بسيطة ========
def ema(series, period):
    k = 2 / (period + 1)
    out = []
    ema_val = None
    for v in series:
        ema_val = v if ema_val is None else (v * k + ema_val * (1 - k))
        out.append(ema_val)
    return out

def rsi(closes, period=14):
    if len(closes) < period + 2:
        return [50.0] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0))
        losses.append(abs(min(ch, 0)))
    rsis = [50.0] * period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = (avg_gain / avg_loss) if avg_loss != 0 else float("inf")
    rsis.append(100 - (100 / (1 + rs)))
    for i in range(period + 1, len(closes)):
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = (avg_gain / avg_loss) if avg_loss != 0 else float("inf")
        rsis.append(100 - (100 / (1 + rs)))
    while len(rsis) < len(closes):
        rsis.insert(0, 50.0)
    return rsis

def stoch_rsi(closes, rsi_period=14, stoch_period=14):
    r = rsi(closes, rsi_period)
    out = []
    for i in range(len(r)):
        start = max(0, i - stoch_period + 1)
        window = r[start : i + 1]
        lo, hi = min(window), max(window)
        val = 50.0 if hi - lo == 0 else (r[i] - lo) / (hi - lo) * 100.0
        out.append(val)
    return out

# ======== منطق الإشارة ========
def generate_signal_from_lists(closes, volumes):
    if len(closes) < 60:
        return None
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    srs = stoch_rsi(closes, 14, 14)
    i = len(closes) - 1
    vol_usdt = closes[i] * volumes[i]
    if vol_usdt < MIN_VOL_USDT:
        return None
    bull_cross = ema20[i] > ema50[i] and ema20[i - 1] <= ema50[i - 1]
    bear_cross = ema20[i] < ema50[i] and ema20[i - 1] >= ema50[i - 1]
    buy = bull_cross and srs[i - 1] < 20 and srs[i] > srs[i - 1]
    sell = bear_cross and srs[i - 1] > 80 and srs[i] < srs[i - 1]
    if not (buy or sell):
        return None
    side = "BUY" if buy else "SELL"
    entry = closes[i]
    if side == "BUY":
        targets = [round(entry * (1 + x), 6) for x in TP_LIST]
        stop = round(entry * (1 - SL_PCT / 100.0), 6)
    else:
        targets = [round(entry * (1 - x), 6) for x in TP_LIST]
        stop = round(entry * (1 + SL_PCT / 100.0), 6)
    return {
        "side": side,
        "entry": round(entry, 6),
        "targets": targets,
        "stop": stop,
        "ema20": round(ema20[i], 6),
        "ema50": round(ema50[i], 6),
        "stochrsi": round(srs[i], 2),
        "volume_usdt": int(vol_usdt),
    }

# ======== WebSocket stream setup ========
# Build streams like: zecusdt@kline_1m
def symbol_to_stream(sym):
    # sym like "ZEC/USDT" -> "zecusdt@kline_1m"
    s = sym.replace("/", "").lower()
    return f"{s}@kline_{TIMEFRAME}"

streams = [symbol_to_stream(s) for s in SYMBOLS]
if not streams:
    raise SystemExit("No symbols provided.")

stream_url = "wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)

# storage: for each symbol store list of closes and volumes (from closed klines)
ohlcv_store = {s: {"closes": [], "volumes": []} for s in SYMBOLS}
last_alert_key = {}

def on_message(ws, message):
    try:
        import json
        msg = json.loads(message)
        data = msg.get("data", {})
        stream = msg.get("stream", "")
        if not data or "k" not in data:
            return
        k = data["k"]
        sym_stream = data.get("s", "")  # e.g., "ZECUSDT"
        # convert back to format like "ZEC/USDT"
        sym = sym_stream[:-4] + "/USDT" if sym_stream.endswith("USDT") else sym_stream
        # only when kline closed
        if not k.get("x", False):
            return
        close = float(k["c"])
        vol = float(k["v"])
        # push to store
        if sym not in ohlcv_store:
            # maybe different symbol formatting, try find matching ignoring case/slashes
            for ksym in list(ohlcv_store.keys()):
                if ksym.replace("/", "").lower() == sym_stream.lower():
                    sym = ksym
                    break
        lst = ohlcv_store.get(sym)
        if lst is None:
            return
        lst["closes"].append(close)
        lst["volumes"].append(vol)
        # trim
        if len(lst["closes"]) > LOOKBACK_BARS:
            lst["closes"] = lst["closes"][-LOOKBACK_BARS:]
            lst["volumes"] = lst["volumes"][-LOOKBACK_BARS:]
        # compute signal
        sig = generate_signal_from_lists(lst["closes"], lst["volumes"])
        if sig:
            ts = datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            key = (sym, sig["side"], k["t"])
            if last_alert_key.get(sym) != key:
                last_alert_key[sym] = key
                targets_str = ", ".join([f"<code>{t}</code>" for t in sig["targets"]])
                lines = [
                    f"📣 <b>إشارة {sig['side']}</b> — <b>{sym}</b> ({TIMEFRAME})",
                    f"⏱ {ts}",
                    f"💰 السعر: <b>{sig['entry']}</b>",
                    f"🎯 الأهداف: {targets_str}",
                    f"🛑 ستوب: <b>{sig['stop']}</b>",
                    f"📈 EMA20: {sig['ema20']} | EMA50: {sig['ema50']}",
                    f"⚡️ StochRSI: {sig['stochrsi']} | حجم~ ${sig['volume_usdt']}",
                    "ℹ️ نصيحة: وزّع الخروج على 2-3 أهداف وحرّك الستوب لـ BE بعد الهدف الأول.",
                ]
                tg_send("\n".join(lines))
    except Exception:
        traceback.print_exc()

def on_error(ws, error):
    print("WS error:", error)

def on_close(ws, close_status_code, close_msg):
    print("WebSocket closed:", close_status_code, close_msg)

def on_open(ws):
    print("WebSocket connected to:", stream_url)

def ws_thread():
    while True:
        try:
            ws = websocket.WebSocketApp(
                stream_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            # run_forever will block; we run it and reconnect on exceptions
            ws.run_forever(ping_interval=60, ping_timeout=10)
        except Exception as e:
            print("WS loop exception:", e)
            time.sleep(5)

# ======== Flask health & starter ========
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return "healthy"

def start():
    # Start websocket in background
    t = threading.Thread(target=ws_thread, daemon=True)
    t.start()
    # Also start a lightweight loop that reloads symbols.txt every minute (hot reload)
    def symbols_watcher():
        global SYMBOLS, streams, stream_url, ohlcv_store
        last_mtime = None
        while True:
            try:
                if os.path.exists("symbols.txt"):
                    mtime = os.path.getmtime("symbols.txt")
                else:
                    mtime = None
                if mtime != last_mtime:
                    last_mtime = mtime
                    new_symbols = load_symbols()
                    if set(new_symbols) != set(SYMBOLS):
                        print("Symbols changed ->", new_symbols)
                        SYMBOLS = new_symbols
                        streams = [symbol_to_stream(s) for s in SYMBOLS]
                        stream_url = "wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)
                        # reset stores
                        ohlcv_store = {s: {"closes": [], "volumes": []} for s in SYMBOLS}
                        # NOTE: websocket thread will reconnect automatically soon (fallback)
                time.sleep(5)
            except Exception:
                traceback.print_exc()
                time.sleep(5)
    tw = threading.Thread(target=symbols_watcher, daemon=True)
    tw.start()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    tg_send("✅ البوت V2 بدأ (websocket) ويخدم 24/24.")
    start()
