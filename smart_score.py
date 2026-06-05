# smart_score.py
# Calcule un Smart Money Score 0-100 par wallet, en combinant les données
# du pipeline existant (results.json fields) avec l'enrichissement Dune
# de dune_smart_signals.py. Retourne aussi un breakdown pour les tooltips UI.
#
# P1.1 backend (refonte score) :
#   Le score pénalise désormais explicitement l'infrastructure (CEX hot
#   wallets, bridges, routers DEX, market makers, MEV bots) qui n'est pas
#   du « alpha discrétionnaire ». La classification est faite par
#   wallet_classifier.classify_wallet() — voir ce module pour les regex.
#
#   Avant : seuls les MEV Bot étaient pénalisés (-45). DEX Protocol / Market
#   Maker / certains Smart Contracts pouvaient finir top du leaderboard
#   alors qu'ils ne sont pas du flux directionnel.
#   Après : tous les types « infrastructure » subissent un cap à 25 + une
#   pénalité graduée (voir _infra_penalty). Le MEV reste le plus pénalisé.
import math

from wallet_classifier import classify_wallet, INFRA_TYPES, TYPE_MEV, TYPE_MM, TYPE_CEX, TYPE_BRIDGE, TYPE_ROUTER


def _vol_pts(volume_usd, volume_scale=1.0):
    # 0-40 pts en tiers stricts — déterminant principal du score.
    # `volume_scale` ajuste les seuils pour les L2 (volumes plus petits qu'ETH).
    # Voir chains.py pour les valeurs par chain.
    v = volume_usd * volume_scale
    if v >= 1_000_000_000: return 40.0
    if v >= 100_000_000:   return 32.0
    if v >= 10_000_000:    return 22.0
    if v >= 1_000_000:     return 12.0
    if v >= 100_000:       return 5.0
    return 0.0


def _avg_trade_pts(avg_trade_usd, volume_scale=1.0):
    # 0-22 pts. Sweet spot 50k-1M (trades discrétionnaires).
    # >5M = OTC/MM peu reproductible. <1k = bot-spam.
    # Scale appliqué de la même façon : sur L2, un avg trade $20k peut
    # correspondre à un sweet spot.
    a = avg_trade_usd * volume_scale
    if a >= 5_000_000:  return 10.0
    if a >= 1_000_000:  return 16.0
    if a >= 200_000:    return 22.0
    if a >= 50_000:     return 18.0
    if a >= 10_000:     return 10.0
    if a >= 1_000:      return 4.0
    return 0.0


def _diversity_pts(distinct_dex, distinct_tokens):
    # 0-10 pts. Vrais traders utilisent ≥2 DEX et ≥5 tokens.
    dex_pts = min(5.0, distinct_dex * 1.5)
    tok_pts = min(5.0, (distinct_tokens or 0) * 0.3)
    return dex_pts + tok_pts


def _activity_pts(active_days, days_window):
    # 0-8 pts. Consistance — actif tous les jours = +.
    if days_window <= 0:
        return 0.0
    ratio = active_days / days_window
    return min(8.0, ratio * 8.0)


def _concentration_penalty(max_day_vol_usd, total_dex_vol_usd):
    # 0 à -10 pts. Volume sur 1 seul jour = trader réactif (peut être pump-chase)
    # mais aussi possible whale ponctuelle. Pénalité légère si >70%.
    if total_dex_vol_usd <= 0:
        return 0.0
    ratio = max_day_vol_usd / total_dex_vol_usd
    if ratio < 0.5:
        return 0.0
    return -min(10.0, (ratio - 0.5) * 20.0)


def _spam_penalty(nb_trades):
    # 0 à -18 pts. >5000 trades/7j = bot très probable.
    if nb_trades > 5000:
        return -18.0
    if nb_trades > 1500:
        return -7.0
    return 0.0


