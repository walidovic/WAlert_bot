# -*- coding: utf-8 -*-
"""
WAlert Pro - Halal Edition (V5)
- Timeframe افتراضي: 5m (قابل للتغيير عبر ENV: TIMEFRAME)
- إشارات BUY/SELL مبنية على توافق مؤشرات: EMA20/50/100 + StochRSI + RSI + Volume + ATR
- الأهداف من الشارت (قمم/قيعان) + إكمال بـ ATR إذا لزم
- وقف الخسارة من الشارت: أقرب قاع (للشراء) أو أقرب قمة (للبيع)
- Telegram برسالة عربية واضحة ومنسقة
- Flask endpoints: "/", "/health", "/test-tg"
"""

import os
import time
import json
import traceback
import threading
from datetime import datetime, timezone

import requests
import websocket
from flask import Flask

# =========================
# ENV / CONFIG
# =========================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TIMEFRAME      = os.environ.get("TIMEFRAME", "5m")   # الافتراضي 5 دقائق
LOOKBACK_BARS  = int(os.environ.get("LOOKBACK_BARS", "300"))
MIN_VOL_USDT   = float(os.environ.get("MIN_VOL_USDT", "50000"))
DEBUG          = os.environ.get("DEBUG", "false").lower() == "true"

# نمط الإشارة: "both" (افتراضي)، أو "cross" أو "breakout" (نحتفظ به للمرونة)
SIGNAL_MODE    = os.environ.get("SIGNAL_MODE", "both").lower()

# فلاتر إضافية (قابلة للضبط من ENV إذا رغبت)
VOL_SPIKE_MULT = float(os.environ.get("VOL_SPIKE_MULT", "1.4"))  # حجم الشمعة ≥ avg * هذا الرقم
RSI_BUY_MIN    = float(os.environ.get("RSI_BUY_MIN", "52"))      # شراء إذا RSI ≥ هذا
RSI_SELL_MAX   = float(os.environ.get("RSI_SELL_MAX", "48"))      # بيع إذا RSI ≤ هذا
ATR_MIN_PORTION= float(os.environ.get("ATR_MIN_PORTION", "0.6"))  # حركة الشمعة ≥ هذا الجزء من ATR

def log(*a):
    if DEBUG:
        print(datetime.utcnow().isoformat(), *a)
    else:
        print(*a)

# =========================
# SYMBOLS
# =========================
def load_symbols():
    """يقرأ العملات من symbols.txt (واحدة في كل سطر).
       لو ما لقاهاش، يستعمل ENV: SYMBOLS أو افتراضي BTC/USDT,ETH/USDT
    """
    try:
        with open("symbols.txt", "r", encoding="utf-8") as f:
            raw = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        # إزالة التكرار مع الحفاظ على الترتيب
        seen = set()
        out = []
        for s in raw:
            if s not in seen:
                out.append(s)
                seen.add(s)
        return out
    except Exception:
        env = os.environ.get("SYMBOLS", "BTC/USDT,ETH/USDT")
        return [x.strip() for x in env.split(",") if x.strip()]

SYMBOLS = load_symbols()

# =========================
# Telegram
# =========================
def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("[TG] not configured.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        r = requests.post(url, data=data, timeout=10)
        if r.status_code != 200:
            log("[TG] fail:", r.status_code, r.text)
            return False
        return True
    except Exception as e:
        log("[TG] exception:", e)
        return False

# =========================
# مؤشرات فنية
# =========================
def ema(series, period):
    k = 2.0 / (period + 1.0)
    out = []
    e = None
    for v in series:
        e = v if e is None else v * k + e * (1.0 - k)
        out.append(e)
    return out

def rsi(closes, period=14):
    n = len(closes)
    if n < period + 2:
        return [50.0] * n
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0.0))
        losses.append(abs(min(d, 0.0)))
    rsis = [50.0] * period
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    rs = (ag / al) if al != 0 else float("inf")
    rsis.append(100.0 - 100.0 / (1.0 + rs))
    for i in range(period + 1, n):
        g = gains[i-1]; l = losses[i-1]
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
        rs = (ag / al) if al != 0 else float("inf")
        rsis.append(100.0 - 100.0 / (1.0 + rs))
    while len(rsis) < n:
        rsis.insert(0, 50.0)
    return rsis

def stoch_rsi(closes, rsi_period=14, stoch_period=14):
    rr = rsi(closes, rsi_period)
    out = []
    for i in range(len(rr)):
        start = max(0, i - stoch_period + 1)
        window = rr[start:i+1]
        lo, hi = min(window), max(window)
        out.append(50.0 if hi - lo == 0 else (rr[i] - lo) / (hi - lo) * 100.0)
    return out

def true_range(h, l, c):
    trs = []
    for i in range(len(c)):
        if i == 0:
            trs.append(h[i] - l[i])
        else:
            trs.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return trs

