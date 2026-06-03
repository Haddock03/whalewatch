"""
whale_signal.py — Signal de trading whale-following calculé côté Whale Watch.

Lit cache/patterns.json (généré par dune_patterns.py) et émet un signal
BUY / SELL / HOLD basé sur le spread MEV WETH et sa variation depuis la
lecture précédente.

État persisté dans cache/signal_state.json (pour pouvoir calculer le delta).

Exposé via GET /api/signal — voir server.py.
"""

import json
import os
import time
from typing import Optional

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PATTERNS    = os.path.join(BASE_DIR, "cache", "patterns.json")
STATE_FILE  = os.path.join(BASE_DIR, "cache", "signal_state.json")

# Seuils — surcharge possible via env
SPREAD_BUY_BPS  = float(os.getenv("WHALE_SPREAD_BUY_BPS",  "2.0"))
SPREAD_SELL_BPS = float(os.getenv("WHALE_SPREAD_SELL_BPS", "-2.0"))


# ── Lecture ──────────────────────────────────────────────────────────
def _load_patterns() -> Optional[dict]:
    if not os.path.exists(PATTERNS):
        return None
    try:
        with open(PATTERNS) as f:
            return json.load(f)
    except Exception:
        return None


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


# ── Métriques ────────────────────────────────────────────────────────
def _spread_bps(patterns: dict) -> Optional[float]:
    """
    Spread MEV WETH en basis points.
    Convention Dune patterns : spread_bps = (sell_median - buy_median) / buy_median × 10_000
    Positif = MEV vend plus cher qu'il n'achète = pression acheteuse = bullish.
    """
    levels = patterns.get("mev_price_levels") or {}
    bps = levels.get("spread_bps")
    if isinstance(bps, (int, float)):
        return float(bps)
    buy  = (levels.get("buy")  or {}).get("median")
    sell = (levels.get("sell") or {}).get("median")
    if buy and sell and buy > 0:
        return (float(sell) - float(buy)) / float(buy) * 10_000.0
    return None


def _whale_volume_share(patterns: dict) -> Optional[float]:
    """Part de volume capté par les trades >$1M (proxy d'agressivité whale)."""
    for ins in patterns.get("insights", []):
        if ins.get("type") == "whale":
            txt = f"{ins.get('title','')} {ins.get('detail','')}".replace("%", " ")
            for tok in txt.split():
                try:
                    v = float(tok)
                    if 0 < v <= 100:
                        return v
                except ValueError:
                    continue
    return None


# ── Signal ───────────────────────────────────────────────────────────
def compute_signal() -> dict:
    """
    Calcule et retourne le signal courant. Met à jour signal_state.json.

    Retourne :
      {
        "signal":      "BUY" | "SELL" | "HOLD",
        "spread_bps":  float | None,
        "delta_bps":   float | None,   # vs lecture précédente
        "whale_share": float | None,
        "reason":      str,
        "patterns_ts": str  | None,    # generated_at du snapshot patterns
        "thresholds":  {"buy_bps": ..., "sell_bps": ...}
      }
    """
    patterns = _load_patterns()
    if not patterns:
        return {
            "signal": "HOLD", "spread_bps": None, "delta_bps": None,
            "whale_share": None, "reason": "no patterns data",
            "patterns_ts": None,
            "thresholds": {"buy_bps": SPREAD_BUY_BPS, "sell_bps": SPREAD_SELL_BPS},
        }

    spread = _spread_bps(patterns)
    share  = _whale_volume_share(patterns)

    state = _load_state()
    prev_spread = state.get("spread_bps")
    prev_share  = state.get("whale_share")
    prev_ts     = state.get("patterns_ts")
    curr_ts     = patterns.get("generated_at")

    # Si le snapshot patterns n'a pas changé, on garde le delta précédent
    # (sinon on aurait toujours delta=0 entre deux appels)
    if curr_ts == prev_ts:
        delta_spread = state.get("delta_bps")
        delta_share  = state.get("delta_share")
    else:
        delta_spread = (spread - prev_spread) if (spread is not None and prev_spread is not None) else None
        delta_share  = (share  - prev_share)  if (share  is not None and prev_share  is not None) else None

    signal = "HOLD"
    reason = "neutre"

    if spread is not None:
        if spread >= SPREAD_BUY_BPS and (delta_spread is None or delta_spread >= 0):
            signal = "BUY"
            reason = f"spread {spread:+.1f} bps ≥ {SPREAD_BUY_BPS}"
            if delta_spread is not None:
                reason += f" (Δ={delta_spread:+.1f})"
        elif spread <= SPREAD_SELL_BPS:
            signal = "SELL"
            reason = f"spread {spread:+.1f} bps ≤ {SPREAD_SELL_BPS}"
        elif delta_spread is not None and delta_spread <= -2 * abs(SPREAD_BUY_BPS):
            signal = "SELL"
            reason = f"spread effondré Δ={delta_spread:+.1f} bps"

    if signal == "HOLD" and delta_share is not None and delta_share >= 2.0:
        signal = "BUY"
        reason = f"part whale +{delta_share:.1f}% vs lecture précédente"

    # Sauvegarde l'état uniquement quand le snapshot patterns change
    if curr_ts != prev_ts:
        _save_state({
            "spread_bps":  spread,
            "whale_share": share,
            "delta_bps":   delta_spread,
            "delta_share": delta_share,
            "patterns_ts": curr_ts,
            "updated_at":  int(time.time()),
            "last_signal": signal,
        })

    return {
        "signal":       signal,
        "spread_bps":   None if spread       is None else round(spread, 2),
        "delta_bps":    None if delta_spread is None else round(delta_spread, 2),
        "whale_share":  None if share        is None else round(share, 2),
        "reason":       reason,
        "patterns_ts":  curr_ts,
        "thresholds":   {"buy_bps": SPREAD_BUY_BPS, "sell_bps": SPREAD_SELL_BPS},
    }


if __name__ == "__main__":
    print(json.dumps(compute_signal(), indent=2, ensure_ascii=False))