def _mev_penalty(category, mev_score):
    # MEV bot : -45 (disqualifie pratiquement).
    # mev_score > 0 sans cat MEV : pénalité graduée.
    # NB : Conservé pour rétrocompat. Le vrai filtre infra est maintenant
    # géré par _infra_penalty() qui appelle wallet_classifier.
    if category == "MEV Bot":
        return -45.0
    if (mev_score or 0) >= 2:
        return -15.0
    if (mev_score or 0) == 1:
        return -6.0
    return 0.0


# Pénalité par type d'infrastructure. MEV reste le plus pénalisé (déjà
# couvert par _mev_penalty, on évite la double-comptabilisation).
# Valeurs choisies pour que le score final tombe sous le seuil "Solid" (65)
# même pour un wallet à 1 Md$ de volume. Volume 40 + avg 22 + … ≈ 75 ;
# avec -55 → ≤ 25. C'est la cible : Smart Money Score < 30 pour de l'infra.
_INFRA_PENALTY_BY_TYPE = {
    TYPE_MEV:    0.0,    # déjà -45 via _mev_penalty
    TYPE_MM:    -45.0,
    TYPE_CEX:   -55.0,
    TYPE_BRIDGE:-55.0,
    TYPE_ROUTER:-55.0,
}


def _infra_penalty(wallet_type):
    """Pénalité graduée par type d'infrastructure (en plus du _mev_penalty).

    `wallet_type` est le dict retourné par classify_wallet().
    Renvoie une valeur ≤ 0.
    """
    if not wallet_type or not wallet_type.get("is_infra"):
        return 0.0
    return _INFRA_PENALTY_BY_TYPE.get(wallet_type["key"], 0.0)


def _eoa_bonus(is_contract):
    # +6 pts si EOA (vrai humain), 0 sinon. Plus modéré qu'avant.
    return 6.0 if is_contract is False else 0.0


def _net_eth_pts(net_eth_usd, total_dex_vol_usd):
    # 0-8 pts. Accumuler net de l'ETH = conviction long.
    # Pondère par taille du flux : un petit net buy sur gros vol = bruit.
    if total_dex_vol_usd <= 0:
        return 0.0
    ratio = (net_eth_usd or 0) / total_dex_vol_usd
    if ratio <= 0:
        return 0.0
    return min(8.0, ratio * 30.0)


def compute_score(wallet, signals=None, days_window=7, volume_scale=1.0):
    """
    wallet : dict avec au moins
      total_volume_usd, dune_volume_usd, dune_nb_trades, category,
      mev_score, is_contract, label, unique_tokens_traded (fallback)
    signals : dict optionnel depuis dune_smart_signals.fetch_smart_signals,
      contient active_days, distinct_dex, distinct_tokens, net_eth_usd,
      total_dex_vol_usd, max_day_vol_usd.
    volume_scale : facteur d'échelle pour calibrer les seuils volume par
      chain (1.0 par défaut = Ethereum ; voir chains.py pour les L2).
    Retourne (score: int 0-100, breakdown: dict pts par composant).
    """
    vol = wallet.get("total_volume_usd") or 0
    nb = wallet.get("dune_nb_trades") or 0
    avg = (wallet.get("dune_volume_usd") or 0) / nb if nb else 0
    cat = wallet.get("category") or "Unknown"
    mev = wallet.get("mev_score") or 0
    is_contract = wallet.get("is_contract")

    sig = signals or {}
    active_days = sig.get("active_days") or 0
    distinct_dex = sig.get("distinct_dex") or 0
    distinct_tokens = sig.get("distinct_tokens") or (wallet.get("unique_tokens_traded") or 0)
    net_eth_usd = sig.get("net_eth_usd") or 0
    total_dex_vol_usd = sig.get("total_dex_vol_usd") or (wallet.get("dune_volume_usd") or 0)
    max_day_vol_usd = sig.get("max_day_vol_usd") or 0

    # P1.1 backend — classification granulaire pour la pénalité infra
    wallet_type = classify_wallet(wallet)

    parts = {
        "base": 8,  # baseline points (mode Free = ~30, smart wallet ≥65)
        "volume": round(_vol_pts(vol, volume_scale), 1),
        "avg_trade": round(_avg_trade_pts(avg, volume_scale), 1),
        "diversity": round(_diversity_pts(distinct_dex, distinct_tokens), 1),
        "activity": round(_activity_pts(active_days, days_window), 1),
        "net_eth": round(_net_eth_pts(net_eth_usd, total_dex_vol_usd), 1),
        "eoa_bonus": round(_eoa_bonus(is_contract), 1),
        "concentration": round(_concentration_penalty(max_day_vol_usd, total_dex_vol_usd), 1),
        "spam": round(_spam_penalty(nb), 1),
        "mev": round(_mev_penalty(cat, mev), 1),
        "infra": round(_infra_penalty(wallet_type), 1),
    }
    raw = sum(parts.values())
    score = max(0, min(100, round(raw)))
    return score, parts


