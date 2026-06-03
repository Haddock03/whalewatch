#!/usr/bin/env python3
"""
Script d'analyse lancé en subprocess par server.py.
Nécessite pandas, requests (installés via pip).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dune_top_wallets import fetch_top_wallets
from combine_and_rank import merge_and_rank, KNOWN_LABELS

def log(msg):
    print(msg, flush=True)

log("=== Démarrage de l'analyse ===")

log("Récupération des données Dune Analytics...")
df_dune = fetch_top_wallets(progress_cb=log)

log("Fusion Etherscan + Dune en cours...")
merge_and_rank(
    df_dune=df_dune,
    additional_wallets=list(KNOWN_LABELS.keys()),
    progress_cb=log,
)

log("=== Analyse terminée ===")
