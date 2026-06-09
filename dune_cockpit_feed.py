# dune_cockpit_feed.py
# Récupère le feed intraday des trades DEX des top smart wallets pour
# alimenter le module Cockpit (fenêtre 60min par défaut).
#
# Différence avec dune_wallet_trades.py / dune_smart_signals.py :
#   - Granularité = trade individuel, pas agrégé. Le worker en a besoin
#     pour calculer la convergence (ms wallets distincts par token sur
#     30 min), la direction (buy/sell par trade), et la pondération $.
#   - Fenêtre très courte (1h) → la query est légère et chunked.
#   - Filtre côté SQL : amount_usd >= MIN_TRADE_USD (élimine le bruit micro).
#
# Pas d'auto-loop ici : le worker (cockpit_worker.py) appelle fetch_feed()
# à la cadence souhaitée.
import os
import time

import requests

from chains import DEFAULT_CHAIN, resolve

DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "")
BASE = "https://api.dune.com/api/v1"

# Seuil minimum pour qu'un trade entre dans le feed cockpit (USD).
# Filtre les micro-trades qui pollueraient la convergence sans information.
MIN_TRADE_USD = float(os.environ.get("COCKPIT_MIN_TRADE_USD", "1000"))

# Tokens "stable" et "natifs" qu'on EXCLUT de la liste des "tokens chauds" :
# convertir USDC↔WETH n'est pas un signal de conviction sur USDC ou WETH.
# On garde ces trades dans le feed mais on les classe « monetary base ».
_STABLES = {"USDC", "USDT", "DAI", "USDC.E", "FDUSD", "TUSD", "USDE",
            "PYUSD", "FRAX", "LUSD", "GUSD"}


def _headers():
    return {"X-Dune-Api-Key": DUNE_API_KEY, "Content-Type": "application/json"}


def _execute_and_wait(sql, timeout=120):
    r = requests.post(f"{BASE}/sql/execute", json={"sql": sql},
                      headers=_headers(), timeout=30)
    r.raise_for_status()
    eid = r.json()["execution_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2.5)
        s = requests.get(f"{BASE}/execution/{eid}/status",
                         headers=_headers(), timeout=15).json()
        st = s.get("state", "")
        if st == "QUERY_STATE_COMPLETED":
            res = requests.get(f"{BASE}/execution/{eid}/results",
                               headers=_headers(), timeout=30).json()
            return res.get("result", {}).get("rows", [])
        if st in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            raise RuntimeError(f"Dune {st}: {s}")
    raise TimeoutError(f"Dune timeout after {timeout}s")


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _normalize_addr(addr):
    a = addr.lower()
    return a if a.startswith("0x") else "0x" + a


def _classify_side(sym_buy, sym_sell):
    """Détermine la direction « directionnelle » du trade pour le token non-stable.
    Si on achète X en vendant un stable → side=buy pour X.
    Si on vend X pour acheter un stable → side=sell pour X.
    Si les 2 sont des stables ou les 2 sont du non-stable → side=neutre."""
    bu = (sym_buy or "").upper()
    se = (sym_sell or "").upper()
    bu_is_stable = bu in _STABLES
    se_is_stable = se in _STABLES
    if bu_is_stable and not se_is_stable:
        # On achète du stable en vendant X → SELL sur X
        return se, "sell"
    if se_is_stable and not bu_is_stable:
        # On vend du stable pour acheter X → BUY sur X
        return bu, "buy"
    # Crypto↔crypto : on garde token bought côté buy (heuristique courte v1)
    if bu and not bu_is_stable:
        return bu, "buy"
    if se and not se_is_stable:
        return se, "sell"
    return None, None


def fetch_feed(addresses, window_min=60, chain=DEFAULT_CHAIN,
               chunk_size=80, progress_cb=None):
    """Renvoie une liste de trades [{addr, token, side, usd, project,
    block_time}] pour les `addresses` données, fenêtre `window_min` minutes,
    sur la `chain`.

    addresses : liste de hex strings (peut être > chunk_size — on splite).
    Renvoie [] si la liste d'entrée est vide.
    """
    if not addresses:
        return []
    if not DUNE_API_KEY:
        raise RuntimeError("DUNE_API_KEY non configurée — feed cockpit impossible")

    chain_cfg = resolve(chain)
    dune_blockchain = chain_cfg["dune_blockchain"]

    out = []
    total_chunks = (len(addresses) + chunk_size - 1) // chunk_size
    for ci, chunk in enumerate(_chunks(addresses, chunk_size), 1):
        if progress_cb:
            progress_cb(f"Cockpit feed {chain} chunk {ci}/{total_chunks}")
        in_list = ", ".join(_normalize_addr(a) for a in chunk)
        sql = f"""
SELECT
  LOWER(CAST(taker AS VARCHAR))                              AS addr,
  block_time,
  project,
  token_bought_symbol                                         AS sym_buy,
  token_sold_symbol                                           AS sym_sell,
  ROUND(CAST(amount_usd AS DECIMAL(38,2)), 2)                AS usd
FROM dex.trades
WHERE taker IN ({in_list})
  AND blockchain = '{dune_blockchain}'
  AND block_time > now() - interval '{window_min}' minute
  AND amount_usd >= {MIN_TRADE_USD}
ORDER BY block_time DESC
LIMIT 5000
"""
        try:
            rows = _execute_and_wait(sql)
        except Exception as e:
            if progress_cb:
                progress_cb(f"Cockpit feed chunk {ci} failed: {e}")
            continue

        for r in rows:
            addr = (r.get("addr") or "").lower()
            sym_buy = r.get("sym_buy")
            sym_sell = r.get("sym_sell")
            token, side = _classify_side(sym_buy, sym_sell)
            if not token or not side:
                continue
            try:
                usd = float(r.get("usd") or 0)
            except (TypeError, ValueError):
                continue
            if usd < MIN_TRADE_USD:
                continue
            out.append({
                "addr": addr,
                "token": token,
                "side": side,
                "usd": round(usd, 2),
                "project": r.get("project") or "",
                "block_time": str(r.get("block_time") or ""),
            })

    return out


if __name__ == "__main__":
    import json, sys
    addrs = sys.argv[1:] or [
        "0x51c72848c68a965f66fa7a88855f9f7784502a7f",
        "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13",
    ]
    feed = fetch_feed(addrs, window_min=60, chain="ethereum",
                      progress_cb=lambda m: print(f"[feed] {m}"))
    print(json.dumps(feed[:10], indent=2))
    print(f"\nTotal trades fetched: {len(feed)}")
