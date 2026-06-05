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

from chains              import resolve, DEFAULT_CHAIN
from dune_top_wallets    import fetch_top_wallets
from combine_and_rank    import merge_and_rank, KNOWN_LABELS
from dune_patterns       import analyze_patterns
from smart_score_enrich  import enrich_results_with_smart_score
from alerts              import archive_snapshot, recompute_alerts


def log(msg):
    print(msg, flush=True)


def main(n_wallets: int = 100, pattern_days: int = 7, chain: str = DEFAULT_CHAIN):
    chain_cfg = resolve(chain)
    log(f"=== Démarrage de l'analyse pour {chain_cfg['label']} ===")

    # 1. Top wallets via Dune
    log(f"Étape 1/4 — Récupération top wallets Dune ({chain_cfg['label']})…")
    df_dune = fetch_top_wallets(chain=chain, progress_cb=log)

    # 2. Etherscan + ranking → cache/results_{chain}.json
    log("Étape 2/4 — Fusion Etherscan + ranking…")
    merge_and_rank(
        df_dune=df_dune,
        additional_wallets=list(KNOWN_LABELS.keys()),
        progress_cb=log,
        chain=chain,
    )

    # 2bis. Smart Money Score + clusters + alertes
    log("Étape 2bis — Smart Money Score + clusters + alertes…")
    try:
        enrich_results_with_smart_score(top_n=n_wallets, days=pattern_days,
                                        progress_cb=log, chain=chain)
        # archive_snapshot/alerts ne sont supportés que pour ethereum
        # (rétrocompat — TODO: les paramétrer aussi par chain)
        if chain == "ethereum":
            archive_snapshot()
            recompute_alerts(progress_cb=log)
        log("✓ Smart score + alertes générés")
    except Exception as e:
        log(f"⚠ Smart score/alertes échoués : {e}")

    # 3. Patterns whales (Ethereum uniquement pour l'instant)
    if chain == "ethereum":
        log(f"Étape 3/4 — Analyse patterns whales ({n_wallets} wallets, {pattern_days}j)…")
        try:
            analyze_patterns(n_wallets=n_wallets, days=pattern_days)
            log("✓ Patterns générés")
        except Exception as e:
            log(f"⚠ Patterns échoués : {e}")
    else:
        log(f"Étape 3/4 — Patterns whales : skip (multi-chain pas encore supporté pour {chain_cfg['label']})")

    log(f"=== Analyse terminée pour {chain_cfg['label']} ===")


if __name__ == "__main__":
    # Paramètres injectés via env par server.run_analysis() ; fallbacks safe.
    n     = int(os.environ.get("WW_N_WALLETS", "100"))
    days  = int(os.environ.get("WW_DAYS", "7"))
    chain = os.environ.get("WW_CHAIN", DEFAULT_CHAIN)
    main(n_wallets=n, pattern_days=days, chain=chain)
