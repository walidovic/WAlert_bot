# bot_v5.py
import os, time, json, traceback, threading
from datetime import datetime, timezone
import requests
import websocket
from flask import Flask

# ========= ENV =========
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TIMEFRAME = os.environ.get("TIMEFRAME", "5m")  # الافتراضي 5 دقائق
LOOKBACK_BARS = int(os.environ.get("LOOKBACK_BARS", "300"))
MIN_VOL_USDT = float(os.environ.get("MIN_VOL_USDT", "50000"))
TP_STEPS_RAW = os.environ.get("TP_STEPS", "0.5,1.0,1.5,2.0,2.5")  # يُستعمل فقط عند نقص الأهداف من الشارت
SL_PCT = float(os.environ.get("SL_PCT", "0.8"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

SIGNAL_MODE = os.environ.get("SIGNAL_MODE", "both").lower()  # cross | breakout | both
BREAKOUT_PCT = float(os.environ.get("BREAKOUT_PCT", "0.30")) / 100.0
STOCH_LOW = float(os.environ.get("STOCH_LOW", "30"))
STOCH_HIGH = float(os.environ.get("STOCH_HIGH", "70"))
VOL_SPIKE_MULT = float(os.environ.get("VOL_SPIKE_MULT", "1.5"))

TP_STEPS = [float(x)/100.0 for x in TP_STEPS_RAW.split(",") if x.strip()]

def log(*a):
    if DEBUG:
        print(datetime.utcnow().isoformat(), *a)
    else:
        print(*a)

# ========= SYMBOLS =========
def load_symbols():
    try:
        with open("symbols.txt", "r") as f:
            syms = []
            for line in f:
                s = line.strip()
                if s and not s.startswith("#"):
                    syms.append(s)
            # أزل التكرارات مع الحفاظ على الترتيب
            seen = set()
            out = []
            for s in syms:
                if s not in seen:
                    out.append(s); seen.add(s)
            return out
    except Exception:
        env = os.environ.get("SYMBOLS", "BTC/USDT,ETH/USDT")
        return [x.strip() for x in env.split(",") if x.strip()]

SYMBOLS = load_symbols()

# ========= Telegram =========
def tg_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("[TG] not configured", text[:100])
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        r = requests.post(url, data=data, timeout=10)
        if r.status_code != 200:
            log("[TG] fail", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        log("[TG] exception", e)
        return False

# ========= مؤشرات =========
def ema(series, period):
    k = 2/(period+1)
    out = []; e = None
    for v in series:
        e = v if e is None else v*k + e*(1-k)
        out.append(e)
    return out

def rsi(closes, period=14):
    if len(closes) < period+2: return [50.0]*len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gains.append(max(d,0)); losses.append(abs(min(d,0)))
    rsis = [50.0]*period
    ag = sum(gains[:period])/period
    al = sum(losses[:period])/period
    rs = ag/al if al != 0 else float("inf")
    rsis.append(100 - 100/(1+rs))
    for i in range(period+1, len(closes)):
        g = gains[i-1]; l = losses[i-1]
        ag = (ag*(period-1)+g)/period
        al = (al*(period-1)+l)/period
        rs = ag/al if al != 0 else float("inf")
        rsis.append(100 - 100/(1+rs))
    while len(rsis) < len(closes): rsis.insert(0,50.0)
    return rsis

def stoch_rsi(closes, rsi_period=14, stoch_period=14):
    rr = rsi(closes, rsi_period)
    out = []
    for i in range(len(rr)):
        start = max(0, i-stoch_period+1)
        w = rr[start:i+1]
        lo, hi = min(w), max(w)
        out.append(50.0 if hi-lo==0 else (rr[i]-lo)/(hi-lo)*100.0)
    return out

def true_range(h, l, c):
    trs = []
    for i in range(len(c)):
        if i == 0:
            trs.append(h[i]-l[i])
        else:
            trs.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return trs

def atr(h, l, c, period=14):
    trs = true_range(h,l,c)
    if len(trs) < period: return [sum(trs)/len(trs)]*len(trs)
    out = []
    a = sum(trs[:period])/period
    out.extend([a]*(period))
    for i in range(period, len(trs)):
        a = (a*(period-1) + trs[i]) / period
        out.append(a)
    while len(out) < len(trs): out.insert(0, out[0])
    return out

# ========= استخراج أهداف من الشارت =========
def pivots(series, left=3, right=3, mode="high"):
    """يرجع مواقع القمم/القيعان المحلية"""
    idxs = []
    for i in range(left, len(series)-right):
        segment = series[i-left:i+right+1]
        if mode == "high":
            if series[i] == max(segment):
                idxs.append(i)
        else:
            if series[i] == min(segment):
                idxs.append(i)
    return idxs

def chart_targets(side, closes, highs, lows, entry, want=5):
    # نبحث عن 5 أهداف من القمم/القيعان الواقعية
    hi_idx = pivots(highs, 3, 3, "high")
    lo_idx = pivots(lows, 3, 3, "low")

    tgs = []
    if side == "BUY":
        # كل مقاومات أعلى من السعر الحالي
        levels = sorted({highs[i] for i in hi_idx if highs[i] > entry})
        tgs = levels[:want]
        # لو ما كفاش، كمّل بـ ATR
        if len(tgs) < want:
            a = atr(highs, lows, closes, 14)[-1]
            missing = want - len(tgs)
            for k in range(1, missing+1):
                tgs.append(round(entry + k*max(a, entry*0.002), 6))
    else:
        # كل دعوم أقل من السعر الحالي
        levels = sorted({lows[i] for i in lo_idx if lows[i] < entry}, reverse=True)
        tgs = levels[:want]
        if len(tgs) < want:
            a = atr(highs, lows, closes, 14)[-1]
            missing = want - len(tgs)
            for k in range(1, missing+1):
                tgs.append(round(entry - k*max(a, entry*0.002), 6))
    # رتّبهم تصاعدي في الشراء، تنازلي في البيع
    if side == "BUY":
        tgs = sorted(tgs)[:want]
    else:
        tgs = sorted(tgs, reverse=True)[:want]
    return [round(x,6) for x in tgs]

# ========= منطق الإشارة =========
def generate_signal_from_lists(opens, highs, lows, closes, volumes, sym):
    if len(closes) < 60: return None
    i = len(closes)-1
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    srs   = stoch_rsi(closes, 14, 14)

    vol_usdt = closes[i] * volumes[i]
    avg_vol  = sum(volumes[max(0,i-19):i+1]) / min(20, i+1)
    if vol_usdt < MIN_VOL_USDT:
        return None

    # Cross
    bull_cross = (ema20[i] > ema50[i] and ema20[i-1] <= ema50[i-1])
    bear_cross = (ema20[i] < ema50[i] and ema20[i-1] >= ema50[i-1])
    buy_cross  = bull_cross and srs[i-1] < STOCH_LOW and srs[i] >= srs[i-1]
    sell_cross = bear_cross and srs[i-1] > STOCH_HIGH and srs[i] <= srs[i-1]

    # Breakout
    buy_breakout  = (closes[i] > ema20[i] > ema50[i]) and (closes[i] > ema20[i]*(1+BREAKOUT_PCT)) \
                    and (srs[i] >= max(55, STOCH_HIGH-10)) and (volumes[i] >= avg_vol*VOL_SPIKE_MULT)
    sell_breakout = (closes[i] < ema20[i] < ema50[i]) and (closes[i] < ema20[i]*(1-BREAKOUT_PCT)) \
                    and (srs[i] <= min(45, STOCH_LOW+10)) and (volumes[i] >= avg_vol*VOL_SPIKE_MULT)

    mode = SIGNAL_MODE
    buy = sell = False
    if mode == "cross":
        buy, sell = buy_cross, sell_cross
    elif mode == "breakout":
        buy, sell = buy_breakout, sell_breakout
    else:
        buy  = buy_cross  or buy_breakout
        sell = sell_cross or sell_breakout

    if not (buy or sell): return None

    side  = "BUY" if buy else "SELL"
    entry = closes[i]

    # أهداف من الشارت (قمم/قيعان + ATR عند الحاجة)
    targets = chart_targets(side, closes, highs, lows, entry, want=5)

    # ستوب لوز كنسبة (قابلة للتعديل لاحقًا لستوب من الشارت)
    if side == "BUY":
        stop = round(entry * (1 - SL_PCT/100.0), 6)
    else:
        stop = round(entry * (1 + SL_PCT/100.0), 6)

    note = []
    if buy_cross or sell_cross: note.append("Cross")
    if buy_breakout or sell_breakout: note.append("Breakout")
    note = " + ".join(note) if note else "Signal"

    return {
        "side": side,
        "entry": round(entry, 6),
        "targets": targets,
        "stop": stop,
        "ema20": round(ema20[i],6),
        "ema50": round(ema50[i],6),
        "stochrsi": round(srs[i],2),
        "volume_usdt": int(vol_usdt),
        "avg_vol": int(avg_vol),
        "note": note
    }

# ========= Binance REST/WS =========
def to_pair(sym): return sym.replace("/", "")
def rest_klines(sym, interval, limit):
    pair = to_pair(sym)
    url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval={interval}&limit={limit}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    # kline: [0 open_time,1 open,2 high,3 low,4 close,5 volume,...]
    opens  = [float(k[1]) for k in data]
    highs  = [float(k[2]) for k in data]
    lows   = [float(k[3]) for k in data]
    closes = [float(k[4]) for k in data]
    vols   = [float(k[5]) for k in data]
    return opens, highs, lows, closes, vols

ohlcv = {s: {"o": [], "h": [], "l": [], "c": [], "v": []} for s in SYMBOLS}
last_alert_key = {}

def seed_all():
    log("Seeding history...")
    for s in SYMBOLS:
        try:
            o,h,l,c,v = rest_klines(s, TIMEFRAME, min(LOOKBACK_BARS, 500))
            ohlcv[s]["o"] = o[-LOOKBACK_BARS:]
            ohlcv[s]["h"] = h[-LOOKBACK_BARS:]
            ohlcv[s]["l"] = l[-LOOKBACK_BARS:]
            ohlcv[s]["c"] = c[-LOOKBACK_BARS:]
            ohlcv[s]["v"] = v[-LOOKBACK_BARS:]
            log(f"Seeded {len(ohlcv[s]['c'])} candles for {s}")
        except Exception as e:
            log("Seed error", s, e)
        time.sleep(0.25)

def build_stream_url(symbols):
    streams = [f"{to_pair(s).lower()}@kline_{TIMEFRAME}" for s in symbols]
    return "wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)

stream_url = build_stream_url(SYMBOLS)

def on_message(ws, message):
    try:
        msg = json.loads(message)
        data = msg.get("data", {})
        if not data or "k" not in data: return
        k = data["k"]
        if not k.get("x", False): return  # نأخذ الشمعة بعد الإغلاق
        pair = data.get("s", "")
        sym = None
        for s in SYMBOLS:
            if to_pair(s).upper() == pair.upper():
                sym = s; break
        if not sym: return

        close = float(k["c"])
        high  = float(k["h"])
        low   = float(k["l"])
        vol   = float(k["v"])
        openp = float(k["o"])

        ohlcv[sym]["o"].append(openp)
        ohlcv[sym]["h"].append(high)
        ohlcv[sym]["l"].append(low)
        ohlcv[sym]["c"].append(close)
        ohlcv[sym]["v"].append(vol)
        # قصّ الزائد
        for key in ("o","h","l","c","v"):
            if len(ohlcv[sym][key]) > LOOKBACK_BARS:
                ohlcv[sym][key] = ohlcv[sym][key][-LOOKBACK_BARS:]

        sig = generate_signal_from_lists(ohlcv[sym]["o"], ohlcv[sym]["h"], ohlcv[sym]["l"], ohlcv[sym]["c"], ohlcv[sym]["v"], sym)
        if sig:
            ts = datetime.fromtimestamp(k["t"]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            key = (sym, sig["side"], k["t"])
            if last_alert_key.get(sym) == key:
                return
            last_alert_key[sym] = key

            # تجهيز الأهداف في رسالة عربية، BUY 🟢 / SELL 🔴
            if sig:
    ts = datetime.fromtimestamp(k["t"]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    key = (sym, sig["side"], k["t"])
    if last_alert_key.get(sym) == key:
        return
    last_alert_key[sym] = key

    # --- عنوان الإشعار: BUY/SELL ---
    if sig["side"] == "BUY":
        header = "🟢 <b>إشارة شراء</b>"
    else:
        header = "🔴 <b>إشارة بيع</b>"

    # --- تنسيق الأهداف ---
    targets_html = "\n".join([f"   {i+1}) <code>{t}</code>" for i, t in enumerate(sig["targets"])])

    # --- نص الرسالة النهائي ---
    text = (
        f"{header} — <b>{sym}</b>  •  {TIMEFRAME}\n"
        f"سبب الإشارة: {sig['note']}\n"
        f"الزمن: {ts}\n"
        f"\n"
        f"السعر الحالي: <b>{sig['entry']}</b>\n"
        f"الأهداف (TP):\n{targets_html}\n"
        f"الوقف (SL): <b>{sig['stop']}</b>\n"
        f"\n"
        f"مؤشرات:\n"
        f"• EMA20: {sig['ema20']}  |  EMA50: {sig['ema50']}\n"
        f"• RSI(14): {sig.get('rsi','-')}  |  StochRSI: {sig['stochrsi']}\n"
        f"• حجم: ~${sig['volume_usdt']}  (متوسط: ~${sig['avg_vol']})\n"
        f"• ATR14: {sig.get('atr','-')}\n"
    )

    tg_send(text)

    except Exception:
        traceback.print_exc()

def on_error(ws, error): log("WS error:", error)
def on_close(ws, code, reason): log("WS closed", code, reason)
def on_open(ws): log("WS connected:", stream_url)

def ws_loop():
    while True:
        try:
            ws = websocket.WebSocketApp(
                stream_url, on_open=on_open, on_message=on_message, on_error=on_error, on_close=on_close
            )
            ws.run_forever(ping_interval=60, ping_timeout=10)
        except Exception as e:
            log("WS loop exception:", e)
            time.sleep(5)

# ========= Flask =========
app = Flask(__name__)

@app.get("/")
def root():
    return "OK"

@app.get("/health")
def health():
    return "healthy"

@app.get("/test-tg")
def test_tg():
    tg_send("🤖 اختبار: البوت يرسل تيليجرام بنجاح.")
    return "sent"

def start():
    seed_all()
    t = threading.Thread(target=ws_loop, daemon=True); t.start()
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    url = os.environ.get("RENDER_EXTERNAL_URL", "")
    tg_send(f"✅ البوت V5 شغّال الآن.\n🌐 {url}\n🕒 الإطار: {TIMEFRAME}\n🔔 رموز: {', '.join(SYMBOLS[:10])}...")
    start()
