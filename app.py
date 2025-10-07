import os, time, threading, requests, math, json
from datetime import datetime, timedelta
from flask import Flask, jsonify
import feedparser
import functools
from random import uniform

# Forzar flush de logs en Render
print = functools.partial(print, flush=True)

# 🔗 Webhook destino (Make)
WEBHOOK_URL = "https://hook.eu2.make.com/rqycnm09n1dvatljeuyptxzsh2jhnx6t"

# ⚙️ Configuración general
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))  # 1 llamada por minuto
REPORT_EVERY_HOURS = int(os.environ.get("REPORT_EVERY_HOURS", "4"))
SMA_FAST = int(os.environ.get("SMA_FAST", "6"))
SMA_SLOW = int(os.environ.get("SMA_SLOW", "70"))
ATR_LEN = int(os.environ.get("ATR_LEN", "14"))
VOL_LEN = int(os.environ.get("VOL_LEN", "20"))
PULLBACK_ATR = float(os.environ.get("PULLBACK_ATR", "0.25"))
RISK_PCT = float(os.environ.get("RISK_PCT", "3.0"))
TP_PCT = float(os.environ.get("TP_PCT", "0.04"))
SL_PCT = float(os.environ.get("SL_PCT", "0.03"))
SEND_TEST_ON_DEPLOY = os.environ.get("SEND_TEST_ON_DEPLOY", "true").lower() == "true"

STATE_PATH = "state.json"
PERF_PATH = "performance.json"
CACHE_PATH = "cache.json"

# -------------------------------
# API CoinGecko Demo
# -------------------------------
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
BASE_URL = "https://api.coingecko.com/api/v3"

def get_headers():
    """Solo usado en endpoints que aceptan header (no market_chart)."""
    headers = {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate",
        "User-Agent": "crypto-agent-ai/1.0"
    }
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    return headers

# -------------------------------
# Utilidades
# -------------------------------
def nowiso():
    return datetime.now().isoformat()

def sym_to_pair(sym):
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

# -------------------------------
# Cache local
# -------------------------------
cache = safe_load_json(CACHE_PATH, {})

def get_cached(key, max_age_min=5):
    entry = cache.get(key)
    if not entry:
        return None
    ts = datetime.fromisoformat(entry["ts"])
    if datetime.now() - ts > timedelta(minutes=max_age_min):
        return None
    return entry["data"]

def set_cache(key, data):
    cache[key] = {"ts": nowiso(), "data": data}
    safe_save_json(CACHE_PATH, cache)

# -------------------------------
# Datos CoinGecko
# -------------------------------
COINS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "SOLUSDT": "solana",
    "XRPUSDT": "ripple"
}

def get_klines(symbol, days=7, interval="hourly"):
    """Devuelve lista de precios desde CoinGecko (Demo API usa parámetro en query)."""
    key = f"klines_{symbol}"
    cached = get_cached(key)
    if cached:
        return cached
    try:
        coin_id = COINS[symbol]
        url = f"{BASE_URL}/coins/{coin_id}/market_chart"
        params = {
            "vs_currency": "usd",
            "days": days,
            "interval": interval,
            "x_cg_demo_api_key": COINGECKO_API_KEY  # <- Clave como parámetro
        }
        r = requests.get(url, headers={"accept": "application/json"}, params=params, timeout=20)
        r.raise_for_status()
        data = r.json().get("prices", [])
        kl = [{"t": t, "o": p, "h": p, "l": p, "c": p, "v": 1.0} for t, p in data]
        set_cache(key, kl)
        return kl
    except Exception as e:
        print(f"⚠️ Error en get_klines({symbol}):", e)
        return get_cached(key) or []

