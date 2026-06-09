# cockpit.py
# Cœur du module Cockpit (P0). Agrège un feed de trades de smart wallets
# (sortie de dune_cockpit_feed) en signaux par token, et calcule le
# Confidence Index 0-100 selon la spec.
#
# Confidence Index v1 — composants pondérés (sub-scores normalisés 0-100) :
#   - convergence    (25%) : profondeur = nb wallets distincts touchant le token
#   - wallet_quality (20%) : Smart Money Score moyen des participants
#   - net_flow       (20%) : conviction = |buy-sell| / (buy+sell)
#   - acceleration   (15%) : inflow_1h / baseline_1h_sur_24h (clampé)
#   - hl_perp        (20%) : alignement perp Hyperliquid (None → redistribué)
#
# Decay : multiplicateur 0.5 ** (age_min / HALF_LIFE_MIN) appliqué au score final.
# Tiers : <40 Faible · 40-59 Modéré · 60-79 Fort · ≥80 Très fort
#
# Toutes les constantes sont overridables via env vars (cf. _env_float).
import math
import os
from collections import defaultdict
from datetime import datetime, timezone


# ── Config (overridable via env) ───────────────────────────────────────────
def _env_float(key, default):
    try:
        v = os.environ.get(key)
        return float(v) if v is not None and v != "" else float(default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(key, default):
    try:
        v = os.environ.get(key)
        return int(v) if v is not None and v != "" else int(default)
    except (TypeError, ValueError):
        return int(default)


CONV_THRESHOLD       = _env_int("COCKPIT_CONV_THRESHOLD", 3)
CONV_WINDOW_MIN      = _env_int("COCKPIT_CONV_WINDOW_MIN", 30)
FEED_WINDOW_MIN      = _env_int("COCKPIT_FEED_WINDOW_MIN", 60)
HALF_LIFE_MIN        = _env_float("COCKPIT_CONFIDENCE_HALF_LIFE_MIN", 20.0)
MIN_SMART_SCORE      = _env_int("COCKPIT_MIN_SMART_SCORE", 65)

W_CONVERGENCE = _env_float("COCKPIT_W_CONVERGENCE", 0.25)
W_QUALITY     = _env_float("COCKPIT_W_QUALITY",     0.20)
W_NETFLOW     = _env_float("COCKPIT_W_NETFLOW",     0.20)
W_ACCEL       = _env_float("COCKPIT_W_ACCEL",       0.15)
W_HL          = _env_float("COCKPIT_W_HL",          0.20)

# Hot Tokens (P1) — module autonome qui liste les tokens en forte
# accélération, INDÉPENDAMMENT du seuil de convergence du Cockpit.
# Permet de détecter un token poussé par 1-2 wallets très convaincus,
# qui serait filtré par le Cockpit faute de convergence.
HOT_MIN_ACCEL_RATIO = _env_float("HOT_TOKENS_MIN_ACCEL_RATIO", 1.5)
HOT_MIN_INFLOW_USD  = _env_float("HOT_TOKENS_MIN_INFLOW_USD", 10000.0)
HOT_TOP_N           = _env_int("HOT_TOKENS_TOP_N", 12)


# ── Sub-score normalizers ──────────────────────────────────────────────────
def convergence_score(n_wallets, threshold=None):
    """Profondeur de convergence → 0-100.

    Sigmoïde-like centrée sur `threshold` :
      - n=0..threshold-1 : ramped vers ~30
      - n=threshold      : ~50
      - n=2*threshold    : ~75
      - n=4*threshold    : ~90
      - asymptote 100
    """
    t = threshold if threshold is not None else CONV_THRESHOLD
    if n_wallets <= 0 or t <= 0:
        return 0.0
    # f(x) = 100 / (1 + exp(-k*(x-t)))  avec k tel que f(2t) ≈ 75
    # k=1.1/t donne f(2t)≈75, f(4t)≈90.
    k = 1.1 / t
    return round(100.0 / (1.0 + math.exp(-k * (n_wallets - t))), 1)


def net_flow_score(buy_usd, sell_usd):
    """Conviction directionnelle → 0-100.

    100 = pression unilatérale (que des buys ou que des sells).
    0   = pression neutre (buy == sell).
    """
    total = (buy_usd or 0) + (sell_usd or 0)
    if total <= 0:
        return 0.0
    return round(abs(buy_usd - sell_usd) / total * 100.0, 1)


def acceleration_score(inflow_1h, baseline_1h):
    """Accélération du flux → 0-100.

    `baseline_1h` = inflow moyen horaire sur 24h (ou autre fenêtre passée).
    Si on n'a pas de baseline (cold start), on renvoie 50 (neutre) plutôt
    que 0 pour ne pas pénaliser injustement les premiers signaux.
    Ratio clampé à [0, 3]. Ratio=1 → 33 ; ratio=2 → 67 ; ratio=3+ → 100.
    """
    if baseline_1h is None or baseline_1h <= 0:
        return 50.0
    ratio = max(0.0, (inflow_1h or 0) / baseline_1h)
    ratio = min(3.0, ratio)
    return round(ratio / 3.0 * 100.0, 1)


def wallet_quality_score(smart_scores):
    """Smart Money Score moyen des wallets participants.

    smart_scores : liste de int 0-100. Si vide → 0.
    On clamp à [0, 100] par sécurité.
    """
    if not smart_scores:
        return 0.0
    avg = sum(smart_scores) / len(smart_scores)
    return round(max(0.0, min(100.0, avg)), 1)


# ── Confidence Index ───────────────────────────────────────────────────────
def confidence_index(sub_scores, age_min=0.0,
                     half_life_min=None, weights=None,
                     baseline_usd=None, wallet_volumes=None):
    """Calcule le Confidence Index final + breakdown.

    sub_scores : dict { "convergence": 0-100, "wallet_quality": 0-100,
                        "net_flow": 0-100, "acceleration": 0-100,
                        "hl_perp": 0-100 ou None }
    Si hl_perp est None → composant neutralisé et son poids redistribué
    proportionnellement sur les 4 autres composants (cf. spec §4).

    Renvoie un dict {
        "confidence": int 0-100,
        "tier": str,
        "decay": float,
        "weighted_raw": float,   # avant decay
        "weights": dict,         # poids effectivement appliqués (post-redist)
        "components": dict,      # mêmes clés que sub_scores
        "hl_status": "available" | "na",
    }
    """
    hl = half_life_min if half_life_min is not None else HALF_LIFE_MIN
    w_in = weights or {
        "convergence":    W_CONVERGENCE,
        "wallet_quality": W_QUALITY,
        "net_flow":       W_NETFLOW,
        "acceleration":   W_ACCEL,
        "hl_perp":        W_HL,
    }
    hl_value = sub_scores.get("hl_perp")
    hl_status = "available" if hl_value is not None else "na"

    # Construction des poids effectifs
    eff_weights = dict(w_in)
    if hl_value is None:
        # Redistribution prorata sur les 4 autres
        eff_weights["hl_perp"] = 0.0
        others = ("convergence", "wallet_quality", "net_flow", "acceleration")
        others_sum = sum(w_in[k] for k in others)
        if others_sum > 0:
            scale = (w_in["convergence"] + w_in["wallet_quality"]
                     + w_in["net_flow"] + w_in["acceleration"]
                     + w_in["hl_perp"]) / others_sum
            for k in others:
                eff_weights[k] = round(w_in[k] * scale, 4)

    # Somme pondérée
    parts = {
        "convergence":    sub_scores.get("convergence")    or 0.0,
        "wallet_quality": sub_scores.get("wallet_quality") or 0.0,
        "net_flow":       sub_scores.get("net_flow")       or 0.0,
        "acceleration":   sub_scores.get("acceleration")   or 0.0,
        "hl_perp":        hl_value if hl_value is not None else 0.0,
    }
    weighted_sum = sum(eff_weights[k] * parts[k] for k in parts)
    weight_total = sum(eff_weights.values())
    weighted_raw = weighted_sum / weight_total if weight_total > 0 else 0.0

    decay = 0.5 ** (max(0.0, age_min) / hl) if hl > 0 else 1.0
    confidence_pre_penalty = weighted_raw * decay

    # Pénalités contextuelles (P2) — appliquées en multiplicateur post-decay.
    confidence_post_penalty, penalty_breakdown = apply_penalties(
        confidence_pre_penalty,
        baseline_usd=baseline_usd,
        wallet_volumes=wallet_volumes,
    )
    confidence = round(confidence_post_penalty)
    confidence = max(0, min(100, confidence))

    return {
        "confidence": confidence,
        "tier": tier_for(confidence),
        "decay": round(decay, 4),
        "weighted_raw": round(weighted_raw, 2),
        "weights": eff_weights,
        "components": {k: (None if (k == "hl_perp" and hl_value is None) else round(parts[k], 1))
                       for k in parts},
        "hl_status": hl_status,
        "penalties": penalty_breakdown,
    }


def tier_for(confidence):
    if confidence >= 80:
        return "Très fort"
    if confidence >= 60:
        return "Fort"
    if confidence >= 40:
        return "Modéré"
    return "Faible"


# ── Pénalités contextuelles (P2) ───────────────────────────────────────────
# Appliquées comme MULTIPLICATEUR au Confidence Index final, après decay.
# Ne sont PAS des composants pondérés (pour ne pas alourdir le breakdown 5
# composants) — ce sont des modulateurs de confiance basés sur le contexte
# de marché du token et la qualité du signal lui-même.

# Liquidité fine — proxy via baseline_usd (moyenne horaire historique du
# flux smart money). Si la baseline est très faible, un signal de quelques
# $10K peut déformer le marché de façon non-réaliste : le wallet n'arrivera
# pas à exécuter à ces prix.
#   baseline >= $100K/h → 0 (token liquide)
#   $50-100K/h          → -5%
#   $10-50K/h           → -15%
#   < $10K/h            → -25%
# Cold-start (pas de baseline) → 0 (pas de pénalité — on n'a pas l'info).
def liquidity_penalty(baseline_usd):
    """Renvoie un float dans [0, _LIQ_PENALTY_MAX] représentant la part de
    confiance à enlever. 0 = pas de pénalité, _LIQ_PENALTY_MAX = pénalité max."""
    if baseline_usd is None or baseline_usd <= 0:
        return 0.0
    if baseline_usd >= 100_000:
        return 0.0
    if baseline_usd >= 50_000:
        return 0.05
    if baseline_usd >= 10_000:
        return 0.15
    return 0.25


# Concentration wallets — proxy toxicité / pump unilatéral. Si 1 seul
# wallet représente >X% du flux, le signal n'est pas vraiment de la
# convergence smart-money : c'est 1 seul acteur, peu importe les sub-scores.
#   max_share < 50%  → 0
#   50-70%           → -5%
#   70-90%           → -15%
#   > 90%            → -20%
def concentration_penalty(wallet_volumes):
    """wallet_volumes : dict {addr: usd_contributé}.
    Renvoie un float dans [0, _CONC_PENALTY_MAX]."""
    if not wallet_volumes:
        return 0.0
    total = sum(wallet_volumes.values())
    if total <= 0:
        return 0.0
    max_share = max(wallet_volumes.values()) / total
    # Seuils inclusifs sur les bornes basses : exactement 50/50 = pas de
    # pénalité (vrai cas de convergence parfaite), pas le tier suivant.
    if max_share <= 0.5:
        return 0.0
    if max_share <= 0.7:
        return 0.05
    if max_share <= 0.9:
        return 0.15
    return 0.20


def apply_penalties(raw_confidence, baseline_usd, wallet_volumes):
    """Applique les pénalités multiplicatives au score post-decay.

    Renvoie (score_final, dict_breakdown_penalties).
    Le dict liste chaque pénalité (valeur 0-1) pour explicabilité UI.
    """
    p_liq = liquidity_penalty(baseline_usd)
    p_conc = concentration_penalty(wallet_volumes)
    # Multiplicateurs combinés (chaque pénalité est indépendante)
    multiplier = (1.0 - p_liq) * (1.0 - p_conc)
    return raw_confidence * multiplier, {
        "liquidity": round(p_liq, 3),
        "concentration": round(p_conc, 3),
        "combined_multiplier": round(multiplier, 4),
    }


# ── Aggregation depuis le feed brut ────────────────────────────────────────
def _parse_block_time(ts_str):
    """Parse une string de block_time (format Dune ISO) → datetime UTC.
    Renvoie None si parsing échoue (le signal sera traité sans age info)."""
    if not ts_str:
        return None
    try:
        # Formats vus : "2026-06-09 13:42:15.000 UTC", "2026-06-09T13:42:15Z"
        s = str(ts_str).replace(" UTC", "").replace("Z", "+00:00")
        if "+" not in s and "T" not in s:
            s = s.replace(" ", "T") + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def aggregate_by_token(feed, wallet_smart_scores, conv_window_min=None,
                       now=None):
    """Agrège un feed de trades en signaux par token.

    feed : liste de {addr, token, side, usd, block_time} (sortie dune_cockpit_feed)
    wallet_smart_scores : dict { addr_lower: smart_score 0-100 }
    conv_window_min : fenêtre pour le count distinct de la convergence
                      (défaut CONV_WINDOW_MIN). Les trades plus vieux que cette
                      fenêtre ne comptent pas pour la convergence (mais comptent
                      pour le net flow / inflow_1h).
    now : datetime UTC override pour les tests.

    Renvoie : dict { token: {
        n_wallets_distinct,           # sur conv_window
        wallets,                      # liste des addr distinctes (conv_window)
        wallets_smart_scores,         # liste de scores pour wallet_quality
        buy_usd, sell_usd,            # sur fenêtre complète (FEED_WINDOW_MIN)
        net_usd,
        net_side,                     # "buy" | "sell" | "neutral"
        inflow_1h,                    # buy+sell sur fenêtre (sert d'inflow)
        latest_block_time,            # iso str du trade le plus récent
        latest_age_min,               # minutes depuis maintenant
        trade_count,
    } }
    """
    cw = conv_window_min if conv_window_min is not None else CONV_WINDOW_MIN
    now = now or datetime.now(timezone.utc)
    cutoff_conv = now.timestamp() - cw * 60

    by_token = defaultdict(lambda: {
        "wallets_full": set(),
        "wallets_conv": set(),
        "wallet_volumes": defaultdict(float),  # addr → usd contributé (proxy concentration)
        "buy_usd": 0.0,
        "sell_usd": 0.0,
        "trade_count": 0,
        "latest_ts": None,
        "latest_block_time": None,
    })

    for trade in feed:
        token = trade.get("token")
        addr = (trade.get("addr") or "").lower()
        side = trade.get("side")
        usd = float(trade.get("usd") or 0)
        if not token or not addr or side not in ("buy", "sell"):
            continue
        bt = _parse_block_time(trade.get("block_time"))
        ts = bt.timestamp() if bt else None

        bucket = by_token[token]
        bucket["wallets_full"].add(addr)
        if ts is None or ts >= cutoff_conv:
            bucket["wallets_conv"].add(addr)
        bucket["wallet_volumes"][addr] += usd
        if side == "buy":
            bucket["buy_usd"] += usd
        else:
            bucket["sell_usd"] += usd
        bucket["trade_count"] += 1
        if ts is not None and (bucket["latest_ts"] is None or ts > bucket["latest_ts"]):
            bucket["latest_ts"] = ts
            bucket["latest_block_time"] = trade.get("block_time")

    out = {}
    for token, b in by_token.items():
        net_usd = b["buy_usd"] - b["sell_usd"]
        if abs(net_usd) < 1e-9:
            net_side = "neutral"
        else:
            net_side = "buy" if net_usd > 0 else "sell"
        smart_scores = [wallet_smart_scores.get(a, 0) for a in b["wallets_full"]]
        age_min = None
        if b["latest_ts"]:
            age_min = max(0.0, (now.timestamp() - b["latest_ts"]) / 60.0)
        out[token] = {
            "n_wallets_distinct": len(b["wallets_conv"]),
            "wallets": sorted(b["wallets_full"]),
            "wallets_smart_scores": smart_scores,
            # Volume contribué par chaque wallet (utilisé par
            # concentration_penalty pour détecter les pump unilatéraux).
            "wallet_volumes": dict(b["wallet_volumes"]),
            "buy_usd": round(b["buy_usd"], 2),
            "sell_usd": round(b["sell_usd"], 2),
            "net_usd": round(net_usd, 2),
            "net_side": net_side,
            "inflow_1h": round(b["buy_usd"] + b["sell_usd"], 2),
            "latest_block_time": b["latest_block_time"],
            "latest_age_min": round(age_min, 1) if age_min is not None else None,
            "trade_count": b["trade_count"],
        }
    return out


def build_signals(aggregates, baselines_1h, hl_asset_ctxs, now=None):
    """À partir des agrégats par token, construit la liste des signaux
    Confidence (1 par token convergent).

    aggregates : sortie de aggregate_by_token.
    baselines_1h : dict { token: avg_inflow_per_hour } pour le composant
                   acceleration. Peut être {} (cold start) — le sub-score
                   renvoie alors 50 (neutre).
    hl_asset_ctxs : sortie de hyperliquid.get_asset_ctxs() — passé une fois
                    pour éviter N fetch dans la boucle.

    Filtre : seuls les tokens avec n_wallets_distinct >= CONV_THRESHOLD
    deviennent des signaux. Les autres restent dans le feed mais pas affichés
    comme Confidence Cards.

    Renvoie une liste triée par confidence desc.
    """
    # Import local pour éviter cycle si hyperliquid n'est pas chargé en test
    from hyperliquid import align_score, to_hl_perp

    signals = []
    for token, agg in aggregates.items():
        if agg["n_wallets_distinct"] < CONV_THRESHOLD:
            continue
        sub = {
            "convergence":    convergence_score(agg["n_wallets_distinct"]),
            "wallet_quality": wallet_quality_score(agg["wallets_smart_scores"]),
            "net_flow":       net_flow_score(agg["buy_usd"], agg["sell_usd"]),
            "acceleration":   acceleration_score(agg["inflow_1h"],
                                                 baselines_1h.get(token)),
            "hl_perp":        align_score(token, agg["net_side"],
                                          asset_ctxs=hl_asset_ctxs),
        }
        ci = confidence_index(
            sub,
            age_min=agg["latest_age_min"] or 0.0,
            baseline_usd=baselines_1h.get(token),
            wallet_volumes=agg.get("wallet_volumes") or {},
        )
        signals.append({
            "token": token,
            "n_wallets": agg["n_wallets_distinct"],
            "net_side": agg["net_side"],
            "buy_usd": agg["buy_usd"],
            "sell_usd": agg["sell_usd"],
            "net_usd": agg["net_usd"],
            "inflow_usd": agg["inflow_1h"],
            "trade_count": agg["trade_count"],
            "latest_block_time": agg["latest_block_time"],
            "age_min": agg["latest_age_min"],
            "wallets": agg["wallets"],
            # Symbole perp HL mappé (None si pas de correspondance). Le frontend
            # l'utilise pour construire le lien Trade-HL 1-clic ; null = bouton caché.
            "hl_perp_symbol": to_hl_perp(token),
            **ci,  # confidence, tier, decay, components, weights, hl_status, weighted_raw
        })
    signals.sort(key=lambda s: (s["confidence"], s["inflow_usd"]), reverse=True)
    return signals


# ── Hot Tokens (P1) — module autonome accélération ─────────────────────────
def build_hot_tokens(aggregates, baselines_1h, top_n=None,
                     min_accel_ratio=None, min_inflow_usd=None):
    """Classe les tokens par ratio d'accélération inflow_1h / baseline_1h.

    Indépendant du seuil de convergence du Cockpit — un token poussé par
    1-2 wallets très convaincus peut entrer dans Hot Tokens sans être un
    signal Cockpit. C'est volontaire : les 2 vues sont complémentaires.

    Filtres v1 :
      - inflow_1h >= min_inflow_usd (élimine le bruit micro)
      - accel_ratio >= min_accel_ratio (pas de baseline OU ratio < seuil
        → exclu, mais une *première fois* sans baseline existante est traitée
        comme « pas encore évaluable » et exclue plutôt que tirée à 1.0)

    Renvoie une liste triée par accel_ratio desc, tronquée à top_n.
    Chaque entrée :
      {token, accel_ratio, accel_score, inflow_usd, baseline_usd,
       buy_usd, sell_usd, net_side, n_wallets, top_wallets, latest_age_min}
    """
    # Import local — évite de forcer l'import de hyperliquid en tests qui n'en
    # ont pas besoin (et évite un cycle si quelqu'un mock le module).
    from hyperliquid import to_hl_perp

    n = top_n if top_n is not None else HOT_TOP_N
    min_ratio = min_accel_ratio if min_accel_ratio is not None else HOT_MIN_ACCEL_RATIO
    min_inflow = min_inflow_usd if min_inflow_usd is not None else HOT_MIN_INFLOW_USD

    out = []
    for token, agg in aggregates.items():
        inflow = agg["inflow_1h"]
        if inflow < min_inflow:
            continue
        baseline = baselines_1h.get(token)
        # Pas de baseline → exclu. Ne pas synthétiser un ratio = 1.0 car ça
        # tromperait l'opérateur sur la pertinence du signal.
        if not baseline or baseline <= 0:
            continue
        ratio = inflow / baseline
        if ratio < min_ratio:
            continue
        # Top wallets : on n'a pas leur poids individuel dans agg, mais on
        # peut au moins en lister jusqu'à 3 pour donner du contexte.
        wallets = agg["wallets"] or []
        out.append({
            "token": token,
            "accel_ratio": round(ratio, 2),
            "accel_score": acceleration_score(inflow, baseline),
            "inflow_usd": round(inflow, 2),
            "baseline_usd": round(baseline, 2),
            "buy_usd": agg["buy_usd"],
            "sell_usd": agg["sell_usd"],
            "net_side": agg["net_side"],
            "n_wallets": len(wallets),
            "top_wallets": wallets[:3],
            "latest_age_min": agg["latest_age_min"],
            "trade_count": agg["trade_count"],
            # Voir build_signals : null si pas de perp HL → bouton caché côté UI.
            "hl_perp_symbol": to_hl_perp(token),
        })
    out.sort(key=lambda h: (h["accel_ratio"], h["inflow_usd"]), reverse=True)
    return out[:n]


# ── Utilitaire : sélection des smart wallets depuis results_*.json ─────────
def select_smart_wallets(cache_data, min_score=None):
    """Renvoie deux outputs depuis un cache results_<chain>.json :
      - addresses : liste d'adresses (lower hex) avec smart_score >= min_score
      - scores    : dict { addr: smart_score } pour wallet_quality

    Filtre aussi les wallets dont la category est infra évidente (MEV/CEX/Bridge)
    même s'ils ont un score élevé par accident — c'est une ceinture
    supplémentaire après le score, pas un substitut.
    """
    threshold = min_score if min_score is not None else MIN_SMART_SCORE
    addrs = []
    scores = {}
    if not cache_data or not isinstance(cache_data, dict):
        return [], {}
    INFRA_CATS = {"MEV Bot", "CEX", "Bridge"}
    for w in cache_data.get("wallets") or []:
        score = w.get("smart_score") or 0
        if score < threshold:
            continue
        if (w.get("category") or "") in INFRA_CATS:
            continue
        addr = (w.get("address") or "").lower()
        if not addr:
            continue
        addrs.append(addr)
        scores[addr] = int(score)
    return addrs, scores


if __name__ == "__main__":
    # Smoke test sur un feed synthétique
    feed = [
        {"addr": "0xa", "token": "ETH", "side": "buy",  "usd": 50000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xb", "token": "ETH", "side": "buy",  "usd": 80000, "block_time": "2026-06-09T13:50:00Z"},
        {"addr": "0xc", "token": "ETH", "side": "buy",  "usd": 30000, "block_time": "2026-06-09T13:30:00Z"},
        {"addr": "0xd", "token": "ETH", "side": "sell", "usd": 10000, "block_time": "2026-06-09T13:00:00Z"},
        {"addr": "0xa", "token": "PEPE","side": "buy",  "usd": 12000, "block_time": "2026-06-09T13:48:00Z"},
    ]
    scores = {"0xa": 78, "0xb": 71, "0xc": 66, "0xd": 70}
    agg = aggregate_by_token(feed, scores, now=datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc))
    print("AGGREGATES:")
    for tok, a in agg.items():
        print(f"  {tok}: n={a['n_wallets_distinct']}  buy={a['buy_usd']}  sell={a['sell_usd']}  age={a['latest_age_min']}min")
    signals = build_signals(agg, baselines_1h={}, hl_asset_ctxs={})
    print(f"\nSIGNALS ({len(signals)}):")
    for s in signals:
        print(f"  {s['token']}: conf={s['confidence']} ({s['tier']}) decay={s['decay']} hl={s['hl_status']}")
        print(f"    components: {s['components']}")
