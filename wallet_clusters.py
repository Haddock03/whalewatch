# wallet_clusters.py
# Détection de "clusters" de wallets contrôlés par une même entité,
# via le pattern : plusieurs Contract wallets partagent le même deployer.
#
# Cas d'usage : une firme prop déploie 5 bots de trading. Chaque bot
# accumule un volume DEX élevé. Sans cluster detection, ils peuvent
# tous finir dans le Smart Money Leaderboard et donner l'illusion de
# "5 alpha indépendants" alors que c'est une seule entité.
#
# Heuristique : si N contracts partagent un deployer, on les groupe.
# Le cluster_id est dérivé de l'adresse du deployer pour stabilité.
#
# Limitations connues :
#   - Ne détecte pas les EOAs qui contrôlent plusieurs wallets via
#     transferts off-chain (pas notre signal)
#   - Ne suit pas les deployer-of-deployer chains (factory contracts)
#   - Un deployer peut être un service partagé (CREATE2 factory) qui
#     produit des contracts indépendants → faux positif. Mitigation :
#     si le deployer a déployé > MAX_CLUSTER_SIZE wallets dans notre top,
#     on ne marque pas (c'est probablement un service).

import os
import time
import requests

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
ETHERSCAN_CHAIN_ID = int(os.environ.get("ETHERSCAN_CHAIN_ID", "1"))
BASE_URL = "https://api.etherscan.io/v2/api"

# Si un deployer apparaît > 30 fois dans notre top → c'est probablement
# une factory partagée (CREATE2 multisig deployers, Safe factories, etc.)
# On le marque comme "shared factory" plutôt que cluster privé.
MAX_PRIVATE_CLUSTER_SIZE = 30
# Taille minimale pour qu'un groupe soit considéré comme cluster.
MIN_CLUSTER_SIZE = 2


def _get(params, retries=3):
    params["apikey"] = ETHERSCAN_API_KEY
    params.setdefault("chainid", ETHERSCAN_CHAIN_ID)
    for attempt in range(retries):
        try:
            r = requests.get(BASE_URL, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            if isinstance(data.get("result"), str) and "Max rate limit" in data["result"]:
                time.sleep(1.2)
                continue
            return data
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(0.5)
    return {}


def get_deployer(address):
    """Renvoie l'adresse du deployer d'un contrat, ou None.

    Pour les EOAs, renvoie None. Pour les contracts non trouvés (pas
    d'historique), renvoie None.
    """
    try:
        data = _get({
            "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": address,
        })
        result = data.get("result")
        if not result or not isinstance(result, list) or not result[0]:
            return None
        creator = result[0].get("contractCreator", "")
        return creator.lower() if creator else None
    except Exception:
        return None


def detect_clusters(wallets, progress_cb=None):
    """Détecte les clusters par deployer commun.

    `wallets` : liste de dicts avec au minimum {address, is_contract}.
                Seuls les is_contract=True sont interrogés.

    Renvoie un dict :
        {
            address_lower: {
                "deployer": "0x…",     # adresse complète du deployer
                "cluster_id": "0x123…", # id court (6 premiers chars hex)
                "cluster_size": int,    # nb de wallets dans le cluster
                "is_shared_factory": bool, # True si > MAX_PRIVATE_CLUSTER_SIZE
            },
            ...
        }
    Wallets sans cluster (uniques ou EOAs) sont absents du dict.
    """
    def log(msg):
        if progress_cb:
            progress_cb(msg)
        else:
            print(msg, flush=True)

    # 1) Fetch deployer pour chaque contract (skip EOAs)
    addr_to_deployer = {}
    contracts = [w for w in wallets if w.get("is_contract")]
    log(f"detect_clusters : {len(contracts)} contracts à interroger…")

    for i, w in enumerate(contracts, 1):
        addr = w["address"].lower()
        deployer = get_deployer(addr)
        if deployer:
            addr_to_deployer[addr] = deployer
        if i % 20 == 0:
            log(f"  deployer fetched {i}/{len(contracts)}")
        time.sleep(0.22)  # ~4.5 req/s, sous le rate limit

    # 2) Group by deployer
    from collections import defaultdict
    by_deployer = defaultdict(list)
    for addr, deployer in addr_to_deployer.items():
        by_deployer[deployer].append(addr)

    # 3) Compose la table de retour pour les clusters ≥ MIN_CLUSTER_SIZE
    result = {}
    cluster_counter = 0
    for deployer, addrs in by_deployer.items():
        if len(addrs) < MIN_CLUSTER_SIZE:
            continue
        is_shared = len(addrs) > MAX_PRIVATE_CLUSTER_SIZE
        cluster_id = deployer[:8]  # short readable id (e.g. "0x858ea9")
        for addr in addrs:
            result[addr] = {
                "deployer": deployer,
                "cluster_id": cluster_id,
                "cluster_size": len(addrs),
                "is_shared_factory": is_shared,
            }
        cluster_counter += 1

    log(f"detect_clusters : {cluster_counter} clusters trouvés, "
        f"{len(result)} wallets impactés.")
    return result


if __name__ == "__main__":
    # Smoke test sur les 5 wallets suspectés de partager le deployer 0x858ea9
    test_addresses = [
        "0x33988614010be265e71ab3a04bd29f0b950bc58c",
        "0x91a7caaf2b770e9ebf7606159d0e1f1ec7f2f423",
        "0x555f240e556788e65306754a0ba6e7a76c2ab59e",
        "0xbab386a3220234f7cce09b3794e9b5a2f44ce775",
        "0x0906a879ea0f66e3559f11b25b866dba247f9e63",
    ]
    test_wallets = [{"address": a, "is_contract": True} for a in test_addresses]
    clusters = detect_clusters(test_wallets)
    print("\nRésultats :")
    for addr, info in clusters.items():
        print(f"  {addr[:10]}…  deployer={info['deployer'][:10]}…  "
              f"cluster={info['cluster_id']}  size={info['cluster_size']}")
