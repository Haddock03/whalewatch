# smart_score_enrich.py
# Lit cache/results.json, fetch les signaux Dune pour les top-N wallets,
# calcule le smart_score + breakdown, et persiste tout dans le même fichier.
#
# P1.1 backend (cluster detection) : enrichit aussi avec cluster_id,
# cluster_size pour les wallets dont le deployer est partagé.
import json
import os

from chains             import resolve, DEFAULT_CHAIN
from dune_smart_signals import fetch_smart_signals
from etherscan_scraper  import set_chain_id
from smart_score        import compute_score, label_for
from wallet_clusters    import detect_clusters

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
# Path rétrocompat pour Ethereum. Surchargeable par chain via enrich(chain=…)
CACHE_FILE = os.path.join(BASE_DIR, "cache", "results.json")


def _noop(msg):
    print(msg, flush=True)


def enrich_results_with_smart_score(top_n=100, days=7, progress_cb=_noop, chain=DEFAULT_CHAIN):
    """Ouvre le cache JSON de la chain donnée, enrichit chaque wallet avec
    smart_score, smart_label, smart_breakdown, cluster_id, puis ré-écrit le
    fichier. `chain` accepte ethereum / arbitrum / base / optimism."""
    chain_cfg = resolve(chain)
    cache_file = chain_cfg["cache_path"]
    # Switch le chain_id Etherscan pour les appels deployer (clusters)
    set_chain_id(chain_cfg["chainid"])

    if not os.path.exists(cache_file):
        progress_cb(f"Cache absent ({cache_file}) — skip enrichment")
        return

    with open(cache_file) as f:
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

    # Scoring (avec volume_scale par chain pour calibrer les L2)
    volume_scale = chain_cfg.get("volume_scale", 1.0)
    scored = 0
    for w in wallets:
        sig = signals_by_addr.get((w.get("address") or "").lower())
        score, breakdown = compute_score(w, signals=sig, days_window=days,
                                         volume_scale=volume_scale)
        w["smart_score"]     = score
        w["smart_label"]     = label_for(score)
        w["smart_breakdown"] = breakdown
        if sig:
            # Conserve les signaux pour affichage UI (tooltip détaillé)
            w["smart_signals"] = sig
        scored += 1

    # ── Cluster detection ────────────────────────────────────────────────
    # On limite aux top-N pour économiser les requêtes Etherscan (1 par
    # contract). Au-delà du top-N le pattern devient moins pertinent.
    progress_cb(f"Détection de clusters par deployer (top {top_n})…")
    try:
        clusters = detect_clusters(targets, progress_cb=progress_cb)
    except Exception as e:
        progress_cb(f"⚠ Cluster detection KO ({e}) — skip")
        clusters = {}

    # Applique l'info cluster aux wallets (clé par address en minuscules)
    nb_clusters = len({c["cluster_id"] for c in clusters.values()})
    for w in wallets:
        addr = (w.get("address") or "").lower()
        info = clusters.get(addr)
        if info:
            w["cluster_id"]         = info["cluster_id"]
            w["cluster_size"]       = info["cluster_size"]
            w["cluster_is_factory"] = info["is_shared_factory"]
        else:
            # Nettoie l'éventuelle info stale
            w.pop("cluster_id", None)
            w.pop("cluster_size", None)
            w.pop("cluster_is_factory", None)

    data["smart_score_meta"] = {
        "scored": scored,
        "with_signals": len(signals_by_addr),
        "days": days,
        "clusters_found": nb_clusters,
        "clustered_wallets": len(clusters),
        "chain": chain_cfg["key"],
    }
    with open(cache_file, "w") as f:
        json.dump(data, f)
    progress_cb(f"✓ {chain_cfg['label']} : {scored} wallets scorés "
                f"({len(signals_by_addr)} avec signaux Dune, "
                f"{nb_clusters} clusters / {len(clusters)} wallets clusterisés)")


if __name__ == "__main__":
    import sys
    chain = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CHAIN
    enrich_results_with_smart_score(chain=chain)
