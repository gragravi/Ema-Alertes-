"""
Bot d'alertes EMA (Deriv + Telegram) — V2 CORRIGÉE

Architecture V2 : chaque alerte est une combinaison indépendante
(1 symbole + 1 intervalle + 1 EMA). La configuration ne contient plus
qu'une liste d'alertes (voir config.json).

Le fichier est volontairement laissé en un seul script (pour rester
simple à déployer via GitHub Actions), mais le code est organisé en
modules logiques clairement séparés par des sections :

    1. CONFIGURATION GENERALE + LOGGING
    2. PERSISTANCE (config.json / state.json)
    3. MODULE TELEGRAM (API bas niveau + claviers à boutons)
    4. MODULE ALERTES (CRUD sur la liste d'alertes)
    5. MODULE COMMANDES TELEGRAM (messages + boutons -> actions)
    6. MODULE DERIV (récupération des bougies)
    7. MODULE ETAT (state.json)
    8. UTILITAIRES (EMA, formatage de date)
    9. MOTEUR DE VERIFICATION DES ALERTES
   10. BOUCLE PRINCIPALE

CORRECTIONS V2 :
- Logging structuré (logging module)
- Validation des secrets au démarrage
- Validation d'état au chargement
- Gestion améliorée des erreurs WebSocket
- Cleanup des IDs d'alerte obsolètes
- Gestion des symboles invalides
- Heartbeat configurable
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import websockets

# ===================================================================
# 1. CONFIGURATION GENERALE + LOGGING
# ===================================================================

# Configuration du logging structuré
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("ema_alert")

DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089")
DERIV_WS_URL = f"wss://ws.binaryws.com/websockets/v3?app_id={DERIV_APP_ID}"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Fuseau horaire utilisé pour afficher l'heure dans les alertes.
LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", "UTC")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

# Limite du nombre d'alertes pour éviter la surcharge
MAX_ALERTS = 50

# Symboles proposés dans le menu /new (label affiché -> code Deriv).
# Pour ajouter un symbole, ajoute simplement une ligne ici.
AVAILABLE_SYMBOLS = [
    ("EUR/USD", "frxEURUSD"),
    ("GBP/USD", "frxGBPUSD"),
    ("AUD/USD", "frxAUDUSD"),
    ("USD/JPY", "frxUSDJPY"),
    ("Or (XAU/USD)", "frxXAUUSD"),
    ("Argent (XAG/USD)", "frxXAGUSD"),
    ("BTC/USD", "cryBTCUSD"),
    ("ETH/USD", "cryETHUSD"),
    ("US Tech 100", "OTC_NDX"),
    ("Step Index", "STPINDEX"),
    ("Volatility 10 Index", "R_10"),
    ("Volatility 25 Index", "R_25"),
    ("Volatility 25 (1s) Index", "R_25_1S"),
    ("Volatility 50 Index", "R_50"),
    ("Volatility 75 Index", "R_75"),
    ("Boom 300", "BOOM300N"),
    ("Boom 500", "BOOM500"),
    ("Boom 1000", "BOOM1000"),
    ("Crash 300", "CRASH300N"),
    ("Crash 500", "CRASH500"),
    ("Crash 1000", "CRASH1000"),
]
SYMBOL_CODE_TO_LABEL = {code: label for label, code in AVAILABLE_SYMBOLS}

# Granularités proposées dans le menu /new (label -> secondes).
ALLOWED_GRANULARITIES = {
    "1min": 60,
    "5min": 300,
    "10min": 600,
    "15min": 900,
    "30min": 1800,
    "1h": 3600,
    "4h": 14400,
    "1j": 86400,
}
SECONDS_TO_LABEL = {v: k for k, v in ALLOWED_GRANULARITIES.items()}

# Périodes EMA proposées dans le menu /new.
AVAILABLE_EMA_PERIODS = [9, 20, 50, 100, 200]

HELP_TEXT = (
    "🤖 Gestionnaire d'alertes EMA\n\n"
    "Chaque alerte surveille UNE combinaison : un symbole, un intervalle "
    "et une EMA.\n\n"
    "📌 Alertes\n"
    "/new — créer une nouvelle alerte (par boutons)\n"
    "/alerts — lister toutes les alertes\n"
    "/pause ID — mettre une alerte en pause (ex: /pause 3)\n"
    "/resume ID — réactiver une alerte (ex: /resume 3)\n"
    "/delete ID — supprimer une alerte (confirmation demandée)\n"
    "/status — voir le statut du bot\n\n"
    "⚙️ Divers\n"
    "/help — afficher ce message"
)

# ===================================================================
# 2. PERSISTANCE (config.json / state.json)
# ===================================================================


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Fichier corrompu {path}, réinitialisation : {e}")
    return default


def save_json(path, data):
    dir_ = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def load_config():
    config = load_json(CONFIG_FILE, {})
    config.setdefault("alerts", [])
    config.setdefault("next_alert_id", 1)
    config.setdefault("last_update_id", 0)
    # "pending" mémorise où en est l'utilisateur dans le flux /new
    # (choix du symbole -> de l'intervalle -> de l'EMA), car chaque
    # appui sur un bouton arrive dans une exécution séparée du script.
    config.setdefault("pending", None)
    config.setdefault("last_heartbeat", 0)
    config.setdefault("heartbeat_enabled", True)
    config.setdefault("heartbeat_interval_hours", 24)
    return config


def save_config(config):
    save_json(CONFIG_FILE, config)


def load_state():
    """Charge l'état en validant que toutes les entrées sont des epochs valides."""
    raw_state = load_json(STATE_FILE, {})
    validated = {}
    for key, value in raw_state.items():
        if isinstance(key, str) and key.startswith("alert_") and isinstance(value, int):
            validated[key] = value
        else:
            logger.warning(f"État invalide ignoré : {key} = {value} (type: {type(value).__name__})")
    return validated


def save_state(state):
    save_json(STATE_FILE, state)


