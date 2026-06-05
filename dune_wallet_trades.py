# dune_wallet_trades.py
# Récupère le résumé des trades DEX d'un wallet via l'API Dune.
#
# Multi-chain : `chain` paramètre filtre dex.trades.blockchain ; sans ça,
# un wallet existant sur plusieurs chains verrait ses trades mélangés
# dans le modal de détail.

import os
import requests
import time

from chains import DEFAULT_CHAIN, resolve

DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "")
BASE = "https://api.dune.com/api/v1"
HEADERS = {"X-Dune-Api-Key": DUNE_API_KEY, "Content-Type": "application/json"}


def _execute_and_wait(sql, timeout=90):
    r = requests.post(f"{BASE}/sql/execute", json={"sql": sql}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    eid = r.json()["execution_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2.5)
        sr = requests.get(f"{BASE}/execution/{eid}/status", headers=HEADERS, timeout=15)
        state = sr.json().get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            rr = requests.get(f"{BASE}/execution/{eid}/results", headers=HEADERS, timeout=30)
            return rr.json().get("result", {}).get("rows", [])
        if state in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            raise RuntimeError(f"Dune query {state}: {sr.json()}")
    raise TimeoutError(f"Dune query timeout after {timeout}s")


def get_wallet_trade_summary(address: str, days: int = 7, chain: str = DEFAULT_CHAIN) -> dict:
    """
    Retourne un résumé complet des trades DEX d'un wallet pour une chain :
    - stats globales (nb trades, volume, avg, top paire, top DEX)
    - top 10 paires par volume
    - top 5 DEX utilisés
    - 30 derniers trades
    """
    # taker est varbinary dans dex.trades — comparaison directe sans lower()
    addr_hex = address if address.startswith("0x") else "0x" + address
    dune_blockchain = resolve(chain)["dune_blockchain"]

    # ── Query 1 : agrégat par paire × DEX ───────────────────────────────────────
    sql_summary = f"""
SELECT
  project,
  token_pair,
  token_bought_symbol,
  token_sold_symbol,
  COUNT(*)                         AS nb_trades,
  SUM(amount_usd)                  AS total_volume_usd,
  AVG(amount_usd)                  AS avg_trade_usd,
  MAX(block_time)                  AS last_trade,
  MIN(block_time)                  AS first_trade
FROM dex.trades
WHERE taker = {addr_hex}
  AND blockchain = '{dune_blockchain}'
  AND block_time > now() - interval '{days}' day
  AND amount_usd > 0
GROUP BY project, token_pair, token_bought_symbol, token_sold_symbol
ORDER BY total_volume_usd DESC
LIMIT 20
"""

    # ── Query 2 : derniers trades ────────────────────────────────────────────────
    sql_recent = f"""
SELECT
  block_time,
  blockchain,
  project,
  token_pair,
  token_bought_symbol,
  ROUND(CAST(token_bought_amount AS DECIMAL(38,6)), 4)  AS token_bought_amount,
  token_sold_symbol,
  ROUND(CAST(token_sold_amount AS DECIMAL(38,6)), 4)    AS token_sold_amount,
  ROUND(CAST(amount_usd AS DECIMAL(38,2)), 2)           AS amount_usd,
  tx_hash
FROM dex.trades
WHERE taker = {addr_hex}
  AND blockchain = '{dune_blockchain}'
  AND block_time > now() - interval '{days}' day
  AND amount_usd > 0
ORDER BY block_time DESC
LIMIT 30
"""

    summary_rows = _execute_and_wait(sql_summary)
    recent_rows  = _execute_and_wait(sql_recent)

    # ── Calcul des stats globales ────────────────────────────────────────────────
    total_volume = sum(r.get("total_volume_usd") or 0 for r in summary_rows)
    total_trades = sum(int(r.get("nb_trades") or 0) for r in summary_rows)
    avg_trade    = total_volume / total_trades if total_trades else 0

    # Top paires
    top_pairs = sorted(summary_rows, key=lambda r: r.get("total_volume_usd") or 0, reverse=True)

    # Top DEX
    dex_agg = {}
    for r in summary_rows:
        p = r.get("project") or "unknown"
        dex_agg[p] = dex_agg.get(p, 0) + (r.get("total_volume_usd") or 0)
    top_dexes = sorted(dex_agg.items(), key=lambda x: x[1], reverse=True)[:5]

    # Chaînes utilisées
    chains_seen = set()
    for r in recent_rows:
        c = r.get("blockchain")
        if c:
            chains_seen.add(c)

    return {
        "address": address,
        "days": days,
        "total_volume_usd": round(total_volume, 2),
        "total_trades": total_trades,
        "avg_trade_usd": round(avg_trade, 2),
        "top_pairs": [
            {
                "pair": r.get("token_pair") or f'{r.get("token_bought_symbol")}/{r.get("token_sold_symbol")}',
                "project": r.get("project"),
                "nb_trades": int(r.get("nb_trades") or 0),
                "volume_usd": round(r.get("total_volume_usd") or 0, 2),
                "avg_usd": round(r.get("avg_trade_usd") or 0, 2),
                "last_trade": str(r.get("last_trade") or ""),
            }
            for r in top_pairs[:10]
        ],
        "top_dexes": [{"project": p, "volume_usd": round(v, 2)} for p, v in top_dexes],
        "chains": sorted(chains_seen),
        "recent_trades": [
            {
                "time": str(r.get("block_time") or ""),
                "chain": r.get("blockchain") or "",
                "project": r.get("project") or "",
                "pair": r.get("token_pair") or "",
                "bought_symbol": r.get("token_bought_symbol") or "",
                "bought_amount": float(r.get("token_bought_amount") or 0),
                "sold_symbol": r.get("token_sold_symbol") or "",
                "sold_amount": float(r.get("token_sold_amount") or 0),
                "amount_usd": float(r.get("amount_usd") or 0),
                "tx_hash": r.get("tx_hash") or "",
            }
            for r in recent_rows
        ],
    }


if __name__ == "__main__":
    import json, sys
    addr = sys.argv[1] if len(sys.argv) > 1 else "0x51c72848c68a965f66fa7a88855f9f7784502a7f"
    print(f"Analyse trades pour {addr}...")
    result = get_wallet_trade_summary(addr)
    print(f"Volume: ${result['total_volume_usd']:,.0f}  |  Trades: {result['total_trades']:,}  |  Avg: ${result['avg_trade_usd']:,.0f}")
    print("\nTop paires:")
    for p in result["top_pairs"][:5]:
        print(f"  {p['pair']:30s}  ${p['volume_usd']:>14,.0f}  {p['nb_trades']:>6} trades  via {p['project']}")
    print("\nTop DEX:")
    for d in result["top_dexes"]:
        print(f"  {d['project']:20s}  ${d['volume_usd']:>14,.0f}")
    print("\nDerniers trades:")
    for t in result["recent_trades"][:5]:
        print(f"  {t['time'][:16]}  {t['bought_amount']} {t['bought_symbol']} ← {t['sold_amount']} {t['sold_symbol']}  ${t['amount_usd']:,.2f}  [{t['project']}]")
