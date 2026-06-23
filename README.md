# Bot d'alerte EMA (Deriv + Telegram) — 100% gratuit, sans PC

Ce bot vérifie toutes les 5 minutes si le prix touche l'EMA 20, 50 ou 200
sur les marchés/intervalles que tu choisis, et t'envoie un message Telegram
quand c'est le cas. Il tourne gratuitement sur les serveurs de GitHub
(GitHub Actions) — pas besoin de PC, de serveur, ni d'app à compiler.

## Étape 1 — Créer le bot Telegram (5 min)

1. Ouvre Telegram, cherche **@BotFather**, démarre une conversation.
2. Envoie `/newbot`, donne un nom puis un identifiant (doit finir par "bot").
3. BotFather te donne un **token** du genre `123456789:AAExxxxxxxxxxxxxxx`.
   Garde-le, c'est ton `TELEGRAM_BOT_TOKEN`.
4. Cherche ton bot dans Telegram et envoie-lui n'importe quel message
   (ex: "salut") pour démarrer la conversation.
5. Dans ton navigateur, ouvre :
   `https://api.telegram.org/bot<TON_TOKEN>/getUpdates`
   (remplace `<TON_TOKEN>` par ton vrai token).
6. Tu verras un champ `"chat":{"id":123456789, ...}` — ce nombre est ton
   `TELEGRAM_CHAT_ID`.

## Étape 2 — Mettre le code sur GitHub (10 min)

1. Crée un compte sur [github.com](https://github.com) si tu n'en as pas.
2. Crée un **nouveau repository** (bouton "+" → "New repository"),
   nomme-le par exemple `ema-alert-bot`, mets-le en **Private**.
3. Dans le repo, utilise **"Add file" → "Upload files"** et envoie tous
   les fichiers de ce dossier :
   - `ema_alert.py`
   - `requirements.txt`
   - `state.json`
   - `.github/workflows/check.yml` (tape bien le chemin complet avec le
     dossier quand tu crées le fichier, si tu le crées à la main avec
     "Create new file" — GitHub crée les dossiers automatiquement)

## Étape 3 — Ajouter tes secrets Telegram (2 min)

1. Dans ton repo → **Settings** → **Secrets and variables** → **Actions**.
2. **New repository secret** → nom `TELEGRAM_BOT_TOKEN`, valeur = ton token.
3. Refais pareil pour `TELEGRAM_CHAT_ID`.

## Étape 4 — Autoriser le bot à sauvegarder son état (1 min)

1. Toujours dans **Settings** → **Actions** → **General**.
2. Descends jusqu'à **"Workflow permissions"**.
3. Sélectionne **"Read and write permissions"**, puis **Save**.

## Étape 5 — Activer et lancer

1. Va dans l'onglet **Actions** de ton repo.
2. Active les workflows si demandé.
3. Clique sur **"EMA Touch Alert"** → **"Run workflow"** pour le tester
   une première fois manuellement.
4. Si tout va bien, tu reçois un message Telegram dès qu'une EMA est
   touchée. Sinon, regarde les logs du workflow pour voir l'erreur.

Ensuite, le bot se relance automatiquement toutes les 5 minutes, 24h/24,
sans rien faire de plus.

## Personnaliser ta watchlist

Ouvre `ema_alert.py` sur GitHub (icône crayon pour éditer), modifie la
liste `WATCHLIST` en haut du fichier :

```python
WATCHLIST = [
    {"symbol": "frxEURUSD", "granularity": 1800},  # EUR/USD, bougies 30min
    {"symbol": "frxXAUUSD", "granularity": 3600},  # Or, bougies 1h
    {"symbol": "BTCUSD", "granularity": 900},      # Bitcoin, bougies 15min
]
```

Quelques codes Deriv utiles :
- Forex : `frxEURUSD`, `frxGBPUSD`, `frxXAUUSD` (or), `frxXAGUSD` (argent)
- Crypto : `BTCUSD`, `ETHUSD`
- Indices synthétiques : `R_10`, `R_25`, `R_50`, `R_75`, `R_100`

Granularités (en secondes) : `60`=1min, `300`=5min, `900`=15min,
`1800`=30min, `3600`=1h, `14400`=4h, `86400`=1jour.

## Limites à connaître

- GitHub Actions ne garantit pas une exécution à la seconde précise :
  le cron "toutes les 5 minutes" peut parfois être décalé de quelques
  minutes en période de forte charge. Très bien pour du 30min/1h/4h,
  moins précis pour du scalping en 1min.
- Le plan gratuit GitHub Actions inclut 2000 minutes/mois pour les repos
  privés — largement suffisant pour ce bot (quelques secondes par
  exécution, toutes les 5 min).
