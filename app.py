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

# Webhook Make (señales/informes) -> Telegram
WEBHOOK_URL = os.environ.get(
    "WEBHOOK_URL",
    "https://hook.eu2.make.com/rqycnm09n1dvatljeuyptxzsh2jhnx6t"
)

# Apps Script de Google (POST para guardar backups en Sheets)
BACKUP_POST_URL = os.environ.get(
    "BACKUP_POST_URL",
    "https://script.google.com/macros/s/AKfycbwRGci2SoE3hh4IXZRtQbqUtsyN3yqxcijgRkgKNg0bs8Mx9YiIqwfquiiCaxs8JDae/exec"
)

# Apps Script de Google (GET para restaurar backups desde Sheets)
BACKUP_RESTORE_URL = os.environ.get(
    "BACKUP_RESTORE_URL",
    "https://script.google.com/macros/s/AKfycbwRGci2SoE3hh4IXZRtQbqUtsyN3yqxcijgRkgKNg0bs8Mx9YiIqwfquiiCaxs8JDae/exec"
)

# Activos (actualizado): BTC, ETH, SOL, AVAX, BNB
SYMBOLS = os.environ.get("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,AVAXUSDT,BNBUSDT").split(",")

# Frecuencia de escaneo (segundos)
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))
SEND_TEST_ON_DEPLOY = os.environ.get("SEND_TEST_ON_DEPLOY", "true").lower() == "true"

# Informes y backups (hora local España)
REPORT_TIMES_LOCAL = {"09:00", "21:00"}
BACKUP_TIMES_LOCAL = {"09:05", "21:05"}
OPEN_REPORT_LOCAL = "22:00"  # opcional, se mantiene por compatibilidad

# Rutas de archivos locales
STATE_PATH  = "state.json"
PERF_PATH   = "performance.json"
CACHE_PATH  = "cache.json"
PARAMS_PATH = "params.json"

# Endpoints Binance
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
    # Fallback aproximado
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
    ts = e.get("ts")
    try:
        ts_dt = datetime.fromisoformat(ts)
    except Exception:
        return None
    now = datetime.now(ts_dt.tzinfo) if ts_dt.tzinfo else datetime.now()
    if (now - ts_dt).total_seconds() > max_age_sec:
        return None
    return e.get("data")

def set_cache(key, data):
    cache[key] = {"ts": nowiso(), "data": data}
    safe_save_json(CACHE_PATH, cache)

# ==========================
#  PARÁMETROS (con autoajuste)
# ==========================
# Defaults (H1, filtros y ATR-stops)
params = safe_load_json(PARAMS_PATH, {
    "SMA_FAST": 10,
    "SMA_SLOW": 60,
    "ATR_LEN": 14,
    "VOL_LEN": 24,
    "PULLBACK_ATR": 0.15,   # proximidad a SMA_fast medida en ATRs
    "SL_PCT": 0.03,         # compatibilidad si no se usan ATR-stops
    "TP_PCT": 0.06,         # compatibilidad si no se usan ATR-stops
    "RISK_PCT": 1.0,
    "USE_ATR_STOPS": True,
    "SL_ATR_MULT": 1.5,
    "TP_ATR_MULT": 3.0,
    "MIN_VOL_RATIO": 1.0,   # v_last >= MIN_VOL_RATIO * v_avg
    # Estructura preparada para params por símbolo (no activada aún)
    "PARAMS_BY_SYMBOL": {}
})

def auto_tune():
    """Ajuste simple según winrate de los últimos 30 trades (global)."""
    trades = performance.get("trades", [])
    if len(trades) < 30:
        return
    recent = trades[-30:]
    wins = sum(1 for t in recent if t.get("result") == "TP")
    losses = sum(1 for t in recent if t.get("result") == "SL")
    total = wins + losses
    if total == 0:
        return
    winrate = wins / total
    print(f"🤖 Auto-tuning: últimos {total} trades, winrate={winrate:.2%}")
    if winrate < 0.45:
        params["PULLBACK_ATR"] = max(params["PULLBACK_ATR"] * 0.9, 0.10)
        params["VOL_LEN"] = min(params["VOL_LEN"] + 2, 60)
    elif winrate > 0.65:
        params["RISK_PCT"] = min(params["RISK_PCT"] * 1.05, 3.0)
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
    """Último precio actual de Binance (ticker)."""
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
#  BACKUP AUTOMÁTICO A GOOGLE DRIVE (STATE + PERFORMANCE + PARAMS)
# ==========================
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials

