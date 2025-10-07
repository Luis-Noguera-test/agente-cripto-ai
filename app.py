import os, time, threading, requests, json, functools, math
from datetime import datetime, timedelta
from random import uniform
from flask import Flask, jsonify
import feedparser

# Logs con flush inmediato (Render)
print = functools.partial(print, flush=True)

# ========== Config ==========
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://hook.eu2.make.com/rqycnm09n1dvatljeuyptxzsh2jhnx6t"
)

SYMBOLS = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT").split(",")
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))          # 1 símbolo/min
REPORT_EVERY_HOURS = int(os.environ.get("REPORT_EVERY_HOURS", "4"))

# Señales (sobre velas 1h)
SMA_FAST = int(os.environ.get("SMA_FAST", "6"))
SMA_SLOW = int(os.environ.get("SMA_SLOW", "70"))
ATR_LEN  = int(os.environ.get("ATR_LEN",  "14"))
VOL_LEN  = int(os.environ.get("VOL_LEN",  "20"))
PULLBACK_ATR = float(os.environ.get("PULLBACK_ATR", "0.25"))
SL_PCT   = float(os.environ.get("SL_PCT", "0.03"))
TP_PCT   = float(os.environ.get("TP_PCT", "0.04"))
RISK_PCT = float(os.environ.get("RISK_PCT", "3.0"))

SEND_TEST_ON_DEPLOY = os.environ.get("SEND_TEST_ON_DEPLOY", "true").lower() == "true"

STATE_PATH = "state.json"
PERF_PATH  = "performance.json"
CACHE_PATH = "cache.json"

BINANCE = "https://api.binance.com"

# ========== Utilidades ==========
def nowiso():
    return datetime.now().isoformat(timespec="seconds")

def sym_to_pair(sym: str) -> str:
    return sym.replace("USDT", "") + "/USD"

def safe_load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def safe_save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("save error", path, e)

# ========== Caché ligera ==========
cache = safe_load_json(CACHE_PATH, {})  # { key: {ts, data} }

def get_cached(key, max_age_sec: int):
    e = cache.get(key)
    if not e:
        return None
    ts = datetime.fromisoformat(e["ts"])
    if (datetime.now() - ts).total_seconds() > max_age_sec:
        return None
    return e["data"]

def set_cache(key, data):
    cache[key] = {"ts": nowiso(), "data": data}
    safe_save_json(CACHE_PATH, cache)

# ========== Llamadas Binance (sin API key) ==========
def http_get(url, params=None, timeout=12):
    """GET con reintentos/backoff simple para 429/5xx."""
    tries = 0
    while True:
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = 2 + tries * 2
                print(f"⚠️ 429 rate limit. Backoff {wait}s...")
                time.sleep(wait)
                tries += 1
                continue
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            # Si es 5xx, reintenta; si es 4xx (excepto 429) retorna None
            code = getattr(e.response, "status_code", None)
            if code and 500 <= code < 600 and tries < 3:
                wait = 1 + tries * 2
                print(f"⚠️ {code} server error. Retry {wait}s...")
                time.sleep(wait)
                tries += 1
                continue
            print("❌ HTTP error:", e)
            return None
        except Exception as e:
            if tries < 2:
                wait = 1 + tries
                print(f"⚠️ network error: {e}. Retry {wait}s...")
                time.sleep(wait)
                tries += 1
                continue
            print("❌ network error:", e)
            return None

def get_klines(symbol, interval="1h", limit=200):
    """
    Velas Binance: open, high, low, close, volume.
    Retorna lista de dicts [{t,o,h,l,c,v}, ...]
    """
    key = f"k_{symbol}_{interval}_{limit}"
    cached = get_cached(key, max_age_sec=55)  # casi 1 minuto
    if cached:
        return cached

    url = f"{BINANCE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = http_get(url, params=params)
    if not data:
        return get_cached(key, 999999) or []  # devuelve última buena

    out = []
    for k in data:
        out.append({
            "t": int(k[0]),  # open time ms
            "o": float(k[1]),
            "h": float(k[2]),
            "l": float(k[3]),
            "c": float(k[4]),
            "v": float(k[5]),
        })
    set_cache(key, out)
    return out

