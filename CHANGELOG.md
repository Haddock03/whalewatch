# Changelog

Toutes les modifications notables apparaissent ici.

## [Unreleased]

## [0.2.0] — Multi-chain refactor (juin 2026)

Transformation majeure : passage d'un radar Ethereum-only à un radar multi-chain
EVM (7 chains), avec score backend rigoureux, détection de clusters et UX cohérente.

### Added — Backend

- **`chains.py`** : module central de configuration multi-chain. 7 chains
  supportées (Ethereum, Arbitrum, Base, Optimism, Polygon, BNB Chain, Avalanche)
  avec `chainid`, `dune_blockchain`, `explorer_url`, `volume_scale`, `cache_file`,
  `patterns_file`, `label`, `symbol`. Fonction `resolve()` avec aliases
  (eth/arb/op/matic/pol/bsc/binance/avax) et case insensitivity.
- **`wallet_classifier.py`** : classification 7 types
  (mev/mm/cex/bridge/router/contract/eoa) via regex sur name tags Etherscan +
  category Dune. Source unique pour la pénalité infra du Smart Score.
  Miroir JS dans `static/index.html`.
- **`wallet_clusters.py`** : détection de clusters de wallets contrôlés par
  une même entité via deployer commun (Etherscan `getcontractcreation`).
  Flag `is_shared_factory` pour les déployeurs > 30 wallets (Safe factory etc.).
- **`smart_score.py`** : nouvelle pénalité `_infra_penalty()` (−45 à −55 selon
  type). Paramètre `volume_scale` pour calibrer les seuils par chain.
- **`test_scoring.py`** : 68 cas de régression couvrant classification, scoring,
  invariants infra, et `chains.resolve()`.
- **`update_all_chains.py`** : script cron-friendly qui lance les Sonars sur
  toutes les chains avec skip cache récent (`--max-age`).
- **`/api/health`** : endpoint de monitoring renvoyant santé par chain
  (cache exists, age, wallets, healthy=True si <24h).
- **`/api/chains/summary`** : agrégat de tous les caches pour le widget UI.
- Validation des clés API au boot serveur + sur `/api/refresh` (HTTP 400
  avec message clair si DUNE_API_KEY ou ETHERSCAN_API_KEY manque).

### Added — Frontend

- Sélecteur de chain dans le header (dropdown 7 options) avec dot coloré
  glow (brand color officielle de chaque chain).
- Live-chip dynamique : `LIVE · ARB · il y a 5m`, couleur d'accent par chain.
- Widget multi-chain summary sous le hero strip : 7 mini-cards cliquables
  avec volume DEX, nb wallets, âge des données, badge ACTIVE, icône ⏱ si stale.
- Bouton Sonar indique la chain : `Sonar · BNB`.
- `document.title` reflète la chain : `WhaleWatch · Arbitrum — On-Chain Radar`.
- Sub-titre KPI s'adapte : `Volume DEX BNB Chain` au lieu d'`Ethereum` hardcodé.
- Support du query param `?chain=X` au boot (persisté en localStorage).
- Export CSV : filename inclut la chain (`top_wallets_arbitrum_2026-06-05.csv`)
  + colonnes ajoutées (`smart_score`, `smart_label`, `cluster_id`, `cluster_size`).
- Badge cluster ⛓ dans le tableau wallets + dans le modal de détail (liste
  des siblings cliquables).
- Smart Money Leaderboard dédupé par cluster (un wallet par cluster).
- Filter chip toolbar `Hors infra` et `Dédup clusters`.
- Widget couverture multi-chain sur `/why`.
- Panel alertes caché sur les chains autres qu'Ethereum (alertes ETH-only).
- triggerRefresh affiche les erreurs serveur dans un toast.

### Changed

- **Etherscan API V2** (critique prod) : migration de
  `https://api.etherscan.io/api` (déprécié, retourne NOTOK depuis 2026) vers
  `https://api.etherscan.io/v2/api` avec paramètre `chainid` dynamique.
  Toutes les chains EVM unifiées sous une seule clé API.
- **Queries Dune** : ajout du filtre `AND blockchain = '{chain}'` dans
  `dune_smart_signals`, `dune_wallet_trades`, `dune_patterns` — évite les
  contaminations cross-chain quand un même wallet existe sur plusieurs chains.
