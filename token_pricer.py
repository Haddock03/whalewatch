# token_pricer.py
# Pricer multi-source pour valoriser les trades du feed Cockpit en USD.
#
# Stratégie (par priorité) :
#   1. Whitelist stables hardcodée → $1.0 sans appel réseau.
#   2. Whitelist natifs/wrapped (ETH, BTC, BNB, MATIC, AVAX) → CoinGecko
#      simple/price (1 appel partagé entre toutes les chains).
#   3. DexScreener par adresse de contrat (gratuit, multichain natif).
#      → on prend le pool avec la plus grosse liquidité USD pour éviter
#         le pricing manipulé via pool fine.
#   4. None → trade ignoré côté feed (pas de bruit).
#
# Cache mémoire 60s par clé (chain, address_lower OR symbol_upper).
# Thread-safe (lock). Fallback sur entrée stale (≤10 min) si DexScreener
# devient injoignable temporairement — mieux que None.
#
# Stdlib only (urllib) pour rester cohérent avec hyperliquid.py.

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


# ── Whitelists ────────────────────────────────────────────────────────────
# Stables : tout map vers $1.0. Couvre les principaux + variantes par chain
# (ex: USDC.e sur Arbitrum/Avalanche, USDT.e, USDbC sur Base).
_STABLES = {
    "USDC", "USDT", "DAI", "USDC.E", "USDT.E", "USDBC",
    "FDUSD", "TUSD", "USDE", "PYUSD", "FRAX", "LUSD", "GUSD",
    "BUSD", "MIM", "USDD", "USDP",
}

# Natifs/wrapped → CoinGecko simple/price ids. Couvre les chains v1 + ETH/BTC
# qui circulent partout en bridged.
_NATIVE_TO_CG_ID = {
    "ETH": "ethereum", "WETH": "ethereum",
    "STETH": "ethereum", "WSTETH": "ethereum", "RETH": "ethereum", "CBETH": "ethereum",
    "BTC": "bitcoin", "WBTC": "bitcoin", "BTCB": "bitcoin", "TBTC": "bitcoin",
    "BNB": "binancecoin", "WBNB": "binancecoin",
    "MATIC": "matic-network", "WMATIC": "matic-network", "POL": "matic-network",
    "AVAX": "avalanche-2", "WAVAX": "avalanche-2",
    "SOL": "solana", "WSOL": "solana",
}

# Mapping nom de chain projet → slug DexScreener. Slugs vérifiés sur leur doc.
_CHAIN_TO_DEXSCREENER = {
    "ethereum": "ethereum",
    "arbitrum": "arbitrum",
    "bnb":      "bsc",
    "base":     "base",
    "optimism": "optimism",
    "polygon":  "polygon",
    "avalanche": "avalanche",
}

# Liquidité minimum acceptée d'un pool DexScreener (USD). En dessous, le
# prix peut être manipulé. $10K est conservateur — un trade smart de $1K
# n'a pas vraiment d'intérêt si le pool n'a que $5K de TVL.
MIN_POOL_LIQUIDITY_USD = 10_000

_CACHE_TTL = 60       # seconds
_STALE_OK_TTL = 600   # seconds — fallback si DexScreener tombe
_HTTP_TIMEOUT = 6
_USER_AGENT = "WhaleWatch-TokenPricer/1.0 (+https://whalewatchapp.io)"

# Cache : key=str → {"price": float, "ts": float}
_CACHE = {}
_LOCK = threading.Lock()


