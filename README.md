# WhaleWatch

Radar on-chain multi-chain : track les baleines DEX EVM (Ethereum, Arbitrum, Base, Optimism, Polygon, BNB Chain), score leur activité par un Smart Money Score documenté, détecte les clusters de wallets contrôlés par une même entité, et expose l'analyse dans un dashboard temps réel.

> Production : [whalewatchapp.io](https://whalewatchapp.io)
> Documentation produit : [/methodology](https://whalewatchapp.io/methodology)
> Manifeste : [/why](https://whalewatchapp.io/why)

---

## Stack

- **Backend** : Python stdlib uniquement (`http.server`) — aucune dépendance web framework.
- **Pipeline** : Python + `pandas` + `requests` (1 dépendance externe : Dune + Etherscan + CoinGecko APIs).
- **Frontend** : HTML/CSS/JS statiques, Chart.js v4 pour les graphes. Pas de build.
- **Hosting** : Dockerfile fourni, prêt pour PaaS (Heroku/Railway/Fly).

## Quick start

### 1. Cloner et installer

```bash
git clone https://github.com/Haddock03/whalewatch.git
cd whalewatch
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configurer les clés API

Copier `.env.example` en `.env` et remplir :

```env
DUNE_API_KEY=ton_dune_key
ETHERSCAN_API_KEY=ton_etherscan_key
```

- **Dune** : [dune.com/settings/api](https://dune.com/settings/api)
- **Etherscan** : [etherscan.io/myapikey](https://etherscan.io/myapikey) (V2, multi-chain via paramètre `chainid`)
- CoinGecko : pas de clé requise (API publique)

### 3. Lancer le serveur

```bash
python3 server.py    # http://localhost:8000
```

### 4. Lancer une analyse

Via le bouton **Sonar** dans le dashboard, ou en CLI :

```bash
WW_CHAIN=ethereum WW_N_WALLETS=100 WW_DAYS=7 python3 _run_analysis.py
WW_CHAIN=arbitrum WW_N_WALLETS=80  WW_DAYS=7 python3 _run_analysis.py
# … pour toutes les chains en séquence :
python3 update_all_chains.py
```

## Architecture

```
whalewatch/
├── server.py                 # HTTP API + routing (stdlib only)
├── _run_analysis.py          # Pipeline complet (lancé en subprocess)
├── chains.py                 # ★ Config des 6 chains (ETH/ARB/BASE/OP/POL/BNB)
├── wallet_classifier.py      # ★ Classification 7 types (mev/mm/cex/bridge/router/contract/eoa)
├── wallet_clusters.py        # ★ Détection bot fleets via deployer commun
├── smart_score.py            # ★ Smart Money Score 0-100 (sans inférence)
├── smart_score_enrich.py     # Enrichit cache avec score + clusters
├── test_scoring.py           # 37 cas de régression
├── update_all_chains.py      # Lance tous les Sonars (cron-friendly)
│
├── dune_top_wallets.py       # Query Dune top wallets DEX (multi-chain)
├── dune_smart_signals.py     # Signaux comportementaux (diversité, net flow)
├── dune_patterns.py          # Patterns whales (multi-chain)
├── dune_wallet_trades.py     # Détail trades par wallet (multi-chain)
├── etherscan_scraper.py      # Etherscan V2 + chainid dynamique
├── combine_and_rank.py       # Fusion + ranking → cache JSON
├── alerts.py                 # Snapshot diff over time (ETH-only)
│
├── cache/                    # JSON caches par chain (gitignored)
│   ├── results.json          # Ethereum (rétrocompat)
│   ├── results_arbitrum.json
│   ├── results_base.json
│   ├── results_optimism.json
│   ├── results_polygon.json
│   ├── results_bnb.json
│   └── patterns_*.json
│
└── static/                   # Frontend
    ├── index.html            # Dashboard
    ├── why.html              # Manifeste
    ├── guide.html            # Guide trading (8 piliers)
    ├── bot.html              # Guide bot
    ├── methodology.html      # Méthodologie ouverte
    ├── pro_*.html            # 4 pages Pro
    └── assets/whale.css      # Design system
```

## Multi-chain

WhaleWatch supporte 6 chains EVM. Tout le pipeline est paramétré via `chains.py` :

| Chain | chainid | Dune blockchain | Volume scale | Explorer |
|---|---|---|---|---|
| Ethereum | 1 | `ethereum` | 1.0 | etherscan.io |
| Arbitrum | 42161 | `arbitrum` | 100.0 | arbiscan.io |
| Base | 8453 | `base` | 5.0 | basescan.org |
| Optimism | 10 | `optimism` | 25.0 | optimistic.etherscan.io |
| Polygon | 137 | `polygon` | 30.0 | polygonscan.com |
| BNB Chain | 56 | `bnb` | 20.0 | bscscan.com |

`volume_scale` ajuste les seuils de tier du score pour compenser les volumes DEX différents par chain. Étendre à une nouvelle chain = ajouter une entrée dans `CHAINS` + tester end-to-end.

### Sélecteur frontend
Le dashboard a un sélecteur de chain dans le header avec un dot coloré (brand color officielle de chaque chain) et une live-chip qui affiche l'âge des données. Le choix est persisté en `localStorage`.

### API multi-chain

| Endpoint | Description |
|---|---|
| `GET /api/chains` | Liste des chains configurées |
| `GET /api/chains/summary` | Snapshot agrégé (vol, wallets, last_updated) par chain |
| `GET /api/wallets?chain=X` | Wallets de la chain X |
| `GET /api/patterns?chain=X` | Patterns whales pour X |
| `GET /api/wallet/{addr}?chain=X` | Détail wallet sur X |
| `GET /api/wallet/{addr}/trades?chain=X` | Trades du wallet sur X |
| `POST /api/refresh?chain=X` | Lance un Sonar pour X |

## Smart Money Score

Le score 0-100 combine :
- **Volume** (0-40 pts, calibré par chain via `volume_scale`)
- **Avg trade size** (0-22 pts, sweet spot 50k-1M$)
- **Diversity** (0-10 pts, nb DEX × nb tokens)
- **Activity** (0-8 pts, % jours actifs)
- **Net ETH flow** (0-8 pts, accumulation = +)
- **EOA bonus** (+6 pts si humain)

Pénalités :
- **MEV bot** : −45 pts (category Dune ou regex sandwich/jared/…)
- **Spam** : −18 si >5000 trades/7j, −7 si >1500
- **Concentration** : jusqu'à −10 si un seul jour fait >50% du volume
- **Infrastructure** : −45 à −55 selon type (CEX/Bridge/Router/MM via `wallet_classifier`)

Invariant garanti par tests : un wallet d'infra ne peut pas dépasser score 65 même à 1 Md$ de volume. 37/37 tests passent (`python3 test_scoring.py`).

Voir [methodology.html](static/methodology.html) pour la formule complète et les sources.

## Cluster detection

Une firme prop peut déployer N bots qui chacun accumulent un volume DEX. Sans dédup, ces N wallets pourraient monopoliser le Smart Money Leaderboard. `wallet_clusters.detect_clusters()` :

1. Pour chaque smart contract du top-N, fetch le deployer via Etherscan `getcontractcreation`
2. Group by deployer
3. Si ≥2 contracts partagent un deployer → cluster (ID = 6 premiers chars du deployer)
4. Si >30 contracts → flag "shared factory" (CREATE2 multisig)

Dans le UI : badge violet ⛓ X à côté de l'adresse. Smart Money Leaderboard dédupe automatiquement (un wallet par cluster, badge +N indique les siblings). Filter chip "Dédup clusters" dans la toolbar.

## Développement

### Tests

```bash
python3 test_scoring.py     # Tests scoring + classifier (37 cas)
python3 chains.py           # Smoke test config chains
python3 wallet_classifier.py # Smoke test classification
python3 wallet_clusters.py  # Smoke test cluster detection (nécessite Etherscan key)
```

### Migration Etherscan V2

L'API Etherscan V1 a été dépréciée en 2026. `etherscan_scraper.py` utilise désormais V2 avec param `chainid` dynamique. Toutes les chains EVM supportées sont unifiées sous une seule clé API.

### Cron multi-chain

```bash
# /etc/crontab — toutes les 2h
0 */2 * * * cd /path/whalewatch && python3 update_all_chains.py --max-age 90 >> logs/cron.log 2>&1
```

## Contributing

Pull requests bienvenus. Pour ajouter une chain :

1. Ajouter une entrée dans `chains.CHAINS`
2. Vérifier que Dune supporte le nom de blockchain (`SELECT DISTINCT blockchain FROM dex.trades`)
3. Faire un Sonar test : `WW_CHAIN=<key> WW_N_WALLETS=50 python3 _run_analysis.py`
4. Calibrer `volume_scale` en regardant le top wallet non-infra
5. Ajouter dans le sélecteur frontend (`static/index.html`)
6. Ajouter une couleur dans `CHAIN_ACCENT` (préférer la brand color officielle)

## License

Code éducatif. Pas de conseil financier. DYOR.
