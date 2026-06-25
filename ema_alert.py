import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import websockets

# ======================= CONFIGURATION =======================
# Watchlist par defaut utilisee uniquement la toute premiere fois
DEFAULT_WATCHLIST = ["frxXAUUSD", "OTC_NDX", "frxAUDUSD", "frxEURUSD", "cryBTCUSD"]

EMA_PERIODS = [20, 50, 200]

# Tous les intervalles verifies automatiquement, pour chaque symbole suivi
GRANULARITIES = [
    (900, "15min"),
    (1800, "30min"),
    (3600, "1h"),
    (14400, "4h"),
    (86400, "1j"),
]

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
DERIV_WS_URL = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Fuseau horaire utilise pour afficher l'heure dans les alertes
LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", "UTC")
# ===============================================================

BASE_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

HELP_TEXT = (
    "Commandes disponibles :\n"
    "/list - afficher les symboles suivis\n"
    "/add SYMBOLE - ajouter un symbole (ex: /add frxGBPUSD)\n"
    "/remove SYMBOLE - retirer un symbole\n"
    "/help - afficher ce message\n\n"
    "Tous les intervalles (1min, 5min, 15min, 30min, 1h, 4h, 1j) sont "
    "surveilles automatiquement pour chaque symbole de la liste."
)


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_config():
    config = load_json(CONFIG_FILE, {})
    config.setdefault("symbols", list(DEFAULT_WATCHLIST))
    config.setdefault("last_update_id", 0)
    return config


def save_config(config):
    save_json(CONFIG_FILE, config)


def load_state():
    return load_json(STATE_FILE, {})


def save_state(state):
    save_json(STATE_FILE, state)


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] TELEGRAM_BOT_TOKEN/CHAT_ID manquant. Message non envoyé:")
        print(message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        if r.status_code != 200:
            print("[Telegram] Erreur d'envoi:", r.text)
    except Exception as e:
        print("[Telegram] Exception:", e)


def get_telegram_updates(offset):
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=10)
        data = r.json()
        if data.get("ok"):
            return data.get("result", [])
    except Exception as e:
        print("[Telegram] Erreur getUpdates:", e)
    return []


def process_commands(config):
    """Lit les messages Telegram recus depuis la derniere fois et met a jour
    la watchlist en fonction des commandes /add, /remove, /list, /help."""
    updates = get_telegram_updates(config["last_update_id"] + 1)

    for update in updates:
        config["last_update_id"] = update["update_id"]
        message = update.get("message", {})
        text = (message.get("text") or "").strip()
        if not text:
            continue

        chat_id = str(message.get("chat", {}).get("id", ""))
        if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
            continue  # on ignore les messages d'un autre chat

        parts = text.split()
        cmd = parts[0].lower()

        if cmd in ("/start", "/help"):
            send_telegram(HELP_TEXT)

        elif cmd == "/list":
            symbols = config["symbols"]
            if symbols:
                send_telegram("Symboles suivis :\n" + "\n".join(f"- {s}" for s in symbols))
            else:
                send_telegram("Aucun symbole suivi actuellement.")

        elif cmd == "/add" and len(parts) >= 2:
            symbol = parts[1].strip()
            if symbol not in config["symbols"]:
                config["symbols"].append(symbol)
                send_telegram(f"✅ {symbol} ajouté à la watchlist.")
            else:
                send_telegram(f"{symbol} est déjà suivi.")

        elif cmd == "/remove" and len(parts) >= 2:
            symbol = parts[1].strip()
            if symbol in config["symbols"]:
                config["symbols"].remove(symbol)
                send_telegram(f"🗑️ {symbol} retiré de la watchlist.")
            else:
                send_telegram(f"{symbol} n'est pas dans la watchlist.")

        else:
            send_telegram("Commande non reconnue.\n\n" + HELP_TEXT)


def compute_ema(closes, period):
    if len(closes) < period:
        return None
    alpha = 2 / (period + 1)
    ema_val = sum(closes[:period]) / period
    for c in closes[period:]:
        ema_val = c * alpha + ema_val * (1 - alpha)
    return ema_val


def history_count_for(granularity):
    # Marge large pour absorber les coupures de marche le week-end (forex)
    if granularity >= 86400:
        return 400
    return 600


def format_local_time(epoch_seconds):
    dt_utc = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    try:
        local_dt = dt_utc.astimezone(ZoneInfo(LOCAL_TIMEZONE))
    except Exception:
        local_dt = dt_utc + timedelta(hours=2)
    return local_dt.strftime("%d/%m %H:%M")


async def fetch_candles(symbol, granularity, count):
    async with websockets.connect(DERIV_WS_URL, ping_interval=None) as ws:
        request = {
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "count": count,
            "end": "latest",
        }
        await ws.send(json.dumps(request))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            data = json.loads(raw)
            if data.get("msg_type") == "candles":
                return data["candles"]
            if data.get("msg_type") == "error":
                raise RuntimeError(data["error"].get("message", "Erreur Deriv"))


async def check_symbol_granularity(symbol, granularity, label, state):
    count = history_count_for(granularity)
    try:
        candles = await fetch_candles(symbol, granularity, count)
    except Exception as e:
        err_text = str(e) or type(e).__name__
        print(f"[{symbol} {label}] Erreur récupération des bougies: {err_text}")
        return

    if len(candles) < max(EMA_PERIODS) + 2:
        print(f"[{symbol} {label}] Pas assez d'historique pour calculer les EMA.")
        return

    closed_candles = candles[:-1]
    last_closed = closed_candles[-1]
    closes = [float(c["close"]) for c in closed_candles]

    epoch = int(last_closed["epoch"])
    high = float(last_closed["high"])
    low = float(last_closed["low"])
    time_str = format_local_time(epoch)

    for period in EMA_PERIODS:
        ema_val = compute_ema(closes, period)
        if ema_val is None:
            continue

        touched = low <= ema_val <= high
        state_key = f"{symbol}_{granularity}_{period}"
        already_alerted_epoch = state.get(state_key)

        if touched and already_alerted_epoch != epoch:
            msg = (
                f"🔔 {symbol} ({label}) - {time_str}\n"
                f"Le prix a touché l'EMA {period} (~{ema_val:.5f})"
            )
            print(msg)
            send_telegram(msg)
            state[state_key] = epoch


async def main():
    config = load_config()
    state = load_state()

    process_commands(config)

    for symbol in config["symbols"]:
        for granularity, label in GRANULARITIES:
            await check_symbol_granularity(symbol, granularity, label, state)
            await asyncio.sleep(0.3)

    save_state(state)
    save_config(config)


if __name__ == "__main__":
    asyncio.run(main())
