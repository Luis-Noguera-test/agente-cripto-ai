import os, time, threading, requests, json, functools
from datetime import datetime, timedelta, timezone
from random import uniform
from flask import Flask, jsonify
import feedparser

# ==========================
#  CONFIGURACIÓN PRINCIPAL
# ==========================
try:
    from zoneinfo import ZoneInfo
    MADRID_TZ = ZoneInfo("Europe/Madrid")
except Exception:
    MADRID_TZ = None

print = functools.partial(print, flush=True)

WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://hook.eu2.make.com/rqycnm09n1dvatljeuyptxzsh2jhnx6t"
)

BACKUP_WEBHOOK_URL = os.environ.get(
    "BACKUP_WEBHOOK_URL",
    "https://hook.eu2.make.com/sjqu4jif4zkmamqyd30eycr19i57wbvb"
)

BACKUP_RESTORE_URL = os.environ.get(
    "BACKUP_RESTORE_URL",
    "https://script.google.com/macros/s/AKfycbytxQTl2_s-LCwhYcGPdjZAL7_0ZILz4eRgcKH4y6ZDVA8foYDmL0AagndW2nHAOA8s/exec"
)

SYMBOLS = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT").split(",")
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))
SEND_TEST_ON_DEPLOY = os.environ.get("SEND_TEST_ON_DEPLOY", "true").lower() == "true"

REPORT_TIMES_LOCAL = {"09:00", "21:00"}
OPEN_REPORT_LOCAL = "22:00"
BACKUP_TIME_LOCAL = "22:05"

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

# ==========================
#  UTILIDADES
# ==========================
def nowiso():
    """Hora local de España (ajusta DST automáticamente)."""
    if MADRID_TZ:
        return datetime.now(MADRID_TZ).isoformat(timespec="seconds")
    return (datetime.utcnow() + timedelta(hours=2)).isoformat(timespec="seconds")

def now_local():
    if MADRID_TZ:
        return datetime.now(MADRID_TZ)
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

# ==========================
#  CACHÉ LOCAL
# ==========================
cache = safe_load_json(CACHE_PATH, {})

def get_cached(key, max_age_sec: int):
    e = cache.get(key)
    if not e:
        return None
    ts = datetime.fromisoformat(e["ts"])
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    if (now - ts).total_seconds() > max_age_sec:
        return None
    return e["data"]

def set_cache(key, data):
    cache[key] = {"ts": nowiso(), "data": data}
    safe_save_json(CACHE_PATH, cache)

# ==========================
#  PARÁMETROS (con autoajuste)
# ==========================
params = safe_load_json(PARAMS_PATH, {
    "SMA_FAST": 6,
    "SMA_SLOW": 70,
    "ATR_LEN": 14,
    "VOL_LEN": 20,
    "PULLBACK_ATR": 0.25,  # proximidad a SMA_fast medida en ATRs
    "SL_PCT": 0.03,
    "TP_PCT": 0.06,        # 6% por defecto
    "RISK_PCT": 3.0
})

def auto_tune():
    """Ajuste simple según winrate de los últimos 30 trades."""
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

# ==========================
#  HTTP / BINANCE
# ==========================
def http_get(url, params=None, timeout=12):
    global BINANCE
    tries = 0
    endpoint_idx = 0
    while True:
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 + tries * 2)
                tries += 1
                continue
            if r.status_code == 451:
                if endpoint_idx + 1 < len(BINANCE_ENDPOINTS):
                    old = BINANCE_ENDPOINTS[endpoint_idx]
                    endpoint_idx += 1
                    BINANCE = BINANCE_ENDPOINTS[endpoint_idx]
                    url = url.replace(old, BINANCE)
                    continue
                else:
                    return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if tries < 2:
                time.sleep(2)
                tries += 1
                continue
            print("❌ HTTP error:", e)
            return None

