# etherscan_cockpit_feed.py
# Feed intraday du Cockpit basé sur Etherscan V2 (et non Dune).
#
# Pourquoi : le free tier Dune était saturé par 60s × 3 chains = ~4300
# queries/jour. Etherscan V2 free supporte 5 req/s et 100k req/jour avec
# une seule clé multichain. Le `chainid` paramètre suffit pour switcher
# entre ethereum / arbitrum / bnb. La clé `ETHERSCAN_API_KEY` est déjà
# configurée pour les Sonars.
#
# Contrat de sortie IDENTIQUE à dune_cockpit_feed.fetch_feed pour ne
# casser AUCUN consumer en aval :
#   [{addr, token, side, usd, project, block_time}]
#
# Sémantique swap (validée avec utilisateur) :
#   - 1 stable + 1 crypto    → 1 entry directionnelle (BUY/SELL crypto)
#   - 2 crypto (pas stable)  → 2 entries (BUY token_in, SELL token_out)
#   - 2 stables              → skip (USDC↔USDT n'est pas un signal)
#   - 1 transfer seul        → skip (claim/airdrop, pas un swap)
#
# Pas de DUNE_API_KEY consommé. Pas d'import Dune nulle part.

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

from chains import DEFAULT_CHAIN, resolve as resolve_chain
import token_pricer


ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
BASE_URL = "https://api.etherscan.io/v2/api"

# Throttle inter-wallets pour rester sous le 5 req/s Etherscan free.
# Avec 0.25s/wallet et ~10 wallets par chain × 3 chains, on est à 7,5s
# par tick de worker — confortable sous le 60s d'intervalle.
WALLET_THROTTLE_SEC = float(os.environ.get("COCKPIT_ETHERSCAN_THROTTLE_SEC", "0.25"))
HTTP_TIMEOUT = 15

# Seuil minimum pour qu'un trade entre dans le feed cockpit (USD).
# Filtre les micro-trades qui pollueraient la convergence sans information.
MIN_TRADE_USD = float(os.environ.get("COCKPIT_MIN_TRADE_USD", "1000"))

# Nombre max de transferts ERC-20 fetchés par wallet (par appel tokentx).
# 200 = 1 page Etherscan, largement assez pour 1h sur des wallets actifs.
TOKENTX_OFFSET = int(os.environ.get("COCKPIT_TOKENTX_OFFSET", "200"))

# Sécurité : si un wallet a > N transferts dans une seule tx (LP add/remove,
# airdrops, claims multiples), on skip cette tx — ce n'est pas un trade DEX.
MAX_LEGS_PER_TX = 6

_USER_AGENT = "WhaleWatch-CockpitFeed/1.0 (+https://whalewatchapp.io)"

# Stables — même set que dune_cockpit_feed (cohérence sémantique).
_STABLES = {"USDC", "USDT", "DAI", "USDC.E", "USDT.E", "USDBC",
            "FDUSD", "TUSD", "USDE", "PYUSD", "FRAX", "LUSD", "GUSD",
            "BUSD", "MIM", "USDD", "USDP"}

# Lock global pour throttle entre threads (le worker tourne séquentiellement
# chain par chain mais on prévoit le cas multi-thread futur).
_LAST_CALL_TS = [0.0]
_THROTTLE_LOCK = threading.Lock()


def _throttle_wait():
    """Garde au moins WALLET_THROTTLE_SEC entre 2 appels Etherscan."""
    with _THROTTLE_LOCK:
        now = time.time()
        elapsed = now - _LAST_CALL_TS[0]
        if elapsed < WALLET_THROTTLE_SEC:
            time.sleep(WALLET_THROTTLE_SEC - elapsed)
        _LAST_CALL_TS[0] = time.time()


def _etherscan_get(params, chainid, retries=3):
    """GET Etherscan V2 avec backoff exponentiel sur rate limit.
    Renvoie le payload JSON ou {} en cas d'échec définitif."""
    params = dict(params)
    params["chainid"] = chainid
    params["apikey"] = ETHERSCAN_API_KEY
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    })
    delay = 1.0
    for attempt in range(retries):
        _throttle_wait()
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return {}
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return {}
        result = data.get("result")
        # Rate limit côté Etherscan se manifeste par status=0 + message texte
        if isinstance(result, str) and "Max rate limit" in result:
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            return {}
        # Notes : status=0 avec result=[] est valide (juste pas de tx récente)
        return data
    return {}


def get_token_transfers(address, chainid, offset=TOKENTX_OFFSET):
    """Renvoie la liste des transferts ERC-20 récents du wallet (sort=desc)."""
    data = _etherscan_get({
        "module": "account",
        "action": "tokentx",
        "address": address,
        "page": 1,
        "offset": offset,
        "sort": "desc",
    }, chainid=chainid)
    result = data.get("result")
    if isinstance(result, list):
        return result
    return []