def price_24h(symbol):
    """Precio spot y rango 24h (Binance)."""
    key = f"p24_{symbol}"
    cached = get_cached(key, max_age_sec=55)
    if cached:
        return cached

    url = f"{BINANCE}/api/v3/ticker/24hr"
    data = http_get(url, params={"symbol": symbol})
    if not data:
        return get_cached(key, 999999) or (0, 0, 0, 0)

    cur = float(data["lastPrice"])
    low = float(data["lowPrice"])
    high = float(data["highPrice"])
    pct = float(data["priceChangePercent"])
    val = (cur, low, high, pct)
    set_cache(key, val)
    return val

# ========== Indicadores ==========
def sma(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

def atr(kl, n):
    if len(kl) < n + 1: return None
    trs = []
    for i in range(1, n + 1):
        h = kl[-i]["h"]; l = kl[-i]["l"]; cprev = kl[-i - 1]["c"]
        tr = max(h - l, abs(h - cprev), abs(l - cprev))
        trs.append(tr)
    return sum(trs) / len(trs)

def avg(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

# ========== Noticias & Sentimiento ==========
def coindesk_headlines(n=3):
    try:
        feed = feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/")
        return [e.title for e in feed.entries[:n]]
    except: return []

def theblock_headlines(n=2):
    try:
        feed = feedparser.parse("https://www.theblock.co/rss.xml")
        return [e.title for e in feed.entries[:n]]
    except: return []

def ft_headlines(n=2):
    try:
        feed = feedparser.parse("https://www.ft.com/technology/cryptocurrencies?format=rss")
        return [e.title for e in feed.entries[:n]]
    except: return []

def fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        j = r.json()["data"][0]
        return j["value"], j["value_classification"]
    except: return None, None

# ========== Webhook ==========
def post_webhook(payload):
    if not WEBHOOK_URL: 
        return
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print("📤 webhook →", payload.get("evento"), payload.get("activo", ""))
    except Exception as e:
        print("webhook error:", e)

# ========== Estado & rendimiento ==========
state = safe_load_json(STATE_PATH, {
    s: {"open": False, "dir": None, "entry": None, "sl": None, "tp": None}
    for s in SYMBOLS
})
performance = safe_load_json(PERF_PATH, {"trades": [], "wins": 0, "losses": 0})

def record_trade(sym, result):
    performance["trades"].append({"sym": sym, "result": result, "ts": nowiso()})
    if result == "TP": performance["wins"] += 1
    if result == "SL": performance["losses"] += 1
    performance["trades"] = performance["trades"][-100:]
    safe_save_json(PERF_PATH, performance)

# ========== Señales ==========
def evaluate_symbol(symbol):
    kl = get_klines(symbol, "1h", 200)
    if not kl:
        return None
    closes = [x["c"] for x in kl]
    vols   = [x["v"] for x in kl]
    p = closes[-1]

    s_fast = sma(closes, SMA_FAST)
    s_slow = sma(closes, SMA_SLOW)
    _atr   = atr(kl, ATR_LEN)
    v_avg  = avg(vols, VOL_LEN)
    if any(x is None for x in [s_fast, s_slow, _atr, v_avg]): 
        return None

    v_last = vols[-1]
    vol_ok = v_last >= 0.8 * v_avg
    pull_ok = abs(p - s_fast) <= _atr * PULLBACK_ATR

    st = state.setdefault(symbol, {"open": False, "dir": None, "entry": None, "sl": None, "tp": None})

    # Entrada LARGA
    if (s_fast is not None and s_slow is not None and s_fast > s_slow) and vol_ok and pull_ok and not st["open"]:
        entry = round(p, 6)
        sl = round(entry * (1 - SL_PCT), 6)
        tp = round(entry * (1 + TP_PCT), 6)
        state[symbol] = {"open": True, "dir": "L", "entry": entry, "sl": sl, "tp": tp}
        safe_save_json(STATE_PATH, state)
        return {
            "evento": "nueva_senal",
            "tipo": "Largo",
            "activo": sym_to_pair(symbol),
            "entrada": entry,
            "sl": sl,
            "tp": tp,
            "riesgo": RISK_PCT,
            "timeframe": "H1",
            "timestamp": nowiso(),
            "comentario": "Cruce SMA6>SMA70 + pullback (ATR) y volumen OK."
        }

    # Gestión
    if st["open"] and st["dir"] == "L":
        if p >= st["tp"]:
            state[symbol] = {"open": False, "dir": None, "entry": None, "sl": None, "tp": None}
            safe_save_json(STATE_PATH, state)
            record_trade(symbol, "TP")
            return {
                "evento": "cierre", "activo": sym_to_pair(symbol),
                "resultado": "TP", "precio_cierre": p, "timestamp": nowiso(),
                "comentario": "TP alcanzado."
            }
        if p <= st["sl"]:
            state[symbol] = {"open": False, "dir": None, "entry": None, "sl": None, "tp": None}
            safe_save_json(STATE_PATH, state)
            record_trade(symbol, "SL")
            return {
                "evento": "cierre", "activo": sym_to_pair(symbol),
                "resultado": "SL", "precio_cierre": p, "timestamp": nowiso(),
                "comentario": "SL alcanzado."
            }
    return None

# ========== Informe ==========
def price_24h_line(symbol):
    c, low, high, pct = price_24h(symbol)
    return f"{sym_to_pair(symbol)} {c:.2f} (24h {pct:+.2f}%)  Rango 24h {low:.2f}–{high:.2f}"

def report_payload():
    lines = [price_24h_line(s) for s in SYMBOLS]
    fg_v, fg_txt = fear_greed()
    fg_line = f"Fear&Greed: {fg_v} ({fg_txt})" if fg_v else "Fear&Greed: s/d"
    headlines = coindesk_headlines(3) + theblock_headlines(2) + ft_headlines(2)
    return {
        "evento": "informe",
        "tipo": "miniresumen_4h",
        "timestamp": nowiso(),
        "precios": lines,
        "sentimiento": fg_line,
        "titulares": headlines[:5],
        "comentario": "Informe 4h (Binance + RSS)."
    }

# ========== Bucles ==========
def scan_loop():
    print(f"🌀 scan loop: {LOOP_SECONDS}s | rotación 1 símbolo/iteración")
    idx = 0
    while True:
        try:
            sym = SYMBOLS[idx % len(SYMBOLS)]
            print(f"🔍 Escaneando {sym} ...")
            sig = evaluate_symbol(sym)
            if sig:
                print(f"📈 Señal detectada en {sym} → {sig['tipo']}")
                post_webhook(sig)
            print(f"✅ Escaneo completado para {sym}. Esperando {LOOP_SECONDS}s...\n")
            idx += 1
        except Exception as e:
            print("scan error:", e)
        time.sleep(LOOP_SECONDS + uniform(0.5, 1.5))

def report_loop():
    print(f"🕓 report loop cada {REPORT_EVERY_HOURS}h")
    next_run = datetime.now()
    while True:
        if datetime.now() >= next_run:
            try:
                post_webhook(report_payload())
                print("📤 Informe 4h enviado.")
            except Exception as e:
                print("report error:", e)
            next_run = datetime.now() + timedelta(hours=REPORT_EVERY_HOURS)
        time.sleep(15)

# ========== Flask ==========
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "time": nowiso(),
        "symbols": SYMBOLS,
        "params": {
            "SMA_FAST": SMA_FAST, "SMA_SLOW": SMA_SLOW, "ATR_LEN": ATR_LEN,
            "VOL_LEN": VOL_LEN, "PULLBACK_ATR": PULLBACK_ATR,
            "SL_PCT": SL_PCT, "TP_PCT": TP_PCT
        },
        "open_state": state,
        "perf": {"wins": performance["wins"], "losses": performance["losses"], "n": len(performance["trades"])}
    })

def start_threads():
    t1 = threading.Thread(target=scan_loop, daemon=True)
    t2 = threading.Thread(target=report_loop, daemon=True)
    t1.start(); t2.start()

# ========== Main ==========
if __name__ == "__main__":
    print("🚀 Iniciando agente Cripto AI (Binance, sin API key)...")
    if SEND_TEST_ON_DEPLOY:
        post_webhook({
            "evento": "nueva_senal",
            "tipo": "Largo",
            "activo": "BTC/USD",
            "entrada": 123100,
            "sl": 119407,
            "tp": 128024,
            "riesgo": RISK_PCT,
            "timeframe": "H1",
            "timestamp": nowiso(),
            "comentario": "Prueba de despliegue (Render)."
        })

    start_threads()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
