# -*- coding: utf-8 -*-
"""
WAlert Pro V6 (rate-limit safe)
- نفس منطق الإشارات القوي (EMA/RSI/Stoch/MACD/ADX/ATR + MTF 15m/1h)
- حماية من Binance 418 عبر throttling + backoff + seed خفيف قابل للضبط
- WebSocket للحي + REST seed آمن وخفيف
"""

import os, time, json, threading, traceback, random
from datetime import datetime, timezone

import requests
import websocket
from flask import Flask

# =========================
# ENV
# =========================
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

TIMEFRAME      = os.environ.get("TIMEFRAME", "5m")
LOOKBACK_BARS  = int(os.environ.get("LOOKBACK_BARS", "300"))
MIN_VOL_USDT   = float(os.environ.get("MIN_VOL_USDT", "50000"))
DEBUG          = os.environ.get("DEBUG", "false").lower() == "true"

# فلترة الإشارات
VOL_SPIKE_MULT  = float(os.environ.get("VOL_SPIKE_MULT", "1.4"))
RSI_BUY_MIN     = float(os.environ.get("RSI_BUY_MIN", "52"))
RSI_SELL_MAX    = float(os.environ.get("RSI_SELL_MAX", "48"))
ATR_MIN_PORTION = float(os.environ.get("ATR_MIN_PORTION", "0.6"))
COOLDOWN_MIN    = int(os.environ.get("COOLDOWN_MIN", "8"))

# ضبط السِيد الآمن (جديد)
SEED_LIMIT        = int(os.environ.get("SEED_LIMIT", "200"))      # أقل من 300 لتخفيف الضغط
SEED_DELAY_SEC    = float(os.environ.get("SEED_DELAY_SEC", "2.0"))# تأخير بين كل رمز
SEED_RETRIES      = int(os.environ.get("SEED_RETRIES", "3"))      # محاولات عند الفشل
SEED_BACKOFF_BASE = float(os.environ.get("SEED_BACKOFF_BASE", "2.0")) # باك-أوف تصاعدي

def log(*a):
    if DEBUG: print(*a)

# =========================
# SYMBOLS
# =========================
def load_symbols():
    try:
        with open("symbols.txt", "r", encoding="utf-8") as f:
            raw = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("#")]
        seen, out = set(), []
        for s in raw:
            if s not in seen:
                out.append(s); seen.add(s)
        return out
    except Exception:
        env = os.environ.get("SYMBOLS",
            "BTC/USDT,ETH/USDT,SOL/USDT,TAO/USDT,ZEC/USDT")
        return [x.strip() for x in env.split(",") if x.strip()]

SYMBOLS = load_symbols()

# =========================
# Telegram
# =========================
def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("[TG] not configured")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=12
        )
        if r.status_code != 200: log("[TG] fail:", r.status_code, r.text); return False
        return True
    except Exception as e:
        log("[TG] exception:", e)
        return False

# =========================
# Indicators
# =========================
def ema(series, period):
    k = 2.0/(period+1.0); e=None; out=[]
    for v in series:
        e = v if e is None else v*k + e*(1-k)
        out.append(e)
    return out

def rsi(closes, period=14):
    n=len(closes)
    if n<period+2: return [50.0]*n
    gains=[]; losses=[]
    for i in range(1,n):
        d=closes[i]-closes[i-1]
        gains.append(max(d,0.0)); losses.append(abs(min(d,0.0)))
    out=[50.0]*period
    ag=sum(gains[:period])/period; al=sum(losses[:period])/period
    rs=(ag/al) if al!=0 else float("inf")
    out.append(100.0-100.0/(1.0+rs))
    for i in range(period+1,n):
        g=gains[i-1]; l=losses[i-1]
        ag=(ag*(period-1)+g)/period; al=(al*(period-1)+l)/period
        rs=(ag/al) if al!=0 else float("inf")
        out.append(100.0-100.0/(1.0+rs))
    while len(out)<n: out.insert(0,50.0)
    return out

def stoch_rsi(closes, rsi_period=14, stoch_period=14):
    rr=rsi(closes,rsi_period); out=[]
    for i in range(len(rr)):
        w=rr[max(0,i-stoch_period+1):i+1]
        lo,hi=min(w),max(w)
        out.append(50.0 if hi-lo==0 else (rr[i]-lo)/(hi-lo)*100.0)
    return out

