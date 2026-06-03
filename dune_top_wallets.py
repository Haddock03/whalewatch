# dune_top_wallets.py
# Requête Dune Analytics pour top wallets DEX par volume

import os
import requests
import pandas as pd
import time

DUNE_API_KEY = os.environ.get("DUNE_API_KEY", "")
BASE = "https://api.dune.com/api/v1"
HEADERS = {"X-Dune-API-Key": DUNE_API_KEY}

QUERY_SQL = """
SELECT
    taker AS wallet,
    COUNT(*) AS nb_trades,
    SUM(amount_usd) AS total_volume_usd,
    AVG(amount_usd) AS avg_trade_size_usd,
    MIN(block_time) AS first_trade,
    MAX(block_time) AS last_trade
FROM dex.trades
WHERE block_time > NOW() - INTERVAL '7' DAY
    AND blockchain = 'ethereum'
    AND amount_usd > 0
    AND taker IS NOT NULL
GROUP BY taker
HAVING COUNT(*) >= 5
ORDER BY total_volume_usd DESC
LIMIT 200
"""


def execute_sql_direct(sql=QUERY_SQL):
    """Exécute une requête SQL directement via l'endpoint /sql/execute"""
    r = requests.post(
        f"{BASE}/sql/execute",
        json={"sql": sql},
        headers=HEADERS,
        timeout=30
    )
    r.raise_for_status()
    data = r.json()
    execution_id = data.get("execution_id")
    if not execution_id:
        raise ValueError(f"Pas d'execution_id dans la réponse: {data}")
    print(f"Exécution lancée: {execution_id}")
    return execution_id


def wait_for_results(execution_id, timeout=300, progress_cb=None):
    """Polling jusqu'à ce que la query soit terminée"""
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(
            f"{BASE}/execution/{execution_id}/status",
            headers=HEADERS,
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
        status = data.get("state", "")
        elapsed = int(time.time() - start)
        msg = f"[{elapsed}s] Statut Dune: {status}"
        print(msg)
        if progress_cb:
            progress_cb(msg)

        if status == "QUERY_STATE_COMPLETED":
            return True
        elif status in ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"):
            raise Exception(f"Query échouée: {data.get('error', 'Erreur inconnue')}")

        time.sleep(5)

    raise TimeoutError(f"Timeout dépassé ({timeout}s)")


def get_results(execution_id):
    """Récupère les résultats et retourne un DataFrame"""
    r = requests.get(
        f"{BASE}/execution/{execution_id}/results",
        headers=HEADERS,
        timeout=30
    )
    r.raise_for_status()
    rows = r.json().get("result", {}).get("rows", [])
    if not rows:
        print("  Aucun résultat retourné par Dune.")
        return pd.DataFrame()
    return pd.DataFrame(rows)


def fetch_top_wallets(progress_cb=None):
    """
    Pipeline complet : exécute le SQL, attend les résultats, retourne un DataFrame.
    progress_cb(msg) : callback optionnel pour les mises à jour de statut.
    """
    print("=== Dune Analytics - Top Wallets DEX ===")
    if progress_cb:
        progress_cb("Lancement de la requête Dune...")

    execution_id = execute_sql_direct()
    wait_for_results(execution_id, progress_cb=progress_cb)
    df = get_results(execution_id)

    if df.empty:
        return df

    # Colonnes numériques
    for col in ["total_volume_usd", "avg_trade_size_usd", "nb_trades"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df = df.rename(columns={"wallet": "address"})
    df = df.sort_values("total_volume_usd", ascending=False).reset_index(drop=True)
    print(f"  {len(df)} wallets récupérés depuis Dune.")
    return df


def format_volume(v):
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    elif v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    elif v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.2f}"


if __name__ == "__main__":
    df = fetch_top_wallets()
    if not df.empty:
        df["rank"] = range(1, len(df) + 1)
        df["volume_display"] = df["total_volume_usd"].apply(format_volume)
        print("\n=== TOP 20 WALLETS PAR VOLUME DEX (30j) ===")
        cols = [c for c in ["rank", "address", "nb_trades", "volume_display", "last_trade"] if c in df.columns]
        print(df[cols].head(20).to_string(index=False))
        df.to_csv("dune_top_wallets.csv", index=False)
        print(f"\nExporté: dune_top_wallets.csv ({len(df)} wallets)")
