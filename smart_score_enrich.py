# smart_score_enrich.py
# Lit cache/results.json, fetch les signaux Dune pour les top-N wallets,
# calcule le smart_score + breakdown, et persiste tout dans le même fichier.
import json
import os

from dune_smart_signals import fetch_smart_signals
from smart_score        import compute_score, label_for

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "cache", "results.json")


def _noop(msg):
    print(msg, flush=True)


def enrich_results_with_smart_score(top_n=100, days=7, progress_cb=_noop):
    """Ouvre results.json, enrichit chaque wallet avec smart_score, smart_label
    et smart_breakdown, puis ré-écrit le fichier."""
    if not os.path.exists(CACHE_FILE):
        progress_cb(f"results.json absent ({CACHE_FILE}) — skip enrichment")
        return

    with open(CACHE_FILE) as f:
        data = json.load(f)

    wallets = data.get("wallets") or []
    if not wallets:
        progress_cb("Aucun wallet à scorer — skip")
        return

    # On enrichit uniquement le top-N pour limiter la requête Dune.
    targets = wallets[:top_n]
    addresses = [w["address"] for w in targets if w.get("address")]
    progress_cb(f"Fetch smart signals Dune pour {len(addresses)} wallets…")
    try:
        signals_by_addr = fetch_smart_signals(addresses, days=days, progress_cb=progress_cb)
    except Exception as e:
        progress_cb(f"⚠ Dune smart signals KO ({e}) — fallback sans signaux")
        signals_by_addr = {}

    # Scoring
    scored = 0
    for w in wallets:
        sig = signals_by_addr.get((w.get("address") or "").lower())
        score, breakdown = compute_score(w, signals=sig, days_window=days)
        w["smart_score"]     = score
        w["smart_label"]     = label_for(score)
        w["smart_breakdown"] = breakdown
        if sig:
            # Conserve les signaux pour affichage UI (tooltip détaillé)
            w["smart_signals"] = sig
        scored += 1

    data["smart_score_meta"] = {
        "scored": scored,
        "with_signals": len(signals_by_addr),
        "days": days,
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f)
    progress_cb(f"✓ {scored} wallets scorés ({len(signals_by_addr)} avec signaux Dune)")


if __name__ == "__main__":
    enrich_results_with_smart_score()
