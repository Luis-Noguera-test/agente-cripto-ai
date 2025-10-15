import os, time, threading, requests, json, functools
from datetime import datetime, timedelta, timezone
from random import uniform
from flask import Flask, jsonify, request
import feedparser

# ==========================
#  CONFIGURACIÓN GLOBAL
# ==========================
try:
    from zoneinfo import ZoneInfo
    MADRID_TZ = ZoneInfo("Europe/Madrid")
except Exception:
    MADRID_TZ = None

print = functools.partial(print, flush=True)

# Webhook Make (señales/informes) -> Telegram
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://hook.eu2.make.com/rqycnm09n1dvatljeuyptxzsh2jhnx6t"
)

# Carpeta Google Drive (para backups)
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "19nbrjd5khqb9J7uJ7HXjJZ_6c8LslbZU")
TOKEN_FILE = os.environ.get("TOKEN_FILE", "token.pkl")

# Activos seguidos
SYMBOLS = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,AVAXUSDT,BNBUSDT").split(",")

# Frecuencia de escaneo (segundos)
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))
SEND_TEST_ON_DEPLOY = os.environ.get("SEND_TEST_ON_DEPLOY", "true").lower() == "true"

# Informes programados (hora local España)
REPORT_TIMES_LOCAL = {"09:00", "21:00"}   # informe mercado
OPEN_REPORT_LOCAL = "22:00"               # informe posiciones

# Rutas locales (Render → /tmp)
BASE_DIR   = os.environ.get("BOT_DATA_DIR", "/tmp")
STATE_PATH  = os.path.join(BASE_DIR, "state.json")
PERF_PATH   = os.path.join(BASE_DIR, "performance.json")
CACHE_PATH  = os.path.join(BASE_DIR, "cache.json")
PARAMS_PATH = os.path.join(BASE_DIR, "params.json")

# Binance endpoints
BINANCE_ENDPOINTS = [
    "https://api.binance.com",
    "https://api.binance.us",
    "https://api.binance.me"
]
BINANCE = BINANCE_ENDPOINTS[0]
HEADERS = {"User-Agent": "Mozilla/5.0 (CriptoAI Bot)"}

# ==========================
#  UTILIDADES BÁSICAS
# ==========================
def nowiso():
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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("save error", path, e)

# ==========================
#  ARCHIVOS LOCALES /tmp
# ==========================
def ensure_local_files():
    """Crea versiones iniciales si no existen (Render borra /tmp al reiniciar)."""
    os.makedirs(BASE_DIR, exist_ok=True)
    if not os.path.exists(STATE_PATH):
        safe_save_json(STATE_PATH, {s: {"trades": []} for s in SYMBOLS})
        print(f"📁 Creado vacío: {STATE_PATH}")
    if not os.path.exists(PERF_PATH):
        safe_save_json(PERF_PATH, {"wins": 0, "losses": 0, "trades": []})
        print(f"📁 Creado vacío: {PERF_PATH}")
    if not os.path.exists(PARAMS_PATH):
        safe_save_json(PARAMS_PATH, {
            "SMA_FAST": 10, "SMA_SLOW": 60, "ATR_LEN": 14,
            "VOL_LEN": 24, "PULLBACK_ATR": 0.15,
            "SL_PCT": 0.03, "TP_PCT": 0.06, "RISK_PCT": 1.0,
            "USE_ATR_STOPS": True, "SL_ATR_MULT": 1.5, "TP_ATR_MULT": 3.0,
            "MIN_VOL_RATIO": 1.0, "PARAMS_BY_SYMBOL": {}
        })
        print(f"📁 Creado vacío: {PARAMS_PATH}")

# ==========================
#  CARGA ESTADO / PARAMS / CACHE
# ==========================
ensure_local_files()
state = safe_load_json(STATE_PATH, {s: {"trades": []} for s in SYMBOLS})
performance = safe_load_json(PERF_PATH, {"wins": 0, "losses": 0, "trades": []})
params = safe_load_json(PARAMS_PATH, {
    "SMA_FAST": 10, "SMA_SLOW": 60, "ATR_LEN": 14,
    "VOL_LEN": 24, "PULLBACK_ATR": 0.15,
    "SL_PCT": 0.03, "TP_PCT": 0.06, "RISK_PCT": 1.0,
    "USE_ATR_STOPS": True, "SL_ATR_MULT": 1.5, "TP_ATR_MULT": 3.0,
    "MIN_VOL_RATIO": 1.0, "PARAMS_BY_SYMBOL": {}
})
cache = safe_load_json(CACHE_PATH, {})