# ID de la carpeta de Google Drive donde guardar los backups
DRIVE_FOLDER_ID = "19nbrjd5khqb9J7uJ7HXjJZ_6c8LslbZU"  # <-- reemplaza con el ID real de tu carpeta Drive

def get_drive_service():
    """Autentica con Google Drive usando el token local (token.pkl)."""
    try:
        creds = Credentials.from_authorized_user_file("token.pkl", ["https://www.googleapis.com/auth/drive.file"])
        service = build("drive", "v3", credentials=creds)
        return service
    except Exception as e:
        print(f"❌ Error autenticando con Google Drive: {e}")
        return None

def backup_all():
    """
    Sube state.json, performance.json y params.json a Google Drive.
    Crea copias independientes en la carpeta especificada.
    """
    try:
        service = get_drive_service()
        if not service:
            print("⚠️ No se pudo conectar con Google Drive.")
            return

        files_to_backup = [STATE_PATH, PERF_PATH, PARAMS_PATH]
        timestamp = nowiso().replace(":", "-")

        for path in files_to_backup:
            if not os.path.exists(path):
                print(f"⚠️ No existe {path}, se omite backup.")
                continue

            filename = f"{os.path.basename(path).replace('.json', '')}_{timestamp}.json"
            file_metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}
            media = MediaFileUpload(path, mimetype="application/json")

            service.files().create(body=file_metadata, media_body=media, fields="id").execute()
            print(f"☁️ Backup subido a Google Drive → {filename}")

    except Exception as e:
        print(f"❌ Error al realizar backup: {e}")

def restore_last_backup():
    """
    Restaura los archivos más recientes (state, performance, params)
    encontrados en la carpeta de Google Drive indicada.
    """
    try:
        service = get_drive_service()
        if not service:
            print("⚠️ No se pudo conectar con Google Drive para restaurar.")
            return False

        files = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/json'",
            orderBy="modifiedTime desc",
            pageSize=10,
            fields="files(id, name, modifiedTime)"
        ).execute().get("files", [])

        if not files:
            print("⚠️ No se encontraron backups en Google Drive.")
            return False

        restored = 0
        for f in files:
            name = f["name"]
            if any(name.startswith(x.replace(".json", "")) for x in [STATE_PATH, PERF_PATH, PARAMS_PATH]):
                request = service.files().get_media(fileId=f["id"])
                with open(name.split("_")[0] + ".json", "wb") as fh:
                    fh.write(request.execute())
                print(f"✅ Restaurado desde Drive → {name}")
                restored += 1

        if restored == 0:
            print("⚠️ Ningún archivo restaurado del backup remoto.")
            return False
        return True

    except Exception as e:
        print(f"❌ Error al restaurar desde Google Drive: {e}")
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
    performance["trades"] = performance["trades"][-300:]  # guarda últimos 300
    safe_save_json(PERF_PATH, performance)