def atr(h, l, c, period=14):
    trs = true_range(h, l, c)
    if len(trs) == 0:
        return [0.0]
    if len(trs) < period:
        base = sum(trs) / len(trs)
        return [base] * len(trs)
    out = []
    a = sum(trs[:period]) / period
    out.extend([a] * period)
    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        out.append(a)
    while len(out) < len(trs):
        out.insert(0, out[0])
    return out

# =========================
# استخراج مستويات من الشارت
# =========================
def pivots(series, left=3, right=3, mode="high"):
    idxs = []
    for i in range(left, len(series) - right):
        seg = series[i-left : i+right+1]
        if mode == "high":
            if series[i] == max(seg):
                idxs.append(i)
        else:
            if series[i] == min(seg):
                idxs.append(i)
    return idxs

def chart_targets(side, closes, highs, lows, entry, want=5):
    hi_idx = pivots(highs, 3, 3, "high")
    lo_idx = pivots(lows, 3, 3, "low")

    if side == "BUY":
        levels = sorted({highs[i] for i in hi_idx if highs[i] > entry})
        tgs = levels[:want]
        if len(tgs) < want:
            a = atr(highs, lows, closes, 14)[-1]
            missing = want - len(tgs)
            for k in range(1, missing+1):
                tgs.append(round(entry + k * max(a, entry * 0.002), 6))
        tgs = sorted(tgs)[:want]
    else:
        levels = sorted({lows[i] for i in lo_idx if lows[i] < entry}, reverse=True)
        tgs = levels[:want]
        if len(tgs) < want:
            a = atr(highs, lows, closes, 14)[-1]
            missing = want - len(tgs)
            for k in range(1, missing+1):
                tgs.append(round(entry - k * max(a, entry * 0.002), 6))
        tgs = sorted(tgs, reverse=True)[:want]

    return [round(x, 6) for x in tgs]

def swing_stop(side, highs, lows, closes, lookback=10):
    i = len(closes) - 1
    start = max(0, i - lookback)
    if side == "BUY":
        zone = lows[start:i] or [lows[i]]
        return round(min(zone), 6)
    else:
        zone = highs[start:i] or [highs[i]]
        return round(max(zone), 6)

# =========================
# منطق الإشارة (Confluence)
# =========================
def generate_signal_from_lists(opens, highs, lows, closes, volumes, sym):
    if len(closes) < 120:
        return None

    i = len(closes) - 1
    ema20  = ema(closes, 20)
    ema50  = ema(closes, 50)
    ema100 = ema(closes, 100)
    srs    = stoch_rsi(closes, 14, 14)
    rsi_now = rsi(closes, 14)[-1]

    # فلاتر الحجم والحركة
    vol_usdt = closes[i] * volumes[i]
    avg_vol  = sum(volumes[max(0, i-19):i+1]) / min(20, i+1)
    if vol_usdt < MIN_VOL_USDT:
        return None
    vol_ok = volumes[i] >= avg_vol * VOL_SPIKE_MULT

    atr14   = atr(highs, lows, closes, 14)[-1]
    tr_curr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    atr_ok  = tr_curr >= ATR_MIN_PORTION * atr14

    # اتجاه عام
    trend_up   = (ema20[i] > ema50[i] > ema100[i])
    trend_down = (ema20[i] < ema50[i] < ema100[i])

    # StochRSI crossing
    stoch_buy  = srs[i-1] < 30 and srs[i] > srs[i-1]
    stoch_sell = srs[i-1] > 70 and srs[i] < srs[i-1]

    # RSI filters
    rsi_buy_ok  = rsi_now >= RSI_BUY_MIN
    rsi_sell_ok = rsi_now <= RSI_SELL_MAX

    # نقاط التوافق (3 شروط أو أكثر)
    buy_score  = sum([trend_up,  stoch_buy,  rsi_buy_ok,  vol_ok, atr_ok])
    sell_score = sum([trend_down, stoch_sell, rsi_sell_ok, vol_ok, atr_ok])

    side = None
    note_bits = []
    if buy_score >= 3 and SIGNAL_MODE in ("both", "cross", "breakout"):
        side = "BUY"
        if trend_up:   note_bits.append("TrendUp")
        if stoch_buy:  note_bits.append("StochUp")
        if rsi_buy_ok: note_bits.append(f"RSI≥{int(RSI_BUY_MIN)}")
        if vol_ok:     note_bits.append("Vol↑")
        if atr_ok:     note_bits.append("ATR ok")

    elif sell_score >= 3 and SIGNAL_MODE in ("both", "cross", "breakout"):
        side = "SELL"
        if trend_down:   note_bits.append("TrendDown")
        if stoch_sell:   note_bits.append("StochDown")
        if rsi_sell_ok:  note_bits.append(f"RSI≤{int(RSI_SELL_MAX)}")
        if vol_ok:       note_bits.append("Vol↑")
        if atr_ok:       note_bits.append("ATR ok")

    if side is None:
        return None

    entry = closes[i]
    targets = chart_targets(side, closes, highs, lows, entry, want=5)
    stop    = swing_stop(side, highs, lows, closes, lookback=10)

    return {
        "side": side,
        "entry": round(entry, 6),
        "targets": targets,
        "stop": stop,
        "ema20": round(ema20[i], 6),
        "ema50": round(ema50[i], 6),
        "stochrsi": round(srs[i], 2),
        "rsi": round(rsi_now, 2),
        "volume_usdt": int(vol_usdt),
        "avg_vol": int(avg_vol),
        "atr": round(atr14, 6),
        "note": " + ".join(note_bits) if note_bits else "Signal"
    }

