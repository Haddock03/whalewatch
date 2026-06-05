# etherscan_scraper.py
# Données on-chain des wallets via l'API Etherscan V2.
#
# 2026 — migration V1 → V2 obligatoire : l'API V1 (api.etherscan.io/api)
# retourne désormais NOTOK avec un message "deprecated V1 endpoint".
# La V2 unifie 50+ chains derrière une seule base URL et requiert un
# paramètre `chainid` (1 = Ethereum mainnet). Docs : https://docs.etherscan.io/

import os
import requests
import time

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
BASE_URL = "https://api.etherscan.io/v2/api"
# Chain ID par défaut. Peut être surchargé par :
#   1) variable d'env ETHERSCAN_CHAIN_ID (rétrocompat)
#   2) thread-local set_chain_id() depuis combine_and_rank.merge_and_rank()
DEFAULT_CHAIN_ID = int(os.environ.get("ETHERSCAN_CHAIN_ID", "1"))
_current_chain_id = DEFAULT_CHAIN_ID


def set_chain_id(chainid):
    """Permet aux modules amont de switcher la chain dynamiquement."""
    global _current_chain_id
    _current_chain_id = int(chainid)


def get_chain_id():
    return _current_chain_id


def _get(params, retries=3):
    params["apikey"] = ETHERSCAN_API_KEY
    # V2 exige chainid sur tous les endpoints
    params.setdefault("chainid", _current_chain_id)
    for attempt in range(retries):
        try:
            r = requests.get(BASE_URL, params=params, timeout=15)
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


def is_contract(address):
    """Retourne True si l'adresse est un smart contract"""
    data = _get({
        "module": "proxy",
        "action": "eth_getCode",
        "address": address,
        "tag": "latest",
    })
    code = data.get("result", "0x")
    return code not in ("0x", "0x0", None, "")


def get_contract_name(address):
    """Retourne le nom du contrat vérifié sur Etherscan, ou None"""
    data = _get({
        "module": "contract",
        "action": "getsourcecode",
        "address": address,
    })
    if data.get("status") == "1" and data.get("result"):
        result = data["result"][0] if isinstance(data["result"], list) else data["result"]
        name = result.get("ContractName", "")
        if name and name not in ("", "0x"):
            return name
    return None


def get_wallet_tx_count(address, is_contract_addr=False):
    """Nombre de transactions sortantes (nonce pour EOA, txlist count pour contrat)"""
    if is_contract_addr:
        # Pour les contrats : compter via txlist
        data = _get({
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": 0,
            "endblock": 99999999,
            "page": 1,
            "offset": 1,
            "sort": "desc",
        })
        # Etherscan ne retourne pas le total — on utilise la norme API
        # Meilleure approche : récupérer le count via tokentx stats
        return None  # sera set depuis recent_transactions
    else:
        data = _get({
            "module": "proxy",
            "action": "eth_getTransactionCount",
            "address": address,
            "tag": "latest",
        })
        result = data.get("result", "0x0")
        try:
            return int(result, 16)
        except (ValueError, TypeError):
            return 0


def get_recent_transactions(address, limit=100):
    data = _get({
        "module": "account",
        "action": "txlist",
        "address": address,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
    })
    if data.get("status") == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []


def get_token_transfers(address, limit=200):
    data = _get({
        "module": "account",
        "action": "tokentx",
        "address": address,
        "page": 1,
        "offset": limit,
        "sort": "desc",
    })
    if data.get("status") == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []


def get_eth_balance(address):
    data = _get({
        "module": "account",
        "action": "balance",
        "address": address,
        "tag": "latest",
    })
    if data.get("status") == "1":
        try:
            return int(data["result"]) / 1e18
        except (ValueError, TypeError):
            pass
    return 0.0


def analyze_wallet_volume(address):
    """
    Analyse complète d'un wallet.
    Détecte automatiquement EOA vs contrat, récupère le nom si disponible.
    """
    # Détection contrat
    contract = is_contract(address)
    contract_name = None
    if contract:
        contract_name = get_contract_name(address)

    txs = get_recent_transactions(address, limit=100)
    total_volume_eth = 0.0
    total_gas_spent = 0.0
    sent_count = 0
    received_count = 0

    for tx in txs:
        try:
            value_eth = int(tx.get("value", 0)) / 1e18
            gas_used = int(tx.get("gasUsed", 0))
            gas_price = int(tx.get("gasPrice", 0))
            total_volume_eth += value_eth
            total_gas_spent += (gas_used * gas_price) / 1e18
            if tx.get("from", "").lower() == address.lower():
                sent_count += 1
            else:
                received_count += 1
        except (ValueError, TypeError):
            continue

    token_transfers = get_token_transfers(address, limit=200)
    unique_tokens = set()
    for t in token_transfers:
        sym = t.get("tokenSymbol", "")
        if sym:
            unique_tokens.add(sym)

    balance = get_eth_balance(address)

    # tx_count : nonce pour EOA, len(txs) approximatif pour contrat
    if contract:
        tx_count = len(txs)
    else:
        tx_count = get_wallet_tx_count(address, is_contract_addr=False) or len(txs)

    # Heuristique MEV : beaucoup de trades, peu de temps, faible balance relative
    mev_score = 0
    if len(txs) >= 50 and sent_count > received_count * 2:
        mev_score += 1
    if len(token_transfers) > 100:
        mev_score += 1
    if contract and len(unique_tokens) < 5 and len(token_transfers) > 50:
        mev_score += 1  # bot spécialisé

    return {
        "address": address,
        "is_contract": contract,
        "contract_name": contract_name,
        "total_tx_count": tx_count,
        "recent_tx_analyzed": len(txs),
        "sent_tx": sent_count,
        "received_tx": received_count,
        "volume_eth_recent": round(total_volume_eth, 4),
        "gas_spent_eth": round(total_gas_spent, 6),
        "token_transfer_count": len(token_transfers),
        "unique_tokens_traded": len(unique_tokens),
        "current_balance_eth": round(balance, 4),
        "mev_score": mev_score,
    }


if __name__ == "__main__":
    import pandas as pd
    SAMPLE_WALLETS = [
        "0xae2Fc483527B8EF99EB5D9B44875F005ba1FaE13",
        "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD",
        "0x1111111254EEB25477B68fb85Ed929f73A960582",
    ]
    results = []
    for wallet in SAMPLE_WALLETS:
        print(f"Analyse de {wallet[:12]}...")
        data = analyze_wallet_volume(wallet)
        results.append(data)
        print(f"  contrat={data['is_contract']} name={data['contract_name']} txs={data['recent_tx_analyzed']}")
        time.sleep(0.3)
    df = pd.DataFrame(results)
    print(df.to_string(index=False))
