# Bot d'alerte EMA (Deriv + Telegram) — V2, 100% gratuit, sans PC

Ce bot surveille des **alertes indépendantes**. Chaque alerte est une
combinaison unique :

- 1 symbole (ex: EUR/USD)
- 1 intervalle (ex: 15min)
- 1 EMA (ex: EMA 50)

Quand le prix touche l'EMA configurée sur cette combinaison, tu reçois un
message Telegram. Tout se configure avec des boutons, directement dans
Telegram. Le bot tourne gratuitement sur les serveurs de GitHub (GitHub
Actions) — pas besoin de PC, de serveur, ni d'app à compiler.

## Ce qui change par rapport à la V1

- Avant : une seule watchlist globale, surveillée sur toutes les
  combinaisons symbole × intervalle × EMA possibles.
- Maintenant : tu crées toi-même chaque alerte que tu veux surveiller,
  une par une, avec `/new`.
- Les anciennes commandes (`/add`, `/remove`, `/addgranularite`,
  `/removegranularite`, `/addema`, `/removeema`, `/setemas`,
  `/granularites`, `/emas`) n'existent plus : tout passe maintenant par
  les alertes.
- Si tu mets à jour un repo existant depuis la V1, tes anciens réglages
  (`symbols`, `granularity_labels`, `ema_periods`) ne sont plus utilisés.
  Recrée simplement les combinaisons qui t'intéressaient avec `/new`
  (ça prend 10 secondes par alerte).

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
   - `config.json`
   - `state.json`
   - `requirements.txt`
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
4. Envoie `/start` à ton bot sur Telegram, puis attends la prochaine
   exécution (ou relance le workflow manuellement) pour voir apparaître
   le menu d'aide.

Ensuite, le bot se relance automatiquement toutes les 5 minutes, 24h/24,
sans rien faire de plus.

## Utilisation — toutes les commandes

### Créer une alerte : `/new`

Le bot t'affiche des boutons en 3 étapes :

1. Choisis un symbole.
2. Choisis un intervalle (1min, 5min, 15min, 30min, 1h, 4h, 1j).
3. Choisis une EMA (9, 20, 50, 100, 200).

À la fin : `✅ Alerte créée !`

Si la combinaison existe déjà (même symbole + même intervalle + même
EMA), le bot répond `⚠️ Cette alerte existe déjà.` au lieu de créer un
doublon.

### Lister les alertes : `/alerts`

```
📋 Alertes

#1 EUR/USD • 15min • EMA20 ✅
#2 EUR/USD • 1h • EMA50 ✅
#3 Or (XAU/USD) • 5min • EMA200 ⏸
```

`✅` = active, `⏸` = en pause.

### Mettre en pause / réactiver : `/pause ID` et `/resume ID`

```
/pause 3
/resume 3
```

L'alerte reste enregistrée (avec son historique) mais n'est plus
vérifiée tant qu'elle est en pause.

### Supprimer une alerte : `/delete ID`

```
/delete 2
```

Le bot demande une confirmation avec deux boutons, pour éviter une
suppression accidentelle :

```
⚠️ Supprimer définitivement cette alerte ?

#2 EUR/USD • 1h • EMA50 ✅

[✅ Confirmer]   [❌ Annuler]
```

### Aide : `/help` ou `/start`

Affiche la liste des commandes disponibles.

## Personnaliser la liste des symboles proposés dans `/new`

Ouvre `ema_alert.py` sur GitHub (icône crayon pour éditer), modifie la
liste `AVAILABLE_SYMBOLS` en haut du fichier :

```python
AVAILABLE_SYMBOLS = [
    ("EUR/USD", "frxEURUSD"),
    ("Or (XAU/USD)", "frxXAUUSD"),
    ("BTC/USD", "cryBTCUSD"),
    # Ajoute tes propres lignes ici : ("Label affiché", "code_deriv")
]
```

Quelques codes Deriv utiles :
- Forex : `frxEURUSD`, `frxGBPUSD`, `frxXAUUSD` (or), `frxXAGUSD` (argent)
- Crypto : `cryBTCUSD`, `cryETHUSD`
- Indices synthétiques : `R_10`, `R_25`, `R_50`, `R_75`, `R_100`,
  `BOOM1000`, `CRASH1000`

## Comment fonctionne le moteur de vérification (sous le capot)

Pour chaque exécution :

1. Le bot lit les messages/boutons Telegram reçus depuis la dernière fois
   et met à jour la liste d'alertes en conséquence.
2. Il regroupe les alertes actives par **(symbole, intervalle)**. S'il y
   a plusieurs EMA différentes sur la même combinaison symbole +
   intervalle (ex: EUR/USD 15min EMA20 et EUR/USD 15min EMA50), les
   bougies ne sont téléchargées **qu'une seule fois** pour ce groupe,
   puis réutilisées pour calculer chaque EMA. Ça réduit fortement le
   nombre de requêtes envoyées à Deriv.
3. Pour chaque alerte, si le prix de la dernière bougie clôturée touche
   l'EMA et que cette bougie n'a pas déjà déclenché d'alerte, un message
   Telegram est envoyé.
4. L'historique de chaque alerte est sauvegardé dans `state.json` sous
   la clé `alert_<id>` (ex: `alert_1`, `alert_2`...), pour éviter les
   alertes en double sur la même bougie.

## Corrections et améliorations (V2)

### 🔴 Critiques
- **Race condition Git** : retry automatique avec rebase en cas de conflit (workflow)
- **WebSocket non fermée** : gestion d'erreurs avec `asyncio.wait_for()`
- **Doublons README** : suppression de sections redondantes

### ⚠️ Importants
- **Validation d'état** : chargement des epochs en validant le format
- **Validation des secrets** : messages d'erreur clairs au démarrage
- **Gestion des symboles invalides** : notification utilisateur si symbole indisponible
- **Logging structuré** : module `logging` Python standard avec timestamps

### 🟡 Améliorations
- **Cleanup IDs** : `next_alert_id` réajusté après suppression d'alertes
- **Heartbeat configurable** : intervalle et on/off paramétrables
- **Timeout plus robuste** : utilisation de `asyncio.wait_for()` global

## Limites à connaître

- GitHub Actions ne garantit pas une exécution à la seconde précise :
  le cron "toutes les 5 minutes" peut parfois être décalé de quelques
  minutes en période de forte charge. Très bien pour du 30min/1h/4h,
  moins précis pour du scalping en 1min.
- Le plan gratuit GitHub Actions inclut 2000 minutes/mois pour les repos
  privés — largement suffisant pour ce bot (quelques secondes par
  exécution, toutes les 5 min).
- Une seule personne peut piloter le bot (le chat Telegram correspondant
  à `TELEGRAM_CHAT_ID`) ; tout autre message est ignoré par sécurité.

## Évolutions futures possibles

- Recherche d'un symbole par texte.
- Catégories de symboles (Forex, Crypto, Indices, Matières premières).
- Modification d'une alerte existante (`/edit ID`).
- Autres types d'alerte (EMA cassée, croisement de deux EMA, etc.).
- Notifications plus détaillées (variation, volume, etc.).