def get_cached(key, max_age_sec: int):
    e = cache.get(key)
    if not e: return None
    try:
        ts = datetime.fromisoformat(e["ts"])
    except:
        return None
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    if (now - ts).total_seconds() > max_age_sec:
        return None
    return e["data"]

def set_cache(key, data):
    cache[key] = {"ts": nowiso(), "data": data}
    safe_save_json(CACHE_PATH, cache)

# ==========================
#  AUTO-APRENDIZAJE (simple)
# ==========================
def auto_tune():
    """Ajuste simple según winrate últimos 30 trades (global)."""
    trades = performance.get("trades", [])
    if len(trades) < 30: return
    recent = trades[-30:]
    wins = sum(1 for t in recent if t.get("result") == "TP")
    losses = sum(1 for t in recent if t.get("result") == "SL")
    total = wins + losses
    if total == 0: return
    winrate = wins / total
    print(f"🤖 Auto-tuning: {total} trades, winrate={winrate:.2%}")
    if winrate < 0.45:
        params["PULLBACK_ATR"] = max(params.get("PULLBACK_ATR", 0.15) * 0.9, 0.10)
        params["VOL_LEN"] = min(params.get("VOL_LEN", 24) + 2, 60)
    elif winrate > 0.65:
        params["RISK_PCT"] = min(params.get("RISK_PCT", 1.0) * 1.05, 3.0)
    safe_save_json(PARAMS_PATH, params)

# ==========================
#  HTTP / BINANCE
# ==========================
def http_get(url, params_=None, timeout=12):
    global BINANCE
    tries = 0
    endpoint_idx = 0
    while True:
        try:
            r = requests.get(url, params=params_, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 + tries * 2); tries += 1; continue
            if r.status_code == 451:
                if endpoint_idx + 1 < len(BINANCE_ENDPOINTS):
                    old = BINANCE_ENDPOINTS[endpoint_idx]
                    endpoint_idx += 1; BINANCE = BINANCE_ENDPOINTS[endpoint_idx]
                    url = url.replace(old, BINANCE); continue
                else:
                    return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if tries < 2:
                time.sleep(2); tries += 1; continue
            print("❌ HTTP error:", e); return None

def get_klines(symbol, interval="1h", limit=200):
    key = f"k_{symbol}_{interval}_{limit}"
    cached = get_cached(key, max_age_sec=55)
    if cached: return cached
    url = f"{BINANCE}/api/v3/klines"
    data = http_get(url, {"symbol": symbol, "interval": interval, "limit": limit})
    if not data: return []
    out = []
    for k in data:
        out.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                    "l": float(k[3]), "c": float(k[4]), "v": float(k[5])})
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
    if cached: return cached
    url = f"{BINANCE}/api/v3/ticker/24hr"
    data = http_get(url, {"symbol": symbol})
    if not data: return (0, 0, 0, 0)
    cur = float(data["lastPrice"]); low = float(data["lowPrice"])
    high = float(data["highPrice"]); pct = float(data["priceChangePercent"])
    val = (cur, low, high, pct); set_cache(key, val); return val

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
#  ESTADO / RENDIMIENTO
# ==========================
def record_trade(sym, result, direction):
    performance["trades"].append({"sym": sym, "result": result, "dir": direction, "ts": nowiso()})
    if result == "TP": performance["wins"] += 1
    if result == "SL": performance["losses"] += 1
    performance["trades"] = performance["trades"][-400:]  # guarda últimas 400
    safe_save_json(PERF_PATH, performance)