def true_range(h,l,c):
    trs=[]
    for i in range(len(c)):
        if i==0: trs.append(h[i]-l[i])
        else: trs.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return trs

def atr(h,l,c,period=14):
    trs=true_range(h,l,c)
    if not trs: return [0.0]
    if len(trs)<period:
        base=sum(trs)/len(trs); return [base]*len(trs)
    out=[]; a=sum(trs[:period])/period; out.extend([a]*period)
    for i in range(period,len(trs)):
        a=(a*(period-1)+trs[i])/period; out.append(a)
    while len(out)<len(trs): out.insert(0,out[0])
    return out

def macd(closes, fast=12, slow=26, signal=9):
    mf=ema(closes,fast); ms=ema(closes,slow)
    macd_line=[a-b for a,b in zip(mf,ms)]
    signal_line=ema(macd_line,signal)
    hist=[m-s for m,s in zip(macd_line,signal_line)]
    return macd_line, signal_line, hist

def adx(highs,lows,closes,period=14):
    n=len(closes)
    if n<period+2: return [0.0]*n,[0.0]*n,[0.0]*n
    plus_dm=[0.0]; minus_dm=[0.0]; tr=[0.0]
    for i in range(1,n):
        up=highs[i]-highs[i-1]; dn=lows[i-1]-lows[i]
        plus_dm.append(up if (up>dn and up>0) else 0.0)
        minus_dm.append(dn if (dn>up and dn>0) else 0.0)
        tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    def wild(arr):
        sm=[]; s=sum(arr[1:period+1]); sm.extend([0.0]*period); sm.append(s)
        for i in range(period+1,n): s=s-(s/period)+arr[i]; sm.append(s)
        return sm
    tr14=wild(tr); pdm14=wild(plus_dm); mdm14=wild(minus_dm)
    plus_di=[0.0]*n; minus_di=[0.0]*n; adxv=[0.0]*n
    for i in range(period,n):
        if tr14[i]==0: plus_di[i]=minus_di[i]=0.0
        else:
            plus_di[i]=100.0*(pdm14[i]/tr14[i]); minus_di[i]=100.0*(mdm14[i]/tr14[i])
        den=plus_di[i]+minus_di[i]; dx=0.0 if den==0 else 100.0*abs(plus_di[i]-minus_di[i])/den
        adxv[i]=dx if i==period else (adxv[i-1]*(period-1)+dx)/period
    return plus_di, minus_di, adxv

# =========================
# Targets/SL
# =========================
def pivots(series, left=3, right=3, mode="high"):
    idx=[]
    for i in range(left, len(series)-right):
        seg=series[i-left:i+right+1]
        if mode=="high":
            if series[i]==max(seg): idx.append(i)
        else:
            if series[i]==min(seg): idx.append(i)
    return idx

def chart_targets(side, closes, highs, lows, entry, want=5):
    hi_idx=pivots(highs,3,3,"high"); lo_idx=pivots(lows,3,3,"low")
    if side=="BUY":
        levels=sorted({highs[i] for i in hi_idx if highs[i]>entry})
        tgs=levels[:want]
        if len(tgs)<want:
            a=atr(highs,lows,closes,14)[-1]
            for k in range(1,want-len(tgs)+1):
                tgs.append(round(entry + k*max(a, entry*0.002),6))
        tgs=sorted(tgs)[:want]
    else:
        levels=sorted({lows[i] for i in lo_idx if lows[i]<entry}, reverse=True)
        tgs=levels[:want]
        if len(tgs)<want:
            a=atr(highs,lows,closes,14)[-1]
            for k in range(1,want-len(tgs)+1):
                tgs.append(round(entry - k*max(a, entry*0.002),6))
        tgs=sorted(tgs, reverse=True)[:want]
    return [round(x,6) for x in tgs]

def swing_stop(side, highs, lows, closes, lookback=12):
    i=len(closes)-1; st=max(0,i-lookback)
    if side=="BUY":  return round(min(lows[st:i] or [lows[i]]),6)
    else:            return round(max(highs[st:i] or [highs[i]]),6)

