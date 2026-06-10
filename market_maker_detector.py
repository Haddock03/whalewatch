# market_maker_detector.py
# Détecte les wallets market-makers qui polluent la convergence Cockpit.
#
# Problème observé en prod : un wallet ranké "smart" peut en réalité faire
# du market-making sur des assets équivalents (ex: WBTC ↔ cbBTC ↔ tBTC,
# WETH ↔ stETH) en buy/sell quasi-simultanés. Il apparaît comme 1 wallet
# distinct sur 4-5 tokens différents → biaise la convergence à la hausse
# ET le ratio buy/sell, alors qu'il n'a aucune direction nette.
#
# Approche v1 — détection par cluster + fenêtre temporelle
#   1. Clusters d'assets équivalents hardcodés (WBTC=cbBTC=tBTC=BTCB=BTC, etc.)
#   2. Pour chaque wallet, scan des trades : s'il a BUY sur cluster X ET
#      SELL sur cluster X dans une fenêtre `window_sec` (défaut 60s) → MM.
#   3. Le wallet entier est flaggé MM (pas juste le trade) — il ne peut pas
#      être un signal directionnel sur d'autres tokens dans cette fenêtre.
#
# Choix volontaires
#   - Pas de suppression hard du wallet : il reste visible dans le feed
#     brut, mais est exclu du compteur n_wallets_distinct par cockpit.py.
#     Permet à l'opérateur d'auditer ("ah oui, regardez ce wallet est MM").
#   - Pas de heuristique buy/sell sur le MÊME token (ex: WBTC buy + WBTC
#     sell à 60s) : c'est déjà couvert par le clustering BTC.
#   - Pas d'heuristique sur les montants : un MM peut buy 1 BTC et sell
#     0.5 + 0.5 BTC, c'est toujours du MM. La détection cluster suffit
#     pour la v1.

import os
from collections import defaultdict
from datetime import datetime, timezone


# Clusters d'assets équivalents.
# Les variantes wrapped/bridged d'un même asset sous-jacent sont dans le
# même cluster. Un wallet qui buy un et sell un autre fait juste du MM
# sur l'écart cents-de-spread.
# Garde tout en MAJUSCULES.
ASSET_CLUSTERS = {
    # Bitcoin (BTC) — wrapped et bridged variants
    "BTC":   "BTC",
    "WBTC":  "BTC",
    "BTCB":  "BTC",   # BNB Chain wrapped BTC
    "TBTC":  "BTC",   # Threshold network
    "CBBTC": "BTC",   # Coinbase wrapped BTC

    # Ethereum (ETH) — wrapped, LSTs, restaking
    "ETH":    "ETH",
    "WETH":   "ETH",
    "STETH":  "ETH",
    "WSTETH": "ETH",
    "RETH":   "ETH",
    "CBETH":  "ETH",
    "ETH.E":  "ETH",  # Avalanche

    # Solana
    "SOL":  "SOL",
    "WSOL": "SOL",

    # BNB
    "BNB":  "BNB",
    "WBNB": "BNB",

    # Avalanche
    "AVAX":  "AVAX",
    "WAVAX": "AVAX",

    # Polygon (legacy MATIC + new POL ticker)
    "MATIC":  "MATIC",
    "WMATIC": "MATIC",
    "POL":    "MATIC",

    # Stables — toutes équivalentes pour la détection MM (rotation
    # stable ↔ stable = aucun signal). Note : ces stables n'apparaissent
    # PAS dans le feed Cockpit côté token directionnel (le feed les
    # classe comme leg de pricing), donc c'est plutôt une ceinture.
    "USDC":   "STABLE",
    "USDT":   "STABLE",
    "DAI":    "STABLE",
    "USDC.E": "STABLE",
    "USDT.E": "STABLE",
    "USDBC":  "STABLE",
    "FDUSD":  "STABLE",
    "TUSD":   "STABLE",
    "USDE":   "STABLE",
    "PYUSD":  "STABLE",
    "FRAX":   "STABLE",
    "LUSD":   "STABLE",
    "BUSD":   "STABLE",
}


# Configuration — env vars overridables
def _env_int(key, default):
    try:
        v = os.environ.get(key)
        return int(v) if v is not None and v != "" else int(default)
    except (TypeError, ValueError):
        return int(default)


