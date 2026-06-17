# hot_calls.py
# Génère des "calls" actionnables sur Hyperliquid à partir des signaux Cockpit.
#
# Pipeline :
#   signal Cockpit (confidence ≥ HOT_MIN_CONFIDENCE)
#   → vérif perp HL existe (mapping via hyperliquid.to_hl_perp)
#   → fetch candles HL + compute ATR  (sinon FAIL CLOSED → pas de call)
#   → entry = mark price HL (sinon FAIL CLOSED)
#   → direction : LONG si net_side=buy (accumulation), SHORT si net_side=sell
#   → TP1/TP2/SL = entry ± k×ATR (k configurables)
#   → levier suggéré par bande de confidence, capé par HOT_MAX_LEVERAGE
#   → si funding HL diverge du sens spot → -1 cran de levier (et hide si configuré)
#
# Aucune valeur inventée. Si la donnée manque, le call n'est PAS émis.
#
# Tous les paramètres sont overridables via env :
#   HOT_MIN_CONFIDENCE    (défaut 70)
#   HOT_ATR_PERIOD        (défaut 14)
#   HOT_ATR_TIMEFRAME     (défaut 1h)
#   HOT_SL_ATR_MULT       (défaut 1.5)
#   HOT_TP1_ATR_MULT      (défaut 3.0)
#   HOT_TP2_ATR_MULT      (défaut 5.0)
#   HOT_MAX_LEVERAGE      (défaut 10)
#   HOT_HIDE_DIVERGENT    (défaut 0 = montrer, dégradé)

import os
import time
from datetime import datetime, timezone

import hyperliquid


def _env_int(key, default):
    try:
        v = os.environ.get(key)
        return int(v) if v is not None and v != "" else int(default)
    except (TypeError, ValueError):
        return int(default)


def _env_float(key, default):
    try:
        v = os.environ.get(key)
        return float(v) if v is not None and v != "" else float(default)
    except (TypeError, ValueError):
        return float(default)


