# alerts.py
# Maintient un historique de snapshots de results.json et calcule les
# alertes (delta vs snapshot précédent) sauvegardées dans cache/alerts.json.
# Le front polle /api/alerts toutes les 30s.
import json
import os
import time
from datetime import datetime, timezone

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR    = os.path.join(BASE_DIR, "cache")
RESULTS_FILE = os.path.join(CACHE_DIR, "results.json")
ALERTS_FILE  = os.path.join(CACHE_DIR, "alerts.json")
SNAPSHOTS_DIR = os.path.join(CACHE_DIR, "snapshots")

MAX_SNAPSHOTS = 12      # ~12 snapshots = 1h si refresh chaque 5min
MAX_ALERTS    = 40      # rolling window des alertes affichées


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_dirs():
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)


def archive_snapshot():
    """Copie results.json vers cache/snapshots/<ts>.json et purge les vieux."""
    _ensure_dirs()
    if not os.path.exists(RESULTS_FILE):
        return None
    ts = int(time.time())
    dst = os.path.join(SNAPSHOTS_DIR, f"{ts}.json")
    with open(RESULTS_FILE) as f:
        data = json.load(f)
    # Garde uniquement les champs nécessaires aux alertes pour ne pas exploser le disque
    light = {
        "ts": ts,
        "iso": _utc_now_iso(),
        "wallets": [
            {
                "address":           w.get("address"),
                "rank":              w.get("rank"),
                "category":          w.get("category"),
                "total_volume_usd":  w.get("total_volume_usd"),
                "dune_nb_trades":    w.get("dune_nb_trades"),
                "smart_score":       w.get("smart_score"),
                "label":             w.get("label"),
            }
            for w in (data.get("wallets") or [])[:200]
        ],
        "total_volume_usd": data.get("total_volume_usd"),
    }
    with open(dst, "w") as f:
        json.dump(light, f)
    _purge_old_snapshots()
    return dst


def _purge_old_snapshots():
    snaps = sorted(
        f for f in os.listdir(SNAPSHOTS_DIR) if f.endswith(".json")
    )
    if len(snaps) <= MAX_SNAPSHOTS:
        return
    for stale in snaps[: len(snaps) - MAX_SNAPSHOTS]:
        try:
            os.remove(os.path.join(SNAPSHOTS_DIR, stale))
        except OSError:
            pass


def _list_snapshots():
    if not os.path.isdir(SNAPSHOTS_DIR):
        return []
    return sorted(
        os.path.join(SNAPSHOTS_DIR, f)
        for f in os.listdir(SNAPSHOTS_DIR)
        if f.endswith(".json")
    )


def _load_snap(path):
    with open(path) as f:
        return json.load(f)


def _truncate_addr(a):
    if not a:
        return "—"
    return a[:6] + "…" + a[-4:]


def _human_delta(seconds):
    if seconds < 60:    return "il y a quelques sec"
    if seconds < 3600:  return f"il y a {int(seconds // 60)} min"
    if seconds < 86400: return f"il y a {int(seconds // 3600)} h"
    return f"il y a {int(seconds // 86400)} j"


# ─── Detection rules ─────────────────────────────────────────────────────────
# Chaque règle reçoit (latest_by_addr, prev_by_addr, latest_meta) et yield des
# dict alert : {type, address, title, detail, ts_iso, severity, score}.

def _alert_volume_spike(latest_by_addr, prev_by_addr, ts_iso):
    """Détecte les wallets dont le volume a bondi de ≥30% entre 2 snapshots."""
    for addr, cur in latest_by_addr.items():
        prev = prev_by_addr.get(addr)
        if not prev:
            continue
        cv, pv = (cur.get("total_volume_usd") or 0), (prev.get("total_volume_usd") or 0)
        if pv <= 0 or cv <= 0:
            continue
        delta_pct = (cv - pv) / pv
        if delta_pct < 0.30:
            continue
        yield {
            "type":     "whale",
            "address":  addr,
            "title":    f"Volume spike · {_truncate_addr(addr)}",
            "detail":   f"+{int(delta_pct*100)}% en {_format_delta_window(prev, cur)} · ${cv/1e6:.1f}M cumulé",
            "ts_iso":   ts_iso,
            "severity": "high" if delta_pct >= 0.6 else "med",
        }