# ===================================================================
# 3. MODULE TELEGRAM (API bas niveau + claviers à boutons)
# ===================================================================


def send_telegram(text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_BOT_TOKEN/CHAT_ID manquant. Message non envoyé:")
        logger.warning(text)
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=payload, timeout=10)
        data = r.json()
        if not data.get("ok"):
            logger.error(f"Erreur d'envoi Telegram: {r.text}")
            return None
        return data["result"]
    except Exception as e:
        logger.error(f"Exception sendMessage: {e}")
        return None


def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(url, data=payload, timeout=10)
        if not r.json().get("ok"):
            logger.error(f"Erreur editMessageText: {r.text}")
    except Exception as e:
        logger.error(f"Exception editMessageText: {e}")


def answer_callback_query(callback_query_id, text=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        logger.error(f"Exception answerCallbackQuery: {e}")


def get_telegram_updates(offset):
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(
            url,
            params={
                "offset": offset,
                "timeout": 0,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            },
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            return data.get("result", [])
    except Exception as e:
        logger.error(f"Erreur getUpdates: {e}")
    return []


def chunk_buttons(buttons, per_row=2):
    """Découpe une liste de boutons inline en rangées de `per_row`."""
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def get_all_symbols(config):
    """Retourne la liste combinee des symboles par defaut + symboles custom."""
    custom = config.get("custom_symbols", [])
    return list(AVAILABLE_SYMBOLS) + [(s["label"], s["code"]) for s in custom]


def keyboard_symbols(config):
    custom = config.get("custom_symbols", [])
    buttons = [
        {"text": label, "callback_data": f"new:symbol:{code}"}
        for label, code in AVAILABLE_SYMBOLS
    ]
    for s in custom:
        buttons.append({"text": f"⭐ {s['label']}", "callback_data": f"new:symbol:{s['code']}"})
    rows = chunk_buttons(buttons, 2)
    rows.append([{"text": "➕ Ajouter un symbole", "callback_data": "new:add_custom"}])
    rows.append([{"text": "🗑 Gérer mes symboles", "callback_data": "new:manage_custom"}])
    rows.append([{"text": "❌ Annuler", "callback_data": "new:cancel"}])
    return {"inline_keyboard": rows}


def keyboard_delete_custom(config):
    """Clavier pour supprimer un symbole custom."""
    custom = config.get("custom_symbols", [])
    if not custom:
        return None
    buttons = [
        {"text": f"🗑 {s['label']}", "callback_data": f"new:del_custom:{s['code']}"}
        for s in custom
    ]
    rows = chunk_buttons(buttons, 1)
    rows.append([{"text": "◀️ Retour", "callback_data": "new:back_to_symbols"}])
    return {"inline_keyboard": rows}


def keyboard_granularities():
    buttons = [
        {"text": label, "callback_data": f"new:gran:{seconds}:{label}"}
        for label, seconds in ALLOWED_GRANULARITIES.items()
    ]
    rows = chunk_buttons(buttons, 3)
    rows.append([{"text": "❌ Annuler", "callback_data": "new:cancel"}])
    return {"inline_keyboard": rows}


def keyboard_emas():
    buttons = [
        {"text": f"EMA {p}", "callback_data": f"new:ema:{p}"}
        for p in AVAILABLE_EMA_PERIODS
    ]
    rows = chunk_buttons(buttons, 3)
    rows.append([{"text": "❌ Annuler", "callback_data": "new:cancel"}])
    return {"inline_keyboard": rows}


def keyboard_confirm_delete(alert_id):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Confirmer", "callback_data": f"del:confirm:{alert_id}"},
                {"text": "❌ Annuler", "callback_data": f"del:cancel:{alert_id}"},
            ]
        ]
    }


# ===================================================================
# 4. MODULE ALERTES (CRUD sur config["alerts"])
# ===================================================================


def find_alert(config, alert_id):
    for alert in config["alerts"]:
        if alert["id"] == alert_id:
            return alert
    return None


def alert_already_exists(config, symbol, granularity, ema):
    """Empêche de créer deux fois la même combinaison
    symbole + intervalle + EMA (même si l'une des deux est en pause)."""
    for alert in config["alerts"]:
        if (
            alert["symbol"] == symbol
            and alert["granularity"] == granularity
            and alert["ema"] == ema
        ):
            return True
    return False


def validate_symbol(symbol, config=None):
    """Valide que le symbole fait partie de AVAILABLE_SYMBOLS ou des custom_symbols.
    
    Args:
        symbol: Code Deriv du symbole
        config: config dict (pour verifier les custom_symbols aussi)
        
    Returns:
        symbol si valide
        
    Raises:
        ValueError si le symbole est invalide
    """
    valid_codes = [code for _, code in AVAILABLE_SYMBOLS]
    if config:
        valid_codes += [s["code"] for s in config.get("custom_symbols", [])]
    if symbol not in valid_codes:
        raise ValueError(f"Symbole invalide : {symbol}")
    return symbol


def create_alert(config, symbol, granularity, label, ema):
    """Crée une nouvelle alerte avec vérifications.
    
    Returns:
        alert dict si créée avec succès, None sinon
    """
    if len(config["alerts"]) >= MAX_ALERTS:
        logger.error(f"Limite atteinte : {MAX_ALERTS} alertes max")
        return None
    if alert_already_exists(config, symbol, granularity, ema):
        return None
    alert = {
        "id": config["next_alert_id"],
        "symbol": symbol,
        "granularity": granularity,
        "label": label,
        "ema": ema,
        "enabled": True,
    }
    config["alerts"].append(alert)
    config["next_alert_id"] += 1
    return alert


def delete_alert(config, alert_id):
    alert = find_alert(config, alert_id)
    if alert is None:
        return None
    config["alerts"] = [a for a in config["alerts"] if a["id"] != alert_id]
    return alert


def set_alert_enabled(config, alert_id, enabled):
    alert = find_alert(config, alert_id)
    if alert is None:
        return None
    alert["enabled"] = enabled
    return alert


