import os, time, threading, requests, math, json
from datetime import datetime, timedelta
from flask import Flask, jsonify
import feedparser

WEBHOOK_URL = "https://hook.eu2.make.com/rqycnm09n1dvatljeuyptxzsh2jhnx6t"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))
REPORT_EVERY_HOURS = int(os.environ.get("REPORT_EVERY_HOURS", "4"))
SMA_FAST = int(os.environ.get("SMA_FAST","6"))
SMA_SLOW = int(os.environ.get("SMA_SLOW","70"))
ATR_LEN  = int(os.environ.get("ATR_LEN","14"))
VOL_LEN  = int(os.environ.get("VOL_LEN","20"))
PULLBACK_ATR = float(os.environ.get("PULLBACK_ATR","0.25"))
RISK_PCT = float(os.environ.get("RISK_PCT","3.0"))
TP_PCT   = float(os.environ.get("TP_PCT","0.04"))
SL_PCT   = float(os.environ.get("SL_PCT","0.03"))
SEND_TEST_ON_DEPLOY = os.environ.get("SEND_TEST_ON_DEPLOY","true").lower()=="true"
STATE_PATH = "state.json"
PERF_PATH  = "performance.json"

def nowiso():
    return datetime.now().isoformat()

def sym_to_pair(sym):
    return sym.replace("USDT","") + "/USD"

def safe_load_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def safe_save_json(path, data):
    try:
        with open(path,"w",encoding="utf-8") as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
    except Exception as e:
        print("save error", path, e)

def get_klines(symbol, interval="1m", limit=200):
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(url, params={"symbol":symbol,"interval":interval,"limit":limit}, timeout=10)
    r.raise_for_status()
    ohlcv = []
    for k in r.json():
        ohlcv.append({
            "t": int(k[0]),
            "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]),
            "v": float(k[5])
        })
    return ohlcv

