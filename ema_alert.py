import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import websockets

# ======================= CONFIGURATION =======================
# Valeurs utilisees uniquement la toute premiere fois (avant toute
# commande Telegram). Ensuite, tout est piloté depuis config.json
# via les commandes du bot.
DEFAULT_WATCHLIST = ["frxXAUUSD", "OTC_NDX", "frxAUDUSD", "frxEURUSD", "cryBTCUSD"]
DEFAULT_EMA_PERIODS = [20, 50, 200]
DEFAULT_GRANULARITY_LABELS = ["15min", "30min", "1h", "4h", "1j"]

# Granularites autorisees (label humain -> secondes). Le bot ne permet
# d'activer que celles-ci, pour eviter des entrees invalides.
ALLOWED_GRANULARITIES = {
    "1min": 60,
    "5min": 300,
    "15min": 900,
    "30min": 1800,
    "1h": 3600,
    "4h": 14400,
    "1j": 86400,
}
SECONDS_TO_LABEL = {v: k for k, v in ALLOWED_GRANULARITIES.items()}

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
    "📌 Watchlist\n"
    "/list - symboles suivis\n"
    "/add SYMBOLE - ajouter un symbole (ex: /add frxGBPUSD)\n"
    "/remove SYMBOLE - retirer un symbole\n\n"
    "⏱ Granularités surveillées\n"
    "/granularites - voir les granularités actives\n"
    "/addgranularite LABEL - activer (ex: /addgranularite 1h)\n"
    "/removegranularite LABEL - désactiver\n"
    f"  Labels valides : {', '.join(ALLOWED_GRANULARITIES.keys())}\n\n"
    "📈 Périodes EMA\n"
    "/emas - voir les périodes EMA suivies\n"
    "/setemas 20 50 200 - remplacer la liste des périodes\n"
    "/addema PERIODE - ajouter une période (ex: /addema 100)\n"
    "/removeema PERIODE - retirer une période\n\n"
    "⚙️ Divers\n"
    "/config - voir toute la configuration actuelle\n"
    "/help - afficher ce message"
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

    # Ancien système (conservé temporairement)
    config.setdefault("symbols", list(DEFAULT_WATCHLIST))

    # Nouveau système
    config.setdefault("alerts", [])
    config.setdefault("next_alert_id", 1)

    config.setdefault("last_update_id", 0)

    return config


def save_config(config):
    save_json(CONFIG_FILE, config)


def load_state():
    return load_json(STATE_FILE, {})


def save_state(state):
    save_json(STATE_FILE, state)


def current_granularities(config):
    """Retourne la liste [(secondes, label), ...] actuellement active."""
    return [(ALLOWED_GRANULARITIES[lbl], lbl) for lbl in config["granularity_labels"]]


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


def format_config_summary(config):
    symbols = config["symbols"]
    granularities = config["granularity_labels"]
    emas = config["ema_periods"]
    return (
        "⚙️ Configuration actuelle\n\n"
        f"Symboles ({len(symbols)}) :\n"
        + ("\n".join(f"- {s}" for s in symbols) if symbols else "(aucun)")
        + "\n\nGranularités actives :\n"
        + (", ".join(granularities) if granularities else "(aucune)")
        + "\n\nPériodes EMA :\n"
        + (", ".join(str(p) for p in emas) if emas else "(aucune)")
    )