# =========================
# Binance REST/WS (آمن)
# =========================
def to_pair(sym:str)->str: return sym.replace("/","")

def rest_klines_safe(sym, interval, limit, retries=SEED_RETRIES):
    """REST مع إعادة المحاولة وباك-أوف وتأخير عشوائي خفيف."""
    pair=to_pair(sym)
    url=f"https://api.binance.com/api/v3/klines?symbol={pair}&interval={interval}&limit={limit}"
    delay = SEED_DELAY_SEC
    for attempt in range(retries):
        try:
            r=requests.get(url, timeout=12)
            if r.status_code==200:
                data=r.json()
                o=[float(k[1]) for k in data]; h=[float(k[2]) for k in data]
                l=[float(k[3]) for k in data]; c=[float(k[4]) for k in data]
                v=[float(k[5]) for k in data]
                return o,h,l,c,v
            else:
                log(f"[REST] {sym} {r.status_code}: {r.text[:120]}")
        except Exception as e:
            log(f"[REST] {sym} exc: {e}")
        # backoff
        sleep_for = delay * (SEED_BACKOFF_BASE ** attempt) + random.uniform(0, 0.5)
        time.sleep(sleep_for)
    return None

def build_stream_url(symbols):
    streams=[f"{to_pair(s).lower()}@kline_{TIMEFRAME}" for s in symbols]
    return "wss://stream.binance.com:9443/stream?streams=" + "/".join(streams)

# =========================
# State
# =========================
ohlcv = {s: {"o": [], "h": [], "l": [], "c": [], "v": []} for s in SYMBOLS}
MTF   = {s: {"m15":"flat","h1":"flat"} for s in SYMBOLS}
last_alert_key = {}
last_sent_at   = {}

def can_send(sym, side, ts):
    k=(sym,side); t=last_sent_at.get(k,0)
    if ts - t >= COOLDOWN_MIN*60:
        last_sent_at[k]=ts; return True
    return False

# =========================
# MTF trends
# =========================
def mtf_trend(sym):
    def trend_on(tf):
        res = rest_klines_safe(sym, tf, min(SEED_LIMIT, 200))
        if not res: return "flat"
        o,h,l,c,v = res
        e20,e50,e100 = ema(c,20)[-1], ema(c,50)[-1], ema(c,100)[-1]
        if e20>e50>e100: return "up"
        if e20<e50<e100: return "down"
        return "flat"
    try:
        MTF[sym]["m15"]=trend_on("15m")
        MTF[sym]["h1"] =trend_on("1h")
    except Exception as e:
        log("MTF error", sym, e)

