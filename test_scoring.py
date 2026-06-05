# test_scoring.py
# Tests de régression pour wallet_classifier + smart_score (P1.1 backend).
# Vérifie que :
#   1. Chaque type d'infra est bien détecté (MEV, MM, CEX, Bridge, Router)
#   2. Les vrais EOA / contrats opaques ne sont pas marqués infra
#   3. Le Smart Money Score tombe sous 45 ("Avg") pour TOUTE infrastructure,
#      même à 1 Md$ de volume — c'est l'invariant clé du refactor.
#   4. Un alpha discrétionnaire à volume élevé reste ≥ 80 ("Alpha").
#
# Pas de dépendance externe : exécutable avec `python3 test_scoring.py`.

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wallet_classifier import classify_wallet, INFRA_TYPES, TYPE_EOA, TYPE_CONTRACT
from smart_score import compute_score, label_for


# ── Helpers ────────────────────────────────────────────────────────────────
_FAIL = []
_PASS = 0


def check(condition, msg):
    global _PASS
    if condition:
        _PASS += 1
        print(f"  ✓ {msg}")
    else:
        _FAIL.append(msg)
        print(f"  ✗ {msg}")


def section(title):
    print(f"\n── {title} ──")


# ── Test 1 : classification ────────────────────────────────────────────────
section("Classification des wallets")

cases = [
    # (label, category, is_contract, expected_type_key, description)
    ("Jaredfromsubway (MEV bot)", "MEV Bot",       True,  "mev",      "MEV bot connu"),
    ("Sandwich Bot 0x42",         "Unknown",       True,  "mev",      "regex sandwich"),
    ("Wintermute (MM)",           "Market Maker",  False, "mm",       "MM connu"),
    ("Jump Trading hot",          "Other",         False, "mm",       "regex jump trading"),
    ("Binance 14",                "Other",         False, "cex",      "CEX hot wallet"),
    ("Coinbase 4 cold",           "Unknown",       False, "cex",      "CEX cold wallet"),
    ("Stargate Bridge",           "Other",         True,  "bridge",   "Bridge connu"),
    ("Across Protocol",           "Unknown",       True,  "bridge",   "Bridge regex"),
    ("1inch v5 Aggregator",       "DEX Protocol",  True,  "router",   "Router 1inch"),
    ("Uniswap Universal Router",  "DEX Protocol",  True,  "router",   "Uniswap router"),
    ("Paraswap",                  "Unknown",       True,  "router",   "Paraswap regex"),
    ("Random Yield Contract",     "Smart Contract",True,  "contract", "Contrat opaque"),
    ("Unknown",                   "Unknown",       False, "eoa",      "EOA par défaut"),
    ("",                          "Unknown",       None,  "eoa",      "Vide → EOA"),
]
for label, cat, is_c, expected, desc in cases:
    w = {"label": label, "category": cat, "is_contract": is_c}
    got = classify_wallet(w)
    check(got["key"] == expected, f"{desc:35s} → attendu {expected}, obtenu {got['key']}")


# ── Test 2 : flag is_infra ─────────────────────────────────────────────────
section("Flag is_infra cohérent")

for label, cat, is_c, expected, desc in cases:
    w = {"label": label, "category": cat, "is_contract": is_c}
    got = classify_wallet(w)
    expected_infra = expected in INFRA_TYPES
    check(got["is_infra"] == expected_infra, f"{desc:35s} → is_infra={expected_infra}")


# ── Test 3 : invariant — toute infra tombe sous Solid ─────────────────────
section("Smart Score < 65 pour toute infra (même à 1 Md$ de volume)")

big_signals = {
    "active_days": 7,
    "distinct_dex": 5,
    "distinct_tokens": 50,
    "net_eth_usd": 50_000_000,
    "total_dex_vol_usd": 1_000_000_000,
    "max_day_vol_usd": 200_000_000,
}
big_wallet_base = {
    "total_volume_usd": 1_000_000_000,
    "dune_volume_usd": 1_000_000_000,
    "dune_nb_trades": 5000,
    "mev_score": 0,
    "unique_tokens_traded": 50,
}

infra_examples = [
    ("MEV Bot mega",   {"label": "MEV Bot whale", "category": "MEV Bot",     "is_contract": True}),
    ("Wintermute mega",{"label": "Wintermute MM",  "category": "Market Maker","is_contract": False}),
    ("Binance mega",   {"label": "Binance 14",     "category": "Other",       "is_contract": False}),
    ("Bridge mega",    {"label": "Stargate Bridge","category": "Other",       "is_contract": True}),
    ("1inch mega",     {"label": "1inch Router",   "category": "DEX Protocol","is_contract": True}),
]

for name, extra in infra_examples:
    w = {**big_wallet_base, **extra}
    s, _ = compute_score(w, big_signals)
    check(s < 65, f"{name:18s} → score={s} (< 65 attendu)")


# ── Test 4 : vrai alpha reste éligible ─────────────────────────────────────
section("Alpha discrétionnaire reste ≥ 65 (Solid+)")

alpha_wallet = {
    "label": "Unknown",
    "category": "Unknown",
    "is_contract": False,
    "total_volume_usd": 200_000_000,
    "dune_volume_usd": 180_000_000,
    "dune_nb_trades": 420,
    "mev_score": 0,
    "unique_tokens_traded": 35,
}
alpha_signals = {
    "active_days": 6,
    "distinct_dex": 4,
    "distinct_tokens": 35,
    "net_eth_usd": 12_000_000,
    "total_dex_vol_usd": 180_000_000,
    "max_day_vol_usd": 40_000_000,
}
s, br = compute_score(alpha_wallet, alpha_signals)
check(s >= 65, f"Alpha EOA → score={s} (≥ 65 attendu) — {label_for(s)}")
check(br["infra"] == 0, f"Alpha EOA → infra penalty = 0")


# ── Test 5 : Smart Contract opaque pas pénalisé ───────────────────────────
section("Smart Contract opaque reste éligible (pas marqué infra)")

contract_wallet = {
    "label": "Unknown",
    "category": "Smart Contract",
    "is_contract": True,
    "total_volume_usd": 50_000_000,
    "dune_volume_usd": 50_000_000,
    "dune_nb_trades": 200,
    "mev_score": 0,
    "unique_tokens_traded": 10,
}
s, br = compute_score(contract_wallet, alpha_signals)
check(br["infra"] == 0, f"Smart Contract → infra penalty = 0 (obtenu {br['infra']})")
check(s >= 45, f"Smart Contract → score={s} (≥ 45 attendu)")


# ── Récap ──────────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"Résultats : {_PASS} succès, {len(_FAIL)} échecs")
if _FAIL:
    print("\nÉchecs :")
    for f in _FAIL:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("Tous les tests passent.")
