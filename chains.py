# chains.py
# Configuration multi-chain pour WhaleWatch.
#
# Une "chain" centralise tout ce dont le pipeline a besoin pour fonctionner
# sur une blockchain donnée :
#   - chainid          : utilisé par Etherscan V2 (param chainid=…)
#   - dune_blockchain  : le nom utilisé dans `dex.trades.blockchain = '…'`
#   - explorer_url     : pour construire les liens UI (etherscan.io,
#                        arbiscan.io, basescan.org…)
#   - label            : nom affiché dans le sélecteur frontend
#   - cache_file       : nom du fichier cache JSON (rétrocompat Ethereum)
#   - symbol           : actif natif (ETH, ETH, MATIC…)
#
# Étendre à une nouvelle chain = ajouter une entrée dans CHAINS + créer/
# adapter la query Dune. Aucun autre module ne doit hardcoder un nom
# de chain.
#
# Limites :
#   - Etherscan V2 sert toutes ces chains avec la même API key → pratique.
#   - Dune dex.trades couvre la plupart des EVM chains via la colonne
#     blockchain. Vérifier que le nom matche avant d'activer une chain.

import os

# Cache toujours stocké dans <repo>/cache/
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


# Note sur `volume_scale` :
#   Le Smart Money Score est calibré sur les volumes Ethereum (seuils :
#   ≥10M$ = 22 pts, ≥1Md$ = 40 pts dans _vol_pts). Sur les L2, les volumes
#   DEX par wallet sont 30–100× plus petits → sans correction, tout le
#   monde stagne à score 40 et le tier "Alpha" est inatteignable.
#
#   volume_scale est un multiplicateur appliqué au volume avant le tier
#   matching : un volume effectif = vol * scale. Choisi pour qu'un wallet
#   « top 5% non-infra » d'une chain donnée tombe en tier Alpha/Solid,
#   comparable à Ethereum.
#
#   Valeurs calibrées sur observations 5 juin 2026 :
#     Ethereum : 1.0   (référence, total vol $7.6B/7j, top alpha 80)
#     Arbitrum : 100   (total $1.77B/7j, top non-infra $3M → 78 Solid)
#     Base     : 5     (total $9.2B/7j — Base très actif, scale conservateur)
#     Optimism : 25    (total $0.15B/7j — chain calme, top à 58 Avg)
#     Polygon  : 30    (total $2.8B/7j, top non-infra 63 Avg)
#     BNB Chain: 20    (total $16.6B/7j, top non-infra $86M → 74 Solid)
#   À recalibrer si la distribution des volumes change significativement.

CHAINS = {
    "ethereum": {
        "chainid": 1,
        "dune_blockchain": "ethereum",
        "explorer_url": "https://etherscan.io",
        "label": "Ethereum",
        "symbol": "ETH",
        "volume_scale": 1.0,
        # Rétrocompat : fichier historique sans suffixe pour ne pas casser
        # les déploiements existants qui s'appuient dessus.
        "cache_file": "results.json",
        "patterns_file": "patterns.json",
    },
    "arbitrum": {
        "chainid": 42161,
        "dune_blockchain": "arbitrum",
        "explorer_url": "https://arbiscan.io",
        "label": "Arbitrum",
        "symbol": "ETH",
        "volume_scale": 100.0,
        "cache_file": "results_arbitrum.json",
        "patterns_file": "patterns_arbitrum.json",
    },
    "base": {
        "chainid": 8453,
        "dune_blockchain": "base",
        "explorer_url": "https://basescan.org",
        "label": "Base",
        "symbol": "ETH",
        "volume_scale": 5.0,
        "cache_file": "results_base.json",
        "patterns_file": "patterns_base.json",
    },
    "optimism": {
        "chainid": 10,
        "dune_blockchain": "optimism",
        "explorer_url": "https://optimistic.etherscan.io",
        "label": "Optimism",
        "symbol": "ETH",
        "volume_scale": 25.0,
        "cache_file": "results_optimism.json",
        "patterns_file": "patterns_optimism.json",
    },
    "polygon": {
        "chainid": 137,
        "dune_blockchain": "polygon",
        "explorer_url": "https://polygonscan.com",
        "label": "Polygon",
        "symbol": "POL",            # POL (formerly MATIC)
        "volume_scale": 30.0,       # initial — à calibrer après 1er Sonar
        "cache_file": "results_polygon.json",
        "patterns_file": "patterns_polygon.json",
    },
    "bnb": {
        "chainid": 56,
        "dune_blockchain": "bnb",
        "explorer_url": "https://bscscan.com",
        "label": "BNB Chain",
        "symbol": "BNB",
        "volume_scale": 20.0,       # initial — à calibrer après 1er Sonar
        "cache_file": "results_bnb.json",
        "patterns_file": "patterns_bnb.json",
    },
}


DEFAULT_CHAIN = "ethereum"


def resolve(chain):
    """Renvoie le dict de config pour une chain, ou raise ValueError.

    Accepte les alias usuels (eth → ethereum, arb → arbitrum, op → optimism).
    Case-insensitive.
    """
    if not chain:
        return CHAINS[DEFAULT_CHAIN]
    key = chain.strip().lower()
    aliases = {"eth": "ethereum", "arb": "arbitrum", "op": "optimism",
               "matic": "polygon", "pol": "polygon",
               "bsc": "bnb", "binance": "bnb"}
    key = aliases.get(key, key)
    if key not in CHAINS:
        raise ValueError(
            f"Chain inconnue : {chain!r}. Connues : {sorted(CHAINS.keys())}"
        )
    cfg = dict(CHAINS[key])
    cfg["key"] = key
    cfg["cache_path"] = os.path.join(CACHE_DIR, cfg["cache_file"])
    cfg["patterns_path"] = os.path.join(CACHE_DIR, cfg["patterns_file"])
    return cfg


def list_chains():
    """Liste des configs (avec clés) pour l'UI."""
    return [resolve(k) for k in CHAINS.keys()]


if __name__ == "__main__":
    for c in list_chains():
        print(f"{c['key']:10s} chainid={c['chainid']:>6d}  dune={c['dune_blockchain']:10s}  "
              f"cache={c['cache_file']:25s} {c['explorer_url']}")