# =========================
# Signal logic
# =========================
def generate_signal_from_lists(opens, highs, lows, closes, volumes, sym):
    if len(closes)<150: return None
    i=len(closes)-1
    ema20,ema50,ema100=ema(closes,20),ema(closes,50),ema(closes,100)
    srs=stoch_rsi(closes,14,14); rsi_now=rsi(closes,14)[-1]
    _m,_s,hist=macd(closes); _,_,adxv=adx(highs,lows,closes,14)

    vol_usdt=closes[i]*volumes[i]
    avg_vol = sum(volumes[max(0,i-19):i+1])/min(20,i+1)
    if vol_usdt<MIN_VOL_USDT: return None
    vol_ok = volumes[i] >= avg_vol*VOL_SPIKE_MULT

    atr14=atr(highs,lows,closes,14)[-1]
    tr_curr=max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    atr_ok = tr_curr >= ATR_MIN_PORTION*atr14

    trend_up_5m   = ema20[i]>ema50[i]>ema100[i]
    trend_down_5m = ema20[i]<ema50[i]<ema100[i]

    mtf15=MTF.get(sym,{}).get("m15","flat"); mtf1h=MTF.get(sym,{}).get("h1","flat")
    mtf_up_ok   = (mtf15=="up" and mtf1h=="up")
    mtf_down_ok = (mtf15=="down" and mtf1h=="down")

    macd_up   = hist[i-1] <= 0 and hist[i] > 0
    macd_down = hist[i-1] >= 0 and hist[i] < 0

    stoch_buy  = srs[i-1]<30 and srs[i]>srs[i-1]
    stoch_sell = srs[i-1]>70 and srs[i]<srs[i-1]

    adx_now=adxv[i]; adx_ok=adx_now>=18.0
    body=abs(closes[i]-opens[i]); rng=max(1e-9, highs[i]-lows[i])
    body_ok=(body/rng)>=0.35

    rsi_buy_ok  = rsi_now>=RSI_BUY_MIN
    rsi_sell_ok = rsi_now<=RSI_SELL_MAX

    buy_flags  = [trend_up_5m, mtf_up_ok, macd_up,  stoch_buy,  rsi_buy_ok,  vol_ok, atr_ok, adx_ok, body_ok]
    sell_flags = [trend_down_5m, mtf_down_ok, macd_down, stoch_sell, rsi_sell_ok, vol_ok, atr_ok, adx_ok, body_ok]
    buy_score, sell_score = sum(buy_flags), sum(sell_flags)

    side=None
    if buy_score>=5: side="BUY"
    elif sell_score>=5: side="SELL"
    else: return None

    entry=closes[i]
    targets=chart_targets(side, closes, highs, lows, entry, want=5)
    stop=swing_stop(side, highs, lows, closes, lookback=12)

    def rr(tp):
        risk=abs(entry-stop); rew=abs(tp-entry)
        return round(rew/risk,2) if risk>0 else 0.0

    note=[]
    def add(c,s): 
        if c: note.append(s)
    if side=="BUY":
        add(trend_up_5m,"5mTrend"); add(mtf_up_ok,"15m+1h UP"); add(macd_up,"MACD↑"); add(stoch_buy,"Stoch↑"); add(rsi_buy_ok,f"RSI≥{int(RSI_BUY_MIN)}")
    else:
        add(trend_down_5m,"5mTrend"); add(mtf_down_ok,"15m+1h DOWN"); add(macd_down,"MACD↓"); add(stoch_sell,"Stoch↓"); add(rsi_sell_ok,f"RSI≤{int(RSI_SELL_MAX)}")
    add(vol_ok,"Vol↑"); add(atr_ok,"ATR"); add(adx_ok,f"ADX={int(adx_now)}"); add(body_ok,"BodyOK")

    return {
        "side": side, "entry": round(entry,6),
        "targets": [round(x,6) for x in targets], "stop": round(stop,6),
        "ema20": round(ema20[i],6), "ema50": round(ema50[i],6),
        "stochrsi": round(srs[i],2), "rsi": round(rsi_now,2),
        "macd_hist": round(hist[i],4), "adx": round(adx_now,2),
        "volume_usdt": int(vol_usdt), "avg_vol": int(avg_vol), "atr": round(atr14,6),
        "score": buy_score if side=="BUY" else sell_score,
        "rr1": rr(targets[0]), "rr3": rr(targets[2]) if len(targets)>=3 else None,
        "mtf": f"{MTF[sym]['m15']}/{MTF[sym]['h1']}", "note": " + ".join(note)
    }

# =========================
# Seed & WS (throttled)
# =========================
def seed_all():
    print("Seeding history (safe mode) ...")
    # نخلط القائمة لتوزيع الضغط على سيرفر Binance
    syms = SYMBOLS[:]
    random.shuffle(syms)
    for s in syms:
        try:
            res = rest_klines_safe(s, TIMEFRAME, min(SEED_LIMIT, LOOKBACK_BARS))
            if res:
                o,h,l,c,v = res
                ohlcv[s]["o"]=o[-LOOKBACK_BARS:]
                ohlcv[s]["h"]=h[-LOOKBACK_BARS:]
                ohlcv[s]["l"]=l[-LOOKBACK_BARS:]
                ohlcv[s]["c"]=c[-LOOKBACK_BARS:]
                ohlcv[s]["v"]=v[-LOOKBACK_BARS:]
                print(f"Seeded {len(ohlcv[s]['c'])} candles for {s}")
            else:
                print(f"Seed error {s}: skipped (will fill by WS)")
            # أحسب ترند MTF بشكل منفصل وبنفس حماية المعدّل
            mtf_trend(s)
        except Exception as e:
            print("Seed exception", s, e)
        # delay أساسي + jitter صغير
        time.sleep(SEED_DELAY_SEC + random.uniform(0, 0.5))