def symbol_display_name(symbol):
    return SYMBOL_CODE_TO_LABEL.get(symbol, symbol)


def format_alert_line(alert):
    status = "✅" if alert.get("enabled", True) else "⏸"
    return (
        f"#{alert['id']} {symbol_display_name(alert['symbol'])} • "
        f"{alert['label']} • EMA{alert['ema']} {status}"
    )


def format_alerts_list(config):
    alerts = config["alerts"]
    if not alerts:
        return "Aucune alerte configurée. Utilise /new pour en créer une."
    alerts_sorted = sorted(alerts, key=lambda a: a["id"])
    return "📋 Alertes\n\n" + "\n".join(format_alert_line(a) for a in alerts_sorted)


# ===================================================================
# 5. MODULE COMMANDES TELEGRAM (messages + boutons -> actions)
# ===================================================================


def is_authorized_chat(chat_id):
    if not TELEGRAM_CHAT_ID:
        # Pas de chat_id configuré -> on ne peut authentifier personne,
        # donc on ignore toute commande par sécurité (au lieu de les
        # accepter de n'importe qui).
        logger.warning("TELEGRAM_CHAT_ID non configuré, commande ignorée par sécurité.")
        return False
    return str(chat_id) == str(TELEGRAM_CHAT_ID)


def start_new_alert_flow(config):
    """Lance le flux de création d'alerte avec un ID de session unique."""
    # ✅ Toujours réinitialiser le pending, même si une session était en cours
    config["pending"] = None
    save_config(config)  # Sauvegarde immédiate pour éviter un état bloqué

    session_id = str(int(time.time() * 1000))  # Timestamp en millisecondes (unique)
    config["pending"] = {"step": "symbol", "session_id": session_id}
    send_telegram(
        "➕ Nouvelle alerte\n\n1/3 — Choisis un symbole :",
        reply_markup=keyboard_symbols(config),
    )


def handle_text_command(config, text):
    parts = text.split()
    cmd = parts[0].lower()

    if cmd in ("/start", "/help"):
        send_telegram(HELP_TEXT)

    elif cmd == "/new":
        start_new_alert_flow(config)

    elif cmd == "/alerts":
        send_telegram(format_alerts_list(config))

    elif cmd == "/status":
        active_count = len([a for a in config["alerts"] if a.get("enabled", True)])
        total_count = len(config["alerts"])
        status_msg = (
            f"💚 Statut du bot\n\n"
            f"Alertes actives : {active_count}/{total_count}\n"
            f"Limite : {MAX_ALERTS}\n"
            f"Dernière mise à jour : {datetime.now(timezone.utc).strftime('%d/%m %H:%M:%S UTC')}"
        )
        send_telegram(status_msg)

    elif cmd == "/pause":
        if len(parts) < 2:
            send_telegram("❌ Précise l'ID de l'alerte. Exemple : /pause 3")
        else:
            try:
                alert_id = int(parts[1])
            except ValueError:
                send_telegram("❌ Format invalide. Exemple : /pause 3")
            else:
                alert = find_alert(config, alert_id)
                if alert is None:
                    send_telegram(f"❌ Alerte #{alert_id} introuvable.")
                elif not alert.get("enabled", True):
                    send_telegram(f"⏸ L'alerte #{alert_id} est déjà en pause.")
                else:
                    set_alert_enabled(config, alert_id, False)
                    send_telegram(f"⏸ Alerte #{alert_id} mise en pause.")

    elif cmd == "/resume":
        if len(parts) < 2:
            send_telegram("❌ Précise l'ID de l'alerte. Exemple : /resume 3")
        else:
            try:
                alert_id = int(parts[1])
            except ValueError:
                send_telegram("❌ Format invalide. Exemple : /resume 3")
            else:
                alert = find_alert(config, alert_id)
                if alert is None:
                    send_telegram(f"❌ Alerte #{alert_id} introuvable.")
                elif alert.get("enabled", True):
                    send_telegram(f"✅ L'alerte #{alert_id} est déjà active.")
                else:
                    set_alert_enabled(config, alert_id, True)
                    send_telegram(f"▶️ Alerte #{alert_id} réactivée.")

    elif cmd == "/delete":
        if len(parts) < 2:
            send_telegram("❌ Précise l'ID de l'alerte. Exemple : /delete 3")
        else:
            try:
                alert_id = int(parts[1])
            except ValueError:
                send_telegram("❌ Format invalide. Exemple : /delete 3")
            else:
                alert = find_alert(config, alert_id)
                if alert is None:
                    send_telegram(f"❌ Alerte #{alert_id} introuvable.")
                else:
                    send_telegram(
                        f"⚠️ Supprimer définitivement cette alerte ?\n\n{format_alert_line(alert)}",
                        reply_markup=keyboard_confirm_delete(alert_id),
                    )

    elif cmd == "/symboles" or cmd == "/symbols":
        custom = config.get("custom_symbols", [])
        if not custom:
            send_telegram("⭐ Aucun symbole personnalisé.\n\nUtilise /new puis '➕ Ajouter un symbole'.")
        else:
            lines = ["⭐ Tes symboles personnalisés :\n"]
            for s in custom:
                lines.append(f"• {s['label']} ({s['code']})")
            send_telegram("\n".join(lines))

    else:
        # Traitement du texte libre pour l'ajout de symbole custom
        pending = config.get("pending") or {}
        if pending.get("step") == "custom_label":
            # Format attendu : "Nom | CODE"
            if "|" not in text:
                send_telegram(
                    "❌ Format incorrect.\n\n"
                    "Envoie le nom ET le code séparés par |\n"
                    "Exemple : USD/CHF | frxUSDCHF"
                )
                return
            parts_custom = text.split("|", 1)
            custom_label = parts_custom[0].strip()
            custom_code = parts_custom[1].strip()
            if not custom_label or not custom_code:
                send_telegram("❌ Nom ou code vide. Réessaie.")
                return
            # Vérifier que le code n'existe pas déjà
            all_codes = [c for _, c in AVAILABLE_SYMBOLS] + [s["code"] for s in config.get("custom_symbols", [])]
            if custom_code in all_codes:
                send_telegram(f"⚠️ Le symbole {custom_code} existe déjà dans la liste.")
                config["pending"] = None
                return
            # Validation live via Deriv (tentative de récupération de 10 bougies)
            send_telegram(f"⏳ Vérification de {custom_code} sur Deriv...")
            try:
                import asyncio as _asyncio
                test_candles = _asyncio.get_event_loop().run_until_complete(
                    fetch_candles(custom_code, 3600, 10)
                )
                if not test_candles:
                    raise RuntimeError("Aucune bougie retournée")
            except Exception as e:
                send_telegram(
                    f"❌ Code Deriv invalide : {custom_code}\n\n"
                    f"Vérifie le code exact sur la documentation Deriv et réessaie."
                )
                config["pending"] = None
                return
            # Sauvegarde du symbole custom
            if "custom_symbols" not in config:
                config["custom_symbols"] = []
            config["custom_symbols"].append({"label": custom_label, "code": custom_code})
            config["pending"] = None
            send_telegram(
                f"✅ Symbole ajouté : {custom_label} ({custom_code})\n\n"
                f"Il apparaît maintenant avec ⭐ dans le menu /new."
            )
        else:
            send_telegram("Commande non reconnue.\n\n" + HELP_TEXT)