# ==========================
#  LÓGICA DE SEÑALES
# ==========================
def evaluate_symbol(symbol):
    """Evalúa señales y gestiona cierres con control de duplicados y entradas múltiples (H1)."""
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
    vol_ok = v_last >= params.get("MIN_VOL_RATIO", 1.0) * v_avg
    pull_ok = abs(p - s_fast) <= _atr * params["PULLBACK_ATR"]

    st = state.setdefault(symbol, {"trades": []})
    new_payloads = []

    # ===== Anti-duplicado últimas 24h (timezone-safe) =====
    recent_trades = []
    for t in performance.get("trades", []):
        ts_str = t.get("ts")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            if ts > now_utc - timedelta(hours=24):
                recent_trades.append(t)
        except Exception as e:
            print("⚠️ Error parseando timestamp trade:", e)
            continue
    recent_symbols = {(t["sym"], t["dir"]) for t in recent_trades}

    # ===== Entradas =====
    if vol_ok and pull_ok:
        # LARGO
        if s_fast and s_slow and s_fast > s_slow:
            active_longs = [tr for tr in st["trades"] if tr.get("open") and tr["dir"] == "L"]
            same_entry = any(abs(tr["entry"] - p) / p < 0.01 for tr in active_longs)  # ±1%
            if not same_entry and (symbol, "L") not in recent_symbols:
                entry = round(p, 4)
                if params.get("USE_ATR_STOPS", False):
                    sl = round(entry - params["SL_ATR_MULT"] * _atr, 4)
                    tp = round(entry + params["TP_ATR_MULT"] * _atr, 4)
                else:
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
                    "comentario": "SMAfast>SMA slow + pullback (ATR) y volumen OK."
                })

        # CORTO
        if s_fast and s_slow and s_fast < s_slow:
            active_shorts = [tr for tr in st["trades"] if tr.get("open") and tr["dir"] == "S"]
            same_entry = any(abs(tr["entry"] - p) / p < 0.01 for tr in active_shorts)
            if not same_entry and (symbol, "S") not in recent_symbols:
                entry = round(p, 4)
                if params.get("USE_ATR_STOPS", False):
                    sl = round(entry + params["SL_ATR_MULT"] * _atr, 4)
                    tp = round(entry - params["TP_ATR_MULT"] * _atr, 4)
                else:
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
                    "comentario": "SMAfast<SMA slow + pullback (ATR) y volumen OK."
                })

    # ===== Gestión intrabar (TP / SL) =====
    if st["trades"]:
        cur = price_now(symbol)
        if cur is not None and cur > 0:
            still_open = []
            for tr in st["trades"]:
                if not tr.get("open"):
                    continue
                dir_ = tr["dir"]
                sl = float(tr["sl"])
                tp = float(tr["tp"])
                entry = float(tr["entry"])
                sym_pair = sym_to_pair(symbol)

                if dir_ == "L":
                    if cur <= sl:
                        print(f"💀 SL tocado (intra-vela) {sym_pair} → cerrando L @ {cur}")
                        tr["open"] = False
                        record_trade(symbol, "SL", "L")
                        new_payloads.append({
                            "evento": "cierre",
                            "activo": sym_pair,
                            "resultado": "SL",
                            "precio_cierre": cur,
                            "timestamp": nowiso(),
                            "comentario": f"Stop-loss alcanzado (Largo). Entrada {entry}, SL {sl}, TP {tp}"
                        })
                    elif cur >= tp:
                        print(f"🎯 TP tocado (intra-vela) {sym_pair} → cerrando L @ {cur}")
                        tr["open"] = False
                        record_trade(symbol, "TP", "L")
                        new_payloads.append({
                            "evento": "cierre",
                            "activo": sym_pair,
                            "resultado": "TP",
                            "precio_cierre": cur,
                            "timestamp": nowiso(),
                            "comentario": f"Take-profit alcanzado (Largo). Entrada {entry}, SL {sl}, TP {tp}"
                        })
                elif dir_ == "S":
                    if cur >= sl:
                        print(f"💀 SL tocado (intra-vela) {sym_pair} → cerrando S @ {cur}")
                        tr["open"] = False
                        record_trade(symbol, "SL", "S")
                        new_payloads.append({
                            "evento": "cierre",
                            "activo": sym_pair,
                            "resultado": "SL",
                            "precio_cierre": cur,
                            "timestamp": nowiso(),
                            "comentario": f"Stop-loss alcanzado (Corto). Entrada {entry}, SL {sl}, TP {tp}"
                        })
                    elif cur <= tp:
                        print(f"🎯 TP tocado (intra-vela) {sym_pair} → cerrando S @ {cur}")
                        tr["open"] = False
                        record_trade(symbol, "TP", "S")
                        new_payloads.append({
                            "evento": "cierre",
                            "activo": sym_pair,
                            "resultado": "TP",
                            "precio_cierre": cur,
                            "timestamp": nowiso(),
                            "comentario": f"Take-profit alcanzado (Corto). Entrada {entry}, SL {sl}, TP {tp}"
                        })
                if tr.get("open"):
                    still_open.append(tr)

            st["trades"] = still_open
            safe_save_json(STATE_PATH, state)
        else:
            print(f"⚠️ Precio actual no disponible para {symbol}, se omite gestión intrabar.")

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

    comentario = metrics if now_local().strftime("%H:%M") in {"09:00"} else "Informe de mercado (Binance + RSS)."

    return {
        "evento": "informe",
        "tipo": "miniresumen_12h",
        "timestamp": nowiso(),
        "precios": lines,
        "sentimiento": fg_line,
        "titulares": (headlines or [])[:5],
        "comentario": comentario
    }

