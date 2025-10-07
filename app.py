import os, time, threading, requests, json, feedparser, functools
from datetime import datetime, timedelta
from random import uniform
from flask import Flask, jsonify

# 🔧 Logs instantáneos (Render)
print = functools.partial(print, flush=True)

# 🔗 Webhook destino
WEBHOOK_URL = "https://hook.eu2.make.com/rqycnm09n1dvatljeuyptxzsh2jhnx6t"

# ⚙️ Configuración general
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))  # 1 ciclo/minuto
REPORT_EVERY_HOURS = int(os.environ.get("REPORT_EVERY_HOURS", "4"))
SMA_FAST, SMA_SLOW = 6, 70
ATR_LEN, VOL_LEN = 14, 20
PULLBACK_ATR, RISK_PCT = 0.25, 3.0
TP_PCT, SL_PCT = 0.04, 0.03
SEND_TEST_ON_DEPLOY = os.environ.get("SEND_TEST_ON_DEPLOY", "true").lower() == "true"

STATE_PATH, PERF_PATH, CACHE_PATH = "state.json", "performance.json", "cache.json"

# 🧠 Utilidades
def nowiso(): return datetime.now().isoformat()

def sym_to_pair(sym): return sym.replace("USDT", "") + "/USD"

def safe_load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return default

def safe_save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e: print("save error", path, e)

# 💾 Caché persistente (5 min)
cache = safe_load_json(CACHE_PATH, {})

def get_cached(key, max_age_min=5):
    entry = cache.get(key)
    if not entry: return None
    ts = datetime.fromisoformat(entry["ts"])
    if datetime.now() - ts > timedelta(minutes=max_age_min): return None
    return entry["data"]

def set_cache(key, data):
    cache[key] = {"ts": nowiso(), "data": data}
    safe_save_json(CACHE_PATH, cache)

# 🪙 Fuente de datos CoinGecko
COINS = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum", "SOLUSDT": "solana", "XRPUSDT": "ripple"}

def get_klines(symbol, days=1, interval="hourly"):
    key = f"klines_{symbol}"
    cached = get_cached(key)
    if cached: return cached
    try:
        coin_id = COINS[symbol]
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        params = {"vs_currency": "usd", "days": days, "interval": interval}
        time.sleep(uniform(0.2, 0.6))
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json().get("prices", [])
        kl = [{"t": t, "o": p, "h": p, "l": p, "c": p, "v": 1.0} for t, p in data]
        set_cache(key, kl)
        return kl
    except Exception as e:
        print(f"⚠️ Error en get_klines({symbol}):", e)
        return get_cached(key) or []

def price_24h(symbol):
    key = f"price_{symbol}"
    cached = get_cached(key)
    if cached: return cached
    try:
        coin_id = COINS[symbol]
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        time.sleep(uniform(0.2, 0.5))
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        d = r.json()["market_data"]
        val = (d["current_price"]["usd"], d["low_24h"]["usd"], d["high_24h"]["usd"], d["price_change_percentage_24h"])
        set_cache(key, val)
        return val
    except Exception as e:
        print(f"⚠️ Error obteniendo price_24h({symbol}):", e)
        return get_cached(key) or (0, 0, 0, 0)

# 🧮 Indicadores
def sma(vals, n): return sum(vals[-n:]) / n if len(vals) >= n else None
def avg(vals, n): return sum(vals[-n:]) / n if len(vals) >= n else None
def atr(kl, n):
    if len(kl) < n + 1: return None
    trs = [max(kl[-i]["h"] - kl[-i]["l"],
               abs(kl[-i]["h"] - kl[-i-1]["c"]),
               abs(kl[-i]["l"] - kl[-i-1]["c"])) for i in range(1, n+1)]
    return sum(trs) / n

