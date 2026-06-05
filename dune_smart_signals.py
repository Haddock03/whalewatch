# dune_smart_signals.py
# Enrichit les top wallets avec des signaux comportementaux utilisés par le
# Smart Money Score (back-end) : jours actifs, diversité DEX/tokens, net flow
# ETH approximatif, ratio buy/sell. Une seule requête Dune (chunked IN(...)).
#
# Multi-chain : le filtre WHERE inclut blockchain = '{chain}' pour ne pas
# mélanger les trades cross-chain d'un même wallet (un EOA existe souvent
# sur plusieurs chains).
import os
import time
import requests

from chains import DEFAULT_CHAIN, resolve

DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "")
BASE = "https://api.dune.com/api/v1"
HEADERS = {"X-Dune-Api-Key": DUNE_API_KEY, "Content-Type": "application/json"}


def _execute_and_wait(sql, timeout=120):
    r = requests.post(f"{BASE}/sql/execute", json={"sql": sql}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    eid = r.json()["execution_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2.5)
        s = requests.get(f"{BASE}/execution/{eid}/status", headers=HEADERS, timeout=15).json()
        st = s.get("state", "")
        if st == "QUERY_STATE_COMPLETED":
            res = requests.get(f"{BASE}/execution/{eid}/results", headers=HEADERS, timeout=30).json()
            return res.get("result", {}).get("rows", [])
        if st in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            raise RuntimeError(f"Dune {st}: {s}")
    raise TimeoutError(f"Dune timeout after {timeout}s")


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def fetch_smart_signals(addresses, days=7, chunk_size=50, progress_cb=None,
                        chain=DEFAULT_CHAIN):
    """
    Pour une liste d'adresses, retourne un dict { addr_lower: { signal: value, ... } }
    contenant : active_days, distinct_dex, distinct_tokens, buy_vol_usd, sell_vol_usd,
    eth_buy_usd, eth_sell_usd, net_eth_usd, stable_buy_usd, stable_sell_usd, max_day_vol.

    Une requête Dune par chunk de N adresses pour rester sous la limite du IN(...).
    Les adresses doivent être en hex lower-case sans '0x' duplication.

    `chain` filtre dex.trades.blockchain pour éviter de mélanger les
    trades cross-chain d'un même wallet.
    """
    if not addresses:
        return {}
    dune_blockchain = resolve(chain)["dune_blockchain"]

    # Normalisation : dex.trades.taker est varbinary → format X'...' attendu
    def _normalize(addr):
        a = addr.lower()
        return a if a.startswith("0x") else "0x" + a

    out = {}
    total_chunks = (len(addresses) + chunk_size - 1) // chunk_size
    for ci, chunk in enumerate(_chunks(addresses, chunk_size), 1):
        if progress_cb:
            progress_cb(f"Smart signals chunk {ci}/{total_chunks} ({len(chunk)} wallets)")
        in_list = ", ".join(_normalize(a) for a in chunk)
        sql = f"""
WITH base AS (
  SELECT
    LOWER(CAST(taker AS VARCHAR))            AS addr,
    DATE(block_time)                          AS day,
    project,
    token_bought_symbol                       AS sym_buy,
    token_sold_symbol                         AS sym_sell,
    amount_usd
  FROM dex.trades
  WHERE taker IN ({in_list})
    AND blockchain = '{dune_blockchain}'
    AND block_time > now() - interval '{days}' day
    AND amount_usd > 0
),
agg AS (
  SELECT
    addr,
    COUNT(DISTINCT day)                       AS active_days,
    COUNT(DISTINCT project)                   AS distinct_dex,
    COUNT(DISTINCT sym_buy) + COUNT(DISTINCT sym_sell) AS distinct_tokens_raw,
    SUM(CASE WHEN sym_buy IN ('WETH','ETH','stETH','wstETH','rETH','cbETH')
             THEN amount_usd ELSE 0 END)      AS eth_buy_usd,
    SUM(CASE WHEN sym_sell IN ('WETH','ETH','stETH','wstETH','rETH','cbETH')
             THEN amount_usd ELSE 0 END)      AS eth_sell_usd,
    SUM(CASE WHEN sym_buy IN ('USDC','USDT','DAI','USDC.e','FDUSD','TUSD','USDe','PYUSD')
             THEN amount_usd ELSE 0 END)      AS stable_buy_usd,
    SUM(CASE WHEN sym_sell IN ('USDC','USDT','DAI','USDC.e','FDUSD','TUSD','USDe','PYUSD')
             THEN amount_usd ELSE 0 END)      AS stable_sell_usd,
    SUM(amount_usd)                           AS total_vol_usd
  FROM base GROUP BY addr
),
peak AS (
  SELECT addr, MAX(day_vol) AS max_day_vol
  FROM (SELECT addr, day, SUM(amount_usd) AS day_vol FROM base GROUP BY addr, day) d
  GROUP BY addr
)
SELECT a.*, p.max_day_vol
FROM agg a LEFT JOIN peak p ON p.addr = a.addr
"""
        try:
            rows = _execute_and_wait(sql)
        except Exception as e:
            if progress_cb:
                progress_cb(f"Smart signals chunk {ci} failed: {e}")
            continue

        for r in rows:
            addr = (r.get("addr") or "").lower()
            if not addr:
                continue
            eth_buy = float(r.get("eth_buy_usd") or 0)
            eth_sell = float(r.get("eth_sell_usd") or 0)
            stable_buy = float(r.get("stable_buy_usd") or 0)
            stable_sell = float(r.get("stable_sell_usd") or 0)
            total_vol = float(r.get("total_vol_usd") or 0)
            max_day = float(r.get("max_day_vol") or 0)

            out[addr] = {
                "active_days": int(r.get("active_days") or 0),
                "distinct_dex": int(r.get("distinct_dex") or 0),
                "distinct_tokens": int(r.get("distinct_tokens_raw") or 0),
                "eth_buy_usd": round(eth_buy, 2),
                "eth_sell_usd": round(eth_sell, 2),
                "net_eth_usd": round(eth_buy - eth_sell, 2),
                "stable_buy_usd": round(stable_buy, 2),
                "stable_sell_usd": round(stable_sell, 2),
                "total_dex_vol_usd": round(total_vol, 2),
                "max_day_vol_usd": round(max_day, 2),
                "concentration": round(max_day / total_vol, 3) if total_vol else 0,
            }

    return out


if __name__ == "__main__":
    import json, sys
    addrs = sys.argv[1:] or [
        "0x51c72848c68a965f66fa7a88855f9f7784502a7f",
        "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13",
    ]
    res = fetch_smart_signals(addrs, days=7)
    print(json.dumps(res, indent=2))
