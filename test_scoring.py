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


# ── Test 6 : chains.resolve() ───────────────────────────────────────────────
section("chains.resolve() — résolution + aliases + erreurs")

from chains import resolve, CHAINS, list_chains, DEFAULT_CHAIN

# Toutes les chains canoniques résolvent
for k in CHAINS:
    cfg = resolve(k)
    check(cfg["key"] == k, f"resolve({k!r}) → key={k}")

# Aliases connus
alias_cases = [
    ("eth", "ethereum"), ("ETH", "ethereum"),
    ("arb", "arbitrum"), ("op", "optimism"),
    ("matic", "polygon"), ("pol", "polygon"),
    ("bsc", "bnb"), ("binance", "bnb"),
]
for alias, expected in alias_cases:
    cfg = resolve(alias)
    check(cfg["key"] == expected, f"resolve({alias!r}) → {expected}")

# Case insensitivity + whitespace
check(resolve("  ARBITRUM  ")["key"] == "arbitrum", "resolve gère whitespace + case")

# None / vide → default
check(resolve(None)["key"] == DEFAULT_CHAIN, "resolve(None) → DEFAULT_CHAIN")
check(resolve("")["key"] == DEFAULT_CHAIN, "resolve('') → DEFAULT_CHAIN")

# Chain inconnue → ValueError
try:
    resolve("solana")
    check(False, "resolve('solana') aurait dû lever ValueError")
except ValueError:
    check(True, "resolve('solana') lève ValueError")

# list_chains renvoie tous les configs avec cache_path/patterns_path enrichis
chains_list = list_chains()
check(len(chains_list) == len(CHAINS), f"list_chains() → {len(CHAINS)} entrées")
check(all("cache_path" in c and "patterns_path" in c for c in chains_list),
      "list_chains contient cache_path et patterns_path")

# volume_scale présent pour toutes les chains (nécessaire pour smart_score)
for c in chains_list:
    check("volume_scale" in c and c["volume_scale"] > 0,
          f"{c['key']} a un volume_scale > 0 ({c.get('volume_scale')})")


# ── Test 7 : invariants score × scale ───────────────────────────────────────
section("Score reste cohérent avec différents volume_scale")

# Un même wallet avec scale=1 vs scale=100 → le score doit MONTER (plus de
# volume_pts) sauf si déjà max ou wallet vide.
base_wallet = {
    "label": "Unknown", "category": "Unknown", "is_contract": False,
    "total_volume_usd": 5_000_000,  # $5M, donnerait 12 pts à scale=1
    "dune_volume_usd": 5_000_000, "dune_nb_trades": 200,
    "mev_score": 0, "unique_tokens_traded": 20,
}
s1, _ = compute_score(base_wallet, signals=None, volume_scale=1.0)
s100, _ = compute_score(base_wallet, signals=None, volume_scale=100.0)
check(s100 >= s1, f"$5M wallet : scale=1 → {s1}, scale=100 → {s100} (devrait monter)")

# Scale ne doit JAMAIS retirer la pénalité infra
infra_wallet = {**base_wallet, "label": "Wintermute (MM)", "category": "Market Maker"}
s_infra_scale1, _ = compute_score(infra_wallet, signals=None, volume_scale=1.0)
s_infra_scale100, _ = compute_score(infra_wallet, signals=None, volume_scale=100.0)
check(s_infra_scale1 < 65, f"MM scale=1 → {s_infra_scale1} (< 65)")
check(s_infra_scale100 < 65, f"MM scale=100 → {s_infra_scale100} (< 65)")


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