# =========================
# Binance REST/WS
# =========================
def to_pair(sym: str) -> str:
    return sym.replace("/", "")

def rest_klines(sym, interval, limit):
    pair = to_pair(sym)
    url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval={interval}&limit={limit}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    opens  = [float(k[1]) for k in data]
    highs  = [float(k[2]) for k in data]
    lows   = [float(k[3]) for k in data]
    closes = [float(k[4]) for k in data]
    vols   = [float(k[5]) for k in data]
    return opens, highs, lows, closes, vols

ohlcv = {s: {"o": [], "h": [], "l": [], "c": [], "v": []} for s in SYMBOLS}
last_alert_key = {}

def seed_all():
    log("Seeding history ...")
    for s in SYMBOLS:
        try:
            o, h, l, c, v = rest_klines(s, TIMEFRAME, min(LOOKBACK_BARS, 500))
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

# =========================
# WS Handlers
# =========================
def on_message(ws, message):
    try:
        msg = json.loads(message)
        data = msg.get("data", {})
        if not data or "k" not in data:
            return

        k = data["k"]
        # نأخذ الشمعة بعد إغلاقها
        if not k.get("x", False):
            return

        pair = data.get("s", "")
        sym = None
        for s in SYMBOLS:
            if to_pair(s).upper() == pair.upper():
                sym = s
                break
        if not sym:
            return

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

        for key in ("o", "h", "l", "c", "v"):
            if len(ohlcv[sym][key]) > LOOKBACK_BARS:
                ohlcv[sym][key] = ohlcv[sym][key][-LOOKBACK_BARS:]

        sig = generate_signal_from_lists(
            ohlcv[sym]["o"], ohlcv[sym]["h"], ohlcv[sym]["l"],
            ohlcv[sym]["c"], ohlcv[sym]["v"], sym
        )

        if sig:
            ts = datetime.fromtimestamp(k["t"]/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            key = (sym, sig["side"], k["t"])
            if last_alert_key.get(sym) == key:
                return
            last_alert_key[sym] = key

            if sig["side"] == "BUY":
                header = "🟢 <b>إشارة شراء</b>"
            else:
                header = "🔴 <b>إشارة بيع</b>"

            targets_html = "\n".join([f"   {i+1}) <code>{t}</code>" for i, t in enumerate(sig["targets"])])

            text = (
                f"{header} — <b>{sym}</b>  •  {TIMEFRAME}\n"
                f"✍ سبب الإشارة: {sig['note']}\n"
                f"🕰 الزمن: {ts}\n"
                f"\n"
                f"💰 السعر الحالي: <b>{sig['entry']}</b>\n"
                f"🎯 الأهداف (TP):\n{targets_html}\n"
                f"✋ الوقف (SL): <b>{sig['stop']}</b>\n"
                f"\n"
                f"📊 مؤشرات:\n"
                f"• EMA20: {sig['ema20']}  |  EMA50: {sig['ema50']}\n"
                f"• RSI(14): {sig.get('rsi','-')}  |  StochRSI: {sig['stochrsi']}\n"
                f"• حجم: ~${sig['volume_usdt']}  (متوسط: ~${sig['avg_vol']})\n"
                f"• ATR14: {sig.get('atr','-')}\n"
            )
            tg_send(text)

    except Exception:
        traceback.print_exc()

def on_error(ws, error):
    log("WS error:", error)

def on_close(ws, code, reason):
    log("WS closed", code, reason)

def on_open(ws):
    log("WS connected:", stream_url)

def ws_loop():
    while True:
        try:
            ws = websocket.WebSocketApp(
                stream_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(ping_interval=60, ping_timeout=10)
        except Exception as e:
            log("WS loop exception:", e)
            time.sleep(5)

# =========================
# Flask (Health / Test)
# =========================
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

# =========================
# START
# =========================
def start():
    seed_all()
    t = threading.Thread(target=ws_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    url = os.environ.get("RENDER_EXTERNAL_URL", "")
    try:
        tg_send(f"✅ البوت V5 شغّال الآن.\n🌐 {url}\n🕒 الإطار: {TIMEFRAME}\n🔔 رموز: {', '.join(SYMBOLS[:10])} ...")
    except Exception:
        pass
    start()
