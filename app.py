import os, time, threading, requests, json, functools
from datetime import datetime, timedelta, timezone
from random import uniform
from flask import Flask, jsonify
import feedparser

# Intentamos usar zona horaria real de Madrid (maneja DST)
try:
    from zoneinfo import ZoneInfo
    MADRID_TZ = ZoneInfo("Europe/Madrid")
except Exception:
    MADRID_TZ = None  # fallback a UTC más abajo

# Logs con flush inmediato (Render)
print = functools.partial(print, flush=True)

# ========== Config ==========
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://hook.eu2.make.com/rqycnm09n1dvatljeuyptxzsh2jhnx6t"
)

SYMBOLS = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT").split(",")
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))  # escaneo por símbolo
SEND_TEST_ON_DEPLOY = os.environ.get("SEND_TEST_ON_DEPLOY", "true").lower() == "true"

# Horarios fijos España
REPORT_TIMES_LOCAL = {"09:00", "21:00"}  # informes de mercado
OPEN_REPORT_LOCAL = "22:00"              # informe de posiciones abiertas

STATE_PATH  = "state.json"
PERF_PATH   = "performance.json"
CACHE_PATH  = "cache.json"
PARAMS_PATH = "params.json"

BINANCE_ENDPOINTS = [
    "https://api.binance.com",
    "https://api.binance.us",
    "https://api.binance.me"
]
BINANCE = BINANCE_ENDPOINTS[0]
HEADERS = {"User-Agent": "Mozilla/5.0 (CriptoAI Bot)"}

# ========== Utilidades ==========
def nowiso():
    return datetime.now().isoformat(timespec="seconds")

def now_local():
    # Devuelve ahora en hora local de Madrid si es posible
    if MADRID_TZ:
        return datetime.now(MADRID_TZ)
    # Fallback: aproximamos con UTC (no ajusta DST, pero evita romper)
    return datetime.now(timezone.utc)

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

# ========== Caché ==========
cache = safe_load_json(CACHE_PATH, {})

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

# ========== Parámetros dinámicos / Auto-ajuste ==========
params = safe_load_json(PARAMS_PATH, {
    "SMA_FAST": int(os.environ.get("SMA_FAST", "6")),
    "SMA_SLOW": int(os.environ.get("SMA_SLOW", "70")),
    "ATR_LEN": int(os.environ.get("ATR_LEN", "14")),
    "VOL_LEN": int(os.environ.get("VOL_LEN", "20")),
    "PULLBACK_ATR": float(os.environ.get("PULLBACK_ATR", "0.25")),
    "SL_PCT": float(os.environ.get("SL_PCT", "0.03")),
    "TP_PCT": float(os.environ.get("TP_PCT", "0.04")),
    "RISK_PCT": float(os.environ.get("RISK_PCT", "3.0"))
})

def auto_tune():
    trades = performance.get("trades", [])
    if len(trades) < 30:
        return
    recent = trades[-30:]
    wins = sum(1 for t in recent if t["result"] == "TP")
    losses = sum(1 for t in recent if t["result"] == "SL")
    total = wins + losses
    if total == 0:
        return
    winrate = wins / total
    print(f"🤖 Auto-tuning: últimos {total} trades, winrate={winrate:.2%}")
    if winrate < 0.45:
        params["PULLBACK_ATR"] = max(params["PULLBACK_ATR"] * 0.9, 0.1)
        params["VOL_LEN"] = min(params["VOL_LEN"] + 2, 50)
    elif winrate > 0.65:
        params["RISK_PCT"] = min(params["RISK_PCT"] * 1.05, 5.0)
    safe_save_json(PARAMS_PATH, params)

# ========== HTTP / Binance ==========
def http_get(url, params=None, timeout=12):
    global BINANCE
    tries = 0
    endpoint_idx = 0
    while True:
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                wait = 2 + tries * 2
                print(f"⚠️ 429 rate limit. Backoff {wait}s...")
                time.sleep(wait); tries += 1; continue
            if r.status_code == 451:
                if endpoint_idx + 1 < len(BINANCE_ENDPOINTS):
                    old = BINANCE_ENDPOINTS[endpoint_idx]
                    endpoint_idx += 1
                    BINANCE = BINANCE_ENDPOINTS[endpoint_idx]
                    print(f"⚠️ 451 bloqueado. Cambiando a {BINANCE}")
                    url = url.replace(old, BINANCE)
                    continue
                else:
                    print("❌ 451 bloqueado en todos los endpoints.")
                    return None
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code and 500 <= code < 600 and tries < 3:
                wait = 1 + tries * 2
                print(f"⚠️ {code} server error. Retry {wait}s...")
                time.sleep(wait); tries += 1; continue
            print("❌ HTTP error:", e)
            return None
        except Exception as e:
            if tries < 2:
                wait = 1 + tries
                print(f"⚠️ network error: {e}. Retry {wait}s...")
                time.sleep(wait); tries += 1; continue
            print("❌ network error:", e)
            return None