def get_klines(symbol, interval="1h", limit=200):
    key = f"k_{symbol}_{interval}_{limit}"
    cached = get_cached(key, max_age_sec=55)
    if cached:
        return cached
    url = f"{BINANCE}/api/v3/klines"
    data = http_get(url, {"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        return []
    out = []
    for k in data:
        out.append({
            "t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]), "v": float(k[5])
        })
    set_cache(key, out)
    return out

def price_now(symbol):
    url = f"{BINANCE}/api/v3/ticker/price"
    d = http_get(url, {"symbol": symbol})
    try:
        return float(d["price"]) if d else None
    except:
        return None

def price_24h(symbol):
    key = f"p24_{symbol}"
    cached = get_cached(key, max_age_sec=55)
    if cached:
        return cached
    url = f"{BINANCE}/api/v3/ticker/24hr"
    data = http_get(url, {"symbol": symbol})
    if not data:
        return (0, 0, 0, 0)
    cur = float(data["lastPrice"]); low = float(data["lowPrice"])
    high = float(data["highPrice"]); pct = float(data["priceChangePercent"])
    val = (cur, low, high, pct); set_cache(key, val)
    return val

# ==========================
#  INDICADORES
# ==========================
def sma(values, n): return sum(values[-n:]) / n if len(values) >= n else None
def avg(values, n): return sum(values[-n:]) / n if len(values) >= n else None
def atr(kl, n):
    if len(kl) < n + 1: return None
    trs = []
    for i in range(1, n + 1):
        h, l, cprev = kl[-i]["h"], kl[-i]["l"], kl[-i - 1]["c"]
        trs.append(max(h - l, abs(h - cprev), abs(l - cprev)))
    return sum(trs) / len(trs)

# ==========================
#  NOTICIAS / SENTIMIENTO
# ==========================
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
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        j = r.json()["data"][0]; return j["value"], j["value_classification"]
    except: return None, None

# ==========================
#  BACKUP Y RESTORE
# ==========================
def backup_state():
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                content = f.read()
            payload = {
                "tipo": "backup",
                "file_name": "state.json",
                "contenido": content,
                "timestamp": nowiso()
            }
            requests.post(BACKUP_WEBHOOK_URL, json=payload, timeout=10)
            print("💾 Backup enviado correctamente a Make (state.json)")
    except Exception as e:
        print(f"❌ Error al enviar backup: {e}")

def restore_last_backup():
    try:
        print("🔄 Intentando recuperar último backup remoto...")
        r = requests.get(BACKUP_RESTORE_URL, timeout=15)
        if r.status_code != 200:
            return False
        data = r.json()
        contenido = data.get("contenido")
        if not contenido:
            return False
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            f.write(contenido)
        print("✅ Backup restaurado correctamente desde Google Sheets.")
        return True
    except Exception as e:
        print(f"❌ Error al restaurar backup: {e}")
        return False

# ==========================
#  ESTADO Y RENDIMIENTO
# ==========================
state = safe_load_json(STATE_PATH, {s: {"trades": []} for s in SYMBOLS})
performance = safe_load_json(PERF_PATH, {"trades": [], "wins": 0, "losses": 0})

def record_trade(sym, result, direction):
    performance["trades"].append({"sym": sym, "result": result, "dir": direction, "ts": nowiso()})
    if result == "TP": performance["wins"] += 1
    if result == "SL": performance["losses"] += 1
    performance["trades"] = performance["trades"][-200:]
    safe_save_json(PERF_PATH, performance)

# ==========================
#  LÓGICA DE SEÑALES
# ==========================
def evaluate_symbol(symbol):
    """Evalúa señales y gestiona cierres con control de duplicados y entradas múltiples."""
    kl = get_klines(symbol, "1h", 200)
    if not kl:
        return None
    closes = [x["c"] for x in kl]
    vols = [x["v"] for x in kl]
    p = closes[-1]

    s_fast = sma(closes, params["SMA_FAST"])
    s_slow = sma(closes, params["SMA_SLOW"])
    _atr = atr(kl, params["ATR_LEN"])
    v_avg = avg(vols, params["VOL_LEN"])
    if any(x is None for x in [s_fast, s_slow, _atr, v_avg]):
        return None

    v_last = vols[-1]
    vol_ok = v_last >= 0.8 * v_avg
    pull_ok = abs(p - s_fast) <= _atr * params["PULLBACK_ATR"]

    st = state.setdefault(symbol, {"trades": []})
    new_payloads = []

    # ===== Anti-duplicado: comprobamos si ya se envió señal similar en últimas 24h =====
    recent_trades = [t for t in performance.get("trades", [])
                     if "ts" in t and datetime.fromisoformat(t["ts"]) > datetime.utcnow() - timedelta(hours=24)]
    recent_symbols = {(t["sym"], t["dir"]) for t in recent_trades}

    # ===== Entradas =====
    if vol_ok and pull_ok:
        # LARGO
        if s_fast > s_slow:
            # Permitimos varias operaciones por dirección si la entrada difiere al menos 1%
            active_longs = [tr for tr in st["trades"] if tr["open"] and tr["dir"] == "L"]
            same_entry = any(abs(tr["entry"] - p) / p < 0.01 for tr in active_longs)
            if not same_entry and (symbol, "L") not in recent_symbols:
                entry = round(p, 4)
                sl = round(entry * (1 - params["SL_PCT"]), 4)
                tp = round(entry * (1 + params["TP_PCT"]), 4)
                trade = {"dir": "L", "entry": entry, "sl": sl, "tp": tp, "open": True}
                st["trades"].append(trade)
                safe_save_json(STATE_PATH, state)
                new_payloads.append({
                    "evento": "nueva_senal", "tipo": "Largo",
                    "activo": sym_to_pair(symbol),
                    "entrada": entry, "sl": sl, "tp": tp,
                    "riesgo": params["RISK_PCT"], "timeframe": "H1",
                    "timestamp": nowiso(),
                    "comentario": "Cruce SMAfast>SMA slow + pullback (ATR) y volumen OK."
                })

        # CORTO
        if s_fast < s_slow:
            active_shorts = [tr for tr in st["trades"] if tr["open"] and tr["dir"] == "S"]
            same_entry = any(abs(tr["entry"] - p) / p < 0.01 for tr in active_shorts)
            if not same_entry and (symbol, "S") not in recent_symbols:
                entry = round(p, 4)
                sl = round(entry * (1 + params["SL_PCT"]), 4)
                tp = round(entry * (1 - params["TP_PCT"]), 4)
                trade = {"dir": "S", "entry": entry, "sl": sl, "tp": tp, "open": True}
                st["trades"].append(trade)
                safe_save_json(STATE_PATH, state)
                new_payloads.append({
                    "evento": "nueva_senal", "tipo": "Corto",
                    "activo": sym_to_pair(symbol),
                    "entrada": entry, "sl": sl, "tp": tp,
                    "riesgo": params["RISK_PCT"], "timeframe": "H1",
                    "timestamp": nowiso(),
                    "comentario": "Cruce SMAfast<SMA slow + pullback (ATR) y volumen OK."
                })

    # ===== Gestión intrabar (TP / SL) =====
    if st["trades"]:
        cur = price_now(symbol)
        if cur is not None:
            still_open = []
            for tr in st["trades"]:
                if not tr["open"]:
                    continue
                if tr["dir"] == "L":
                    if cur <= tr["sl"]:
                        tr["open"] = False; record_trade(symbol, "SL", "L")
                        new_payloads.append({
                            "evento": "cierre", "activo": sym_to_pair(symbol),
                            "resultado": "SL", "precio_cierre": cur,
                            "timestamp": nowiso(), "comentario": "SL alcanzado (Largo)."
                        })
                    elif cur >= tr["tp"]:
                        tr["open"] = False; record_trade(symbol, "TP", "L")
                        new_payloads.append({
                            "evento": "cierre", "activo": sym_to_pair(symbol),
                            "resultado": "TP", "precio_cierre": cur,
                            "timestamp": nowiso(), "comentario": "TP alcanzado (Largo)."
                        })
                elif tr["dir"] == "S":
                    if cur >= tr["sl"]:
                        tr["open"] = False; record_trade(symbol, "SL", "S")
                        new_payloads.append({
                            "evento": "cierre", "activo": sym_to_pair(symbol),
                            "resultado": "SL", "precio_cierre": cur,
                            "timestamp": nowiso(), "comentario": "SL alcanzado (Corto)."
                        })
                    elif cur <= tr["tp"]:
                        tr["open"] = False; record_trade(symbol, "TP", "S")
                        new_payloads.append({
                            "evento": "cierre", "activo": sym_to_pair(symbol),
                            "resultado": "TP", "precio_cierre": cur,
                            "timestamp": nowiso(), "comentario": "TP alcanzado (Corto)."
                        })
                if tr["open"]:
                    still_open.append(tr)
            st["trades"] = still_open
            safe_save_json(STATE_PATH, state)

    return new_payloads if new_payloads else None

# ==========================
#  INFORMES
# ==========================
def price_24h_line(symbol):
    c, low, high, pct = price_24h(symbol)
    return f"{sym_to_pair(symbol)} {c:.2f} (24h {pct:+.2f}%) Rango {low:.2f}–{high:.2f}"

def report_payload_market():
    lines = [price_24h_line(s) for s in SYMBOLS]
    fg_v, fg_txt = fear_greed()
    fg_line = f"Fear&Greed: {fg_v} ({fg_txt})" if fg_v else "Fear&Greed: s/d"
    headlines = coindesk_headlines(3) + theblock_headlines(2) + ft_headlines(2)

    total = len(performance.get("trades", []))
    wins = performance.get("wins", 0)
    losses = performance.get("losses", 0)
    winrate = (wins / total * 100) if total > 0 else 0
    rentabilidad = ((wins - losses) / total * 100) if total > 0 else 0

    metrics = (f"📊 Métricas del sistema\n"
               f"Rentabilidad histórica: {rentabilidad:+.2f}%\n"
               f"Aciertos: {winrate:.1f}% ({wins}W/{losses}L)\n"
               f"Operaciones totales: {total}")

    comentario = metrics if now_local().strftime("%H:%M") == "09:00" else "Informe de mercado (Binance + RSS)."

    return {
        "evento": "informe",
        "tipo": "miniresumen_12h",
        "timestamp": nowiso(),
        "precios": lines,
        "sentimiento": fg_line,
        "titulares": headlines[:5],
        "comentario": comentario
    }

def report_payload_open_positions():
    open_lines = []
    today_signals = {"abiertas": [], "cerradas": []}
    today_date = now_local().date()

    for t in performance.get("trades", []):
        ts = datetime.fromisoformat(t["ts"]).date() if "ts" in t else None
        if ts == today_date:
            if t["result"] in ("TP", "SL"):
                today_signals["cerradas"].append(f"{t['sym']} {t['result']}")
            else:
                today_signals["abiertas"].append(t["sym"])

    wins_today = sum(1 for t in performance.get("trades", [])
                     if "ts" in t and datetime.fromisoformat(t["ts"]).date() == today_date and t["result"] == "TP")
    losses_today = sum(1 for t in performance.get("trades", [])
                       if "ts" in t and datetime.fromisoformat(t["ts"]).date() == today_date and t["result"] == "SL")
    total_today = wins_today + losses_today
    rent_today = ((wins_today - losses_today) / total_today * 100) if total_today > 0 else 0

    for sym, st in state.items():
        for tr in st["trades"]:
            if tr["open"]:
                open_lines.append(f"{sym_to_pair(sym)} {tr['dir']} @ {tr['entry']} (SL {tr['sl']}, TP {tr['tp']})")
    if not open_lines:
        open_lines = ["Sin operaciones abiertas actualmente."]

    comentario = (f"📊 Resumen diario de operaciones\n\n"
                  f"🟢 Aperturas del día: {', '.join(today_signals['abiertas']) or 'Ninguna'}\n"
                  f"🔴 Cierres del día: {', '.join(today_signals['cerradas']) or 'Ninguno'}\n"
                  f"💰 Rentabilidad del día: {rent_today:+.2f}%")

    return {
        "evento": "informe",
        "tipo": "resumen_operaciones",
        "timestamp": nowiso(),
        "precios": open_lines,
        "sentimiento": "",
        "titulares": [],
        "comentario": comentario
    }

# ==========================
#  BUCLES PRINCIPALES
# ==========================
def report_loop():
    print("🕓 report loop activo → 09:00 & 21:00 (mercado), 22:00 (posiciones), 22:05 (backup) hora España")
    last_report_min = None
    last_open_min = None
    last_backup_min = None
    last_state_log = None
    last_heartbeat_min = None

    while True:
        try:
            now_loc = now_local()
            hhmm = now_loc.strftime("%H:%M")

            # Heartbeat cada 5 min
            if (now_loc.minute % 5 == 0) and last_heartbeat_min != hhmm:
                print(f"💓 Heartbeat {nowiso()}")
                last_heartbeat_min = hhmm

            # Log de operaciones abiertas cada hora
            if now_loc.minute == 0 and (not last_state_log or now_loc.hour != last_state_log.hour):
                print("📊 Estado de operaciones abiertas:")
                for sym, st in state.items():
                    if not st["trades"]:
                        print(f" - {sym}: sin operaciones abiertas")
                    else:
                        for tr in st["trades"]:
                            status = "abierta" if tr["open"] else "cerrada"
                            print(f" - {sym} {tr['dir']} @ {tr['entry']} → {status} (SL {tr['sl']}, TP {tr['tp']})")
                last_state_log = now_loc

            # Informes mercado
            if hhmm in REPORT_TIMES_LOCAL and last_report_min != hhmm:
                requests.post(WEBHOOK_URL, json=report_payload_market(), timeout=10)
                print(f"📤 Informe 12h enviado ({hhmm} local).")
                auto_tune()
                last_report_min = hhmm

            # Informe posiciones abiertas
            if hhmm == OPEN_REPORT_LOCAL and last_open_min != hhmm:
                requests.post(WEBHOOK_URL, json=report_payload_open_positions(), timeout=10)
                print(f"📤 Informe de posiciones abiertas enviado ({hhmm} local).")
                last_open_min = hhmm

            # Backup
            if hhmm == BACKUP_TIME_LOCAL and last_backup_min != hhmm:
                backup_state()
                last_backup_min = hhmm

        except Exception as e:
            print("report error:", e)
        time.sleep(15)

def scan_loop():
    print(f"🌀 scan loop: {LOOP_SECONDS}s | rotación 1 símbolo/iteración")
    idx = 0
    while True:
        try:
            sym = SYMBOLS[idx % len(SYMBOLS)]
            print(f"🔍 Escaneando {sym} ...")
            payloads = evaluate_symbol(sym)
            if payloads:
                for pld in payloads:
                    print(f"📈 Evento {pld['evento']} → {pld.get('tipo', pld.get('resultado',''))} {pld.get('activo','')}")
                    requests.post(WEBHOOK_URL, json=pld, timeout=10)
            print(f"✅ Escaneo completado para {sym}. Esperando {LOOP_SECONDS}s...\n")
            idx += 1
        except Exception as e:
            print("scan error:", e)
        time.sleep(LOOP_SECONDS + uniform(0.5, 1.5))

# ==========================
#  FLASK / MAIN
# ==========================
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({
        "status": "ok", "time": nowiso(), "symbols": SYMBOLS,
        "params": params,
        "open_state": state,
        "perf": {"wins": performance["wins"], "losses": performance["losses"], "n": len(performance["trades"])}
    })

def start_threads():
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=report_loop, daemon=True).start()

if __name__ == "__main__":
    print("🚀 Iniciando agente Cripto AI con métricas, backup y autoaprendizaje…")
    restored = restore_last_backup()
    if not restored:
        print("⚠️ No se pudo restaurar backup remoto, usando estado local.")
    if SEND_TEST_ON_DEPLOY:
        requests.post(WEBHOOK_URL, json={
            "evento": "nueva_senal", "tipo": "Largo", "activo": "BTC/USD",
            "entrada": 123100, "sl": 119407, "tp": 130,  # valores dummy
            "riesgo": params["RISK_PCT"], "timeframe": "H1",
            "timestamp": nowiso(), "comentario": "Prueba de despliegue (Render)."
        })
    start_threads()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