def price_24h(symbol):
    """Obtiene precio spot y rango 24h desde CoinGecko (acepta header)."""
    key = f"price_{symbol}"
    cached = get_cached(key)
    if cached:
        return cached
    try:
        coin_id = COINS[symbol]
        url = f"{BASE_URL}/coins/{coin_id}"
        r = requests.get(url, headers=get_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        current = data["market_data"]["current_price"]["usd"]
        low = data["market_data"]["low_24h"]["usd"]
        high = data["market_data"]["high_24h"]["usd"]
        pct = data["market_data"]["price_change_percentage_24h"]
        val = (float(current), float(low), float(high), float(pct))
        set_cache(key, val)
        return val
    except Exception as e:
        print(f"⚠️ Error obteniendo price_24h({symbol}):", e)
        return get_cached(key) or (0, 0, 0, 0)

# -------------------------------
# Indicadores técnicos
# -------------------------------
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

# -------------------------------
# Noticias y sentimiento
# -------------------------------
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

# -------------------------------
# Webhook
# -------------------------------
def post_webhook(payload):
    if not WEBHOOK_URL: return
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print("📤 webhook enviado:", payload.get("evento"), payload.get("activo", ""))
    except Exception as e:
        print("webhook error:", e)

# -------------------------------
# Estado y rendimiento
# -------------------------------
state = safe_load_json(STATE_PATH, {
    s: {"open": False, "dir": None, "entry": None, "sl": None, "tp": None}
    for s in SYMBOLS
})
performance = safe_load_json(PERF_PATH, {"trades": [], "wins": 0, "losses": 0})

# -------------------------------
# Evaluación de señales
# -------------------------------
def evaluate_symbol(symbol):
    kl = get_klines(symbol, 7, "hourly")
    closes = [x["c"] for x in kl]
    vols = [x["v"] for x in kl]
    if not closes: return None
    p = closes[-1]
    s_fast = sma(closes, SMA_FAST)
    s_slow = sma(closes, SMA_SLOW)
    _atr = atr(kl, ATR_LEN)
    v_avg = avg(vols, VOL_LEN)
    if any(x is None for x in [s_fast, s_slow, _atr, v_avg]): return None
    v_last = vols[-1]
    vol_ok = v_last >= 0.8 * v_avg
    pull_ok = abs(p - s_fast) <= _atr * PULLBACK_ATR
    st = state.setdefault(symbol, {"open": False, "dir": None, "entry": None, "sl": None, "tp": None})

    if (s_fast > s_slow) and vol_ok and pull_ok and not st["open"]:
        entry = round(p, 6)
        sl = round(entry * (1 - SL_PCT), 6)
        tp = round(entry * (1 + TP_PCT), 6)
        state[symbol] = {"open": True, "dir": "L", "entry": entry, "sl": sl, "tp": tp}
        safe_save_json(STATE_PATH, state)
        return {"evento": "nueva_senal", "tipo": "Largo", "activo": sym_to_pair(symbol),
                "entrada": entry, "sl": sl, "tp": tp, "riesgo": RISK_PCT,
                "timeframe": "H1", "timestamp": nowiso(),
                "comentario": "Cruce SMA6>SMA70 + pullback con volumen."}

    if st["open"] and st["dir"] == "L":
        if p >= st["tp"]:
            state[symbol] = {"open": False, "dir": None, "entry": None, "sl": None, "tp": None}
            safe_save_json(STATE_PATH, state)
            return {"evento": "cierre", "activo": sym_to_pair(symbol),
                    "resultado": "TP", "precio_cierre": p, "timestamp": nowiso(),
                    "comentario": "TP alcanzado."}
        if p <= st["sl"]:
            state[symbol] = {"open": False, "dir": None, "entry": None, "sl": None, "tp": None}
            safe_save_json(STATE_PATH, state)
            return {"evento": "cierre", "activo": sym_to_pair(symbol),
                    "resultado": "SL", "precio_cierre": p, "timestamp": nowiso(),
                    "comentario": "SL alcanzado."}
    return None

# -------------------------------
# Reporte 4h
# -------------------------------
def price_24h_line(symbol):
    c, low, high, pct = price_24h(symbol)
    return f"{sym_to_pair(symbol)} {c:.2f} (24h {pct:+.2f}%) Rango 24h {low:.2f}–{high:.2f}"

def report_payload():
    lines = [price_24h_line(s) for s in SYMBOLS]
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]
        fg_text = f"Fear&Greed: {fg['value']} ({fg['value_classification']})"
    except:
        fg_text = "Fear&Greed: s/d"
    headlines = coindesk_headlines(3) + theblock_headlines(2) + ft_headlines(2)
    return {"evento": "informe", "tipo": "miniresumen_4h", "timestamp": nowiso(),
            "precios": lines, "sentimiento": fg_text, "titulares": headlines[:5],
            "comentario": "Informe 4h (CoinGecko + RSS, con cache y autenticación demo)."}

# -------------------------------
# Bucles
# -------------------------------
def scan_loop():
    print("🌀 scan loop iniciado (1 símbolo por iteración)")
    index = 0
    while True:
        try:
            symbol = SYMBOLS[index % len(SYMBOLS)]
            print(f"🔍 Escaneando {symbol} ...")
            sig = evaluate_symbol(symbol)
            if sig:
                print(f"📈 Señal detectada en {symbol}: {sig['tipo']}")
                post_webhook(sig)
            index += 1
            print(f"✅ Escaneo completado para {symbol}. Esperando {LOOP_SECONDS}s...\n")
        except Exception as e:
            print("⚠️ scan error:", e)
        time.sleep(LOOP_SECONDS + uniform(2.0, 4.0))

def report_loop():
    print("🕓 report loop cada", REPORT_EVERY_HOURS, "h")
    next_run = datetime.now()
    while True:
        if datetime.now() >= next_run:
            try:
                post_webhook(report_payload())
                print("📤 Informe 4h enviado correctamente.")
            except Exception as e:
                print("⚠️ report error:", e)
            next_run = datetime.now() + timedelta(hours=REPORT_EVERY_HOURS)
        time.sleep(20)

# -------------------------------
# Flask
# -------------------------------
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "time": nowiso(),
        "cache_keys": list(cache.keys()),
        "api_key_loaded": bool(COINGECKO_API_KEY),
        "endpoint": BASE_URL
    })

def start_threads():
    t1 = threading.Thread(target=scan_loop, daemon=True)
    t2 = threading.Thread(target=report_loop, daemon=True)
    t1.start(); t2.start()

# -------------------------------
# Inicio
# -------------------------------
if __name__ == "__main__":
    print("🚀 Iniciando agente Cripto AI...")
    print("🔑 API Key cargada:", bool(COINGECKO_API_KEY))
    print("🌐 Endpoint:", BASE_URL)

    if SEND_TEST_ON_DEPLOY:
        post_webhook({
            "evento": "nueva_senal",
            "tipo": "Largo",
            "activo": "BTC/USD",
            "entrada": 123100,
            "sl": 119407,
            "tp": 128024,
            "riesgo": 3.0,
            "timeframe": "H1",
            "timestamp": nowiso(),
            "comentario": "Prueba de despliegue (Render con CoinGecko Demo API)."
        })

    start_threads()
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