def _alert_new_wallet(latest_by_addr, prev_by_addr, ts_iso):
    """Wallets entrés dans le top depuis le snapshot précédent."""
    new_addrs = set(latest_by_addr.keys()) - set(prev_by_addr.keys())
    for addr in new_addrs:
        w = latest_by_addr[addr]
        # On ne notifie que pour des wallets significatifs (smart_score>=55 ou top 30)
        if (w.get("smart_score") or 0) < 55 and (w.get("rank") or 9999) > 30:
            continue
        yield {
            "type":     "new",
            "address":  addr,
            "title":    f"Nouveau wallet repéré · {_truncate_addr(addr)}",
            "detail":   f"Entrée top · ${(w.get('total_volume_usd') or 0)/1e6:.1f}M · score {w.get('smart_score') or '—'}",
            "ts_iso":   ts_iso,
            "severity": "med",
        }


def _alert_smart_jump(latest_by_addr, prev_by_addr, ts_iso):
    """Smart score qui passe ≥75 ou bondit de +15 points."""
    for addr, cur in latest_by_addr.items():
        prev = prev_by_addr.get(addr)
        if not prev:
            continue
        cs = cur.get("smart_score") or 0
        ps = prev.get("smart_score") or 0
        crossed_threshold = ps < 75 <= cs
        big_jump = (cs - ps) >= 15
        if not (crossed_threshold or big_jump):
            continue
        yield {
            "type":     "smart",
            "address":  addr,
            "title":    f"Score Alpha · {_truncate_addr(addr)}",
            "detail":   f"Smart score {ps}→{cs} · {(cur.get('label') or 'Unknown')}",
            "ts_iso":   ts_iso,
            "severity": "high" if cs >= 85 else "med",
        }


def _alert_mev_shift(latest_meta, prev_meta, latest_wallets, ts_iso):
    """Variation du ratio MEV (vol MEV / vol total) entre 2 snapshots."""
    def _mev_ratio(meta_w):
        total = sum((w.get("total_volume_usd") or 0) for w in meta_w)
        mev   = sum((w.get("total_volume_usd") or 0)
                    for w in meta_w if w.get("category") == "MEV Bot")
        return (mev / total) if total else 0
    if not prev_meta:
        return
    cur_w = latest_meta.get("wallets") or []
    prev_w = prev_meta.get("wallets") or []
    cur_r, prev_r = _mev_ratio(cur_w), _mev_ratio(prev_w)
    delta_pct = cur_r - prev_r
    if abs(delta_pct) < 0.05:  # <5 points → bruit
        return
    direction = "↑" if delta_pct > 0 else "↓"
    yield {
        "type":     "mev",
        "address":  None,
        "title":    f"MEV pressure {direction} {abs(delta_pct)*100:.1f} pts",
        "detail":   f"Part MEV : {prev_r*100:.0f}% → {cur_r*100:.0f}% du volume top wallets",
        "ts_iso":   ts_iso,
        "severity": "high" if abs(delta_pct) >= 0.10 else "med",
    }


def _format_delta_window(prev, cur):
    pt, ct = prev.get("ts") or 0, cur.get("ts") or 0
    if not (pt and ct):
        return "—"
    return _human_delta(max(60, ct - pt))


def _wallets_by_addr(snap):
    return {(w.get("address") or "").lower(): w for w in (snap.get("wallets") or [])}


