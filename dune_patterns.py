# dune_patterns.py
# Analyse les patterns de trading communs sur les top wallets via Dune.
# Utilise un CTE subquery — pas besoin de cache préalable, scalable à N wallets.
#
# Multi-chain : le paramètre `chain` injecte le filtre blockchain dans toutes
# les sous-requêtes (top_wallets CTE + 4 queries d'analyse). Le cache de
# sortie est dérivé de chains.resolve : patterns.json (ETH rétrocompat),
# patterns_arbitrum.json, patterns_base.json, patterns_optimism.json.

import json
import os
import requests
import time

from chains import DEFAULT_CHAIN, resolve

DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "")
BASE = "https://api.dune.com/api/v1"
HEADERS = {"X-Dune-Api-Key": DUNE_API_KEY, "Content-Type": "application/json"}
# Path rétrocompat (Ethereum) ; les autres chains passent par chains.resolve
CACHE_FILE = os.path.join(os.path.dirname(__file__), "cache", "patterns.json")


def _execute_and_wait(sql, timeout=180):
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
            raise RuntimeError(f"Dune {state}: {sr.json().get('error', {})}")
    raise TimeoutError(f"Dune timeout après {timeout}s")


def analyze_patterns(n_wallets: int = 100, days: int = 7, progress_cb=None,
                     chain: str = DEFAULT_CHAIN):
    """
    Analyse les patterns des top N wallets DEX sur la chain donnée.

    Utilise un CTE (WITH top_wallets AS ...) pour identifier les top N wallets
    directement via Dune — aucune dépendance au cache results.json.

    Args:
        n_wallets: nombre de wallets à analyser (défaut 100, max ~500)
        days: fenêtre temporelle en jours
        progress_cb: callback(msg) pour les mises à jour de statut
        chain: nom de la chain (ethereum, arbitrum, base, optimism)
    """
    chain_cfg = resolve(chain)
    dune_blockchain = chain_cfg["dune_blockchain"]
    cache_path = chain_cfg["patterns_path"]

    def log(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    log(f"Analyse patterns — top {n_wallets} wallets · {days}j · {chain_cfg['label']}")

    # ── CTE commun : top N wallets par volume sur la période ────────────────────
    top_wallets_cte = f"""
WITH top_wallets AS (
  SELECT taker
  FROM dex.trades
  WHERE block_time > now() - interval '{days}' day
    AND blockchain = '{dune_blockchain}'
    AND amount_usd > 0
    AND taker IS NOT NULL
  GROUP BY taker
  ORDER BY SUM(amount_usd) DESC
  LIMIT {n_wallets}
)"""

    # ── Q1 : Top paires × DEX ───────────────────────────────────────────────────
    log("1/4 · Top paires × DEX…")
    sql_pairs = top_wallets_cte + f"""
SELECT
  t.token_pair,
  t.project,
  COUNT(DISTINCT t.taker)                    AS wallet_count,
  COUNT(*)                                   AS total_trades,
  SUM(t.amount_usd)                          AS total_volume_usd,
  AVG(t.amount_usd)                          AS avg_trade_usd,
  approx_percentile(t.amount_usd, 0.5)       AS median_trade_usd
FROM dex.trades t
JOIN top_wallets tw ON t.taker = tw.taker
WHERE t.block_time > now() - interval '{days}' day
  AND t.amount_usd > 0
GROUP BY t.token_pair, t.project
ORDER BY total_volume_usd DESC
LIMIT 20
"""

    # ── Q2 : Top DEX ────────────────────────────────────────────────────────────
    log("2/4 · Top DEX…")
    sql_dex = top_wallets_cte + f"""
SELECT
  t.project,
  COUNT(DISTINCT t.taker)  AS wallet_count,
  COUNT(*)                 AS total_trades,
  SUM(t.amount_usd)        AS total_volume_usd,
  AVG(t.amount_usd)        AS avg_trade_usd
FROM dex.trades t
JOIN top_wallets tw ON t.taker = tw.taker
WHERE t.block_time > now() - interval '{days}' day
  AND t.amount_usd > 0
GROUP BY t.project
ORDER BY total_volume_usd DESC
LIMIT 10
"""

    # ── Q3 : Activité horaire ────────────────────────────────────────────────────
    log("3/4 · Activité horaire…")
    sql_hours = top_wallets_cte + f"""
SELECT
  hour(t.block_time)        AS hour_utc,
  COUNT(*)                  AS total_trades,
  SUM(t.amount_usd)         AS total_volume_usd,
  COUNT(DISTINCT t.taker)   AS active_wallets
FROM dex.trades t
JOIN top_wallets tw ON t.taker = tw.taker
WHERE t.block_time > now() - interval '{days}' day
  AND t.amount_usd > 0
GROUP BY hour(t.block_time)
ORDER BY hour_utc
"""

    # ── Q4 : Distribution tailles ────────────────────────────────────────────────
    log("4/4 · Distribution tailles…")
    sql_sizes = top_wallets_cte + f"""
SELECT
  CASE
    WHEN t.amount_usd < 1000     THEN '< $1K'
    WHEN t.amount_usd < 10000    THEN '$1K–$10K'
    WHEN t.amount_usd < 100000   THEN '$10K–$100K'
    WHEN t.amount_usd < 1000000  THEN '$100K–$1M'
    ELSE '> $1M'
  END AS size_bucket,
  COUNT(*)                       AS trade_count,
  SUM(t.amount_usd)              AS total_volume_usd,
  COUNT(DISTINCT t.taker)        AS wallet_count
FROM dex.trades t
JOIN top_wallets tw ON t.taker = tw.taker
WHERE t.block_time > now() - interval '{days}' day
  AND t.amount_usd > 0
GROUP BY 1
ORDER BY MIN(t.amount_usd)
"""

    # ── Q5 : Prix d'entrée/sortie MEV sur WETH ──────────────────────────────────
    log("5/6 · Prix MEV WETH (buy vs sell)…")
    sql_mev_prices = top_wallets_cte + f"""
,
mev_wallets AS (
  SELECT t.taker
  FROM dex.trades t
  JOIN top_wallets tw ON t.taker = tw.taker
  WHERE t.block_time > now() - interval '{days}' day
    AND t.blockchain = '{dune_blockchain}'
  GROUP BY t.taker
  HAVING COUNT(*) >= 200
),
eth_ops AS (
  SELECT
    CASE
      WHEN t.token_bought_symbol = 'WETH' THEN 'BUY'
      WHEN t.token_sold_symbol   = 'WETH' THEN 'SELL'
    END AS direction,
    CASE
      WHEN t.token_bought_symbol = 'WETH'
        THEN CAST(t.amount_usd AS DOUBLE) / NULLIF(CAST(t.token_bought_amount AS DOUBLE), 0)
      ELSE
        CAST(t.amount_usd AS DOUBLE) / NULLIF(CAST(t.token_sold_amount   AS DOUBLE), 0)
    END AS price_usd,
    t.amount_usd
  FROM dex.trades t
  JOIN mev_wallets mw ON t.taker = mw.taker
  WHERE t.block_time > now() - interval '{days}' day
    AND t.blockchain = '{dune_blockchain}'
    AND (t.token_bought_symbol = 'WETH' OR t.token_sold_symbol = 'WETH')
    AND t.amount_usd BETWEEN 10 AND 20000000
)
SELECT
  direction,
  COUNT(*)                                   AS trade_count,
  COUNT(DISTINCT (SELECT taker FROM mev_wallets LIMIT 1)) AS mev_wallet_count,
  AVG(price_usd)                             AS avg_price,
  APPROX_PERCENTILE(price_usd, 0.1)          AS p10_price,
  APPROX_PERCENTILE(price_usd, 0.25)         AS p25_price,
  APPROX_PERCENTILE(price_usd, 0.5)          AS median_price,
  APPROX_PERCENTILE(price_usd, 0.75)         AS p75_price,
  APPROX_PERCENTILE(price_usd, 0.9)          AS p90_price,
  AVG(amount_usd)                            AS avg_trade_size_usd,
  SUM(amount_usd)                            AS total_volume_usd
FROM eth_ops
WHERE direction IS NOT NULL
  AND price_usd > 500
  AND price_usd < 50000
GROUP BY direction
ORDER BY direction
"""

    # ── Q6 : Temps de détention MEV (buy→sell WETH) ──────────────────────────────
    log("6/6 · Temps de détention MEV (hold time buy→sell)…")
    sql_hold_time = top_wallets_cte + f"""
,
mev_wallets AS (
  SELECT t.taker
  FROM dex.trades t
  JOIN top_wallets tw ON t.taker = tw.taker
  WHERE t.block_time > now() - interval '{days}' day
    AND t.blockchain = '{dune_blockchain}'
  GROUP BY t.taker
  HAVING COUNT(*) >= 200
),
eth_trades_ordered AS (
  SELECT
    t.taker,
    t.block_time,
    CASE
      WHEN t.token_bought_symbol = 'WETH' THEN 'BUY'
      ELSE 'SELL'
    END AS direction
  FROM dex.trades t
  JOIN mev_wallets mw ON t.taker = mw.taker
  WHERE t.block_time > now() - interval '{days}' day
    AND t.blockchain = '{dune_blockchain}'
    AND (t.token_bought_symbol = 'WETH' OR t.token_sold_symbol = 'WETH')
    AND t.amount_usd BETWEEN 10 AND 20000000
),
with_lead AS (
  SELECT
    direction,
    block_time,
    LEAD(block_time) OVER (PARTITION BY taker ORDER BY block_time) AS next_time,
    LEAD(direction)  OVER (PARTITION BY taker ORDER BY block_time) AS next_dir
  FROM eth_trades_ordered
)
SELECT
  CASE
    WHEN date_diff('second', block_time, next_time) <= 13   THEN '≤1 bloc (≤13s)'
    WHEN date_diff('second', block_time, next_time) <= 60   THEN '14s–1min'
    WHEN date_diff('second', block_time, next_time) <= 300  THEN '1–5min'
    WHEN date_diff('second', block_time, next_time) <= 3600 THEN '5min–1h'
    WHEN date_diff('second', block_time, next_time) <= 86400 THEN '1h–24h'
    ELSE '> 24h'
  END                   AS hold_bucket,
  COUNT(*)              AS flip_count,
  AVG(CAST(date_diff('second', block_time, next_time) AS DOUBLE))              AS avg_hold_sec,
  APPROX_PERCENTILE(CAST(date_diff('second', block_time, next_time) AS DOUBLE), 0.5) AS median_hold_sec
FROM with_lead
WHERE direction   = 'BUY'
  AND next_dir    = 'SELL'
  AND next_time   IS NOT NULL
  AND date_diff('second', block_time, next_time) >= 0
GROUP BY 1
ORDER BY MIN(date_diff('second', block_time, next_time))
"""

    pairs_rows = _execute_and_wait(sql_pairs)
    dex_rows   = _execute_and_wait(sql_dex)
    hours_rows = _execute_and_wait(sql_hours)
    sizes_rows = _execute_and_wait(sql_sizes)
    try:
        mev_price_rows = _execute_and_wait(sql_mev_prices, timeout=240)
    except Exception as e:
        log(f"  ⚠ MEV prices query failed: {e}")
        mev_price_rows = []
    try:
        hold_time_rows = _execute_and_wait(sql_hold_time, timeout=240)
    except Exception as e:
        log(f"  ⚠ Hold time query failed: {e}")
        hold_time_rows = []

    log("Calcul des insights…")

    # ── Stats globales ───────────────────────────────────────────────────────────
    total_trades_real = sum(int(r.get("trade_count") or 0) for r in sizes_rows)
    total_vol_real    = sum(float(r.get("total_volume_usd") or 0) for r in sizes_rows)

    # Paire dominante
    top_pair = pairs_rows[0] if pairs_rows else {}

    # DEX concentration
    dex_total_vol = sum(float(r.get("total_volume_usd") or 0) for r in dex_rows)
    top_dex = dex_rows[0] if dex_rows else {}
    top2_vol = sum(float(dex_rows[i].get("total_volume_usd") or 0) for i in range(min(2, len(dex_rows))))
    top2_pct = top2_vol / dex_total_vol * 100 if dex_total_vol else 0

    # Heure de pointe
    peak_hour = max(hours_rows, key=lambda r: r.get("total_trades") or 0, default={})

    # Taille dominante
    top_bucket    = max(sizes_rows, key=lambda r: int(r.get("trade_count") or 0),      default={})
    bucket_by_vol = max(sizes_rows, key=lambda r: float(r.get("total_volume_usd") or 0), default={})
    top_count_pct = int(top_bucket.get("trade_count") or 0) / max(total_trades_real, 1) * 100
    top_vol_bkt   = float(bucket_by_vol.get("total_volume_usd") or 0)
    top_vol_pct   = top_vol_bkt / max(total_vol_real, 1) * 100

    # Insight "long tail" : les trades > $1M représentent quel % du volume ?
    big_trades = next((r for r in sizes_rows if r.get("size_bucket") == "> $1M"), {})
    big_vol_pct = float(big_trades.get("total_volume_usd") or 0) / max(total_vol_real, 1) * 100
    big_count   = int(big_trades.get("trade_count") or 0)

    insights = []

    if top_pair.get("token_pair"):
        pct_wallets = int(top_pair.get("wallet_count") or 0) / n_wallets * 100
        insights.append({
            "icon": "🔁", "type": "pair",
            "title": f"Paire dominante : {top_pair['token_pair']}",
            "detail": f"présente chez {int(top_pair.get('wallet_count',0))}/{n_wallets} wallets ({pct_wallets:.0f}%) · via {top_pair.get('project','?')} · avg {_fmt(float(top_pair.get('avg_trade_usd') or 0))}/trade"
        })

    if top_dex.get("project"):
        insights.append({
            "icon": "🏛️", "type": "dex",
            "title": f"Concentration DEX : top 2 = {top2_pct:.0f}% du volume",
            "detail": f"{top_dex.get('project')} domine — {int(top_dex.get('wallet_count',0))}/{n_wallets} wallets · {int(top_dex.get('total_trades',0)):,} trades"
        })

    if peak_hour.get("hour_utc") is not None:
        h = int(peak_hour["hour_utc"])
        insights.append({
            "icon": "⏰", "type": "time",
            "title": f"Pic d'activité : {h:02d}h–{(h+1)%24:02d}h UTC",
            "detail": f"{int(peak_hour.get('total_trades',0)):,} trades · {int(peak_hour.get('active_wallets',0))}/{n_wallets} wallets actifs · vol {_fmt(float(peak_hour.get('total_volume_usd') or 0))}"
        })

    if top_bucket.get("size_bucket"):
        insights.append({
            "icon": "📏", "type": "size",
            "title": f"Taille majoritaire : {top_bucket['size_bucket']}",
            "detail": f"{top_count_pct:.0f}% des trades par nombre · volume dominant : {bucket_by_vol.get('size_bucket','?')} ({top_vol_pct:.0f}% du vol)"
        })

    if big_count > 0:
        insights.append({
            "icon": "🐋", "type": "whale",
            "title": f"Baleines : {big_count:,} trades > $1M",
            "detail": f"représentent {big_vol_pct:.0f}% du volume total malgré {big_count/max(total_trades_real,1)*100:.2f}% des trades"
        })

    # ── MEV price levels ─────────────────────────────────────────────────────────
    mev_buy  = next((r for r in mev_price_rows if r.get("direction") == "BUY"),  {})
    mev_sell = next((r for r in mev_price_rows if r.get("direction") == "SELL"), {})

    def _f(r, k): return round(float(r.get(k) or 0), 2)

    mev_levels = None
    if mev_buy and mev_sell:
        buy_med  = _f(mev_buy,  "median_price")
        sell_med = _f(mev_sell, "median_price")
        spread   = round(sell_med - buy_med, 2)
        spread_bps = round(spread / buy_med * 10000, 1) if buy_med else 0
        mev_levels = {
            "asset": "WETH",
            "buy": {
                "trade_count":    int(mev_buy.get("trade_count") or 0),
                "avg_price":      _f(mev_buy, "avg_price"),
                "p10":            _f(mev_buy, "p10_price"),
                "p25":            _f(mev_buy, "p25_price"),
                "median":         buy_med,
                "p75":            _f(mev_buy, "p75_price"),
                "p90":            _f(mev_buy, "p90_price"),
                "avg_trade_size": _f(mev_buy, "avg_trade_size_usd"),
                "total_volume":   _f(mev_buy, "total_volume_usd"),
            },
            "sell": {
                "trade_count":    int(mev_sell.get("trade_count") or 0),
                "avg_price":      _f(mev_sell, "avg_price"),
                "p10":            _f(mev_sell, "p10_price"),
                "p25":            _f(mev_sell, "p25_price"),
                "median":         sell_med,
                "p75":            _f(mev_sell, "p75_price"),
                "p90":            _f(mev_sell, "p90_price"),
                "avg_trade_size": _f(mev_sell, "avg_trade_size_usd"),
                "total_volume":   _f(mev_sell, "total_volume_usd"),
            },
            "spread_usd":  spread,
            "spread_bps":  spread_bps,
        }
        arrow = "↑" if spread > 0 else "↓"
        insights.append({
            "icon": "⚡", "type": "mev_price",
            "title": f"Prix MEV WETH — buy {_price(buy_med)} / sell {_price(sell_med)}",
            "detail": f"spread {arrow}${abs(spread):.2f} ({spread_bps:+.1f} bps) · {int(mev_buy.get('trade_count',0))+int(mev_sell.get('trade_count',0)):,} trades MEV · wallets ≥200 trades/7j"
        })

    # ── Hold time distribution ────────────────────────────────────────────────────
    total_flips = sum(int(r.get("flip_count") or 0) for r in hold_time_rows)
    hold_dist = []
    for r in hold_time_rows:
        cnt  = int(r.get("flip_count") or 0)
        hold_dist.append({
            "bucket":          r.get("hold_bucket") or "?",
            "flip_count":      cnt,
            "pct":             round(cnt / max(total_flips, 1) * 100, 1),
            "avg_hold_sec":    round(float(r.get("avg_hold_sec") or 0), 1),
            "median_hold_sec": round(float(r.get("median_hold_sec") or 0), 1),
        })

    # Insight hold time
    if hold_dist:
        ultra_fast = next((r for r in hold_dist if "bloc" in r["bucket"]), {})
        fast_pct   = ultra_fast.get("pct", 0)
        med_row    = next((r for r in hold_dist if r["pct"] == max(x["pct"] for x in hold_dist)), hold_dist[0])
        cum_fast   = sum(r["pct"] for r in hold_dist if r["avg_hold_sec"] <= 300)
        insights.append({
            "icon": "⏱", "type": "hold_time",
            "title": f"Détention MEV : {fast_pct:.0f}% flips dans le même bloc (≤13s)",
            "detail": f"{cum_fast:.0f}% des buy→sell WETH sous 5min · tranche dominante : {med_row['bucket']} · {total_flips:,} paires buy→sell analysées"
        })

    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_wallets": n_wallets,
        "days": days,
        "total_trades": total_trades_real,
        "total_volume_usd": round(total_vol_real, 2),
        "insights": insights,
        "mev_price_levels": mev_levels,
        "hold_time_distribution": hold_dist,
        "top_pairs": [
            {
                "pair":        r.get("token_pair") or "?",
                "project":     r.get("project") or "?",
                "wallet_count":int(r.get("wallet_count") or 0),
                "total_trades":int(r.get("total_trades") or 0),
                "volume_usd":  float(r.get("total_volume_usd") or 0),
                "avg_usd":     float(r.get("avg_trade_usd") or 0),
                "median_usd":  float(r.get("median_trade_usd") or 0),
            } for r in pairs_rows
        ],
        "top_dexes": [
            {
                "project":     r.get("project") or "?",
                "wallet_count":int(r.get("wallet_count") or 0),
                "total_trades":int(r.get("total_trades") or 0),
                "volume_usd":  float(r.get("total_volume_usd") or 0),
                "avg_usd":     float(r.get("avg_trade_usd") or 0),
            } for r in dex_rows
        ],
        "hourly_activity": [
            {
                "hour":          int(r.get("hour_utc") or 0),
                "trades":        int(r.get("total_trades") or 0),
                "volume_usd":    float(r.get("total_volume_usd") or 0),
                "active_wallets":int(r.get("active_wallets") or 0),
            } for r in sorted(hours_rows, key=lambda r: int(r.get("hour_utc") or 0))
        ],
        "size_distribution": [
            {
                "bucket":      r.get("size_bucket") or "?",
                "trade_count": int(r.get("trade_count") or 0),
                "trade_pct":   round(int(r.get("trade_count") or 0) / max(total_trades_real, 1) * 100, 1),
                "volume_usd":  float(r.get("total_volume_usd") or 0),
                "vol_pct":     round(float(r.get("total_volume_usd") or 0) / max(total_vol_real, 1) * 100, 1),
                "wallet_count":int(r.get("wallet_count") or 0),
            } for r in sizes_rows
        ],
    }

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(result, f)

    log(f"✓ Patterns sauvegardés ({n_wallets} wallets, {total_trades_real:,} trades, {_fmt(total_vol_real)} vol)")
    return result


