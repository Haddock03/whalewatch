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


CHAINS = {
    "ethereum": {
        "chainid": 1,
        "dune_blockchain": "ethereum",
        "explorer_url": "https://etherscan.io",
        "label": "Ethereum",
        "symbol": "ETH",
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
        "cache_file": "results_arbitrum.json",
        "patterns_file": "patterns_arbitrum.json",
    },
    "base": {
        "chainid": 8453,
        "dune_blockchain": "base",
        "explorer_url": "https://basescan.org",
        "label": "Base",
        "symbol": "ETH",
        "cache_file": "results_base.json",
        "patterns_file": "patterns_base.json",
    },
    "optimism": {
        "chainid": 10,
        "dune_blockchain": "optimism",
        "explorer_url": "https://optimistic.etherscan.io",
        "label": "Optimism",
        "symbol": "ETH",
        "cache_file": "results_optimism.json",
        "patterns_file": "patterns_optimism.json",
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
    aliases = {"eth": "ethereum", "arb": "arbitrum", "op": "optimism"}
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