- **`smart_score.compute_score(volume_scale=…)`** : nouveau paramètre pour
  calibrer les seuils volume par chain. Rétrocompat préservée
  (default 1.0 = Ethereum).
- **`_run_analysis.main(chain=…)`** : pipeline complet paramétré par chain.
- **`/methodology`** : section multi-chain documentée + détail des scales L2.
- **`/why`** : section "Ce que WhaleWatch ne voit pas" + chiffres marqués
  illustratifs si pas live.
- README.md créé (196 lignes) avec quick start, architecture, API multi-chain,
  formule Smart Score, cluster detection, procédure d'ajout de chain.

### Fixed

- `resolve(None)` ne crashait plus avec KeyError (renvoyait CHAINS[default]
  sans ajouter cache_path/patterns_path).
- `countUp` (P0.2 hardening) : fallback `setTimeout` pour garantir l'affichage
  de la valeur finale quand `requestAnimationFrame` est gelé (tab background).
- BTC arbitrage example (`/bot`) : remplacement de "$2 100 → $2 102" par
  un écart en % réaliste.
- `classifyWalletType` détecte désormais AugustusV6 (Paraswap) et
  Settler L2 (Across Bridge cross-chain).
- Add Across Bridge Hub + Fluid Liquidity à KNOWN_LABELS (étaient top du
  leaderboard alors que c'est de l'infra).
- Patterns whales fonctionnent sur les 7 chains (était hardcodé Ethereum).
- Live-chip reset au switch de chain (ne montre plus l'âge de l'ancienne chain).

### Performance / Polish

- Skeleton loaders pendant le chargement (KPI + tableau).
- États explicites loading/empty/error avec bouton Réessayer.
- Meta description + OG + Twitter cards + canonical uniques sur les 5 routes
  principales (P0.3 audit).
- A11y ticker crypto : flèches ▲/▼ + ARIA labels.
- OG image générée (1200×630 PNG depuis SVG via qlmanage + sips).
- WCAG focus-visible déjà en place.
- Disclaimer financier global en footer ("Contenu éducatif — pas un conseil
  financier · DEX on-chain seulement · DYOR").
- Harmonisation nav sur les 9 pages (ordre unique, emojis allégés, ajout
  Méthode).

### Calibrations observées (snapshot 5 juin 2026)

| Chain | Total vol /7j | Volume scale | Top non-infra score |
|---|---|---|---|
| Ethereum | $7.6B | 1.0 | 80 (Alpha) |
| Arbitrum | $1.77B | 100 | 78 (Solid) |
| Base | $9.2B | 5 | 63 (Avg) |
| Optimism | $0.15B | 25 | 58 (Avg) |
| Polygon | $2.8B | 30 | 63 (Avg) |
| BNB Chain | $16.6B | 20 | 74 (Solid) |
| Avalanche | (à venir) | 50 | — |

## [0.1.0] — P0–P2 audit fixes (juin 2026)

Premier commit suite à l'audit externe. Hier soir.

### Fixed (P0 — Bloquants)
- **P0.1** : Exemple BTC arbitrage `/bot` (était $2 100/$2 102 → écart % réaliste).
- **P0.2** : Plus d'état "Océan vide" à froid (skeleton loaders + auto-fetch +
  states loading/empty/error avec retry).
- **P0.3** : `meta description` + OG + Twitter cards uniques sur 5 pages.
- **P0.4** : Cohérence message "Traquer, pas copier" (remplacement
  "copy-trade picks" par "wallets à surveiller").

### Added (P1 — Crédibilité)
- Filtre/badges infrastructure (frontend) — `Hors infra` chip.
- Sources visibles sous les KPI (Dune, Etherscan, CoinGecko).
- Page `/methodology` complète (sources, score, fréquence, limites, équipe,
  disclaimer).
- Note "DEX on-chain seulement" sur `/why` et `/methodology`.
- Marquage explicite des chiffres `/why` quand pas de données live.

### Polish (P2 — Conversion)
- Clarification offre Pro (beta gratuite + tarification à venir).
- Recalibration promesses bot ("démarrer proprement" au lieu de
  "aller profitable rapidement").
- Renvois éducatif → produit dans `/guide` et `/bot`.
- Harmonisation nav (ordre unique, emojis allégés, ajout Méthode).
- OG image placeholder.
- Pages `/pro/backtest`, `/pro/watchlist`, `/pro/guide` harmonisées.