def get_klines(symbol, interval="1h", limit=200):
    key = f"k_{symbol}_{interval}_{limit}"
    cached = get_cached(key, max_age_sec=55)
    if cached: return cached
    url = f"{BINANCE}/api/v3/klines"
    params_req = {"symbol": symbol, "interval": interval, "limit": limit}
    data = http_get(url, params=params_req)
    if not data: return get_cached(key, 999999) or []
    out = []
    for k in data:
        out.append({
            "t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]), "v": float(k[5])
        })
    set_cache(key, out)
    return out

def price_now(symbol):
    # Precio spot actual para cierres intrabar
    url = f"{BINANCE}/api/v3/ticker/price"
    d = http_get(url, params={"symbol": symbol})
    try:
        return float(d["price"]) if d else None
    except:
        return None

def price_24h(symbol):
    key = f"p24_{symbol}"
    cached = get_cached(key, max_age_sec=55)
    if cached: return cached
    url = f"{BINANCE}/api/v3/ticker/24hr"
    data = http_get(url, params={"symbol": symbol})
    if not data: return get_cached(key, 999999) or (0,0,0,0)
    cur = float(data["lastPrice"]); low = float(data["lowPrice"])
    high = float(data["highPrice"]); pct = float(data["priceChangePercent"])
    val = (cur, low, high, pct); set_cache(key, val)
    return val

# ========== Indicadores ==========
def sma(values, n):
    if len(values) < n: return None
    return sum(values[-n:]) / n

def atr(kl, n):
    if len(kl) < n+1: return None
    trs=[]
    for i in range(1, n+1):
        h=kl[-i]["h"]; l=kl[-i]["l"]; cprev=kl[-i-1]["c"]
        tr = max(h-l, abs(h-cprev), abs(l-cprev))
        trs.append(tr)
    return sum(trs)/len(trs)

def avg(values, n):
    if len(values)<n: return None
    return sum(values[-n:])/n

# ========== Noticias & Sentimiento ==========
def coindesk_headlines(n=3):
    try: return [e.title for e in feedparser.parse("https://www.coindesk.com/arc/outboundfeeds/rss/").entries[:n]]
    except: return []
def theblock_headlines(n=2):
    try: return [e.title for e in feedparser.parse("https://www.theblock.co/rss.xml").entries[:n]]
    except: return []
def ft_headlines(n=2):
    try: return [e.title for e in feedparser.parse("https://www.ft.com/technology/cryptocurrencies?format=rss").entries[:n]]
    except: return []
def fear_greed():
    try:
        r=requests.get("https://api.alternative.me/fng/?limit=1",timeout=10)
        j=r.json()["data"][0]; return j["value"], j["value_classification"]
    except: return None,None

# ========== Webhook ==========
def post_webhook(payload):
    if not WEBHOOK_URL: return
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10)
        print("📤 webhook →", payload.get("evento"), payload.get("activo",""))
    except Exception as e:
        print("webhook error:", e)

# ========== Estado & rendimiento ==========
# Multi-trades por símbolo; evitamos duplicados por dirección
state = safe_load_json(STATE_PATH, {s: {"trades": []} for s in SYMBOLS})
performance = safe_load_json(PERF_PATH, {"trades": [], "wins":0, "losses":0})

def record_trade(sym,result,direction):
    performance["trades"].append({"sym":sym,"result":result,"dir":direction,"ts":nowiso()})
    if result=="TP": performance["wins"]+=1
    if result=="SL": performance["losses"]+=1
    performance["trades"]=performance["trades"][-200:]
    safe_save_json(PERF_PATH,performance)

