#!/usr/bin/env python3
"""
Pipeline d'analyse complet lancé en subprocess par server.py / POST /api/refresh.
Étapes :
  1. Top wallets DEX via Dune
  2. Enrichissement Etherscan + ranking → cache/results.json
  3. Patterns whales (MEV price levels, hold time, etc.) → cache/patterns.json

Le frontend a juste à cliquer « Sonar » : un seul pipeline, deux caches générés.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dune_top_wallets import fetch_top_wallets
from combine_and_rank import merge_and_rank, KNOWN_LABELS
from dune_patterns    import analyze_patterns


def log(msg):
    print(msg, flush=True)


def main(n_wallets: int = 100, pattern_days: int = 7):
    log("=== Démarrage de l'analyse ===")

    # 1. Top wallets via Dune
    log("Étape 1/3 — Récupération top wallets Dune…")
    df_dune = fetch_top_wallets(progress_cb=log)

    # 2. Etherscan + ranking → cache/results.json
    log("Étape 2/3 — Fusion Etherscan + ranking…")
    merge_and_rank(
        df_dune=df_dune,
        additional_wallets=list(KNOWN_LABELS.keys()),
        progress_cb=log,
    )

    # 3. Patterns whales → cache/patterns.json
    log(f"Étape 3/3 — Analyse patterns whales ({n_wallets} wallets, {pattern_days}j)…")
    try:
        analyze_patterns(n_wallets=n_wallets, days=pattern_days)
        log("✓ Patterns générés")
    except Exception as e:
        # Échec patterns ≠ échec total : les wallets sont déjà cachés
        log(f"⚠ Patterns échoués : {e}")

    log("=== Analyse terminée ===")


if __name__ == "__main__":
    # Paramètres injectés via env par server.run_analysis() ; fallbacks safe.
    n    = int(os.environ.get("WW_N_WALLETS", "100"))
    days = int(os.environ.get("WW_DAYS", "7"))
    main(n_wallets=n, pattern_days=days)