def handle_new_alert_callback(config, chat_id, message_id, data_parts):
    pending = config.get("pending") or {}
    action = data_parts[1] if len(data_parts) > 1 else ""

    if action == "cancel":
        config["pending"] = None
        edit_telegram_message(chat_id, message_id, "❌ Création d'alerte annulée.")
        return

    if action == "add_custom":
        config["pending"]["step"] = "custom_label"
        edit_telegram_message(
            chat_id, message_id,
            "➕ Nouveau symbole\n\n"
            "Envoie le nom ET le code Deriv séparés par |\n\n"
            "Exemple : USD/CHF | frxUSDCHF\n"
            "Exemple : Volatility 100 | R_100"
        )
        return

    if action == "manage_custom":
        custom = config.get("custom_symbols", [])
        if not custom:
            edit_telegram_message(chat_id, message_id,
                "⭐ Aucun symbole personnalisé ajouté.\n\nUtilise '➕ Ajouter un symbole' pour en ajouter.")
        else:
            edit_telegram_message(
                chat_id, message_id,
                "🗑 Quel symbole veux-tu supprimer ?",
                reply_markup=keyboard_delete_custom(config)
            )
        return

    if action == "del_custom" and len(data_parts) >= 3:
        code_to_del = data_parts[2]
        custom = config.get("custom_symbols", [])
        found = next((s for s in custom if s["code"] == code_to_del), None)
        if found:
            config["custom_symbols"] = [s for s in custom if s["code"] != code_to_del]
            edit_telegram_message(chat_id, message_id,
                f"✅ Symbole '{found['label']}' supprimé.\n\n"
                "Tu peux relancer /new pour continuer.")
        else:
            edit_telegram_message(chat_id, message_id, "❌ Symbole introuvable.")
        config["pending"] = None
        return

    if action == "back_to_symbols":
        config["pending"]["step"] = "symbol"
        edit_telegram_message(
            chat_id, message_id,
            "➕ Nouvelle alerte\n\n1/3 — Choisis un symbole :",
            reply_markup=keyboard_symbols(config)
        )
        return

    if action == "symbol" and len(data_parts) >= 3:
        if pending.get("step") not in (None, "symbol"):
            edit_telegram_message(chat_id, message_id, "⚠️ Session expirée, relance /new.")
            config["pending"] = None
            return
        
        # ✅ Validation du symbole (inclut les custom)
        try:
            symbol = validate_symbol(data_parts[2], config)
        except ValueError as e:
            logger.warning(f"Validation symbole échouée: {e}")
            edit_telegram_message(chat_id, message_id, "❌ Symbole invalide, relance /new.")
            config["pending"] = None
            return
        
        config["pending"] = {
            "step": "granularity",
            "symbol": symbol,
            "session_id": pending.get("session_id")
        }
        edit_telegram_message(
            chat_id,
            message_id,
            f"➕ Nouvelle alerte\n\n"
            f"Symbole : {symbol_display_name(symbol)} ✅\n\n"
            f"2/3 — Choisis l'intervalle :",
            reply_markup=keyboard_granularities(),
        )
        return

    if action == "gran" and len(data_parts) >= 4:
        if pending.get("step") != "granularity":
            edit_telegram_message(chat_id, message_id, "⚠️ Session expirée, relance /new.")
            config["pending"] = None
            return
        granularity = int(data_parts[2])
        label = data_parts[3]
        config["pending"] = {
            "step": "ema",
            "symbol": pending["symbol"],
            "granularity": granularity,
            "label": label,
            "session_id": pending.get("session_id")
        }
        edit_telegram_message(
            chat_id,
            message_id,
            f"➕ Nouvelle alerte\n\n"
            f"Symbole : {symbol_display_name(pending['symbol'])} ✅\n"
            f"Intervalle : {label} ✅\n\n"
            f"3/3 — Choisis l'EMA :",
            reply_markup=keyboard_emas(),
        )
        return

    if action == "ema" and len(data_parts) >= 3:
        if pending.get("step") != "ema":
            edit_telegram_message(chat_id, message_id, "⚠️ Session expirée, relance /new.")
            config["pending"] = None
            return
        ema = int(data_parts[2])
        symbol = pending["symbol"]
        granularity = pending["granularity"]
        label = pending["label"]
        config["pending"] = None

        if alert_already_exists(config, symbol, granularity, ema):
            edit_telegram_message(
                chat_id,
                message_id,
                "⚠️ Cette alerte existe déjà.\n\n"
                f"{symbol_display_name(symbol)} • {label} • EMA{ema}",
            )
            return

        alert = create_alert(config, symbol, granularity, label, ema)
        
        if alert is None:
            edit_telegram_message(
                chat_id,
                message_id,
                f"❌ Impossible de créer l'alerte.\n\nVérifie que tu n'as pas atteint la limite ({MAX_ALERTS} alertes max).",
            )
            return
        
        edit_telegram_message(
            chat_id,
            message_id,
            f"✅ Alerte créée !\n\n{format_alert_line(alert)}",
        )