def process_commands(config):
    """Lit les messages Telegram recus depuis la derniere fois et met a jour
    toute la configuration (watchlist, granularites, periodes EMA) en
    fonction des commandes recues."""
    updates = get_telegram_updates(config["last_update_id"] + 1)

    for update in updates:
        config["last_update_id"] = update["update_id"]
        message = update.get("message", {})
        text = (message.get("text") or "").strip()
        if not text:
            continue

        chat_id = str(message.get("chat", {}).get("id", ""))
        if not TELEGRAM_CHAT_ID:
            # Pas de chat_id configuré -> on ne peut authentifier personne,
            # donc on ignore toute commande par securite (au lieu de les
            # accepter de n'importe qui).
            print("[Telegram] TELEGRAM_CHAT_ID non configuré, commande ignorée par sécurité.")
            continue
        if chat_id != str(TELEGRAM_CHAT_ID):
            continue  # on ignore les messages d'un autre chat

        parts = text.split()
        cmd = parts[0].lower()

        # ---------- Watchlist ----------
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

        # ---------- Granularités ----------
        elif cmd == "/granularites":
            labels = config["granularity_labels"]
            send_telegram(
                "Granularités actives :\n" + (", ".join(labels) if labels else "(aucune)")
            )

        elif cmd == "/addgranularite" and len(parts) >= 2:
            label = parts[1].strip().lower()
            if label not in ALLOWED_GRANULARITIES:
                send_telegram(
                    f"❌ Label inconnu '{label}'. Labels valides : "
                    + ", ".join(ALLOWED_GRANULARITIES.keys())
                )
            elif label in config["granularity_labels"]:
                send_telegram(f"{label} est déjà actif.")
            else:
                config["granularity_labels"].append(label)
                send_telegram(f"✅ Granularité {label} activée.")

        elif cmd == "/removegranularite" and len(parts) >= 2:
            label = parts[1].strip().lower()
            if label in config["granularity_labels"]:
                if len(config["granularity_labels"]) == 1:
                    send_telegram("❌ Impossible : il doit rester au moins une granularité active.")
                else:
                    config["granularity_labels"].remove(label)
                    send_telegram(f"🗑️ Granularité {label} désactivée.")
            else:
                send_telegram(f"{label} n'est pas active.")

        # ---------- Périodes EMA ----------
        elif cmd == "/emas":
            periods = config["ema_periods"]
            send_telegram(
                "Périodes EMA suivies :\n"
                + (", ".join(str(p) for p in periods) if periods else "(aucune)")
            )

        elif cmd == "/setemas" and len(parts) >= 2:
            try:
                new_periods = sorted({int(p) for p in parts[1:]})
                if any(p <= 1 for p in new_periods):
                    raise ValueError
            except ValueError:
                send_telegram("❌ Format invalide. Exemple : /setemas 20 50 200")
            else:
                config["ema_periods"] = new_periods
                send_telegram("✅ Périodes EMA mises à jour : " + ", ".join(str(p) for p in new_periods))

        elif cmd == "/addema" and len(parts) >= 2:
            try:
                period = int(parts[1])
                if period <= 1:
                    raise ValueError
            except ValueError:
                send_telegram("❌ Format invalide. Exemple : /addema 100")
            else:
                if period in config["ema_periods"]:
                    send_telegram(f"EMA {period} est déjà suivie.")
                else:
                    config["ema_periods"] = sorted(config["ema_periods"] + [period])
                    send_telegram(f"✅ EMA {period} ajoutée.")

        elif cmd == "/removeema" and len(parts) >= 2:
            try:
                period = int(parts[1])
            except ValueError:
                send_telegram("❌ Format invalide. Exemple : /removeema 200")
            else:
                if period in config["ema_periods"]:
                    if len(config["ema_periods"]) == 1:
                        send_telegram("❌ Impossible : il doit rester au moins une période EMA.")
                    else:
                        config["ema_periods"].remove(period)
                        send_telegram(f"🗑️ EMA {period} retirée.")
                else:
                    send_telegram(f"EMA {period} n'est pas suivie.")

        # ---------- Divers ----------
        elif cmd == "/config":
            send_telegram(format_config_summary(config))

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


async def check_symbol_granularity(symbol, granularity, label, ema_periods, state):
    count = history_count_for(granularity)
    try:
        candles = await fetch_candles(symbol, granularity, count)
    except Exception as e:
        err_text = str(e) or type(e).__name__
        print(f"[{symbol} {label}] Erreur récupération des bougies: {err_text}")
        return

    if len(candles) < max(ema_periods) + 2:
        print(f"[{symbol} {label}] Pas assez d'historique pour calculer les EMA.")
        return

    closed_candles = candles[:-1]
    last_closed = closed_candles[-1]
    closes = [float(c["close"]) for c in closed_candles]

    epoch = int(last_closed["epoch"])
    high = float(last_closed["high"])
    low = float(last_closed["low"])
    time_str = format_local_time(epoch)

    for period in ema_periods:
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


def prune_state(config, state):
    """Supprime du state les clés qui ne correspondent plus a un symbole,
    une granularite ou une periode EMA actuellement actifs (evite que
    state.json grossisse indefiniment)."""
    valid_granularities = {str(g) for g, _ in current_granularities(config)}
    valid_periods = {str(p) for p in config["ema_periods"]}
    valid_symbols = set(config["symbols"])
    for key in list(state.keys()):
        parts = key.split("_")
        if len(parts) < 3:
            state.pop(key, None)
            continue
        period = parts[-1]
        granularity = parts[-2]
        symbol = "_".join(parts[:-2])
        if (
            symbol not in valid_symbols
            or granularity not in valid_granularities
            or period not in valid_periods
        ):
            state.pop(key, None)


async def main():
    config = load_config()
    state = load_state()

    process_commands(config)
    prune_state(config, state)

    granularities = current_granularities(config)
    ema_periods = config["ema_periods"]

    for symbol in config["symbols"]:
        for granularity, label in granularities:
            await check_symbol_granularity(symbol, granularity, label, ema_periods, state)
            await asyncio.sleep(0.3)

    save_state(state)
    save_config(config)


if __name__ == "__main__":
    asyncio.run(main())
