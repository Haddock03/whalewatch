# cockpit_worker.py
# Thread daemon qui refresh les fichiers cache/cockpit_<chain>.json toutes
# les COCKPIT_REFRESH_INTERVAL_SEC secondes.
#
# Stratégie : 1 query Dune par chain par tick. Pour ne pas saturer Dune au
# boot, on stagger les chains (1 par seconde) puis on attend l'intervalle
# avant de redémarrer une passe.
#
# v1 : chains activées via env COCKPIT_ENABLED_CHAINS (CSV).
# Défaut : "ethereum,arbitrum,bnb" — c'est le périmètre choisi en v1.
#
# Le worker peut être désactivé entièrement via WW_DISABLE_COCKPIT=1
# (utile en dev local sans clés Dune).
#
# Baseline accélération : moyenne mobile glissante d'inflow_1h par token,
# stockée en mémoire dans le worker (pas persistée — au redémarrage on
# reprend à zéro et acceleration renvoie 50 le temps que la baseline se
# construise).
import collections
import json
import os
import threading
import time
from datetime import datetime, timezone

from chains import resolve as resolve_chain
import cockpit
import dune_cockpit_feed
import hyperliquid


REFRESH_INTERVAL_SEC = int(os.environ.get("COCKPIT_REFRESH_INTERVAL_SEC", "60"))
ENABLED_CHAINS = [c.strip() for c in
                  os.environ.get("COCKPIT_ENABLED_CHAINS", "ethereum,arbitrum,bnb").split(",")
                  if c.strip()]

# Nombre d'inflow_1h passés gardés en mémoire par token pour calculer la baseline.
# 24 valeurs ≈ baseline 24h glissante (à 60s d'intervalle… non, ça ferait 24min).
# Avec REFRESH=60s, on garde 60 ticks pour avoir une fenêtre baseline ≈ 1h.
# Pour 24h de baseline il faudrait 1440 ticks — ça consomme trop de mémoire,
# on garde une approximation : 60 ticks = 1h baseline. À ajuster.
BASELINE_TICKS = int(os.environ.get("COCKPIT_BASELINE_TICKS", "60"))

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def _cockpit_cache_path(chain_key):
    return os.path.join(CACHE_DIR, f"cockpit_{chain_key}.json")