def handle_delete_callback(config, chat_id, message_id, data_parts):
    if len(data_parts) < 3:
        return
    action = data_parts[1]
    try:
        alert_id = int(data_parts[2])
    except ValueError:
        return

    if action == "cancel":
        edit_telegram_message(chat_id, message_id, "Suppression annulée.")
        return

    if action == "confirm":
        alert = delete_alert(config, alert_id)
        if alert is None:
            edit_telegram_message(
                chat_id, message_id, f"❌ Alerte #{alert_id} introuvable (déjà supprimée ?)."
            )
        else:
            edit_telegram_message(
                chat_id, message_id, f"🗑️ Alerte supprimée :\n\n{format_alert_line(alert)}"
            )


def handle_callback_query(config, callback_query):
    callback_id = callback_query["id"]
    data = callback_query.get("data", "")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if chat_id is None or not is_authorized_chat(chat_id):
        answer_callback_query(callback_id)
        return

    answer_callback_query(callback_id)

    data_parts = data.split(":")
    namespace = data_parts[0] if data_parts else ""

    if namespace == "new":
        handle_new_alert_callback(config, chat_id, message_id, data_parts)
    elif namespace == "del":
        handle_delete_callback(config, chat_id, message_id, data_parts)


def process_updates(config):
    """Lit les messages et boutons Telegram reçus depuis la dernière fois
    et met à jour la liste d'alertes en conséquence."""
    updates = get_telegram_updates(config["last_update_id"] + 1)

    for update in updates:
        config["last_update_id"] = update["update_id"]

        if "callback_query" in update:
            handle_callback_query(config, update["callback_query"])
            continue

        message = update.get("message", {})
        text = (message.get("text") or "").strip()
        if not text:
            continue

        chat_id = message.get("chat", {}).get("id", "")
        if not is_authorized_chat(chat_id):
            continue

        handle_text_command(config, text)


# ===================================================================
# 6. MODULE DERIV (récupération des bougies)
# ===================================================================


def history_count_for(granularity):
    # Plus l historique est long, plus l EMA est precise et stable.
    # Deriv autorise jusqu a 5000 bougies par requete.
    if granularity >= 86400:   # 1j
        return 1000
    if granularity >= 14400:   # 4h
        return 2000
    if granularity >= 3600:    # 1h
        return 3000
    return 5000                # 1min, 5min, 15min, 30min -> max precision


async def fetch_candles(symbol, granularity, count):
    """Récupère les bougies de Deriv avec gestion robuste des erreurs.
    
    Args:
        symbol: Code Deriv du symbole
        granularity: Intervalle en secondes
        count: Nombre de bougies à récupérer
        
    Returns:
        Liste des bougies
        
    Raises:
        TimeoutError si aucune réponse après MAX_WAIT secondes
        RuntimeError si Deriv retourne une erreur
    """
    MAX_WAIT = 30
    try:
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
                raw = await asyncio.wait_for(ws.recv(), timeout=MAX_WAIT)
                data = json.loads(raw)
                if data.get("msg_type") == "candles":
                    return data["candles"]
                if data.get("msg_type") == "error":
                    raise RuntimeError(data["error"].get("message", "Erreur Deriv inconnue"))
    except asyncio.TimeoutError:
        raise TimeoutError(f"Pas de réponse 'candles' de Deriv après {MAX_WAIT}s")
    except Exception as e:
        logger.error(f"Erreur lors de la récupération des bougies : {e}")
        raise


# ===================================================================
# 7. MODULE ETAT (state.json — historique d'alertes déjà envoyées)
# ===================================================================


def state_key_for(alert_id):
    """Génère une clé d'état unique et vérifiable pour une alerte.
    
    Args:
        alert_id: ID numérique de l'alerte
        
    Returns:
        Clé formatée pour le state
        
    Raises:
        ValueError si alert_id est invalide
    """
    if not isinstance(alert_id, int) or alert_id < 1:
        raise ValueError(f"alert_id invalide : {alert_id}")
    return f"alert_{alert_id}"


def cleanup_old_alert_ids(config):
    """Réinitialise next_alert_id en fonction des IDs réellement utilisés.
    Évite que next_alert_id croisse indéfiniment lors de suppressions d'alertes."""
    if not config["alerts"]:
        config["next_alert_id"] = 1
        logger.info("Aucune alerte, reset next_alert_id à 1")
    else:
        max_id = max(a["id"] for a in config["alerts"])
        config["next_alert_id"] = max_id + 1
        logger.info(f"next_alert_id réajusté à {config['next_alert_id']}")


def prune_state(config, state):
    """Supprime du state les clés qui ne correspondent plus à une alerte
    existante (évite que state.json grossisse indéfiniment) et affiche les logs."""
    valid_keys = {state_key_for(a["id"]) for a in config["alerts"]}
    deleted_keys = [key for key in state.keys() if key not in valid_keys]
    
    for key in deleted_keys:
        state.pop(key, None)
        logger.info(f"État supprimé (alerte inexistante) : {key}")
    
    cleanup_old_alert_ids(config)


# ===================================================================
# 8. UTILITAIRES (EMA, formatage de date)
# ===================================================================


def compute_ema(closes, period):
    """Calcule l'EMA (Exponential Moving Average).
    
    Args:
        closes: Liste des prix de fermeture
        period: Période EMA
        
    Returns:
        Valeur EMA ou None si impossible à calculer
    """
    if len(closes) < period + 1:
        return None
    alpha = 2 / (period + 1)
    ema_val = sum(closes[:period]) / period
    for c in closes[period:]:
        ema_val = c * alpha + ema_val * (1 - alpha)
    return ema_val


