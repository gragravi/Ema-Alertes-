import asyncio
import json
import os

import requests
import websockets

# ======================= CONFIGURATION =======================
# Modifie cette liste pour choisir tes marchés et intervalles.
# symbol : code Deriv (ex: "frxEURUSD", "frxXAUUSD"=or, "cryBTCUSD", "R_100"...)
# granularity : taille de la bougie en secondes
#   60=1min, 300=5min, 900=15min, 1800=30min, 3600=1h, 14400=4h, 86400=1jour
WATCHLIST = [
    {"symbol": "frxEURUSD", "granularity": 1800},
    {"symbol": "frxXAUUSD", "granularity": 1800},
    {"symbol": "cryBTCUSD", "granularity": 1800},
]

EMA_PERIODS = [20, 50, 200]
HISTORY_COUNT = 600  # nombre de bougies utilisées pour le calcul des EMA
# (marge large pour absorber les coupures de marche le week-end sur le forex)

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
DERIV_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# ===============================================================

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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


def compute_ema(closes, period):
    if len(closes) < period:
        return None
    alpha = 2 / (period + 1)
    ema_val = sum(closes[:period]) / period
    for c in closes[period:]:
        ema_val = c * alpha + ema_val * (1 - alpha)
    return ema_val


def granularity_label(seconds):
    if seconds % 86400 == 0 and seconds >= 86400:
        return f"{seconds // 86400}j"
    if seconds % 3600 == 0 and seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}min"


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
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            data = json.loads(raw)
            if data.get("msg_type") == "candles":
                return data["candles"]
            if data.get("msg_type") == "error":
                raise RuntimeError(data["error"].get("message", "Erreur Deriv"))


async def check_symbol(item, state):
    symbol = item["symbol"]
    granularity = item["granularity"]
    label = granularity_label(granularity)

    try:
        candles = await fetch_candles(symbol, granularity, HISTORY_COUNT)
    except Exception as e:
        err_text = str(e) or type(e).__name__
        print(f"[{symbol}] Erreur récupération des bougies: {err_text}")
        return

    if len(candles) < max(EMA_PERIODS) + 2:
        print(f"[{symbol}] Pas assez d'historique pour calculer les EMA.")
        return

    # On retire la bougie en cours de formation (la dernière), on ne garde
    # que les bougies déjà fermées pour un calcul fiable.
    closed_candles = candles[:-1]
    last_closed = closed_candles[-1]
    closes = [float(c["close"]) for c in closed_candles]

    epoch = int(last_closed["epoch"])
    high = float(last_closed["high"])
    low = float(last_closed["low"])

    for period in EMA_PERIODS:
        ema_val = compute_ema(closes, period)
        if ema_val is None:
            continue

        touched = low <= ema_val <= high
        state_key = f"{symbol}_{granularity}_{period}"
        already_alerted_epoch = state.get(state_key)

        if touched and already_alerted_epoch != epoch:
            msg = (
                f"🔔 {symbol} ({label})\n"
                f"Le prix a touché l'EMA {period} (~{ema_val:.5f})"
            )
            print(msg)
            send_telegram(msg)
            state[state_key] = epoch


async def main():
    state = load_state()
    for item in WATCHLIST:
        await check_symbol(item, state)
        await asyncio.sleep(0.5)
    save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