# ==========================
#  ESTRATEGIA + SEÑALES (H1, SMA/ATR/Volumen/Pullback, ATR-stops)
# ==========================
def evaluate_symbol(symbol):
    """Evalúa entradas y gestiona cierres intrabar (H1)."""
    kl = get_klines(symbol, "1h", 200)
    if not kl: return None
    closes = [x["c"] for x in kl]; vols = [x["v"] for x in kl]; p = closes[-1]

    # params por símbolo (si definidos)
    pmap = params.get("PARAMS_BY_SYMBOL", {}).get(symbol, {})
    SMA_FAST = pmap.get("SMA_FAST", params["SMA_FAST"])
    SMA_SLOW = pmap.get("SMA_SLOW", params["SMA_SLOW"])
    ATR_LEN  = pmap.get("ATR_LEN",  params["ATR_LEN"])
    VOL_LEN  = pmap.get("VOL_LEN",  params["VOL_LEN"])
    PULL_ATR = pmap.get("PULLBACK_ATR", params["PULLBACK_ATR"])
    MIN_VOLR = pmap.get("MIN_VOL_RATIO", params.get("MIN_VOL_RATIO", 1.0))
    USE_ATR  = pmap.get("USE_ATR_STOPS", params["USE_ATR_STOPS"])
    SLm      = pmap.get("SL_ATR_MULT", params["SL_ATR_MULT"])
    TPm      = pmap.get("TP_ATR_MULT", params["TP_ATR_MULT"])

    s_fast = sma(closes, SMA_FAST)
    s_slow = sma(closes, SMA_SLOW)
    _atr   = atr(kl, ATR_LEN)
    v_avg  = avg(vols, VOL_LEN)
    if any(x is None for x in [s_fast, s_slow, _atr, v_avg]): return None

    v_last = vols[-1]
    vol_ok = v_last >= MIN_VOLR * v_avg
    pull_ok = abs(p - s_fast) <= _atr * PULL_ATR

    st = state.setdefault(symbol, {"trades": []})
    new_payloads = []

    # Anti-duplicado señales últimas 24h
    recent = []
    for t in performance.get("trades", []):
        ts = t.get("ts")
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc) - timedelta(hours=24):
                recent.append(t)
        except: pass
    recent_tags = {(t["sym"], t["dir"]) for t in recent}

    # === Entradas ===
    if vol_ok and pull_ok:
        # Largo
        if s_fast > s_slow and (symbol, "L") not in recent_tags:
            entry = round(p, 4)
            if USE_ATR:
                sl = round(entry - _atr * SLm, 4)
                tp = round(entry + _atr * TPm, 4)
            else:
                sl = round(entry * (1 - params["SL_PCT"]), 4)
                tp = round(entry * (1 + params["TP_PCT"]), 4)
            already_similar = any(tr["open"] and tr["dir"]=="L" and abs(tr["entry"]-entry)/entry<0.01 for tr in st["trades"])
            if not already_similar:
                st["trades"].append({"dir":"L","entry":entry,"sl":sl,"tp":tp,"open":True})
                safe_save_json(STATE_PATH, state)
                new_payloads.append({
                    "evento":"nueva_senal","tipo":"Largo","activo":sym_to_pair(symbol),
                    "entrada":entry,"sl":sl,"tp":tp,"riesgo":params["RISK_PCT"],"timeframe":"H1",
                    "timestamp":nowiso(),"comentario":"SMAfast>SMAslow + pullback (ATR) + volumen OK."
                })

        # Corto
        if s_fast < s_slow and (symbol, "S") not in recent_tags:
            entry = round(p, 4)
            if USE_ATR:
                sl = round(entry + _atr * SLm, 4)
                tp = round(entry - _atr * TPm, 4)
            else:
                sl = round(entry * (1 + params["SL_PCT"]), 4)
                tp = round(entry * (1 - params["TP_PCT"]), 4)
            already_similar = any(tr["open"] and tr["dir"]=="S" and abs(tr["entry"]-entry)/entry<0.01 for tr in st["trades"])
            if not already_similar:
                st["trades"].append({"dir":"S","entry":entry,"sl":sl,"tp":tp,"open":True})
                safe_save_json(STATE_PATH, state)
                new_payloads.append({
                    "evento":"nueva_senal","tipo":"Corto","activo":sym_to_pair(symbol),
                    "entrada":entry,"sl":sl,"tp":tp,"riesgo":params["RISK_PCT"],"timeframe":"H1",
                    "timestamp":nowiso(),"comentario":"SMAfast<SMAslow + pullback (ATR) + volumen OK."
                })

    # === Gestión intrabar (precio en vivo) ===
    if st["trades"]:
        cur = price_now(symbol)
        if cur and cur > 0:
            still_open = []
            for tr in st["trades"]:
                if not tr.get("open"): continue
                dir_, sl, tp, entry = tr["dir"], float(tr["sl"]), float(tr["tp"]), float(tr["entry"])
                pair = sym_to_pair(symbol)

                if dir_ == "L":
                    if cur <= sl:
                        tr["open"]=False; record_trade(symbol,"SL","L")
                        new_payloads.append({"evento":"cierre","activo":pair,"resultado":"SL","precio_cierre":cur,
                                             "timestamp":nowiso(),
                                             "comentario":f"SL tocado (L). Entrada {entry}, SL {sl}, TP {tp}"})
                    elif cur >= tp:
                        tr["open"]=False; record_trade(symbol,"TP","L")
                        new_payloads.append({"evento":"cierre","activo":pair,"resultado":"TP","precio_cierre":cur,
                                             "timestamp":nowiso(),
                                             "comentario":f"TP tocado (L). Entrada {entry}, SL {sl}, TP {tp}"})
                else:  # Short
                    if cur >= sl:
                        tr["open"]=False; record_trade(symbol,"SL","S")
                        new_payloads.append({"evento":"cierre","activo":pair,"resultado":"SL","precio_cierre":cur,
                                             "timestamp":nowiso(),
                                             "comentario":f"SL tocado (S). Entrada {entry}, SL {sl}, TP {tp}"})
                    elif cur <= tp:
                        tr["open"]=False; record_trade(symbol,"TP","S")
                        new_payloads.append({"evento":"cierre","activo":pair,"resultado":"TP","precio_cierre":cur,
                                             "timestamp":nowiso(),
                                             "comentario":f"TP tocado (S). Entrada {entry}, SL {sl}, TP {tp}"})
                if tr.get("open"): still_open.append(tr)

            st["trades"] = still_open
            safe_save_json(STATE_PATH, state)

    return new_payloads or None

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
    headlines = (coindesk_headlines(3) + theblock_headlines(2) + ft_headlines(2))[:5]

    total = len(performance.get("trades", []))
    wins = performance.get("wins", 0); losses = performance.get("losses", 0)
    winrate = (wins / total * 100) if total else 0
    rentabilidad = ((wins - losses) / total * 100) if total else 0

    metrics = (f"📊 Métricas del sistema\n"
               f"Rentabilidad histórica: {rentabilidad:+.2f}%\n"
               f"Aciertos: {winrate:.1f}% ({wins}W/{losses}L)\n"
               f"Operaciones totales: {total}")
    comentario = metrics if now_local().strftime("%H:%M") == "09:00" else "Informe de mercado (Binance + RSS)."

    return {"evento":"informe","tipo":"miniresumen_12h","timestamp":nowiso(),
            "precios":lines,"sentimiento":fg_line,"titulares":headlines,"comentario":comentario}