# ─── Recompute ───────────────────────────────────────────────────────────────
def recompute_alerts(progress_cb=lambda m: print(m, flush=True)):
    """Ouvre les 2 derniers snapshots, calcule les alertes, persiste alerts.json."""
    _ensure_dirs()
    snaps = _list_snapshots()
    if not snaps:
        progress_cb("Aucun snapshot — skip alerts")
        return
    latest_path = snaps[-1]
    prev_path   = snaps[-2] if len(snaps) >= 2 else None
    latest = _load_snap(latest_path)
    prev   = _load_snap(prev_path) if prev_path else None
    latest_by = _wallets_by_addr(latest)
    prev_by   = _wallets_by_addr(prev) if prev else {}
    ts_iso = latest.get("iso") or _utc_now_iso()

    fresh = []
    if prev:
        fresh.extend(_alert_volume_spike(latest_by, prev_by, ts_iso))
        fresh.extend(_alert_new_wallet(latest_by, prev_by, ts_iso))
        fresh.extend(_alert_smart_jump(latest_by, prev_by, ts_iso))
        fresh.extend(_alert_mev_shift(latest, prev, latest_by, ts_iso))
    # Toujours générer des « hilights » statiques pour avoir un fond utile.
    fresh.extend(_alerts_from_current_state(latest_by, ts_iso))

    # Merge avec l'historique récent
    existing = []
    if os.path.exists(ALERTS_FILE):
        try:
            existing = json.load(open(ALERTS_FILE)).get("alerts") or []
        except Exception:
            existing = []
    # Dedup : (type, address, title) clef d'unicité
    seen = set()
    merged = []
    for a in fresh + existing:
        key = (a.get("type"), a.get("address"), a.get("title"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(a)
    merged = merged[:MAX_ALERTS]

    payload = {
        "generated_at": ts_iso,
        "alerts":       merged,
        "snapshot_count": len(snaps),
    }
    with open(ALERTS_FILE, "w") as f:
        json.dump(payload, f)
    progress_cb(f"✓ Alertes : {len(merged)} (dont {len(fresh)} fraîches)")


def _alerts_from_current_state(latest_by_addr, ts_iso):
    """Toujours quelques alertes basées sur l'état actuel pour avoir du contenu
    même avant le 2e snapshot (fallback démarrage à froid)."""
    out = []
    # Top score Alpha
    alphas = sorted(
        ((a, w) for a, w in latest_by_addr.items() if (w.get("smart_score") or 0) >= 75
                                                     and w.get("category") != "MEV Bot"),
        key=lambda kv: -(kv[1].get("smart_score") or 0),
    )[:3]
    for addr, w in alphas:
        out.append({
            "type":     "smart",
            "address":  addr,
            "title":    f"Score Alpha {w.get('smart_score')} · {_truncate_addr(addr)}",
            "detail":   f"${(w.get('total_volume_usd') or 0)/1e6:.1f}M · {w.get('label') or 'Unknown'}",
            "ts_iso":   ts_iso,
            "severity": "med",
        })
    # Top vol MEV
    mevs = sorted(
        ((a, w) for a, w in latest_by_addr.items() if w.get("category") == "MEV Bot"),
        key=lambda kv: -(kv[1].get("total_volume_usd") or 0),
    )[:2]
    for addr, w in mevs:
        out.append({
            "type":     "mev",
            "address":  addr,
            "title":    f"MEV bot actif · {_truncate_addr(addr)}",
            "detail":   f"${(w.get('total_volume_usd') or 0)/1e6:.1f}M extraits · {(w.get('dune_nb_trades') or 0):,} trades",
            "ts_iso":   ts_iso,
            "severity": "med",
        })
    return out


def read_alerts():
    """API helper : retourne le payload alerts.json ou un fallback vide."""
    if not os.path.exists(ALERTS_FILE):
        return {"generated_at": None, "alerts": [], "snapshot_count": 0}
    try:
        with open(ALERTS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"generated_at": None, "alerts": [], "snapshot_count": 0}


if __name__ == "__main__":
    archive_snapshot()
    recompute_alerts()
