# smart_score.py
# Calcule un Smart Money Score 0-100 par wallet, en combinant les données
# du pipeline existant (results.json fields) avec l'enrichissement Dune
# de dune_smart_signals.py. Retourne aussi un breakdown pour les tooltips UI.
import math


def _vol_pts(volume_usd):
    # 0-40 pts en tiers stricts — déterminant principal du score.
    if volume_usd >= 1_000_000_000: return 40.0
    if volume_usd >= 100_000_000:   return 32.0
    if volume_usd >= 10_000_000:    return 22.0
    if volume_usd >= 1_000_000:     return 12.0
    if volume_usd >= 100_000:       return 5.0
    return 0.0


def _avg_trade_pts(avg_trade_usd):
    # 0-22 pts. Sweet spot 50k-1M (trades discrétionnaires).
    # >5M = OTC/MM peu reproductible. <1k = bot-spam.
    if avg_trade_usd >= 5_000_000:  return 10.0
    if avg_trade_usd >= 1_000_000:  return 16.0
    if avg_trade_usd >= 200_000:    return 22.0
    if avg_trade_usd >= 50_000:     return 18.0
    if avg_trade_usd >= 10_000:     return 10.0
    if avg_trade_usd >= 1_000:      return 4.0
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
    if category == "MEV Bot":
        return -45.0
    if (mev_score or 0) >= 2:
        return -15.0
    if (mev_score or 0) == 1:
        return -6.0
    return 0.0


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


def compute_score(wallet, signals=None, days_window=7):
    """
    wallet : dict avec au moins
      total_volume_usd, dune_volume_usd, dune_nb_trades, category,
      mev_score, is_contract, label, unique_tokens_traded (fallback)
    signals : dict optionnel depuis dune_smart_signals.fetch_smart_signals,
      contient active_days, distinct_dex, distinct_tokens, net_eth_usd,
      total_dex_vol_usd, max_day_vol_usd.
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

    parts = {
        "base": 8,  # baseline points (mode Free = ~30, smart wallet ≥65)
        "volume": round(_vol_pts(vol), 1),
        "avg_trade": round(_avg_trade_pts(avg), 1),
        "diversity": round(_diversity_pts(distinct_dex, distinct_tokens), 1),
        "activity": round(_activity_pts(active_days, days_window), 1),
        "net_eth": round(_net_eth_pts(net_eth_usd, total_dex_vol_usd), 1),
        "eoa_bonus": round(_eoa_bonus(is_contract), 1),
        "concentration": round(_concentration_penalty(max_day_vol_usd, total_dex_vol_usd), 1),
        "spam": round(_spam_penalty(nb), 1),
        "mev": round(_mev_penalty(cat, mev), 1),
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
    test_wallet = {
        "total_volume_usd": 200_000_000,
        "dune_volume_usd": 180_000_000,
        "dune_nb_trades": 420,
        "category": "Unknown",
        "mev_score": 0,
        "is_contract": False,
        "unique_tokens_traded": 35,
    }
    test_signals = {
        "active_days": 7,
        "distinct_dex": 4,
        "distinct_tokens": 38,
        "net_eth_usd": 12_000_000,
        "total_dex_vol_usd": 180_000_000,
        "max_day_vol_usd": 40_000_000,
    }
    s, br = compute_score(test_wallet, test_signals)
    print(f"Score: {s}  ({label_for(s)})")
    for k, v in br.items():
        print(f"  {k:14s} {v:+.1f}")