def report_payload_open_positions():
    open_lines = []
    today_signals = {"abiertas": [], "cerradas": []}
    today_date = now_local().date()

    for t in performance.get("trades", []):
        ts = datetime.fromisoformat(t["ts"]).date() if "ts" in t else None
        if ts == today_date:
            if t.get("result") in ("TP","SL"):
                today_signals["cerradas"].append(f"{t['sym']} {t['result']}")
            else:
                today_signals["abiertas"].append(t["sym"])

    wins_today = sum(1 for t in performance.get("trades", [])
                     if "ts" in t and datetime.fromisoformat(t["ts"]).date()==today_date and t.get("result")=="TP")
    losses_today = sum(1 for t in performance.get("trades", [])
                       if "ts" in t and datetime.fromisoformat(t["ts"]).date()==today_date and t.get("result")=="SL")
    total_today = wins_today + losses_today
    rent_today = ((wins_today - losses_today) / total_today * 100) if total_today else 0

    for sym, st in state.items():
        for tr in st["trades"]:
            if tr.get("open"):
                open_lines.append(f"{sym_to_pair(sym)} {tr['dir']} @ {tr['entry']} (SL {tr['sl']}, TP {tr['tp']})")
    if not open_lines: open_lines = ["Sin operaciones abiertas actualmente."]

    comentario = (f"📊 Resumen diario de operaciones\n\n"
                  f"🟢 Aperturas del día: {', '.join(today_signals['abiertas']) or 'Ninguna'}\n"
                  f"🔴 Cierres del día: {', '.join(today_signals['cerradas']) or 'Ninguno'}\n"
                  f"💰 Rentabilidad del día: {rent_today:+.2f}%")

    return {"evento":"informe","tipo":"resumen_operaciones","timestamp":nowiso(),
            "precios":open_lines,"sentimiento":"", "titulares":[], "comentario":comentario}