# ========== Señales + Gestión ==========
def evaluate_symbol(symbol):
    kl=get_klines(symbol,"1h",200)
    if not kl: return None
    closes=[x["c"] for x in kl]; vols=[x["v"] for x in kl]; p=closes[-1]

    s_fast=sma(closes,params["SMA_FAST"])
    s_slow=sma(closes,params["SMA_SLOW"])
    _atr=atr(kl,params["ATR_LEN"]); v_avg=avg(vols,params["VOL_LEN"])
    if any(x is None for x in [s_fast,s_slow,_atr,v_avg]): return None

    v_last=vols[-1]; vol_ok=v_last>=0.8*v_avg
    pull_ok=abs(p-s_fast)<=_atr*params["PULLBACK_ATR"]
    st=state.setdefault(symbol,{"trades":[]}); new_payloads=[]

    # ===== Entradas =====
    # LARGO
    if (s_fast > s_slow) and vol_ok and pull_ok:
        if not any(tr["open"] and tr["dir"]=="L" for tr in st["trades"]):
            entry=round(p,6); sl=round(entry*(1-params["SL_PCT"]),6); tp=round(entry*(1+params["TP_PCT"]),6)
            trade={"dir":"L","entry":entry,"sl":sl,"tp":tp,"open":True}
            st["trades"].append(trade); safe_save_json(STATE_PATH,state)
            new_payloads.append({
                "evento":"nueva_senal","tipo":"Largo",
                "activo":sym_to_pair(symbol),
                "entrada":entry,"sl":sl,"tp":tp,
                "riesgo":params["RISK_PCT"],
                "timeframe":"H1","timestamp":nowiso(),
                "comentario":"Cruce SMAfast>SMA slow + pullback (ATR) y volumen OK."
            })
    # CORTO
    if (s_fast < s_slow) and vol_ok and pull_ok:
        if not any(tr["open"] and tr["dir"]=="S" for tr in st["trades"]):
            entry=round(p,6); sl=round(entry*(1+params["SL_PCT"]),6); tp=round(entry*(1-params["TP_PCT"]),6)
            trade={"dir":"S","entry":entry,"sl":sl,"tp":tp,"open":True}
            st["trades"].append(trade); safe_save_json(STATE_PATH,state)
            new_payloads.append({
                "evento":"nueva_senal","tipo":"Corto",
                "activo":sym_to_pair(symbol),
                "entrada":entry,"sl":sl,"tp":tp,
                "riesgo":params["RISK_PCT"],
                "timeframe":"H1","timestamp":nowiso(),
                "comentario":"Cruce SMAfast<SMA slow + pullback (ATR) y volumen OK."
            })

    # ===== Gestión intrabar (precio actual) =====
    if st["trades"]:
        cur = price_now(symbol)
        if cur is not None:
            still_open=[]
            for tr in st["trades"]:
                if not tr["open"]:
                    continue
                if tr["dir"]=="L":
                    if cur <= tr["sl"]:
                        tr["open"]=False; record_trade(symbol,"SL","L")
                        new_payloads.append({"evento":"cierre","activo":sym_to_pair(symbol),"resultado":"SL","precio_cierre":cur,"timestamp":nowiso(),"comentario":"SL alcanzado (Largo)."})
                    elif cur >= tr["tp"]:
                        tr["open"]=False; record_trade(symbol,"TP","L")
                        new_payloads.append({"evento":"cierre","activo":sym_to_pair(symbol),"resultado":"TP","precio_cierre":cur,"timestamp":nowiso(),"comentario":"TP alcanzado (Largo)."})
                elif tr["dir"]=="S":
                    if cur >= tr["sl"]:
                        tr["open"]=False; record_trade(symbol,"SL","S")
                        new_payloads.append({"evento":"cierre","activo":sym_to_pair(symbol),"resultado":"SL","precio_cierre":cur,"timestamp":nowiso(),"comentario":"SL alcanzado (Corto)."})
                    elif cur <= tr["tp"]:
                        tr["open"]=False; record_trade(symbol,"TP","S")
                        new_payloads.append({"evento":"cierre","activo":sym_to_pair(symbol),"resultado":"TP","precio_cierre":cur,"timestamp":nowiso(),"comentario":"TP alcanzado (Corto)."})
                if tr["open"]:
                    still_open.append(tr)
            st["trades"]=still_open
            safe_save_json(STATE_PATH,state)

    return new_payloads if new_payloads else None

# ========== Informes ==========
def price_24h_line(symbol):
    c,low,high,pct=price_24h(symbol)
    return f"{sym_to_pair(symbol)} {c:.2f} (24h {pct:+.2f}%) Rango {low:.2f}–{high:.2f}"

def report_payload_market():
    lines=[price_24h_line(s) for s in SYMBOLS]
    fg_v,fg_txt=fear_greed()
    fg_line=f"Fear&Greed: {fg_v} ({fg_txt})" if fg_v else "Fear&Greed: s/d"
    headlines=coindesk_headlines(3)+theblock_headlines(2)+ft_headlines(2)
    return {
        "evento":"informe",
        "tipo":"miniresumen_12h",
        "timestamp":nowiso(),
        "precios":lines,
        "sentimiento":fg_line,
        "titulares":headlines[:5],
        "comentario":"Informe de mercado (Binance + RSS)."
    }