def _is_stable(symbol):
    return (symbol or "").upper() in _STABLES


def _normalize_amount(raw_value, decimals):
    """Convertit la valeur brute Etherscan (string int) en float décimal."""
    try:
        raw = int(raw_value)
        dec = int(decimals)
    except (TypeError, ValueError):
        return 0.0
    if dec < 0 or dec > 36:
        return 0.0
    return raw / (10 ** dec)


def _block_time_iso(ts_str):
    """Convertit timeStamp Etherscan (UNIX seconds string) en ISO 8601 UTC."""
    try:
        ts = int(ts_str)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _swap_legs(transfers, wallet):
    """À partir des transferts d'une SEULE tx pour le wallet donné, retourne
    deux dicts (in_legs, out_legs) :
      in_legs[symbol]  = {"amount": float, "addr": contractAddress,
                          "decimals": int, "block_time": ts}
      out_legs[symbol] = idem

    Les multiples transferts du même token côté in (ou out) sont sommés
    (cas multi-hop avec router renvoyant en plusieurs étapes au wallet)."""
    w = wallet.lower()
    in_legs = defaultdict(lambda: {"amount": 0.0, "addr": None,
                                   "decimals": None, "block_time": None})
    out_legs = defaultdict(lambda: {"amount": 0.0, "addr": None,
                                    "decimals": None, "block_time": None})
    for t in transfers:
        sym = (t.get("tokenSymbol") or "").strip()
        if not sym:
            continue
        amount = _normalize_amount(t.get("value"), t.get("tokenDecimal"))
        if amount <= 0:
            continue
        contract = (t.get("contractAddress") or "").lower()
        ts = t.get("timeStamp")
        from_addr = (t.get("from") or "").lower()
        to_addr = (t.get("to") or "").lower()
        # Wallet reçoit
        if to_addr == w:
            d = in_legs[sym]
            d["amount"] += amount
            d["addr"] = d["addr"] or contract
            d["decimals"] = d["decimals"] or t.get("tokenDecimal")
            d["block_time"] = d["block_time"] or ts
        # Wallet envoie
        if from_addr == w:
            d = out_legs[sym]
            d["amount"] += amount
            d["addr"] = d["addr"] or contract
            d["decimals"] = d["decimals"] or t.get("tokenDecimal")
            d["block_time"] = d["block_time"] or ts
    return in_legs, out_legs


def _build_entries(wallet, chain, in_legs, out_legs, block_time_iso):
    """Construit les entries du feed pour 1 swap.

    Logique :
      - Si exactly 1 in_token et exactly 1 out_token et tokens différents :
          * stable+crypto  → 1 entry directionnelle sur le non-stable
          * crypto+crypto  → 2 entries (BUY token_in + SELL token_out)
          * 2 stables       → skip
      - Multi-token in OU multi-token out (multi-hop ou LP) :
          * Si exactly 1 in_token non-stable AND 0+ stables out → BUY non-stable
          * Si exactly 1 out_token non-stable AND 0+ stables in → SELL non-stable
          * Sinon (forme complexe) → skip pour la v1

    Renvoie une liste de 0..2 entries au format consommé en aval.
    """
    in_tokens = list(in_legs.keys())
    out_tokens = list(out_legs.keys())
    if not in_tokens or not out_tokens:
        # Pas un swap (déposit seul, claim seul, etc.)
        return []

    # Cas simple : 1 in + 1 out
    if len(in_tokens) == 1 and len(out_tokens) == 1:
        in_sym = in_tokens[0]
        out_sym = out_tokens[0]
        if in_sym == out_sym:
            return []  # same token in/out (wrap/unwrap dans le même token, skip)
        in_stable = _is_stable(in_sym)
        out_stable = _is_stable(out_sym)

        if in_stable and out_stable:
            return []  # USDC ↔ USDT — pas un signal

        if in_stable and not out_stable:
            # On a reçu stable en envoyant un crypto → SELL crypto
            return _maybe_entry(wallet, chain, block_time_iso,
                                token_sym=out_sym, token_leg=out_legs[out_sym],
                                side="sell")

        if out_stable and not in_stable:
            # On a envoyé stable en recevant un crypto → BUY crypto
            return _maybe_entry(wallet, chain, block_time_iso,
                                token_sym=in_sym, token_leg=in_legs[in_sym],
                                side="buy")

        # crypto ↔ crypto : 2 entries opposées
        entries = []
        entries += _maybe_entry(wallet, chain, block_time_iso,
                                token_sym=in_sym, token_leg=in_legs[in_sym],
                                side="buy")
        entries += _maybe_entry(wallet, chain, block_time_iso,
                                token_sym=out_sym, token_leg=out_legs[out_sym],
                                side="sell")
        return entries

    # Cas multi-leg : on accepte si exactly 1 non-stable d'un côté
    non_stable_in = [s for s in in_tokens if not _is_stable(s)]
    non_stable_out = [s for s in out_tokens if not _is_stable(s)]

    if len(non_stable_in) == 1 and len(non_stable_out) == 0:
        # Multi-stable → 1 crypto in : BUY crypto
        sym = non_stable_in[0]
        return _maybe_entry(wallet, chain, block_time_iso,
                            token_sym=sym, token_leg=in_legs[sym], side="buy")
    if len(non_stable_out) == 1 and len(non_stable_in) == 0:
        # 1 crypto out → multi-stable : SELL crypto
        sym = non_stable_out[0]
        return _maybe_entry(wallet, chain, block_time_iso,
                            token_sym=sym, token_leg=out_legs[sym], side="sell")
    # Forme complexe (LP add/remove, multi-asset bundle) → skip v1
    return []