# ==========================
#  ENVÍO A MAKE (con reintentos)
# ==========================
def send_to_make(payload, desc=""):
    max_tries = 3
    for i in range(1, max_tries+1):
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=12)
            if 200 <= r.status_code < 300:
                tag = desc or f"{payload.get('evento','?')} {payload.get('tipo', payload.get('resultado',''))}"
                print(f"📤 Enviado a Make ({tag}) ✓")
                return True
            else:
                body = (r.text or "")[:160].replace("\n"," ")
                print(f"⚠️ HTTP {r.status_code} enviando a Make: {body} [intento {i}/{max_tries}]")
        except Exception as e:
            print(f"❌ Error enviando a Make ({desc or payload.get('evento','?')}): {e} [intento {i}/{max_tries}]")
        time.sleep(1.5 * i)
    return False

# ==========================
#  GOOGLE DRIVE (SDK opcional en Render)
# ==========================
def get_drive_service():
    try:
        if not os.path.exists(TOKEN_FILE):
            print("⚠️ Sin token.pkl → Render en modo sin Drive.")
            return None
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, ["https://www.googleapis.com/auth/drive.file"])
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"❌ Error autenticando con Google Drive: {e}")
        return None

def backup_all():
    """Sube state/performance/params a Drive si hay token local (uso típico: script manual en tu PC)."""
    try:
        ensure_local_files()
        service = get_drive_service()
        if not service:
            print("⚠️ Backup omitido (sin token Drive).")
            return
        from googleapiclient.http import MediaFileUpload
        ts = nowiso().replace(":", "-")
        for path in [STATE_PATH, PERF_PATH, PARAMS_PATH]:
            if not os.path.exists(path): 
                print(f"⚠️ No existe {path}, se omite.")
                continue
            meta = {"name": f"{os.path.basename(path).replace('.json','')}_{ts}.json", "parents":[DRIVE_FOLDER_ID]}
            media = MediaFileUpload(path, mimetype="application/json")
            service.files().create(body=meta, media_body=media, fields="id").execute()
            print(f"☁️ Subido a Drive → {meta['name']}")
        print("✅ Backup completado.")
    except Exception as e:
        print(f"❌ Error backup_all: {e}")

def restore_last_backup():
    """Restaura el último backup desde Drive (si hay token)."""
    try:
        service = get_drive_service()
        if not service:
            print("⚠️ Sin Drive (token), no se restaura.")
            return False
        files = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/json'",
            orderBy="modifiedTime desc", pageSize=9,
            fields="files(id,name,modifiedTime)"
        ).execute().get("files", [])
        if not files:
            print("⚠️ No se encontraron backups en Drive."); return False
        restored = 0
        for f in files:
            base = f["name"].split("_")[0]
            if base in ["state","performance","params"]:
                req = service.files().get_media(fileId=f["id"])
                with open(os.path.join(BASE_DIR, f"{base}.json"), "wb") as fh:
                    fh.write(req.execute())
                print(f"✅ Restaurado → {base}.json")
                restored += 1
        # recargar a memoria
        global state, performance, params
        state = safe_load_json(STATE_PATH, state)
        performance = safe_load_json(PERF_PATH, performance)
        params = safe_load_json(PARAMS_PATH, params)
        return restored > 0
    except Exception as e:
        print(f"❌ Error restore_last_backup: {e}")
        return False

# ==========================
#  LOOPS PRINCIPALES
# ==========================
def report_loop():
    print("🕓 report loop → 09:00/21:00 (mercado), 22:00 (posiciones) hora España")
    last_report_min = None; last_open_min = None; last_heartbeat = None; last_state_log=None
    while True:
        try:
            now_loc = now_local(); hhmm = now_loc.strftime("%H:%M")
            # Heartbeat cada 1 min (para mantener activo Render Free con UptimeRobot)
            if (last_heartbeat != hhmm):
                try:
                    requests.get("https://agente-cripto-ai.onrender.com/", timeout=6)
                except: pass
                print(f"💓 Heartbeat {nowiso()}"); last_heartbeat = hhmm

            # Log de estado cada hora
            if now_loc.minute == 0 and (not last_state_log or now_loc.hour != last_state_log.hour):
                print("📊 Estado de operaciones abiertas:")
                for sym, st in state.items():
                    if not st["trades"]: print(f" - {sym}: sin operaciones abiertas")
                    else:
                        for tr in st["trades"]:
                            status = "abierta" if tr.get("open") else "cerrada"
                            print(f" - {sym} {tr['dir']} @ {tr['entry']} → {status} (SL {tr['sl']}, TP {tr['tp']})")
                last_state_log = now_loc

            # Informes mercado
            if hhmm in REPORT_TIMES_LOCAL and last_report_min != hhmm:
                send_to_make(report_payload_market(), desc=f"informe {hhmm}")
                print(f"📤 Informe 12h procesado ({hhmm} local).")
                auto_tune()
                last_report_min = hhmm

            # Informe posiciones abiertas
            if hhmm == OPEN_REPORT_LOCAL and last_open_min != hhmm:
                send_to_make(report_payload_open_positions(), desc="resumen posiciones")
                print(f"📤 Informe posiciones procesado ({hhmm} local).")
                last_open_min = hhmm

        except Exception as e:
            print("report error:", e)
        time.sleep(15)