def report_payload_open_positions():
    open_lines = []
    today_signals = {"abiertas": [], "cerradas": []}
    today_date = now_local().date()

    for t in performance.get("trades", []):
        ts = datetime.fromisoformat(t["ts"]).date() if "ts" in t else None
        if ts == today_date:
            if t.get("result") in ("TP", "SL"):
                today_signals["cerradas"].append(f"{t['sym']} {t['result']}")
            else:
                today_signals["abiertas"].append(t["sym"])

    wins_today = sum(1 for t in performance.get("trades", [])
                     if "ts" in t and datetime.fromisoformat(t["ts"]).date() == today_date and t.get("result") == "TP")
    losses_today = sum(1 for t in performance.get("trades", [])
                       if "ts" in t and datetime.fromisoformat(t["ts"]).date() == today_date and t.get("result") == "SL")
    total_today = wins_today + losses_today
    rent_today = ((wins_today - losses_today) / total_today * 100) if total_today > 0 else 0

    for sym, st in state.items():
        for tr in st["trades"]:
            if tr.get("open"):
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
#  ENVÍO A MAKE (helper con reintentos y logging)
# ==========================
def send_to_make(payload, desc=""):
    """
    Envía un payload al webhook de Make con:
    - reintentos exponenciales (hasta 2 reintentos),
    - logs claros de éxito / error,
    - truncado de respuesta para evitar logs enormes.
    """
    max_tries = 3
    for i in range(1, max_tries + 1):
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=12)
            if 200 <= r.status_code < 300:
                tag = desc or f"{payload.get('evento','?')} {payload.get('tipo', payload.get('resultado',''))}"
                print(f"📤 Enviado a Make ({tag}) ✓")
                return True
            else:
                body = (r.text or "")[:160].replace("\n", " ")
                print(f"⚠️ HTTP {r.status_code} enviando a Make: {body} [intento {i}/{max_tries}]")
        except Exception as e:
            print(f"❌ Error enviando a Make ({desc or payload.get('evento','?')}): {e} [intento {i}/{max_tries}]")
        time.sleep(1.5 * i)  # backoff sencillo
    return False

# ==========================
#  FLASK / ENDPOINTS
# ==========================
app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "time": nowiso(),
        "symbols": SYMBOLS,
        "params": params,
        "open_state": state,
        "perf": {
            "wins": performance.get("wins", 0),
            "losses": performance.get("losses", 0),
            "n": len(performance.get("trades", []))
        }
    })