STREAM_URL = build_stream_url(SYMBOLS)

def on_message(ws, message):
    try:
        msg=json.loads(message); data=msg.get("data", {})
        if not data or "k" not in data: return
        k=data["k"]
        if not k.get("x", False): return  # نأخذ الشمعة المغلقة فقط

        pair=data.get("s","")
        sym=next((s for s in SYMBOLS if to_pair(s).upper()==pair.upper()), None)
        if not sym: return

        close=float(k["c"]); high=float(k["h"]); low=float(k["l"]); vol=float(k["v"]); op=float(k["o"])

        for key,val in (("o",op),("h",high),("l",low),("c",close),("v",vol)):
            ohlcv[sym][key].append(val)
            if len(ohlcv[sym][key])>LOOKBACK_BARS:
                ohlcv[sym][key]=ohlcv[sym][key][-LOOKBACK_BARS:]

        sig=generate_signal_from_lists(ohlcv[sym]["o"], ohlcv[sym]["h"], ohlcv[sym]["l"], ohlcv[sym]["c"], ohlcv[sym]["v"], sym)
        if not sig: return

        ts=int(k["t"]/1000)
        key=(sym, sig["side"], k["t"])
        if last_alert_key.get(sym)==key: return
        if not can_send(sym, sig["side"], ts): return
        last_alert_key[sym]=key

        label = "قوية جدًا ⭐️⭐️⭐️" if sig["score"]>=7 else ("قوية ⭐️⭐️" if sig["score"]>=6 else "عادية ⭐️")
        header= "🟢 <b>إشارة شراء</b>" if sig["side"]=="BUY" else "🔴 <b>إشارة بيع</b>"
        targets_html="\n".join([f"   {i+1}) <code>{t}</code>" for i,t in enumerate(sig["targets"])])

        text=(
            f"{header} — <b>{sym}</b>  •  {TIMEFRAME}\n"
            f"التقييم: {label}  (Score: {sig['score']}/9)\n"
            f"MTF: {sig['mtf']}\n"
            f"الزمن: {datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"\n"
            f"السعر: <b>{sig['entry']}</b>\n"
            f"الأهداف:\n{targets_html}\n"
            f"الوقف: <b>{sig['stop']}</b>\n"
            f"RR~TP1: {sig['rr1']}  |  RR~TP3: {sig['rr3']}\n"
            f"\n"
            f"مؤشرات: {sig['note']}\n"
            f"EMA20: {sig['ema20']} | EMA50: {sig['ema50']} | RSI: {sig['rsi']} | StochRSI: {sig['stochrsi']} | MACDhist: {sig['macd_hist']} | ADX: {sig['adx']}\n"
        )
        tg_send(text)
    except Exception:
        traceback.print_exc()

def on_open(ws): print("WS connected:", STREAM_URL)
def on_error(ws, error): print("WS error:", error)
def on_close(ws, code, reason): print("WS closed", code, reason)

def ws_loop():
    while True:
        try:
            ws=websocket.WebSocketApp(
                STREAM_URL, on_open=on_open, on_message=on_message,
                on_error=on_error, on_close=on_close
            )
            ws.run_forever(ping_interval=60, ping_timeout=10)
        except Exception as e:
            print("WS loop exception:", e)
            time.sleep(5)

# =========================
# Flask
# =========================
app = Flask(__name__)

@app.get("/")
def root(): return "OK"

@app.get("/health")
def health(): return "healthy"

@app.get("/test-tg")
def test_tg():
    tg_send("🚀 Test: Telegram is working.")
    return "sent"

# =========================
# Start
# =========================
def start():
    seed_all()  # آمن الآن
    t=threading.Thread(target=ws_loop, daemon=True); t.start()
    port=int(os.environ.get("PORT","10000"))
    app.run(host="0.0.0.0", port=port)

if __name__=="__main__":
    try:
        url=os.environ.get("RENDER_EXTERNAL_URL","")
        tg_send(f"✅ البوت V6 (safe) شغّال.\n🌐 {url}\n⏱ TF: {TIMEFRAME}\n🔔 Symbols: {', '.join(SYMBOLS[:12])}{' ...' if len(SYMBOLS)>12 else ''}")
    except Exception:
        pass
    start()