def _env_flag(key, default):
    v = (os.environ.get(key) or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return bool(default)


MM_WINDOW_SEC = _env_int("COCKPIT_MM_WINDOW_SEC", 60)
MM_DETECTION_ENABLED = _env_flag("COCKPIT_MM_DETECTION", True)


def _parse_block_time(ts_str):
    """Parse une string ISO ou Dune-style → epoch seconds, ou None."""
    if not ts_str:
        return None
    try:
        s = str(ts_str).replace(" UTC", "").replace("Z", "+00:00")
        if "+" not in s and "T" not in s:
            s = s.replace(" ", "T") + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def cluster_for(token_sym):
    """Renvoie le cluster d'un token (BTC, ETH, ...) ou None si non clusterisé."""
    if not token_sym:
        return None
    return ASSET_CLUSTERS.get(str(token_sym).strip().upper())


def detect_market_makers(feed, window_sec=None, enabled=None):
    """À partir d'un feed de trades (sortie etherscan_cockpit_feed.fetch_feed),
    renvoie un set d'adresses (lowercase) identifiées comme market-makers.

    Heuristique : un wallet est MM s'il fait BUY ET SELL sur le MÊME cluster
    (assets équivalents) dans une fenêtre `window_sec` (défaut 60s).

    feed : liste de dicts {addr, token, side, usd, block_time, ...}
    window_sec : fenêtre maximale entre 2 trades opposés pour flag MM
    enabled : permet de short-circuit (renvoie set() vide). Si None, lit l'env.

    Renvoie : set[addr_lowercase]. Set vide si la feature est OFF.
    """
    if enabled is False:
        return set()
    if enabled is None and not MM_DETECTION_ENABLED:
        return set()
    win = window_sec if window_sec is not None else MM_WINDOW_SEC
    if win <= 0:
        return set()

    # Indexe les trades par wallet + cluster
    # by_wallet[addr][cluster] = list of {ts, side}
    by_wallet = defaultdict(lambda: defaultdict(list))
    for t in feed or []:
        addr = (t.get("addr") or "").lower()
        side = t.get("side")
        if not addr or side not in ("buy", "sell"):
            continue
        cluster = cluster_for(t.get("token"))
        if not cluster:
            continue  # token hors clusters → ne peut pas être détecté MM
        ts = _parse_block_time(t.get("block_time"))
        if ts is None:
            continue
        by_wallet[addr][cluster].append({"ts": ts, "side": side})

    mm = set()
    for addr, clusters in by_wallet.items():
        if addr in mm:
            continue
        for cluster, trades in clusters.items():
            # Trie par ts pour balayage linéaire
            trades.sort(key=lambda x: x["ts"])
            # Sliding window : pour chaque trade, regarder les trades
            # ultérieurs dans la fenêtre. Si on trouve un side opposé → MM.
            n = len(trades)
            found = False
            for i in range(n):
                for j in range(i + 1, n):
                    dt = trades[j]["ts"] - trades[i]["ts"]
                    if dt > win:
                        break  # tri par ts → plus rien dans la fenêtre
                    if trades[i]["side"] != trades[j]["side"]:
                        mm.add(addr)
                        found = True
                        break
                if found:
                    break
            if found:
                break  # wallet déjà flaggé, inutile de scanner d'autres clusters
    return mm


if __name__ == "__main__":
    # Smoke test rapide
    import time
    now = time.time()
    feed = [
        # Wallet A : BUY WBTC + SELL cbBTC à 30s → MM
        {"addr": "0xA", "token": "WBTC",  "side": "buy",  "usd": 10000,
         "block_time": datetime.fromtimestamp(now - 90, tz=timezone.utc).isoformat().replace("+00:00", "Z")},
        {"addr": "0xA", "token": "cbBTC", "side": "sell", "usd": 10000,
         "block_time": datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat().replace("+00:00", "Z")},
        # Wallet B : BUY WBTC + SELL ETH à 30s → PAS MM (clusters différents)
        {"addr": "0xB", "token": "WBTC", "side": "buy",  "usd": 10000,
         "block_time": datetime.fromtimestamp(now - 90, tz=timezone.utc).isoformat().replace("+00:00", "Z")},
        {"addr": "0xB", "token": "ETH",  "side": "sell", "usd": 10000,
         "block_time": datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat().replace("+00:00", "Z")},
        # Wallet C : BUY WBTC + SELL cbBTC à 120s → PAS MM (hors fenêtre 60s)
        {"addr": "0xC", "token": "WBTC",  "side": "buy",  "usd": 10000,
         "block_time": datetime.fromtimestamp(now - 180, tz=timezone.utc).isoformat().replace("+00:00", "Z")},
        {"addr": "0xC", "token": "cbBTC", "side": "sell", "usd": 10000,
         "block_time": datetime.fromtimestamp(now - 60, tz=timezone.utc).isoformat().replace("+00:00", "Z")},
    ]
    mm = detect_market_makers(feed, window_sec=60, enabled=True)
    print(f"MM detected: {mm}")
    print("Expected: {'0xa'} only")