# 🗞️ Noticias
def coindesk_headlines(n=3):
    try:
        return [e.title for e in feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/").entries[:n]]
    except: return []
def theblock_headlines(n=2):
    try:
        return [e.title for e in feedparser.parse("https://www.theblock.co/rss.xml").entries[:n]]
    except: return []
def ft_headlines(n=2):
    try:
        return [e.title for e in feedparser.parse("https://www.ft.com/technology/cryptocurrencies?format=rss").entries[:n]]
    except: return []
def fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        j = r.json()["data"][0]
        return j["value"], j["value_classification"]
    except: return None, None

# 📤 Webhook
def post_webhook(payload):
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print("📤 webhook:", payload.get("evento"), payload.get("activo", ""))
    except Exception as e:
        print("webhook error:", e)

# 📊 Estado
state = safe_load_json(STATE_PATH, {s: {"open": False, "dir": None, "entry": None, "sl": None, "tp": None} for s in SYMBOLS})
performance = safe_load_json(PERF_PATH, {"trades": [], "wins": 0, "losses": 0})

# ⚙️ Evaluación
def evaluate_symbol(symbol):
    kl = get_klines(symbol, 1, "hourly")
    closes = [x["c"] for x in kl]
    vols = [x["v"] for x in kl]
    if not closes: return None
    p = closes[-1]
    s_fast, s_slow, _atr, v_avg = sma(closes, SMA_FAST), sma(closes, SMA_SLOW), atr(kl, ATR_LEN), avg(vols, VOL_LEN)
    if any(x is None for x in [s_fast, s_slow, _atr, v_avg]): return None
    v_last, vol_ok = vols[-1], vols[-1] >= 0.8 * v_avg
    pull_ok = abs(p - s_fast) <= _atr * PULLBACK_ATR
    st = state.setdefault(symbol, {"open": False, "dir": None})
    if (s_fast > s_slow) and vol_ok and pull_ok and not st["open"]:
        entry, sl, tp = round(p, 6), round(p * (1 - SL_PCT), 6), round(p * (1 + TP_PCT), 6)
        state[symbol] = {"open": True, "dir": "L", "entry": entry, "sl": sl, "tp": tp}
        safe_save_json(STATE_PATH, state)
        return {"evento": "nueva_senal", "tipo": "Largo", "activo": sym_to_pair(symbol),
                "entrada": entry, "sl": sl, "tp": tp, "riesgo": RISK_PCT,
                "timestamp": nowiso(), "comentario": "Cruce SMA6>SMA70 + pullback con volumen."}
    if st["open"] and st["dir"] == "L":
        if p >= st["tp"]:
            state[symbol]["open"] = False
            safe_save_json(STATE_PATH, state)
            return {"evento": "cierre", "activo": sym_to_pair(symbol),
                    "resultado": "TP", "precio_cierre": p, "timestamp": nowiso(),
                    "comentario": "TP alcanzado."}
        if p <= st["sl"]:
            state[symbol]["open"] = False
            safe_save_json(STATE_PATH, state)
            return {"evento": "cierre", "activo": sym_to_pair(symbol),
                    "resultado": "SL", "precio_cierre": p, "timestamp": nowiso(),
                    "comentario": "SL alcanzado."}
    return None

# 📈 Informe
def price_24h_line(symbol):
    c, low, high, pct = price_24h(symbol)
    return f"{sym_to_pair(symbol)} {c:.2f} (24h {pct:+.2f}%) rango {low:.2f}–{high:.2f}"

def report_payload():
    lines = [price_24h_line(s) for s in SYMBOLS]
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]
        fg_text = f"Fear&Greed: {fg['value']} ({fg['value_classification']})"
    except: fg_text = "Fear&Greed: s/d"
    headlines = coindesk_headlines(2) + theblock_headlines(1) + ft_headlines(1)
    return {"evento": "informe", "tipo": "miniresumen_4h", "timestamp": nowiso(),
            "precios": lines, "sentimiento": fg_text, "titulares": headlines,
            "comentario": "Informe 4h (CoinGecko + RSS, cacheado)."}

# 🔁 Loops
def scan_loop():
    print("🌀 scan loop iniciado (1 activo/minuto, cache 5 min)")
    i = 0
    while True:
        try:
            sym = SYMBOLS[i % len(SYMBOLS)]
            print(f"🔍 Escaneando {sym}...")
            sig = evaluate_symbol(sym)
            if sig: post_webhook(sig)
            i += 1
        except Exception as e:
            print("⚠️ scan error:", e)
        time.sleep(LOOP_SECONDS)  # 1 ciclo por minuto

def report_loop():
    print(f"🕓 report loop cada {REPORT_EVERY_HOURS}h")
    next_run = datetime.now()
    while True:
        if datetime.now() >= next_run:
            try:
                post_webhook(report_payload())
                print("📤 Informe 4h enviado.")
            except Exception as e:
                print("⚠️ report error:", e)
            next_run = datetime.now() + timedelta(hours=REPORT_EVERY_HOURS)
        time.sleep(30)

# 🌐 Flask
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({"status": "ok", "time": nowiso(), "cache_keys": list(cache.keys())})

def start_threads():
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=report_loop, daemon=True).start()

# 🚀 Inicio
if __name__ == "__main__":
    if SEND_TEST_ON_DEPLOY:
        post_webhook({"evento": "nueva_senal", "tipo": "Largo", "activo": "BTC/USD",
                      "entrada": 123100, "sl": 119407, "tp": 128024,
                      "riesgo": 3.0, "timestamp": nowiso(),
                      "comentario": "Prueba de despliegue (Render)."})
    start_threads()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