def _maybe_entry(wallet, chain, block_time_iso, token_sym, token_leg, side):
    """Valorise et renvoie une entry [dict] ou [] si pricing impossible ou
    sous le seuil MIN_TRADE_USD."""
    amount = token_leg["amount"]
    if amount <= 0:
        return []
    addr = token_leg["addr"]
    price = token_pricer.get_price_usd(addr, chain, symbol=token_sym)
    if not price or price <= 0:
        return []
    usd = amount * price
    if usd < MIN_TRADE_USD:
        return []
    return [{
        "addr": wallet.lower(),
        "token": token_sym,
        "side": side,
        "usd": round(usd, 2),
        "project": "etherscan",  # le router exact n'est pas dispo dans tokentx
        "block_time": block_time_iso,
    }]


def fetch_feed(addresses, window_min=60, chain=DEFAULT_CHAIN,
               chunk_size=None, progress_cb=None):
    """Renvoie une liste de trades au format :
        [{addr, token, side, usd, project, block_time}]

    Signature IDENTIQUE à dune_cockpit_feed.fetch_feed pour drop-in replace
    dans cockpit_worker. `chunk_size` ignoré (pas pertinent ici car séquentiel
    par wallet) mais accepté pour rétrocompat.
    """
    if not addresses:
        return []
    if not ETHERSCAN_API_KEY:
        raise RuntimeError("ETHERSCAN_API_KEY non configurée — feed cockpit impossible")

    chain_cfg = resolve_chain(chain)
    chainid = chain_cfg["chainid"]

    cutoff_ts = time.time() - window_min * 60
    out = []
    total = len(addresses)

    for i, raw_addr in enumerate(addresses, 1):
        wallet = (raw_addr or "").lower()
        if not wallet:
            continue
        if progress_cb:
            progress_cb(f"Etherscan feed {chain} {i}/{total} {wallet[:10]}…")

        try:
            transfers = get_token_transfers(wallet, chainid=chainid)
        except Exception as e:
            if progress_cb:
                progress_cb(f"  wallet {wallet[:10]} failed: {e}")
            continue
        if not transfers:
            continue

        # Group by tx_hash, filtrer par fenêtre temporelle d'abord
        by_tx = defaultdict(list)
        for t in transfers:
            try:
                ts = int(t.get("timeStamp") or 0)
            except (TypeError, ValueError):
                continue
            if ts < cutoff_ts:
                # tokentx sort=desc → dès qu'on tombe sous cutoff on peut break
                break
            tx_hash = t.get("hash")
            if not tx_hash:
                continue
            by_tx[tx_hash].append(t)

        # Pour chaque tx : reconstruire swap + générer entries
        for tx_hash, legs in by_tx.items():
            if len(legs) > MAX_LEGS_PER_TX:
                # Trop de transferts dans la même tx → probable batch/LP, skip
                continue
            in_legs, out_legs = _swap_legs(legs, wallet)
            if not in_legs or not out_legs:
                continue
            # Block time = timestamp du premier transfert de la tx
            ts_str = legs[0].get("timeStamp")
            bt_iso = _block_time_iso(ts_str)
            out.extend(_build_entries(wallet, chain, in_legs, out_legs, bt_iso))

    return out


if __name__ == "__main__":
    import sys
    addrs = sys.argv[1:] or [
        "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13",
    ]
    chain = os.environ.get("WW_CHAIN", "ethereum")
    feed = fetch_feed(addrs, window_min=60, chain=chain,
                      progress_cb=lambda m: print(f"[feed] {m}"))
    print(json.dumps(feed[:10], indent=2))
    print(f"\nTotal trades fetched: {len(feed)} (chain={chain})")