# ── HTTP helpers (urllib stdlib) ───────────────────────────────────────────
def _get_json(url, timeout=_HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _cached_or_fetch(key, fetch_fn):
    """Renvoie le prix caché si frais, sinon appelle fetch_fn(). Fallback
    sur stale entry si fetch_fn raise."""
    now = time.time()
    with _LOCK:
        entry = _CACHE.get(key)
        if entry and (now - entry["ts"]) < _CACHE_TTL:
            return entry["price"]
    try:
        price = fetch_fn()
    except Exception:
        # Fallback stale
        with _LOCK:
            entry = _CACHE.get(key)
            if entry and (now - entry["ts"]) < _STALE_OK_TTL:
                return entry["price"]
        return None
    if price is not None:
        with _LOCK:
            _CACHE[key] = {"price": float(price), "ts": now}
    return price


# ── CoinGecko (pour les natifs/wrapped) ───────────────────────────────────
# 1 seule URL pour toutes les natives en cache global. Évite N appels par
# tick. CoinGecko free tier : 10-30 req/min sans clé — largement suffisant
# avec cache 60s.
_CG_NATIVE_CACHE = {"data": None, "ts": 0.0}
_CG_LOCK = threading.Lock()
_CG_BATCH_TTL = 60


def _get_native_prices():
    """Renvoie un dict {cg_id: usd_price} pour les natives whitelistées.
    Cache 60s partagé entre toutes les chains."""
    now = time.time()
    with _CG_LOCK:
        if _CG_NATIVE_CACHE["data"] is not None and (now - _CG_NATIVE_CACHE["ts"]) < _CG_BATCH_TTL:
            return _CG_NATIVE_CACHE["data"]
    ids = sorted(set(_NATIVE_TO_CG_ID.values()))
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={','.join(ids)}&vs_currencies=usd")
    try:
        data = _get_json(url, timeout=_HTTP_TIMEOUT)
    except Exception:
        with _CG_LOCK:
            # Fallback stale jusqu'à 10 min
            if _CG_NATIVE_CACHE["data"] is not None and (now - _CG_NATIVE_CACHE["ts"]) < _STALE_OK_TTL:
                return _CG_NATIVE_CACHE["data"]
        return {}
    out = {cg_id: float(d.get("usd") or 0) for cg_id, d in data.items() if isinstance(d, dict)}
    with _CG_LOCK:
        _CG_NATIVE_CACHE["data"] = out
        _CG_NATIVE_CACHE["ts"] = now
    return out


def _price_native(symbol):
    """Prix d'un natif/wrapped via CoinGecko. None si pas dans la whitelist
    ou erreur réseau."""
    sym = (symbol or "").upper()
    cg_id = _NATIVE_TO_CG_ID.get(sym)
    if not cg_id:
        return None
    prices = _get_native_prices()
    p = prices.get(cg_id)
    return p if p and p > 0 else None


# ── DexScreener (pour le reste) ───────────────────────────────────────────
def _price_dexscreener(token_addr, chain):
    """Prix USD du token via DexScreener. Choisit le pool avec la plus
    grosse liquidité USD sur la chain demandée. Renvoie None si :
      - chain non supportée par DexScreener
      - token sans pool
      - tous les pools en-dessous de MIN_POOL_LIQUIDITY_USD
    """
    if not token_addr or not isinstance(token_addr, str):
        return None
    addr = token_addr.strip().lower()
    if not addr.startswith("0x") or len(addr) != 42:
        return None
    chain_slug = _CHAIN_TO_DEXSCREENER.get((chain or "").lower())
    if not chain_slug:
        return None
    url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
    try:
        data = _get_json(url, timeout=_HTTP_TIMEOUT)
    except Exception:
        return None
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    # Filtre : pool sur la bonne chain ET liquidité minimum
    best = None
    best_liq = 0
    for p in pairs:
        if (p.get("chainId") or "").lower() != chain_slug:
            continue
        liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq_usd < MIN_POOL_LIQUIDITY_USD:
            continue
        price_usd = p.get("priceUsd")
        try:
            price_val = float(price_usd) if price_usd is not None else 0
        except (TypeError, ValueError):
            continue
        if price_val <= 0:
            continue
        if liq_usd > best_liq:
            best_liq = liq_usd
            best = price_val
    return best


# ── API publique ──────────────────────────────────────────────────────────
def get_price_usd(token_addr, chain, symbol=None):
    """Prix USD d'un token donné (1 unité).

    token_addr : adresse du contrat (0x + 40 hex). Peut être None si symbol
                 est passé (utile pour les natifs comme ETH balance brute).
    chain      : nom de chain projet (ethereum/arbitrum/bnb/...).
    symbol     : ticker, utilisé pour la whitelist stables/natifs avant
                 l'appel DexScreener. Optionnel.

    Renvoie :
      - float > 0  : prix trouvé
      - None       : aucune source ne connaît ce token (trade ignoré côté feed)
    """
    sym = (symbol or "").upper()

    # 1. Stable
    if sym in _STABLES:
        return 1.0

    # 2. Native / wrapped (CoinGecko)
    if sym in _NATIVE_TO_CG_ID:
        key = f"native:{sym}"
        return _cached_or_fetch(key, lambda: _price_native(sym))

    # 3. DexScreener par adresse — clé inclut la chain car le même contrat
    # peut être déployé sur plusieurs chains à des prix différents (bridged).
    if token_addr:
        addr = token_addr.strip().lower()
        key = f"dex:{chain}:{addr}"
        return _cached_or_fetch(key, lambda: _price_dexscreener(addr, chain))

    return None


def clear_cache():
    """Pour les tests."""
    with _LOCK:
        _CACHE.clear()
    with _CG_LOCK:
        _CG_NATIVE_CACHE["data"] = None
        _CG_NATIVE_CACHE["ts"] = 0.0


if __name__ == "__main__":
    import sys
    addr = sys.argv[1] if len(sys.argv) > 1 else "0xdAC17F958D2ee523a2206206994597C13D831ec7"  # USDT
    chain = sys.argv[2] if len(sys.argv) > 2 else "ethereum"
    sym = sys.argv[3] if len(sys.argv) > 3 else None
    print(f"→ get_price_usd({addr=}, {chain=}, {sym=})")
    p = get_price_usd(addr, chain, sym)
    print(f"  price = {p}")
    print(f"\n→ Whitelist samples :")
    for s in ("USDC", "WETH", "WBTC", "ETH", "BNB", "WBNB", "UNKNOWN"):
        print(f"  {s:8s} → {get_price_usd(None, 'ethereum', s)}")