def label_for(score):
    if score >= 80:
        return "Alpha"
    if score >= 65:
        return "Solid"
    if score >= 45:
        return "Avg"
    return "Low"


if __name__ == "__main__":
    test_signals = {
        "active_days": 7,
        "distinct_dex": 4,
        "distinct_tokens": 38,
        "net_eth_usd": 12_000_000,
        "total_dex_vol_usd": 180_000_000,
        "max_day_vol_usd": 40_000_000,
    }
    test_cases = [
        ("Alpha EOA",          {"label": "Unknown", "category": "Unknown", "is_contract": False, "total_volume_usd": 200_000_000, "dune_volume_usd": 180_000_000, "dune_nb_trades": 420, "mev_score": 0, "unique_tokens_traded": 35}),
        ("MEV Bot",            {"label": "Jaredfromsubway (MEV bot)", "category": "MEV Bot", "is_contract": True, "total_volume_usd": 900_000_000, "dune_volume_usd": 900_000_000, "dune_nb_trades": 95000, "mev_score": 3, "unique_tokens_traded": 12}),
        ("Wintermute MM",      {"label": "Wintermute (MM)", "category": "Market Maker", "is_contract": False, "total_volume_usd": 1_500_000_000, "dune_volume_usd": 1_500_000_000, "dune_nb_trades": 3000, "mev_score": 0, "unique_tokens_traded": 80}),
        ("Binance hot wallet", {"label": "Binance 14", "category": "Other", "is_contract": False, "total_volume_usd": 5_000_000_000, "dune_volume_usd": 5_000_000_000, "dune_nb_trades": 12000, "mev_score": 0, "unique_tokens_traded": 200}),
        ("1inch router",       {"label": "1inch v5 Aggregator", "category": "DEX Protocol", "is_contract": True, "total_volume_usd": 3_000_000_000, "dune_volume_usd": 3_000_000_000, "dune_nb_trades": 80000, "mev_score": 0, "unique_tokens_traded": 500}),
        ("Stargate Bridge",    {"label": "Stargate Bridge", "category": "Other", "is_contract": True, "total_volume_usd": 800_000_000, "dune_volume_usd": 800_000_000, "dune_nb_trades": 15000, "mev_score": 0, "unique_tokens_traded": 25}),
        ("Smart Contract",     {"label": "Unknown", "category": "Smart Contract", "is_contract": True, "total_volume_usd": 50_000_000, "dune_volume_usd": 50_000_000, "dune_nb_trades": 200, "mev_score": 0, "unique_tokens_traded": 10}),
    ]
    print(f"{'Case':22s} {'Score':>6s}  {'Label':>8s}   breakdown")
    print("-" * 80)
    for name, w in test_cases:
        s, br = compute_score(w, test_signals)
        nonzero = ", ".join(f"{k}{v:+.0f}" for k, v in br.items() if v != 0)
        print(f"{name:22s} {s:>6d}  {label_for(s):>8s}   {nonzero}")