def _atomic_write_json(path, data):
    """Écrit le JSON sur disque de façon atomique pour éviter qu'un reader
    voie un fichier corrompu pendant l'écriture."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


class _BaselineStore:
    """Maintient une fenêtre glissante d'inflow_1h par (chain, token) pour
    alimenter le sub-score acceleration. Thread-safe."""
    def __init__(self, max_ticks=BASELINE_TICKS):
        self.max_ticks = max_ticks
        self._buf = collections.defaultdict(lambda: collections.deque(maxlen=max_ticks))
        self._lock = threading.Lock()

    def push(self, chain, token, inflow):
        with self._lock:
            self._buf[(chain, token)].append(float(inflow or 0))

    def baselines_for_chain(self, chain):
        """Renvoie { token: avg(historical inflow) } pour cette chain.
        Le tick courant N'EST PAS dans la baseline (le worker push après
        avoir compute, mais on évite quand même : on demande la baseline
        avant le push)."""
        out = {}
        with self._lock:
            for (c, tok), buf in self._buf.items():
                if c != chain or not buf:
                    continue
                out[tok] = sum(buf) / len(buf)
        return out


_baselines = _BaselineStore()


def _load_results_cache(chain_key):
    """Charge results_<chain>.json (ou results.json pour ethereum). Renvoie
    None si absent."""
    try:
        cfg = resolve_chain(chain_key)
    except ValueError:
        return None
    path = cfg["cache_path"]
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def refresh_one_chain(chain_key, progress_cb=None):
    """Une passe complète pour une chain : pick smart wallets → fetch Dune
    feed → fetch HL ctxs → aggregate → build signals → write JSON.

    Renvoie le payload écrit (dict) ou None si skip (pas de cache wallets ou
    pas de smart wallets éligibles).
    """
    def log(msg):
        if progress_cb:
            progress_cb(f"[{chain_key}] {msg}")

    results = _load_results_cache(chain_key)
    if not results:
        log("skip — pas de cache wallets")
        return None
    addresses, scores = cockpit.select_smart_wallets(results)
    if len(addresses) < 5:
        log(f"skip — {len(addresses)} smart wallets seulement (<5)")
        return None
    log(f"{len(addresses)} smart wallets sélectionnés")

    # Fetch Dune feed (chunked-IN)
    try:
        feed = dune_cockpit_feed.fetch_feed(
            addresses, window_min=cockpit.FEED_WINDOW_MIN,
            chain=chain_key, progress_cb=lambda m: log(m),
        )
    except Exception as e:
        log(f"erreur Dune: {e}")
        return None
    log(f"feed {len(feed)} trades")

    # Hyperliquid (1 fetch partagé)
    hl_ctxs, hl_err = hyperliquid.get_asset_ctxs()
    if hl_err:
        log(f"HL erreur (composant neutralisé): {hl_err}")

    # Agrégation + baselines
    aggregates = cockpit.aggregate_by_token(feed, scores)
    baselines = _baselines.baselines_for_chain(chain_key)
    signals = cockpit.build_signals(aggregates, baselines_1h=baselines,
                                    hl_asset_ctxs=hl_ctxs)
    log(f"{len(signals)} signaux convergents (seuil N={cockpit.CONV_THRESHOLD})")

    # Push baselines après avoir compute (le tick courant ne biaise pas
    # son propre baseline)
    for token, agg in aggregates.items():
        _baselines.push(chain_key, token, agg["inflow_1h"])

    # Préparation du feed pour l'UI : on tronque à 200 trades les plus récents
    # pour limiter la taille du JSON servi (le front affiche ~50 lignes).
    feed_ui = sorted(feed, key=lambda t: t.get("block_time") or "",
                     reverse=True)[:200]
    # Enrichi de smart_score par addr — utile pour la colonne « score » UI
    for t in feed_ui:
        t["smart_score"] = scores.get((t.get("addr") or "").lower(), 0)

    # Convergence Radar = tokens ≥ N (même si pas devenus signaux pour cause
    # de filtres autres — pour v1 c'est identique aux signaux mais on le
    # garde séparé pour évolution future).
    convergence_radar = sorted(
        ({"token": tok, "n_wallets": a["n_wallets_distinct"],
          "inflow_usd": a["inflow_1h"], "net_side": a["net_side"],
          "latest_age_min": a["latest_age_min"]}
         for tok, a in aggregates.items()
         if a["n_wallets_distinct"] >= cockpit.CONV_THRESHOLD),
        key=lambda x: x["n_wallets"], reverse=True,
    )

    payload = {
        "chain": chain_key,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "feed_window_min": cockpit.FEED_WINDOW_MIN,
        "conv_window_min": cockpit.CONV_WINDOW_MIN,
        "conv_threshold": cockpit.CONV_THRESHOLD,
        "half_life_min": cockpit.HALF_LIFE_MIN,
        "min_smart_score": cockpit.MIN_SMART_SCORE,
        "weights": {
            "convergence":    cockpit.W_CONVERGENCE,
            "wallet_quality": cockpit.W_QUALITY,
            "net_flow":       cockpit.W_NETFLOW,
            "acceleration":   cockpit.W_ACCEL,
            "hl_perp":        cockpit.W_HL,
        },
        "smart_wallets_count": len(addresses),
        "feed_trades_count": len(feed),
        "signals": signals,
        "convergence_radar": convergence_radar,
        "feed": feed_ui,
        "hl_available": not bool(hl_err),
    }
    _atomic_write_json(_cockpit_cache_path(chain_key), payload)
    log(f"cache écrit ({len(signals)} signaux)")
    return payload


def _worker_loop():
    """Boucle principale du daemon — appelée dans un thread. Tourne en continu
    jusqu'à arrêt du process."""
    print(f"[cockpit] worker started — chains={ENABLED_CHAINS} interval={REFRESH_INTERVAL_SEC}s", flush=True)
    while True:
        start = time.time()
        for chain in ENABLED_CHAINS:
            try:
                refresh_one_chain(chain, progress_cb=lambda m: print(f"[cockpit] {m}", flush=True))
            except Exception as e:
                print(f"[cockpit] {chain} unexpected error: {e}", flush=True)
            # Stagger 1s entre chains pour pas grouper les requests Dune
            time.sleep(1.0)
        elapsed = time.time() - start
        sleep_for = max(5.0, REFRESH_INTERVAL_SEC - elapsed)
        time.sleep(sleep_for)


_started = False
_thread = None


def start_background():
    """Lance le worker en thread daemon. Idempotent — appels multiples sans
    effet."""
    global _started, _thread
    if _started:
        return _thread
    if os.environ.get("WW_DISABLE_COCKPIT") == "1":
        print("[cockpit] disabled via WW_DISABLE_COCKPIT=1", flush=True)
        return None
    if not os.environ.get("DUNE_API_KEY"):
        print("[cockpit] DUNE_API_KEY missing — worker non démarré", flush=True)
        return None
    _thread = threading.Thread(target=_worker_loop, daemon=True, name="cockpit-worker")
    _thread.start()
    _started = True
    return _thread


if __name__ == "__main__":
    # Exécution standalone : 1 passe pour chaque chain activée
    for chain in ENABLED_CHAINS:
        print(f"\n=== {chain} ===")
        refresh_one_chain(chain, progress_cb=print)