@app.post("/force-backup")
def force_backup():
    try:
        backup_all()
        return jsonify({"ok": True, "msg": "Backup ejecutado correctamente"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ==========================
#  BUCLES PRINCIPALES
# ==========================
def report_loop():
    print("🕓 report loop activo → 09:00 & 21:00 (mercado) | backups 09:05 & 21:05 (hora Madrid)")
    last_report_min = None
    last_open_min = None
    last_backup_min = None
    last_state_log = None
    last_heartbeat_min = None

    while True:
        try:
            now_loc = now_local()
            hhmm = now_loc.strftime("%H:%M")

            # Heartbeat + auto-ping cada minuto (mantiene despierto Render)
            if last_heartbeat_min != hhmm:
                print(f"💓 Heartbeat {nowiso()} → ping interno")
                try:
                    requests.get("http://localhost:" + os.environ.get("PORT", "10000"), timeout=3)
                except Exception:
                    pass
                last_heartbeat_min = hhmm

            # Log de operaciones abiertas cada hora
            if now_loc.minute == 0 and (not last_state_log or now_loc.hour != last_state_log.hour):
                print("📊 Estado de operaciones abiertas:")
                for sym, st in state.items():
                    if not st["trades"]:
                        print(f" - {sym}: sin operaciones abiertas")
                    else:
                        for tr in st["trades"]:
                            status = "abierta" if tr.get("open") else "cerrada"
                            print(f" - {sym} {tr['dir']} @ {tr['entry']} → {status} (SL {tr['sl']}, TP {tr['tp']})")
                last_state_log = now_loc

            # Informes mercado
            if hhmm in REPORT_TIMES_LOCAL and last_report_min != hhmm:
                payload = report_payload_market()
                send_to_make(payload, desc=f"informe {hhmm}")
                print(f"📤 Informe procesado ({hhmm} local).")
                auto_tune()
                last_report_min = hhmm

            # (Opcional) informe posiciones abiertas
            if OPEN_REPORT_LOCAL and hhmm == OPEN_REPORT_LOCAL and last_open_min != hhmm:
                payload = report_payload_open_positions()
                send_to_make(payload, desc="resumen posiciones")
                print(f"📤 Informe de posiciones abiertas procesado ({hhmm} local).")
                last_open_min = hhmm

            # Backups automáticos (directo a Sheets)
            if hhmm in BACKUP_TIMES_LOCAL and last_backup_min != hhmm:
                backup_all()
                last_backup_min = hhmm

        except Exception as e:
            print("report error:", e)
        time.sleep(15)

def scan_loop():
    print(f"🌀 scan loop: {LOOP_SECONDS}s | rotación 1 símbolo/iteración | activos={','.join(SYMBOLS)} | TF=H1")
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
            print(f"✅ Escaneo completado para {sym}. Esperando {LOOP_SECONDS}s...\n")
            idx += 1
        except Exception as e:
            print("scan error:", e)
        time.sleep(LOOP_SECONDS + uniform(0.5, 1.5))

# ==========================
#  MAIN (Web Service con Backup en Google Drive)
# ==========================
if __name__ == "__main__":
    print("🚀 Iniciando agente Cripto AI (Web Service) con métricas, backup en Google Drive y autoaprendizaje…")

    # Restaurar automáticamente el último backup desde Google Drive
    restored = restore_last_backup()
    if restored:
        print("✅ Backup restaurado correctamente desde Google Drive.")
    else:
        print("⚠️ No se pudo restaurar backup remoto, usando estado local.")

    # Envío de prueba (solo en despliegue)
    if SEND_TEST_ON_DEPLOY:
        try:
            requests.post(WEBHOOK_URL, json={
                "evento": "nueva_senal", "tipo": "Largo", "activo": "BTC/USD",
                "entrada": 123100, "sl": 119407, "tp": 130,  # valores dummy
                "riesgo": params["RISK_PCT"], "timeframe": "H1",
                "timestamp": nowiso(), "comentario": "Prueba de despliegue (Render Web Service)."
            }, timeout=10)
            print("📤 Señal de prueba enviada correctamente al canal.")
        except Exception as e:
            print("⚠️ No se pudo enviar prueba de despliegue:", e)

    # Lanzar hilos principales
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=report_loop, daemon=True).start()

    # Servidor Flask
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))


# ==========================
#  ENDPOINT MANUAL (opcional)
# ==========================
@app.post("/restore-state")
def restore_state():
    """
    Endpoint opcional para restaurar manualmente los backups desde Google Drive.
    Solo restaura state.json (útil para pruebas o mantenimiento).
    """
    try:
        service = get_drive_service()
        if not service:
            return jsonify({"ok": False, "error": "No se pudo conectar con Google Drive"}), 500

        files = service.files().list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and name contains 'state_'",
            orderBy="modifiedTime desc",
            pageSize=1,
            fields="files(id, name)"
        ).execute().get("files", [])

        if not files:
            return jsonify({"ok": False, "msg": "No se encontró ningún state.json en Drive"}), 404

        f = files[0]
        request = service.files().get_media(fileId=f["id"])
        with open("state.json", "wb") as fh:
            fh.write(request.execute())
        print(f"✅ state.json restaurado manualmente desde Drive → {f['name']}")
        return jsonify({"ok": True, "msg": f"state.json restaurado desde {f['name']}"}), 200

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