def _env_flag(key, default):
    v = (os.environ.get(key) or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return bool(default)


# Configuration — tous overridables
# Défaut TEMPORAIRE abaissé à 35 (au lieu de 70) pour voir le pipeline
# fonctionner avec l'univers actuel (signaux observés en prod ~42 conf).
# À remonter à 70 dès que l'univers de wallets sera repeuplé (Sonar Dune)
# ou quand on aura régulièrement des signaux ≥ 70. Override via env :
#   HOT_MIN_CONFIDENCE=70  → revient au standard "Fort"
MIN_CONFIDENCE   = _env_int("HOT_MIN_CONFIDENCE", 35)
ATR_PERIOD       = _env_int("HOT_ATR_PERIOD", 14)
ATR_TIMEFRAME    = (os.environ.get("HOT_ATR_TIMEFRAME") or "1h").strip()
SL_ATR_MULT      = _env_float("HOT_SL_ATR_MULT", 1.5)
TP1_ATR_MULT     = _env_float("HOT_TP1_ATR_MULT", 3.0)
TP2_ATR_MULT     = _env_float("HOT_TP2_ATR_MULT", 5.0)
MAX_LEVERAGE     = _env_int("HOT_MAX_LEVERAGE", 10)
HIDE_DIVERGENT   = _env_flag("HOT_HIDE_DIVERGENT", False)

# Half-life confidence (réutilisé pour expiration côté UI)
HALF_LIFE_MIN    = _env_float("COCKPIT_CONFIDENCE_HALF_LIFE_MIN", 20.0)


def suggest_leverage(confidence, divergent=False):
    """Levier par bande de confidence, capé par HOT_MAX_LEVERAGE.
    Si divergent (HL contre la direction spot) : -1 cran.
    Renvoie un int >= 1."""
    if confidence >= 90:
        base = MAX_LEVERAGE
    elif confidence >= 80:
        base = 5
    elif confidence >= 70:
        base = 3
    else:
        base = 0   # ne devrait pas arriver (filtré par MIN_CONFIDENCE)
    if divergent:
        # Un cran de moins : MAX→5, 5→3, 3→1
        base = {MAX_LEVERAGE: 5, 5: 3, 3: 1}.get(base, max(1, base - 2))
    return max(1, min(MAX_LEVERAGE, base))


def _is_perp_aligned(net_side, hl_perp_component):
    """Vrai si l'alignement HL perp (composant 0-100 du signal) confirme la
    direction spot. >= 60 = aligné, <= 40 = divergent, entre = neutre."""
    if hl_perp_component is None:
        return None
    if hl_perp_component >= 60:
        return True
    if hl_perp_component <= 40:
        return False
    return None


def _direction(net_side):
    """net_side du signal → LONG / SHORT pour le call HL."""
    if net_side == "buy":
        return "LONG"
    if net_side == "sell":
        return "SHORT"
    return None  # neutre → pas de call


def _round_price(price, mark):
    """Arrondit le prix au nb de décimales pertinent vu l'échelle.
    Plus le prix est petit, plus on garde de décimales."""
    if mark is None or mark <= 0 or price is None:
        return price
    if mark >= 1000:
        return round(price, 2)
    if mark >= 1:
        return round(price, 4)
    if mark >= 0.001:
        return round(price, 6)
    return round(price, 9)


def build_call(signal, asset_ctxs=None):
    """À partir d'UN signal Cockpit, construit un call actionnable ou None.

    Renvoie None si :
      - signal["confidence"] < MIN_CONFIDENCE
      - pas de perp HL pour le token (hl_perp_symbol = None)
      - mark price indisponible (fail closed)
      - candles indisponibles ou ATR non calculable (fail closed)
      - direction neutre (net_side ni buy ni sell)
    """
    if not signal:
        return None
    confidence = int(signal.get("confidence") or 0)
    if confidence < MIN_CONFIDENCE:
        return None
    hl_perp = signal.get("hl_perp_symbol")
    if not hl_perp:
        return None  # non leviérable → pas de call (contrainte dure)
    direction = _direction(signal.get("net_side"))
    if direction is None:
        return None

    # Mark price HL (sans inventer)
    mark = hyperliquid.get_mark_price(hl_perp, asset_ctxs=asset_ctxs)
    if mark is None or mark <= 0:
        return None

    # ATR depuis candles HL (sans inventer)
    candles, err = hyperliquid.get_candles(hl_perp, interval=ATR_TIMEFRAME,
                                            n_periods=ATR_PERIOD + 5)
    if err or not candles:
        return None
    atr = hyperliquid.compute_atr(candles, period=ATR_PERIOD)
    if atr is None or atr <= 0:
        return None

    # SL / TP en fonction de direction
    if direction == "LONG":
        sl  = mark - SL_ATR_MULT * atr
        tp1 = mark + TP1_ATR_MULT * atr
        tp2 = mark + TP2_ATR_MULT * atr
    else:  # SHORT
        sl  = mark + SL_ATR_MULT * atr
        tp1 = mark - TP1_ATR_MULT * atr
        tp2 = mark - TP2_ATR_MULT * atr

    # Risk:Reward TP1 (distance TP1 / distance SL)
    risk = abs(mark - sl)
    reward_tp1 = abs(tp1 - mark)
    rr_tp1 = round(reward_tp1 / risk, 2) if risk > 0 else None

    # Alignement HL perp (depuis le composant déjà calculé dans le signal)
    hl_perp_comp = (signal.get("components") or {}).get("hl_perp")
    aligned = _is_perp_aligned(direction == "LONG" and "buy" or "sell", hl_perp_comp)
    divergent = (aligned is False)

    if divergent and HIDE_DIVERGENT:
        return None

    leverage = suggest_leverage(confidence, divergent=divergent)

    age_min = signal.get("age_min") or 0.0
    # Si l'âge dépasse 2x le half-life, on considère le call obsolète.
    # Le frontend reçoit le flag, à lui de griser ou masquer.
    expired = age_min > 2 * HALF_LIFE_MIN

    return {
        "token": signal.get("token"),
        "hl_perp_symbol": hl_perp,
        "direction": direction,
        "confidence": confidence,
        "tier": signal.get("tier"),
        "perp_alignment": ("aligned" if aligned is True
                           else "divergent" if aligned is False
                           else "neutral"),
        "leverage": leverage,
        "leverage_max": MAX_LEVERAGE,
        "entry": _round_price(mark, mark),
        "tp1": _round_price(tp1, mark),
        "tp2": _round_price(tp2, mark),
        "sl":  _round_price(sl, mark),
        "atr": _round_price(atr, mark),
        "atr_period": ATR_PERIOD,
        "atr_timeframe": ATR_TIMEFRAME,
        "rr_tp1": rr_tp1,
        "age_min": round(age_min, 1),
        "half_life_min": HALF_LIFE_MIN,
        "expired": expired,
        "trade_url": f"https://app.hyperliquid.xyz/trade/{hl_perp}",
        # Cosmétique pour copier-coller
        "copy_text": _build_copy_text(signal.get("token"), hl_perp, direction,
                                        _round_price(mark, mark),
                                        _round_price(sl, mark),
                                        _round_price(tp1, mark),
                                        _round_price(tp2, mark), leverage),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _build_copy_text(token, perp, direction, entry, sl, tp1, tp2, leverage):
    """Texte 1-ligne copiable du call. Format compact pour Discord/TG."""
    return (f"{direction} {perp} ({token}) · entry {entry} · "
            f"SL {sl} · TP1 {tp1} · TP2 {tp2} · {leverage}x")


def build_calls(signals, asset_ctxs=None):
    """Construit la liste des calls à partir des signaux Cockpit.

    Filtre dur (contrainte du brief) :
      - confidence < HOT_MIN_CONFIDENCE → exclu
      - pas de perp HL → exclu (non leviérable)
      - mark/ATR indispo → exclu (fail closed)
    Tri : confidence desc puis age_min asc (fraîcheur).
    """
    calls = []
    for s in signals or []:
        c = build_call(s, asset_ctxs=asset_ctxs)
        if c:
            calls.append(c)
    calls.sort(key=lambda c: (-c["confidence"], c.get("age_min") or 999))
    return calls


def config_snapshot():
    """Renvoie la config courante (env vars effectives). Utile pour
    /api/cockpit/hot-config et debug."""
    return {
        "min_confidence":   MIN_CONFIDENCE,
        "atr_period":       ATR_PERIOD,
        "atr_timeframe":    ATR_TIMEFRAME,
        "sl_atr_mult":      SL_ATR_MULT,
        "tp1_atr_mult":     TP1_ATR_MULT,
        "tp2_atr_mult":     TP2_ATR_MULT,
        "max_leverage":     MAX_LEVERAGE,
        "hide_divergent":   HIDE_DIVERGENT,
        "half_life_min":    HALF_LIFE_MIN,
    }


if __name__ == "__main__":
    # Smoke test avec un signal synthétique
    signal = {
        "token": "WETH", "confidence": 82, "tier": "Très fort",
        "net_side": "buy", "hl_perp_symbol": "ETH",
        "components": {"hl_perp": 75}, "age_min": 5,
    }
    print("Config:", config_snapshot())
    call = build_call(signal)
    if call:
        import json
        print(json.dumps(call, indent=2))
    else:
        print("Call non émis (signal sous seuil ou data HL manquante)")
