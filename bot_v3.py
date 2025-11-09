# bot_v3.py
import os
import time
import threading
import traceback
import json
from datetime import datetime, timezone

import requests
import websocket
from flask import Flask

# ------------------ CONFIG (ENV) ------------------
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# fallback to environment SYMBOLS if symbols.txt missing
def load_symbols():
    try:
        with open("symbols.txt", "r") as f:
            return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    except Exception:
        s = os.environ.get("SYMBOLS", "ZEC/USDT,TAO/USDT")
        return [x.strip() for x in s.split(",") if x.strip()]

SYMBOLS = load_symbols()                # each like "ZEC/USDT"
TIMEFRAME = os.environ.get("TIMEFRAME", "1m")
LOOKBACK_BARS = int(os.environ.get("LOOKBACK_BARS", "200"))
SLEEP_SECONDS = int(os.environ.get("SLEEP_SECONDS", "5"))
TP_STEPS_RAW = os.environ.get("TP_STEPS", "0.5,1.0,1.5,2.0,2.5")  # in percent
SL_PCT = float(os.environ.get("SL_PCT", "0.8"))  # percent
MIN_VOL_USDT = float(os.environ.get("MIN_VOL_USDT", "100000"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

TP_STEPS = [float(x) / 100.0 for x in TP_STEPS_RAW.split(",") if x.strip()]

# ------------------ Telegram helper ------------------
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] TELEGRAM not configured. Message:\n", text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print("TG send failed:", r.status_code, r.text)
    except Exception as e:
        print("Telegram send exception:", e)

# ------------------ Indicators ------------------
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
        ch = closes[i] - closes[i-1]
        gains.append(max(ch, 0))
        losses.append(abs(min(ch, 0)))
    rsis = [50.0] * period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = (avg_gain / avg_loss) if avg_loss != 0 else float("inf")
    rsis.append(100 - (100 / (1 + rs)))
    for i in range(period+1, len(closes)):
        gain = gains[i-1]
        loss = losses[i-1]
        avg_gain = (avg_gain * (period-1) + gain) / period
        avg_loss = (avg_loss * (period-1) + loss) / period
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
        window = r[start:i+1]
        lo, hi = min(window), max(window)
        val = 50.0 if hi - lo == 0 else (r[i] - lo) / (hi - lo) * 100.0
        out.append(val)
    return out

# ------------------ Signal logic ------------------
def generate_signal_from_lists(closes, volumes):
    if len(closes) < 60:
        return None
    i = len(closes) - 1
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    srs = stoch_rsi(closes, 14, 14)
    vol_usdt = closes[i] * volumes[i]
    if vol_usdt < MIN_VOL_USDT:
        return None
    bull_cross = ema20[i] > ema50[i] and ema20[i-1] <= ema50[i-1]
    bear_cross = ema20[i] < ema50[i] and ema20[i-1] >= ema50[i-1]
    buy = bull_cross and srs[i-1] < 20 and srs[i] > srs[i-1]
    sell = bear_cross and srs[i-1] > 80 and srs[i] < srs[i-1]
    if not (buy or sell):
        return None
    side = "BUY" if buy else "SELL"
    entry = closes[i]
    if side == "BUY":
        targets = [round(entry * (1 + p), 6) for p in TP_STEPS]
        stop = round(entry * (1 - SL_PCT / 100.0), 6)
    else:
        targets = [round(entry * (1 - p), 6) for p in TP_STEPS]
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

# ------------------ Binance helpers (REST seed + WS) ------------------
def to_pair(sym):
    # "ZEC/USDT" -> "ZECUSDT"
    return sym.replace("/", "")

def fetch_klines_rest(sym, interval, limit):
    try:
        pair = to_pair(sym)
        url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval={interval}&limit={limit}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        closes = [float(k[4]) for k in data]
        vols = [float(k[5]) for k in data]
        return closes, vols
    except Exception as e:
        print("Seed fetch error", sym, e)
        return [], []

# storage
ohlcv_store = {s: {"closes": [], "volumes": []} for s in SYMBOLS}
last_alert_key = {}

# seed initial history so indicators work quickly
def seed_all():
    for s in SYMBOLS:
        c, v = fetch_klines_rest(s, TIMEFRAME, min(LOOKBACK_BARS, 500))
        if c and v:
            ohlcv_store[s]["closes"] = c[-LOOKBACK_BARS:]
            ohlcv_store[s]["volumes"] = v[-LOOKBACK_BARS:]
            print(f"Seeded {len(ohlcv_store[s]['closes'])} candles for {s}")
        time.sleep(0.25)

# build stream URL (combined)
def symbol_to_stream(sym):
    return f"{to_pair(sym).lower()}@kline_{TIMEFRAME}"

def build_stream_url(symbols):
    streams = [symbol_to_stream(s) for s in symbols]
    return "wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)

stream_url = build_stream_url(SYMBOLS)

# ------------------ WebSocket callbacks ------------------
def on_message(ws, message):
    try:
        msg = json.loads(message)
        data = msg.get("data", {})
        if not data or "k" not in data:
            return
        k = data["k"]
        if not k.get("x", False):  # only closed candles
            return
        sym_stream = data.get("s", "")  # e.g., "ZECUSDT"
        pair = sym_stream
        # convert back to our format if possible
        sym = None
        for s in SYMBOLS:
            if to_pair(s).upper() == pair.upper():
                sym = s
                break
        if not sym:
            return
        close = float(k["c"])
        vol = float(k["v"])
        store = ohlcv_store.get(sym)
        if store is None:
            return
        store["closes"].append(close)
        store["volumes"].append(vol)
        if len(store["closes"]) > LOOKBACK_BARS:
            store["closes"] = store["closes"][-LOOKBACK_BARS:]
            store["volumes"] = store["volumes"][-LOOKBACK_BARS:]
        sig = generate_signal_from_lists(store["closes"], store["volumes"])
        if sig:
            ts = datetime.fromtimestamp(k["t"]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            key = (sym, sig["side"], k["t"])
            if last_alert_key.get(sym) != key:
                last_alert_key[sym] = key
                targets = sig["targets"]
                targets_html = "\n".join([f"{i+1}. <code>{t}</code>" for i,t in enumerate(targets)])
                text = (
                    f"📣 <b>إشارة {sig['side']}</b> — <b>{sym}</b> ({TIMEFRAME})\n"
                    f"⏱ {ts}\n"
                    f"💰 السعر: <b>{sig['entry']}</b>\n"
                    f"🎯 الأهداف:\n{targets_html}\n"
                    f"🛑 ستوب: <b>{sig['stop']}</b>\n"
                    f"📈 EMA20: {sig['ema20']} | EMA50: {sig['ema50']}\n"
                    f"⚡️ StochRSI: {sig['stochrsi']} | حجم~ ${sig['volume_usdt']}\n"
                    "ℹ️ نصيحة: قسّم الخروج على الأهداف وحرّك الستوب لــ BE بعد الهدف الأول."
                )
                tg_send(text)
    except Exception:
        traceback.print_exc()

def on_error(ws, error):
    print("WS error:", error)

def on_close(ws, code, reason):
    print("WebSocket closed", code, reason)

def on_open(ws):
    print("WebSocket connected to:", stream_url)

def ws_loop():
    while True:
        try:
            ws = websocket.WebSocketApp(stream_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close)
            ws.run_forever(ping_interval=60, ping_timeout=10)
        except Exception as e:
            print("WS loop exception:", e)
            time.sleep(5)

# ------------------ Flask health + test ------------------
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return "healthy"

@app.get("/test-tg")
def test_tg():
    tg_send("🤖 اختبار: البوت يقدر يبعث تيليجرام بنجاح.")
    return "sent"

# ------------------ hot-reload symbols watcher ------------------
def symbols_watcher():
    global SYMBOLS, stream_url, ohlcv_store
    last_mtime = None
    while True:
        try:
            if os.path.exists("symbols.txt"):
                mtime = os.path.getmtime("symbols.txt")
            else:
                mtime = None
            if mtime != last_mtime:
                last_mtime = mtime
                new = load_symbols()
                if set(new) != set(SYMBOLS):
                    print("Symbols changed:", new)
                    SYMBOLS = new
                    stream_url = build_stream_url(SYMBOLS)
                    ohlcv_store = {s: {"closes": [], "volumes": []} for s in SYMBOLS}
                    seed_all()
            time.sleep(5)
        except Exception:
            traceback.print_exc()
            time.sleep(5)

# ------------------ starter ------------------
def send_startup_message():
    try:
        url = os.environ.get("RENDER_EXTERNAL_URL", "")
        tg_send(
            "✅ البوت V3 شغّال الآن.\n"
            f"🌐 {url}\n"
            f"🔔 Symbols: {', '.join(SYMBOLS)}\n"
            f"⏱ Timeframe: {TIMEFRAME}\n"
            f"⚙️ TP steps: {TP_STEPS_RAW} % | SL: {SL_PCT}%"
        )
    except Exception as e:
        print("Startup msg error:", e)

def start():
    seed_all()
    t_ws = threading.Thread(target=ws_loop, daemon=True)
    t_ws.start()
    t_w = threading.Thread(target=symbols_watcher, daemon=True)
    t_w.start()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    send_startup_message()
    start()