def compute_pivot_points(high, low, close):
    """Calcule les Pivot Points classiques (R1/R2/R3, S1/S2/S3)."""
    pivot = (high + low + close) / 3
    r1 = 2 * pivot - low
    r2 = pivot + (high - low)
    r3 = high + 2 * (pivot - low)
    s1 = 2 * pivot - high
    s2 = pivot - (high - low)
    s3 = low - 2 * (high - pivot)
    return {"pivot": pivot, "r1": r1, "r2": r2, "r3": r3, "s1": s1, "s2": s2, "s3": s3}


def detect_sr_levels(candles, current_price, tolerance_pct=0.003, min_touches=2, max_levels=3, min_amplitude_pct=0.002):
    """Detecte les niveaux S/R sur les 300 dernieres bougies.
    
    Filtre les micro-oscillations via min_amplitude_pct :
    un sommet/creux n'est valide que si son ecart avec les voisins
    depasse ce seuil (evite les faux niveaux sur indices volatils).
    """
    # Limiter a 300 bougies (niveaux recents = pertinents pour day trading)
    candles = candles[-300:]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    min_amp = current_price * min_amplitude_pct

    candidates = []
    # Fenetre de 3 bougies de chaque cote pour etre plus selectif
    for i in range(3, len(candles) - 3):
        # Sommet local : high[i] superieur aux 3 voisins de chaque cote
        if (highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i-3]
                and highs[i] > highs[i+1] and highs[i] > highs[i+2] and highs[i] > highs[i+3]):
            # Amplitude minimum : ecart avec le plus bas voisin
            amplitude = highs[i] - min(lows[i-3:i+4])
            if amplitude >= min_amp:
                candidates.append(highs[i])
        # Creux local : low[i] inferieur aux 3 voisins de chaque cote
        if (lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i-3]
                and lows[i] < lows[i+1] and lows[i] < lows[i+2] and lows[i] < lows[i+3]):
            amplitude = max(highs[i-3:i+4]) - lows[i]
            if amplitude >= min_amp:
                candidates.append(lows[i])

    if not candidates:
        return [], []

    tolerance = current_price * tolerance_pct
    clusters = []
    for price in sorted(candidates):
        merged = False
        for cluster in clusters:
            if abs(price - cluster["center"]) <= tolerance:
                cluster["prices"].append(price)
                cluster["center"] = sum(cluster["prices"]) / len(cluster["prices"])
                merged = True
                break
        if not merged:
            clusters.append({"center": price, "prices": [price]})

    strong = [c for c in clusters if len(c["prices"]) >= min_touches]
    strong.sort(key=lambda c: len(c["prices"]), reverse=True)
    resistances = sorted([c for c in strong if c["center"] > current_price], key=lambda c: c["center"])[:max_levels]
    supports = sorted([c for c in strong if c["center"] < current_price], key=lambda c: c["center"], reverse=True)[:max_levels]
    return supports, resistances


def format_pivot_block(pp, label, dec=5):
    fmt = f"{{:.{dec}f}}"
    lines = [f"📌 PIVOT POINTS ({label}) :"]
    lines.append(f"🔴 R3 : {fmt.format(pp['r3'])}")
    lines.append(f"🔴 R2 : {fmt.format(pp['r2'])}")
    lines.append(f"🔴 R1 : {fmt.format(pp['r1'])}")
    lines.append(f"⚪ Pivot : {fmt.format(pp['pivot'])}")
    lines.append(f"🟢 S1 : {fmt.format(pp['s1'])}")
    lines.append(f"🟢 S2 : {fmt.format(pp['s2'])}")
    lines.append(f"🟢 S3 : {fmt.format(pp['s3'])}")
    return "\n".join(lines)


def format_sr_block(supports, resistances, dec=5):
    fmt = f"{{:.{dec}f}}"
    lines = ["📌 NIVEAUX CLÉS DÉTECTÉS :"]
    if not resistances and not supports:
        lines.append("  Aucun niveau significatif")
        return "\n".join(lines)
    for r in reversed(resistances):
        lines.append(f"🔴 Résistance : {fmt.format(r['center'])} (testé {len(r['prices'])}×)")
    for s in supports:
        lines.append(f"🟢 Support : {fmt.format(s['center'])} (testé {len(s['prices'])}×)")
    return "\n".join(lines)


def get_price_decimals(symbol):
    if symbol.startswith("cry") or symbol in ("BOOM1000","CRASH1000","BOOM500","CRASH500",
                                               "BOOM300N","CRASH300N","R_10","R_25","R_25_1S","R_50","R_75",
                                               "STPINDEX","OTC_NDX"):
        return 2
    return 5


def format_local_time(epoch_seconds):
    """Formate un timestamp en heure locale.
    
    Args:
        epoch_seconds: Timestamp Unix
        
    Returns:
        String au format "JJ/MM HH:MM"
        
    Raises:
        Exception si le fuseau horaire est invalide
    """
    dt_utc = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    try:
        local_dt = dt_utc.astimezone(ZoneInfo(LOCAL_TIMEZONE))
    except Exception as e:
        logger.error(f"❌ ERREUR : Fuseau '{LOCAL_TIMEZONE}' invalide. {e}")
        logger.error(f"ℹ️ Utilisez un fuseau IANA valide (ex: 'Europe/Paris', 'UTC')")
        raise
    return local_dt.strftime("%d/%m %H:%M")


# ===================================================================
# 9. MOTEUR DE VERIFICATION DES ALERTES
# ===================================================================


def group_enabled_alerts(config):
    """Regroupe les alertes actives par (symbole, granularité) afin de ne
    télécharger les bougies qu'une seule fois par groupe, même si
    plusieurs EMA différentes sont surveillées sur la même combinaison
    symbole + intervalle. Les bougies téléchargées une fois sont ensuite
    réutilisées pour calculer toutes les EMA du groupe."""
    groups = {}
    for alert in config["alerts"]:
        if not alert.get("enabled", True):
            continue
        key = (alert["symbol"], alert["granularity"])
        groups.setdefault(key, []).append(alert)
    return groups