def scan_loop():
    print(f"🌀 scan loop: {LOOP_SECONDS}s | 1 símbolo/iteración")
    idx = 0
    while True:
        try:
            sym = SYMBOLS[idx % len(SYMBOLS)]
            print(f"🔍 Escaneando {sym} ...")
            payloads = evaluate_symbol(sym)
            if payloads:
                for pld in payloads:
                    tag = f"{pld['evento']} → {pld.get('tipo', pld.get('resultado',''))} {pld.get('activo','')}"
                    print(f"📈 {tag}")
                    send_to_make(pld, desc=tag)
            print(f"✅ Escaneo {sym} OK. Esperando {LOOP_SECONDS}s...\n")
            idx += 1
        except Exception as e:
            print("scan error:", e)
        time.sleep(LOOP_SECONDS + uniform(0.5, 1.5))

# ==========================
#  FLASK APP / ENDPOINTS
# ==========================
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({
        "status": "ok", "time": nowiso(), "symbols": SYMBOLS,
        "params": params, "open_state": state,
        "perf": {"wins": performance["wins"], "losses": performance["losses"], "n": len(performance["trades"])}
    })

@app.post("/force-backup")
def force_backup():
    """Devuelve los archivos locales actuales para tu script manual."""
    try:
        ensure_local_files()
        backup_all()  # si hay token, sube a Drive; si no, sigue
        archivos = []
        for path in [STATE_PATH, PERF_PATH, PARAMS_PATH]:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    archivos.append({"file_name": os.path.basename(path), "contenido": f.read()})
            else:
                print(f"⚠️ {path} no existe (omitido en respuesta).")
        print("📤 Force-backup OK: archivos devueltos al cliente.")
        return jsonify({"archivos": archivos, "timestamp": nowiso(), "status": "ok"}), 200
    except Exception as e:
        print(f"❌ Error en force-backup: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/restore-state")
def restore_state():
    """
    Permite restaurar archivos individuales (state.json, performance.json, params.json)
    enviados desde el script de restauración manual.
    """
    try:
        data = request.get_json(force=True)
        file_name = data.get("file_name")
        contenido = data.get("contenido")

        if not file_name or not contenido:
            return jsonify({"ok": False, "error": "Faltan campos en el payload"}), 400

        # Guardar el archivo en /tmp o raíz
        save_path = os.path.join("/tmp", file_name)
        with open(save_path, "w", encoding="utf-8") as f:
            f.write(contenido)

        print(f"✅ {file_name} restaurado manualmente desde cliente remoto → {save_path}")
        return jsonify({"ok": True, "msg": f"{file_name} restaurado correctamente"}), 200

    except Exception as e:
        print(f"❌ Error al restaurar archivo recibido: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================
#  MAIN (Web Service)
# ==========================
if __name__ == "__main__":
    print("🚀 Iniciando Agente Cripto AI (Web Service) con señales, informes, backups y autoaprendizaje…")
    ensure_local_files()
    restored = restore_last_backup()
    if restored: print("♻️ Backup restaurado desde Drive.")
    else:        print("⚠️ Sin restore desde Drive (token ausente o sin backups).")

    # Señal de despliegue (solo 1 vez)
    if SEND_TEST_ON_DEPLOY:
        try:
            requests.post(WEBHOOK_URL, json={
                "evento": "nueva_senal", "tipo": "Largo", "activo": "BTC/USD",
                "entrada": 123100, "sl": 119407, "tp": 130,
                "riesgo": params["RISK_PCT"], "timeframe": "H1",
                "timestamp": nowiso(), "comentario": "Prueba de despliegue (Render)."
            }, timeout=10)
        except Exception as e:
            print("⚠️ No se pudo enviar prueba de despliegue:", e)

    # Hilos
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=report_loop, daemon=True).start()

    # Flask
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
