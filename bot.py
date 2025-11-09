import os, time, threading, traceback
from datetime import datetime, timezone

import ccxt
import requests
from flask import Flask

# ========= الإعدادات من Environment =========
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SYMBOLS          = [s.strip() for s in os.environ.get("SYMBOLS", "ZEC/USDT,TAO/USDT").split(",") if s.strip()]
TIMEFRAME        = os.environ.get("TIMEFRAME", "1m")
LOOKBACK_BARS    = int(os.environ.get("LOOKBACK_BARS", "200"))
SLEEP_SECONDS    = int(os.environ.get("SLEEP_SECONDS", "20"))
TP_STEPS_RAW     = os.environ.get("TP_STEPS", "0.8,1.6,2.4")
SL_PCT           = float(os.environ.get("SL_PCT", "0.8"))
MIN_VOL_USDT     = float(os.environ.get("MIN_VOL_USDT", "200000"))
EXCHANGE_ID      = os.environ.get("EXCHANGE", "binance")
DEBUG            = os.environ.get("DEBUG", "false").lower() == "true"

TP_LIST = [float(x) / 100.0 for x in TP_STEPS_RAW.split(",") if x.strip()]

# ========= Telegram =========
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

# ========= مؤشرات خفيفة =========
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

# ========= منطق الإشارة =========
def generate_signal(ohlcv):
    closes = [c[4] for c in ohlcv]
    volumes = [c[5] for c in ohlcv]
    i = len(closes) - 1
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    srs   = stoch_rsi(closes, 14, 14)

    vol_usdt = closes[i] * volumes[i]
    if vol_usdt < MIN_VOL_USDT:
        return None

    bull_cross = ema20[i] > ema50[i] and ema20[i - 1] <= ema50[i - 1]
    bear_cross = ema20[i] < ema50[i] and ema20[i - 1] >= ema50[i - 1]

    buy  = bull_cross and srs[i - 1] < 20 and srs[i] > srs[i - 1]
    sell = bear_cross and srs[i - 1] > 80 and srs[i] < srs[i - 1]

    if not (buy or sell):
        return None

    side  = "BUY" if buy else "SELL"
    entry = closes[i]

    if side == "BUY":
        targets = [round(entry * (1 + x), 6) for x in TP_LIST]
        stop    = round(entry * (1 - SL_PCT / 100.0), 6)
    else:
        targets = [round(entry * (1 - x), 6) for x in TP_LIST]
        stop    = round(entry * (1 + SL_PCT / 100.0), 6)

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

# ========= التهيئة =========
exchange = getattr(ccxt, EXCHANGE_ID)({"enableRateLimit": True, "options": {"defaultType": "spot"}})
last_alert_key = {}

# ========= الحلقة الرئيسية =========
def monitor_loop():
    tg_send("✅ البوت اشتغل بنجاح ويخدم 24/24 على Render.")
    while True:
        try:
            for sym in SYMBOLS:
                ohlcv = exchange.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=LOOKBACK_BARS)
                if not ohlcv or len(ohlcv) < 60:
                    continue

                sig = generate_signal(ohlcv)
                if not sig:
                    if DEBUG:
                        print(f"[{sym}] no signal")
                    continue

                k = (sym, sig["side"], ohlcv[-1][0])
                if last_alert_key.get(sym) == k:
                    continue
                last_alert_key[sym] = k

                ts_ms = ohlcv[-1][0]
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

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

                if DEBUG:
                    time.sleep(0.5)

        except Exception as e:
            traceback.print_exc()
            tg_send(f"⚠️ خطأ غير متوقع: {e}")

        time.sleep(SLEEP_SECONDS)

# ========= Flask Health =========
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return "healthy"

def start():
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    start()