def report_payload_open_positions():
    open_lines=[]
    for sym, st in state.items():
        if not st["trades"]:
            continue
        for tr in st["trades"]:
            if tr["open"]:
                open_lines.append(f"{sym_to_pair(sym)} {tr['dir']} @ {tr['entry']}  (SL {tr['sl']}, TP {tr['tp']})")
    if not open_lines:
        open_lines=["Sin operaciones abiertas actualmente."]
    return {
        "evento":"informe",
        "tipo":"resumen_operaciones",
        "timestamp":nowiso(),
        "precios":open_lines,
        "sentimiento":"",
        "titulares":[],
        "comentario":"Informe de posiciones abiertas."
    }

# ========== Bucles ==========
def scan_loop():
    print(f"🌀 scan loop: {LOOP_SECONDS}s | rotación 1 símbolo/iteración")
    idx=0
    while True:
        try:
            sym=SYMBOLS[idx%len(SYMBOLS)]
            print(f"🔍 Escaneando {sym} ...")
            payloads=evaluate_symbol(sym)
            if payloads:
                for pld in payloads:
                    print(f"📈 Evento {pld['evento']} → {pld.get('tipo', pld.get('resultado',''))} {pld.get('activo','')}")
                    post_webhook(pld)
            print(f"✅ Escaneo completado para {sym}. Esperando {LOOP_SECONDS}s...\n")
            idx+=1
        except Exception as e:
            print("scan error:", e)
        time.sleep(LOOP_SECONDS + uniform(0.5,1.5))

def report_loop():
    print("🕓 report loop activo → 09:00 & 21:00 (mercado), 22:00 (posiciones) hora España")
    last_report_min = None
    last_open_min = None
    last_state_log = None
    last_heartbeat_min = None

    while True:
        try:
            now_loc = now_local()
            hhmm = now_loc.strftime("%H:%M")

            # Heartbeat cada 5 min (1 vez por minuto objetivo)
            if (now_loc.minute % 5 == 0):
                if last_heartbeat_min != hhmm:
                    print(f"💓 Heartbeat {nowiso()}"); last_heartbeat_min = hhmm

            # Log de operaciones abiertas cada hora en punto
            if now_loc.minute == 0 and (not last_state_log or now_loc.hour != last_state_log.hour):
                print("📊 Estado de operaciones abiertas:")
                for sym, st in state.items():
                    if not st["trades"]:
                        print(f" - {sym}: sin operaciones abiertas")
                    else:
                        for tr in st["trades"]:
                            status="abierta" if tr["open"] else "cerrada"
                            print(f" - {sym} {tr['dir']} @ {tr['entry']} → {status} (SL {tr['sl']}, TP {tr['tp']})")
                last_state_log = now_loc

            # Informes de mercado (09:00 y 21:00)
            if hhmm in REPORT_TIMES_LOCAL and last_report_min != hhmm:
                post_webhook(report_payload_market())
                print(f"📤 Informe 12h enviado ({hhmm} local).")
                auto_tune()
                last_report_min = hhmm

            # Informe de posiciones abiertas (22:00)
            if hhmm == OPEN_REPORT_LOCAL and last_open_min != hhmm:
                post_webhook(report_payload_open_positions())
                print(f"📤 Informe de posiciones abiertas enviado ({hhmm} local).")
                last_open_min = hhmm

        except Exception as e:
            print("report error:", e)

        time.sleep(15)

# ========== Flask ==========
app=Flask(__name__)

@app.get("/")
def health():
    return jsonify({
        "status":"ok","time":nowiso(),"symbols":SYMBOLS,
        "params":params,
        "open_state":state,
        "perf":{"wins":performance["wins"],"losses":performance["losses"],"n":len(performance["trades"])}
    })

def start_threads():
    t1=threading.Thread(target=scan_loop,daemon=True)
    t2=threading.Thread(target=report_loop,daemon=True)
    t1.start(); t2.start()

# ========== Main ==========
if __name__=="__main__":
    print("🚀 Iniciando agente Cripto AI (híbrido)…")
    if SEND_TEST_ON_DEPLOY:
        post_webhook({
            "evento":"nueva_senal","tipo":"Largo","activo":"BTC/USD",
            "entrada":123100,"sl":119407,"tp":128024,
            "riesgo":params["RISK_PCT"],"timeframe":"H1",
            "timestamp":nowiso(),"comentario":"Prueba de despliegue (Render)."
        })
    start_threads()
    port=int(os.environ.get("PORT","10000"))
    app.run(host="0.0.0.0",port=port)