def _fmt(v):
    if v >= 1e9: return f"${v/1e9:.2f}B"
    if v >= 1e6: return f"${v/1e6:.2f}M"
    if v >= 1e3: return f"${v/1e3:.1f}K"
    return f"${v:.0f}"

def _price(v):
    """Format a token price like $2,045.30"""
    return f"${v:,.2f}"


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    res = analyze_patterns(n_wallets=n, days=days)
    print(f"\n{'='*60}")
    print(f"TOP {n} WALLETS — {days}J — {res['total_trades']:,} trades — {_fmt(res['total_volume_usd'])}")
    print(f"{'='*60}")
    for ins in res["insights"]:
        print(f"\n  {ins['icon']}  {ins['title']}\n     {ins['detail']}")
    print(f"\n{'─'*60}\nTOP PAIRES")
    for p in res["top_pairs"][:6]:
        print(f"  {p['pair']:28s} {p['wallet_count']:3d}w  {_fmt(p['volume_usd']):>10}  {p['project']}")
    print(f"\n{'─'*60}\nTOP DEX")
    for d in res["top_dexes"][:6]:
        print(f"  {d['project']:20s} {d['wallet_count']:3d}w  {_fmt(d['volume_usd']):>10}")
    print(f"\n{'─'*60}\nTAILLES")
    for s in res["size_distribution"]:
        print(f"  {s['bucket']:15s} {s['trade_pct']:5.1f}% des trades  {s['vol_pct']:5.1f}% du vol  {_fmt(s['volume_usd']):>10}")