MAX_RETRIES = 3


async def check_alert_group(symbol, granularity, label, alerts, state):
    """Vérifie un groupe d'alertes (même symbole + granularité).
    
    Gère les erreurs de connexion et les symboles invalides.
    """
    count = history_count_for(granularity)
    candles = None
    
    logger.info(f"Vérification: {symbol} ({label}) — {len(alerts)} alerte(s)")
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            candles = await fetch_candles(symbol, granularity, count)
            logger.info(f"✅ {len(candles)} bougies récupérées pour {symbol}")
            break
        except RuntimeError as e:
            # Erreur Deriv (symbole invalide, etc.)
            logger.error(f"❌ Erreur Deriv (symbole/API) : {symbol} — {e}")
            # ⚠️ Notifier l'utilisateur que ce symbole est en problème
            for alert in alerts:
                msg = f"⚠️ Alerte #{alert['id']} — Symbole {symbol} indisponible chez Deriv.\n\nVérifie config.json ou contacte le support Deriv."
                send_telegram(msg)
            return  # Arrêter là, ne pas continuer
        except Exception as e:
            err_text = str(e) or type(e).__name__
            logger.warning(f"Tentative {attempt}/{MAX_RETRIES} échouée : {symbol} — {err_text}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 ** attempt)
    
    if candles is None:
        logger.warning(f"Abandon après {MAX_RETRIES} tentatives : {symbol} ({label})")
        return

    max_period = max(a["ema"] for a in alerts)
    if len(candles) < max_period + 2:
        logger.warning(f"{symbol} ({label}) — Pas assez d'historique pour calculer les EMA (besoin: {max_period + 2}, reçu: {len(candles)}).")
        return

    closed_candles = candles[:-1]
    live_candle = candles[-1]  # bougie EN COURS (non fermée) — pour alerte quasi temps réel
    closes = [float(c["close"]) for c in closed_candles]

    epoch = int(live_candle["epoch"])
    high = float(live_candle["high"])
    low = float(live_candle["low"])

    try:
        time_str = format_local_time(epoch)
    except Exception as e:
        logger.warning(f"Erreur formatage heure : {e}, utilisation de l'epoch brut")
        time_str = str(epoch)

    # Pré-calcul EMA20 et EMA50 pour l'analyse de tendance
    ema20_trend = compute_ema(closes, 20)
    ema50_trend = compute_ema(closes, 50)
    ema20_prev  = compute_ema(closes[:-5], 20) if len(closes) > 25 else None
    ema50_prev  = compute_ema(closes[:-5], 50) if len(closes) > 55 else None
    close_price = float(live_candle["close"])  # prix actuel (bougie en cours)
    dec = get_price_decimals(symbol)

    # --- Pivot Points sur la derniere bougie fermee (meme intervalle) ---
    last_closed_candle = candles[-2]
    pp_same = compute_pivot_points(
        float(last_closed_candle["high"]),
        float(last_closed_candle["low"]),
        float(last_closed_candle["close"])
    )

    # --- Pivot Points journaliers : recuperer la bougie 1j la plus recente ---
    pp_daily = None
    try:
        daily_candles = await fetch_candles(symbol, 86400, 3)
        if daily_candles and len(daily_candles) >= 2:
            prev_day = daily_candles[-2]  # avant-dernier jour (ferme)
            pp_daily = compute_pivot_points(
                float(prev_day["high"]),
                float(prev_day["low"]),
                float(prev_day["close"])
            )
    except Exception as e:
        logger.warning(f"Impossible de recuperer les bougies journalieres pour les pivots : {e}")

    # --- Niveaux S/R detectes automatiquement ---
    # S/R sur les 300 dernieres bougies fermees (niveaux recents uniquement)
    closed_for_sr = candles[:-1]
    supports, resistances = detect_sr_levels(closed_for_sr, close_price)

    def build_trend_analysis(close, ema20, ema50, ema20_p, ema50_p):
        lines = []
        score = 0

        if ema20 is not None and ema20_p is not None:
            diff20 = ema20 - ema20_p
            if diff20 > 0:
                lines.append("• EMA20 direction    : ↗️ Monte")
                score += 1
            elif diff20 < 0:
                lines.append("• EMA20 direction    : ↘️ Descend")
            else:
                lines.append("• EMA20 direction    : ➡️ Plat")

        if ema50 is not None and ema50_p is not None:
            diff50 = ema50 - ema50_p
            if diff50 > 0:
                lines.append("• EMA50 direction    : ↗️ Monte")
                score += 1
            elif diff50 < 0:
                lines.append("• EMA50 direction    : ↘️ Descend")
            else:
                lines.append("• EMA50 direction    : ➡️ Plat")

        if ema20 is not None:
            if close > ema20:
                lines.append("• Prix vs EMA20      : ✅ Au-dessus")
                score += 1
            else:
                lines.append("• Prix vs EMA20      : ❌ En-dessous")

        if ema50 is not None:
            if close > ema50:
                lines.append("• Prix vs EMA50      : ✅ Au-dessus")
                score += 1
            else:
                lines.append("• Prix vs EMA50      : ❌ En-dessous")

        if ema20 is not None and ema50 is not None:
            if ema20 > ema50:
                lines.append("• EMA20 vs EMA50     : ✅ EMA20 > EMA50")
                score += 1
            else:
                lines.append("• EMA20 vs EMA50     : ❌ EMA20 < EMA50")

        if ema20 is not None and ema20_p is not None:
            slope = ema20 - ema20_p
            slope_str = f"+{slope:.5f}" if slope >= 0 else f"{slope:.5f}"
            if abs(slope) > abs(ema20) * 0.0005:
                direction = "↗️ Forte" if slope > 0 else "↘️ Forte"
            elif abs(slope) > abs(ema20) * 0.0001:
                direction = "↗️ Moyenne" if slope > 0 else "↘️ Moyenne"
            else:
                direction = "➡️ Faible"
            lines.append(f"• Pente EMA20        : {direction} ({slope_str})")

        total = 5
        if score >= 4:
            verdict = "📈 TENDANCE : HAUSSIÈRE FORTE"
        elif score == 3:
            verdict = "📈 TENDANCE : HAUSSIÈRE MOYENNE"
        elif score == 2:
            verdict = "📉 TENDANCE : BAISSIÈRE MOYENNE"
        else:
            verdict = "📉 TENDANCE : BAISSIÈRE FORTE"

        return "\n".join(lines), f"{verdict} ({score}/{total} critères)"

    # Une seule récupération de bougies pour ce groupe (symbole +
    # granularité), réutilisée ici pour calculer chaque EMA demandée.
    for alert in alerts:
        period = alert["ema"]
        ema_val = compute_ema(closes, period)
        if ema_val is None:
            logger.warning(f"Alerte #{alert['id']} : EMA{period} ne peut pas être calculée")
            continue

        touched = low <= ema_val <= high
        key = state_key_for(alert["id"])
        already_alerted_epoch = state.get(key)

        if touched and already_alerted_epoch != epoch:
            trend_details, trend_verdict = build_trend_analysis(
                close_price, ema20_trend, ema50_trend, ema20_prev, ema50_prev
            )
            # Blocs Pivot Points
            pp_same_block = format_pivot_block(pp_same, label, dec)
            pp_daily_block = format_pivot_block(pp_daily, "1j", dec) if pp_daily else ""
            # Bloc S/R détectés
            sr_block = format_sr_block(supports, resistances, dec)
            # Prix formaté selon le symbole
            fmt = f"{ema_val:.{dec}f}"
            msg_parts = [
                f"🔔 {symbol_display_name(symbol)} ({label}) - {time_str}",
                f"⚡ Prix a touché l'EMA{period} (~{fmt})",
                "",
                f"📊 ANALYSE DE TENDANCE :",
                trend_details,
                "",
                trend_verdict,
                "",
                pp_same_block,
            ]
            if pp_daily_block:
                msg_parts += ["", pp_daily_block]
            msg_parts += ["", sr_block, "", f"Alerte #{alert['id']}"]
            msg = "\n".join(msg_parts)
            logger.info(f"Envoi notification : {msg}")
            result = send_telegram(msg)

            # ✅ N'enregistrer l'état que si l'envoi Telegram a réussi
            if result is not None:
                state[key] = epoch
                logger.info(f"État enregistré pour alerte #{alert['id']} (epoch: {epoch})")
            else:
                logger.warning(f"Telegram échoué, état NON enregistré pour alerte #{alert['id']} (retry à la prochaine exécution)")


async def run_engine(config, state):
    """Lance le moteur de vérification de toutes les alertes actives."""
    groups = group_enabled_alerts(config)
    logger.info(f"Vérification de {len(groups)} groupe(s) d'alertes")
    for (symbol, granularity), alerts in groups.items():
        label = alerts[0]["label"]
        await check_alert_group(symbol, granularity, label, alerts, state)
        await asyncio.sleep(0.3)


def send_heartbeat(config):
    """Envoie un message de statut périodique (configurable)."""
    if not config.get("heartbeat_enabled", True):
        return
    
    now = time.time()
    last_heartbeat = config.get("last_heartbeat", 0)
    interval_seconds = config.get("heartbeat_interval_hours", 24) * 3600
    
    if now - last_heartbeat > interval_seconds:
        active_count = len([a for a in config["alerts"] if a.get("enabled", True)])
        total_count = len(config["alerts"])
        heartbeat_msg = (
            f"💚 Bot actif — Statut quotidien\n\n"
            f"Alertes actives : {active_count}/{total_count}\n"
            f"Limite : {MAX_ALERTS}\n"
            f"Dernière mise à jour : {datetime.now(timezone.utc).strftime('%d/%m %H:%M:%S UTC')}"
        )
        result = send_telegram(heartbeat_msg)
        if result is not None:
            config["last_heartbeat"] = int(now)
            logger.info(f"✅ Heartbeat envoyé")
        else:
            logger.warning(f"❌ Heartbeat échoué, retry plus tard")


# ===================================================================
# 10. BOUCLE PRINCIPALE
# ===================================================================


async def main():
    """Boucle principale du bot."""
    config = load_config()
    state = load_state()

    logger.info("=" * 60)
    logger.info("Démarrage du bot d'alertes EMA V2")
    logger.info("=" * 60)
    
    # ✅ Validation des secrets au démarrage
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ ERREUR : TELEGRAM_BOT_TOKEN non configuré.")
        logger.error("ℹ️ Ajoute ce secret dans Settings → Secrets and variables → Actions")
    
    if not TELEGRAM_CHAT_ID:
        logger.error("❌ ERREUR : TELEGRAM_CHAT_ID non configuré.")
        logger.error("ℹ️ Configure-le en suivant les étapes du README")
    
    logger.info(f"Config: {len(config['alerts'])} alerte(s) chargée(s)")
    logger.info(f"State: {len(state)} entrée(s) d'historique chargée(s)")
    
    process_updates(config)
    save_config(config)          # sauvegarde pending + alertes CRUD immédiatement
    prune_state(config, state)

    # ✅ Timeout global : 4 minutes (workflow s'exécute toutes les 5 min)
    try:
        logger.info("Lancement du moteur de vérification avec timeout de 240s")
        await asyncio.wait_for(run_engine(config, state), timeout=240)
    except asyncio.TimeoutError:
        logger.warning("⚠️ Moteur d'alertes dépassé (>240s), abandon.")
    except Exception as e:
        logger.error(f"❌ Erreur dans le moteur : {e}")

    # Envoyer un heartbeat optionnel
    send_heartbeat(config)
    
    save_state(state)
    save_config(config)
    
    logger.info("=" * 60)
    logger.info("Bot terminé")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