def sma(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

def atr(kl, n):
    if len(kl) < n+1: return None
    trs = []
    for i in range(1, n+1):
        h = kl[-i]["h"]; l = kl[-i]["l"]; cprev = kl[-i-1]["c"]
        tr = max(h-l, abs(h-cprev), abs(l-cprev))
        trs.append(tr)
    return sum(trs)/len(trs)

def avg(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

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

def price_24h(symbol):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    r = requests.get(url, params={"symbol":symbol}, timeout=10)
    r.raise_for_status()
    j = r.json()
    return float(j["lastPrice"]), float(j["lowPrice"]), float(j["highPrice"]), float(j["priceChangePercent"])

def post_webhook(payload):
    if not WEBHOOK_URL: return
    try:
        requests.post("https://hook.eu2.make.com/rqycnm09n1dvatljeuyptxzsh2jhnx6t", json=payload, timeout=10)
        print("webhook", payload.get("evento"), payload.get("activo"), payload.get("resultado",""))
    except Exception as e:
        print("webhook error:", e)

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

def adaptive_tuning():
    recent = performance["trades"][-20:]
    if not recent: return
    wins = sum(1 for t in recent if t["result"]=="TP")
    rate = wins / len(recent)
    global PULLBACK_ATR, SMA_FAST, SMA_SLOW, SL_PCT, TP_PCT
    if rate < 0.45:
        PULLBACK_ATR = min(PULLBACK_ATR + 0.05, 0.6)
        SMA_FAST = min(SMA_FAST + 1, 12)
        SMA_SLOW = min(SMA_SLOW + 5, 100)
        SL_PCT = min(SL_PCT + 0.005, 0.05)
        TP_PCT = max(TP_PCT - 0.005, 0.025)
        print("tuning conservative", rate)
    elif rate > 0.60:
        PULLBACK_ATR = max(PULLBACK_ATR - 0.05, 0.10)
        SMA_FAST = max(SMA_FAST - 1, 5)
        SMA_SLOW = max(SMA_SLOW - 5, 50)
        SL_PCT = max(SL_PCT - 0.002, 0.02)
        TP_PCT = min(TP_PCT + 0.005, 0.06)
        print("tuning aggressive", rate)

def evaluate_symbol(symbol):
    kl = get_klines(symbol, "1m", 200)
    closes = [x["c"] for x in kl]
    vols   = [x["v"] for x in kl]
    p = closes[-1]
    s_fast = sma(closes, SMA_FAST)
    s_slow = sma(closes, SMA_SLOW)
    _atr   = atr(kl, 14)
    v_avg  = avg(vols, 20)
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
        return {"evento":"nueva_senal","tipo":"Largo","activo": sym_to_pair(symbol),
                "entrada": entry, "sl": sl, "tp": tp, "riesgo": 3.0, "timeframe":"H1",
                "timestamp": nowiso(), "comentario":"Cruce SMA6>SMA70 + pullback con volumen."}
    if st["open"] and st["dir"]=="L":
        if p >= st["entry"] + (st["entry"]-st["sl"]) and st["sl"] < st["entry"]:
            st["sl"] = st["entry"]; safe_save_json(STATE_PATH, state)
            return {"evento":"modificacion","activo": sym_to_pair(symbol),
                    "sl": st["sl"], "timestamp": nowiso(),
                    "comentario":"Ajuste SL a break-even (+1R)."}
        if p >= st["tp"]:
            state[symbol] = {"open": False, "dir": None, "entry": None, "sl": None, "tp": None}
            safe_save_json(STATE_PATH, state); record_trade(symbol,"TP"); adaptive_tuning()
            return {"evento":"cierre","activo": sym_to_pair(symbol),
                    "resultado":"TP","precio_cierre": p,"timestamp": nowiso(),
                    "comentario":"TP alcanzado."}
        if p <= st["sl"]:
            state[symbol] = {"open": False, "dir": None, "entry": None, "sl": None, "tp": None}
            safe_save_json(STATE_PATH, state); record_trade(symbol,"SL"); adaptive_tuning()
            return {"evento":"cierre","activo": sym_to_pair(symbol),
                    "resultado":"SL","precio_cierre": p,"timestamp": nowiso(),
                    "comentario":"SL alcanzado."}
    return None

def price_24h_line(symbol):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    j = requests.get(url, params={"symbol":symbol}, timeout=10).json()
    return f"{sym_to_pair(symbol)} {float(j['lastPrice']):.4f} (24h {float(j['priceChangePercent']):+.2f}%)  Rango 24h {float(j['lowPrice']):.4f}–{float(j['highPrice']):.4f}"

def report_payload():
    lines = [price_24h_line(s) for s in SYMBOLS]
    try:
        fg = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]
        fg_text = f"Fear&Greed: {fg['value']} ({fg['value_classification']})"
    except:
        fg_text = "Fear&Greed: s/d"
    headlines = coindesk_headlines(3) + theblock_headlines(2) + ft_headlines(2)
    return {"evento":"informe","tipo":"miniresumen_4h","timestamp": nowiso(),
            "precios": lines,"sentimiento": fg_text,"titulares": headlines[:5],
            "comentario":"Informe 4h (Binance + RSS)."}

def scan_loop():
    print("scan loop", LOOP_SECONDS, "s")
    while True:
        try:
            for s in SYMBOLS:
                sig = evaluate_symbol(s)
                if sig: post_webhook(sig)
        except Exception as e:
            print("scan error:", e)
        time.sleep(LOOP_SECONDS)

def report_loop():
    print("report loop each", REPORT_EVERY_HOURS, "h")
    next_run = datetime.now()
    while True:
        if datetime.now() >= next_run:
            try: post_webhook(report_payload())
            except Exception as e: print("report error:", e)
            next_run = datetime.now() + timedelta(hours=REPORT_EVERY_HOURS)
        time.sleep(15)

from flask import Flask, jsonify
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({"status":"ok","time":nowiso(),
                    "open_state": state,
                    "perf":{"wins":performance["wins"],"losses":performance["losses"],"n":len(performance["trades"])},
                    "params":{"SMA_FAST":SMA_FAST,"SMA_SLOW":SMA_SLOW,"ATR_LEN":ATR_LEN,
                              "VOL_LEN":VOL_LEN,"PULLBACK_ATR":PULLBACK_ATR,"SL_PCT":SL_PCT,"TP_PCT":TP_PCT}})

def start_threads():
    t1 = threading.Thread(target=scan_loop, daemon=True)
    t2 = threading.Thread(target=report_loop, daemon=True)
    t1.start(); t2.start()

if __name__ == "__main__":
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
            "comentario": "Prueba de despliegue (Render)."
        })

    import threading
    import time
    import os

    # 🧠 Bucle principal del agente: escaneo continuo del mercado
    def loop_agente():
        while True:
            try:
                print("🔁 Escaneando mercado y generando informe...")

                # Aquí llamamos a tus funciones ya existentes del agente
                generar_informe()            # genera el informe de las 4 criptos base
                detectar_senales()           # detecta oportunidades de entrada/salida

                # Si detectar_senales() devuelve señales, las envía al webhook de Make
                # Ejemplo dentro de esa función: post_webhook({...})

            except Exception as e:
                print("⚠️ Error en el loop del agente:", e)

            # Esperar 300 segundos (5 minutos) antes del siguiente ciclo
            time.sleep(300)

    # 🧵 Lanzamos el bucle del agente en un hilo aparte para que no bloquee Flask
    threading.Thread(target=loop_agente, daemon=True).start()

    # 🌐 Mantener el servidor Flask vivo (Render requiere esto)
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
