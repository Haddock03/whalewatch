#!/usr/bin/env python3
# test_cockpit.py
# Tests de régression pour le module Cockpit (P0).
#
# Aligné sur la convention test_scoring.py : pas de framework — fonctions
# `assert` + récap final. Exit 1 si au moins un test échoue.
#
# Couvre :
#   - Decay exponentiel (half-life 20 min → multiplicateur 0.5 à 20min)
#   - Redistribution des poids quand hl_perp = None
#   - Convergence sigmoid (monotonie + ancrage threshold≈50)
#   - Tiers (Faible / Modéré / Fort / Très fort)
#   - Mapping HL whitelist (wrapped, memecoin k-version, miss → None)
#   - Net flow score (cas extrêmes 100% buy / 100% sell / 50/50)
#   - Agrégation par token (convergence fenêtre courte, classification side)
import sys
from datetime import datetime, timezone

import os
import tempfile
import time

import cockpit
import cockpit_worker
import hyperliquid
import alert_dispatcher
import etherscan_cockpit_feed
import token_pricer
import market_maker_detector


_failures = []


def check(name, cond, detail=""):
    status = "OK" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


# ── Decay ──────────────────────────────────────────────────────────────────
def test_decay():
    print("\n▶ Decay exponentiel (half-life 20min)")
    # full score 100, hl_perp = 100 pour ne pas redistribuer
    sub = {"convergence": 100, "wallet_quality": 100, "net_flow": 100,
           "acceleration": 100, "hl_perp": 100}
    c0  = cockpit.confidence_index(sub, age_min=0,  half_life_min=20)
    c20 = cockpit.confidence_index(sub, age_min=20, half_life_min=20)
    c40 = cockpit.confidence_index(sub, age_min=40, half_life_min=20)
    check("age=0 → confidence=100", c0["confidence"] == 100, f"got {c0['confidence']}")
    check("age=20 → decay≈0.5",      abs(c20["decay"] - 0.5)   < 1e-6, f"got {c20['decay']}")
    check("age=20 → confidence=50",  c20["confidence"] == 50,  f"got {c20['confidence']}")
    check("age=40 → decay≈0.25",     abs(c40["decay"] - 0.25)  < 1e-6, f"got {c40['decay']}")
    check("age=40 → confidence=25",  c40["confidence"] == 25,  f"got {c40['confidence']}")


# ── Redistribution des poids quand HL=N/A ──────────────────────────────────
def test_hl_redistribution():
    print("\n▶ Redistribution des poids quand hl_perp = None")
    # Tous les autres sub-scores à 80, hl_perp = None
    sub = {"convergence": 80, "wallet_quality": 80, "net_flow": 80,
           "acceleration": 80, "hl_perp": None}
    ci = cockpit.confidence_index(sub, age_min=0, half_life_min=20)
    # hl_perp doit être marqué N/A
    check("hl_status == 'na'", ci["hl_status"] == "na")
    # Le poids hl_perp doit être 0 après redistribution
    check("eff_weights[hl_perp] == 0", ci["weights"]["hl_perp"] == 0.0)
    # La somme des poids des 4 autres doit valoir la somme initiale des 5
    initial_sum = (cockpit.W_CONVERGENCE + cockpit.W_QUALITY
                   + cockpit.W_NETFLOW + cockpit.W_ACCEL + cockpit.W_HL)
    eff_sum_others = (ci["weights"]["convergence"] + ci["weights"]["wallet_quality"]
                      + ci["weights"]["net_flow"] + ci["weights"]["acceleration"])
    check("Σ(eff_weights des 4 autres) ≈ Σ(poids initiaux 5)",
          abs(eff_sum_others - initial_sum) < 1e-3,
          f"got {eff_sum_others:.4f} vs {initial_sum:.4f}")
    # Les ratios entre les 4 sub-scores doivent être préservés (W_CONV / W_QUALITY)
    initial_ratio = cockpit.W_CONVERGENCE / cockpit.W_QUALITY
    eff_ratio = ci["weights"]["convergence"] / ci["weights"]["wallet_quality"]
    check("Ratio convergence/quality préservé",
          abs(initial_ratio - eff_ratio) < 1e-3,
          f"got {eff_ratio:.4f} vs {initial_ratio:.4f}")
    # Avec tous les autres = 80 → confidence = 80 (poids redistribués ne change rien
    # si tous les composants ont la même valeur)
    check("Confidence == 80 quand tous les 4 sub-scores = 80",
          ci["confidence"] == 80, f"got {ci['confidence']}")


# ── Pas de redistribution si hl_perp != None ───────────────────────────────
def test_hl_kept_when_available():
    print("\n▶ Poids non-redistribués quand hl_perp disponible")
    sub = {"convergence": 0, "wallet_quality": 0, "net_flow": 0,
           "acceleration": 0, "hl_perp": 100}
    ci = cockpit.confidence_index(sub, age_min=0, half_life_min=20)
    check("hl_status == 'available'", ci["hl_status"] == "available")
    check("eff_weights[hl_perp] == W_HL",
          abs(ci["weights"]["hl_perp"] - cockpit.W_HL) < 1e-6)
    # confidence = 100 * W_HL / Σ = 100 * 0.20 / 1.00 = 20
    expected = round(100 * cockpit.W_HL / 1.0)
    check(f"Confidence ≈ {expected} (hl seul contribue)",
          abs(ci["confidence"] - expected) <= 1, f"got {ci['confidence']}")


# ── Convergence sigmoid ────────────────────────────────────────────────────
def test_convergence_sigmoid():
    print("\n▶ Convergence sigmoid (monotonie + ancrage threshold)")
    c0 = cockpit.convergence_score(0, threshold=3)
    c1 = cockpit.convergence_score(1, threshold=3)
    c3 = cockpit.convergence_score(3, threshold=3)
    c6 = cockpit.convergence_score(6, threshold=3)
    c20 = cockpit.convergence_score(20, threshold=3)
    check("n=0 → 0",            c0 == 0.0, f"got {c0}")
    check("monotone n=1<3<6<20", c1 < c3 < c6 < c20, f"{c1},{c3},{c6},{c20}")
    check("n=threshold ≈ 50",   abs(c3 - 50.0) < 1e-6, f"got {c3}")
    check("n=2*threshold ≈ 75 (±2)",  abs(c6 - 75.0) < 2.5, f"got {c6}")
    check("asymptote vers 100",  c20 > 90.0 and c20 <= 100.0, f"got {c20}")


# ── Tiers ──────────────────────────────────────────────────────────────────
def test_tiers():
    print("\n▶ Tiers de confidence")
    check("39 → Faible",      cockpit.tier_for(39) == "Faible")
    check("40 → Modéré",      cockpit.tier_for(40) == "Modéré")
    check("59 → Modéré",      cockpit.tier_for(59) == "Modéré")
    check("60 → Fort",        cockpit.tier_for(60) == "Fort")
    check("79 → Fort",        cockpit.tier_for(79) == "Fort")
    check("80 → Très fort",   cockpit.tier_for(80) == "Très fort")
    check("100 → Très fort",  cockpit.tier_for(100) == "Très fort")


# ── Net flow ───────────────────────────────────────────────────────────────
def test_net_flow():
    print("\n▶ Net flow score")
    check("100% buy → 100",  cockpit.net_flow_score(100, 0)   == 100.0)
    check("100% sell → 100", cockpit.net_flow_score(0, 100)   == 100.0)
    check("50/50 → 0",       cockpit.net_flow_score(50, 50)   == 0.0)
    check("80/20 → 60",      cockpit.net_flow_score(80, 20)   == 60.0)
    check("buy=0 sell=0 → 0", cockpit.net_flow_score(0, 0)    == 0.0)


# ── Acceleration ───────────────────────────────────────────────────────────
def test_acceleration():
    print("\n▶ Acceleration score")
    check("baseline=None → 50 (neutre)", cockpit.acceleration_score(100, None) == 50.0)
    check("baseline=0 → 50 (neutre)",    cockpit.acceleration_score(100, 0) == 50.0)
    s1 = cockpit.acceleration_score(100, 100)   # ratio 1 → 33
    s2 = cockpit.acceleration_score(200, 100)   # ratio 2 → 67
    s3 = cockpit.acceleration_score(500, 100)   # ratio capped 3 → 100
    check("ratio=1 → ~33",         abs(s1 - 33.3) < 1.0, f"got {s1}")
    check("ratio=2 → ~67",         abs(s2 - 66.7) < 1.0, f"got {s2}")
    check("ratio>=3 (capped) → 100", s3 == 100.0, f"got {s3}")


# ── Wallet quality ─────────────────────────────────────────────────────────
def test_wallet_quality():
    print("\n▶ Wallet quality score")
    check("[] → 0",            cockpit.wallet_quality_score([]) == 0.0)
    check("[80,70,90] → 80",   cockpit.wallet_quality_score([80, 70, 90]) == 80.0)
    check("clamp à 100",       cockpit.wallet_quality_score([110, 150]) == 100.0)


# ── Mapping HL ─────────────────────────────────────────────────────────────
def test_hl_mapping():
    print("\n▶ Mapping HL whitelist")
    check("WETH → ETH",   hyperliquid.to_hl_perp("WETH") == "ETH")
    check("weth → ETH (case)", hyperliquid.to_hl_perp("weth") == "ETH")
    check("WBTC → BTC",   hyperliquid.to_hl_perp("WBTC") == "BTC")
    check("BTCB → BTC",   hyperliquid.to_hl_perp("BTCB") == "BTC")
    check("WBNB → BNB",   hyperliquid.to_hl_perp("WBNB") == "BNB")
    check("stETH → ETH",  hyperliquid.to_hl_perp("stETH") == "ETH")
    check("PEPE → kPEPE", hyperliquid.to_hl_perp("PEPE") == "kPEPE")
    check("BONK → kBONK", hyperliquid.to_hl_perp("BONK") == "kBONK")
    check("SOL → SOL",    hyperliquid.to_hl_perp("SOL") == "SOL")
    check("USDC → None (stable)", hyperliquid.to_hl_perp("USDC") is None)
    check("RANDOMTOKEN → None",    hyperliquid.to_hl_perp("RANDOMTOKEN") is None)
    check("'' → None",            hyperliquid.to_hl_perp("") is None)
    check("None → None",          hyperliquid.to_hl_perp(None) is None)


# ── Aggregation feed → tokens ──────────────────────────────────────────────
def test_aggregate_by_token():
    print("\n▶ Agrégation feed → tokens")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    feed = [
        # 3 wallets distincts achètent ETH dans la fenêtre 30min
        {"addr": "0xa", "token": "ETH", "side": "buy",  "usd": 50000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xb", "token": "ETH", "side": "buy",  "usd": 80000, "block_time": "2026-06-09T13:50:00Z"},
        {"addr": "0xc", "token": "ETH", "side": "buy",  "usd": 30000, "block_time": "2026-06-09T13:40:00Z"},
        # 1 sell dans la fenêtre full mais hors conv_window (35min de retard)
        {"addr": "0xd", "token": "ETH", "side": "sell", "usd": 10000, "block_time": "2026-06-09T13:25:00Z"},
        # 1 wallet sur PEPE
        {"addr": "0xa", "token": "PEPE","side": "buy",  "usd": 12000, "block_time": "2026-06-09T13:48:00Z"},
    ]
    scores = {"0xa": 78, "0xb": 71, "0xc": 66, "0xd": 70}
    agg = cockpit.aggregate_by_token(feed, scores, conv_window_min=30, now=now)
    eth = agg.get("ETH")
    check("ETH présent", eth is not None)
    check("ETH n_wallets_distinct == 3 (sur conv_window)",
          eth and eth["n_wallets_distinct"] == 3,
          f"got {eth['n_wallets_distinct'] if eth else 'absent'}")
    check("ETH buy_usd == 160000", eth and eth["buy_usd"] == 160000.0)
    check("ETH sell_usd == 10000", eth and eth["sell_usd"] == 10000.0)
    check("ETH net_side == 'buy'", eth and eth["net_side"] == "buy")
    check("ETH wallets_smart_scores includes 4 entries (all addrs)",
          eth and len(eth["wallets_smart_scores"]) == 4,
          f"got {len(eth['wallets_smart_scores']) if eth else 'absent'}")
    pepe = agg.get("PEPE")
    check("PEPE n_wallets_distinct == 1", pepe and pepe["n_wallets_distinct"] == 1)


# ── Build signals (filtre convergence) ─────────────────────────────────────
def test_build_signals_filter():
    print("\n▶ build_signals filtre convergence (forcé seuil 3 pour le test)")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    # On force le seuil à 3 LOCALEMENT pour ne pas dépendre du défaut courant
    # (qui peut bouger pour des raisons opérationnelles).
    saved = cockpit.CONV_THRESHOLD
    cockpit.CONV_THRESHOLD = 3
    try:
        feed = [
            # Token UNDER : 2 wallets → sous le seuil 3 → filtré
            {"addr": "0x1", "token": "UNDER", "side": "buy", "usd": 5000,
             "block_time": "2026-06-09T13:55:00Z"},
            {"addr": "0x2", "token": "UNDER", "side": "buy", "usd": 5000,
             "block_time": "2026-06-09T13:55:00Z"},
            # Token OVER : 4 wallets → au-dessus du seuil 3 → signal
            {"addr": "0x101", "token": "OVER", "side": "buy", "usd": 5000,
             "block_time": "2026-06-09T13:55:00Z"},
            {"addr": "0x102", "token": "OVER", "side": "buy", "usd": 5000,
             "block_time": "2026-06-09T13:55:00Z"},
            {"addr": "0x103", "token": "OVER", "side": "buy", "usd": 5000,
             "block_time": "2026-06-09T13:55:00Z"},
            {"addr": "0x104", "token": "OVER", "side": "buy", "usd": 5000,
             "block_time": "2026-06-09T13:55:00Z"},
        ]
        scores = {f"0x{i}": 70 for i in [1, 2, 101, 102, 103, 104]}
        agg = cockpit.aggregate_by_token(feed, scores, now=now)
        signals = cockpit.build_signals(agg, baselines_1h={}, hl_asset_ctxs={})
        tokens = {s["token"] for s in signals}
        check("OVER devient signal (n=4 ≥ 3)", "OVER" in tokens, f"got {tokens}")
        check("UNDER filtré (n=2 < 3)", "UNDER" not in tokens, f"got {tokens}")
    finally:
        cockpit.CONV_THRESHOLD = saved


# ── Select smart wallets ──────────────────────────────────────────────────
def test_select_smart_wallets():
    print("\n▶ Sélection des smart wallets depuis cache results")
    cache_data = {
        "wallets": [
            {"address": "0xa", "smart_score": 80, "category": "Other", "is_contract": False},
            {"address": "0xB", "smart_score": 66, "category": "Other", "is_contract": False},  # case insensitive
            {"address": "0xc", "smart_score": 90, "category": "MEV Bot", "is_contract": True},  # filtré infra MEV
            {"address": "0xd", "smart_score": 50, "category": "Other", "is_contract": False},   # sous seuil
            {"address": "0xe", "smart_score": 70, "category": "Other",
             "label": "Binance Hot Wallet", "is_contract": False},  # CEX via label regex
            {"address": "0xf", "smart_score": 70, "category": "Smart Contract", "is_contract": True},  # filtré contrat
            # Blacklist hardcodée : 1inch V5 router
            {"address": "0x1111111254eeb25477b68fb85ed929f73a960582",
             "smart_score": 90, "category": "Other", "is_contract": True,
             "label": "Unknown"},
        ]
    }
    addrs, scores, meta = cockpit.select_smart_wallets(cache_data, min_score=65)
    check("0xa retenu (80, EOA)",  "0xa" in addrs, f"got {addrs}")
    check("0xb retenu lowercased (66, EOA)", "0xb" in addrs, f"got {addrs}")
    check("0xc filtré (MEV Bot)",       "0xc" not in addrs, f"got {addrs}")
    check("0xd filtré (sous seuil 65)", "0xd" not in addrs, f"got {addrs}")
    check("0xe filtré (CEX via label Binance)", "0xe" not in addrs, f"got {addrs}")
    check("0xf filtré (Smart Contract)", "0xf" not in addrs, f"got {addrs}")
    check("1inch router filtré (blacklist hardcodée)",
          "0x1111111254eeb25477b68fb85ed929f73a960582" not in addrs, f"got {addrs}")
    check("scores[0xa] == 80", scores.get("0xa") == 80)
    check("meta retourné (dict)", isinstance(meta, dict))


# ── Hot Tokens (P1) ────────────────────────────────────────────────────────
def test_hot_tokens_filtering():
    print("\n▶ Hot Tokens — filtres min_inflow / min_accel_ratio / pas de baseline")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    # 4 tokens, chacun teste un filtre
    feed = [
        # Token A : accel ratio 2.0 (10K/5K), inflow 10K, devrait passer
        {"addr": "0xa", "token": "TOKA", "side": "buy", "usd": 10000, "block_time": "2026-06-09T13:55:00Z"},
        # Token B : accel ratio 1.0 (5K/5K), pas assez accel, devrait être filtré
        {"addr": "0xb", "token": "TOKB", "side": "buy", "usd": 5000, "block_time": "2026-06-09T13:55:00Z"},
        # Token C : inflow 500 (<min 10K), devrait être filtré
        {"addr": "0xc", "token": "TOKC", "side": "buy", "usd": 500, "block_time": "2026-06-09T13:55:00Z"},
        # Token D : pas de baseline → exclu
        {"addr": "0xd", "token": "TOKD", "side": "buy", "usd": 20000, "block_time": "2026-06-09T13:55:00Z"},
    ]
    scores = {"0xa": 70, "0xb": 70, "0xc": 70, "0xd": 70}
    agg = cockpit.aggregate_by_token(feed, scores, now=now)
    baselines = {"TOKA": 5000, "TOKB": 5000, "TOKC": 5000}  # TOKD absent
    hot = cockpit.build_hot_tokens(agg, baselines_1h=baselines,
                                   min_accel_ratio=1.5, min_inflow_usd=1000)
    tokens = [h["token"] for h in hot]
    check("TOKA présent (ratio 2.0)", "TOKA" in tokens, f"got {tokens}")
    check("TOKB filtré (ratio 1.0 < seuil 1.5)", "TOKB" not in tokens, f"got {tokens}")
    check("TOKC filtré (inflow trop bas)", "TOKC" not in tokens, f"got {tokens}")
    check("TOKD filtré (pas de baseline)", "TOKD" not in tokens, f"got {tokens}")
    if tokens:
        toka = next(h for h in hot if h["token"] == "TOKA")
        check("TOKA accel_ratio == 2.0", toka["accel_ratio"] == 2.0, f"got {toka['accel_ratio']}")
        check("TOKA net_side == 'buy'", toka["net_side"] == "buy")
        check("TOKA baseline_usd reporté", toka["baseline_usd"] == 5000.0)


def test_hot_tokens_sort_and_topn():
    print("\n▶ Hot Tokens — tri descendant + top_n")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    feed = []
    # 5 tokens avec ratios différents : 10x, 5x, 3x, 2x, 1.6x
    for i, (sym, infl) in enumerate([("T10X", 100000), ("T5X", 50000),
                                      ("T3X", 30000), ("T2X", 20000), ("T1_6X", 16000)]):
        feed.append({"addr": f"0x{i}", "token": sym, "side": "buy", "usd": infl,
                     "block_time": "2026-06-09T13:55:00Z"})
    scores = {f"0x{i}": 70 for i in range(5)}
    agg = cockpit.aggregate_by_token(feed, scores, now=now)
    baselines = {"T10X": 10000, "T5X": 10000, "T3X": 10000, "T2X": 10000, "T1_6X": 10000}
    hot = cockpit.build_hot_tokens(agg, baselines_1h=baselines,
                                   min_accel_ratio=1.5, min_inflow_usd=1000, top_n=3)
    tokens = [h["token"] for h in hot]
    check("top_n=3 limite la liste", len(hot) == 3, f"got {len(hot)}")
    check("Tri descendant par ratio", tokens == ["T10X", "T5X", "T3X"], f"got {tokens}")


def test_hot_tokens_empty_when_no_baseline():
    print("\n▶ Hot Tokens — vide si aucune baseline (cold start)")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    feed = [
        {"addr": "0xa", "token": "ANY", "side": "buy", "usd": 50000, "block_time": "2026-06-09T13:55:00Z"},
    ]
    scores = {"0xa": 70}
    agg = cockpit.aggregate_by_token(feed, scores, now=now)
    hot = cockpit.build_hot_tokens(agg, baselines_1h={})
    check("Aucun hot token au cold start", len(hot) == 0, f"got {len(hot)}")


# ── Persistance baselines (P1+) ────────────────────────────────────────────
def test_baselines_save_load_roundtrip():
    print("\n▶ Baselines — save/load roundtrip")
    store = cockpit_worker._BaselineStore(max_ticks=5)
    # push pour 2 chains × 2 tokens
    store.push("ethereum", "ETH", 100.0)
    store.push("ethereum", "ETH", 200.0)
    store.push("ethereum", "PEPE", 50.0)
    store.push("arbitrum", "ARB", 1000.0)
    baselines_before = store.baselines_for_chain("ethereum")
    check("baselines avant save : ETH avg = 150",
          abs(baselines_before["ETH"] - 150.0) < 1e-6,
          f"got {baselines_before.get('ETH')}")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "baselines_eth.json")
        n_saved = store.save("ethereum", path)
        check("save renvoie 2 tokens (ETH + PEPE)", n_saved == 2, f"got {n_saved}")
        check("fichier créé", os.path.exists(path))
        # Nouvelle instance → load
        store2 = cockpit_worker._BaselineStore(max_ticks=5)
        n_loaded = store2.load("ethereum", path)
        check("load renvoie 2 tokens", n_loaded == 2, f"got {n_loaded}")
        baselines_after = store2.baselines_for_chain("ethereum")
        check("ETH avg préservée après load",
              abs(baselines_after["ETH"] - 150.0) < 1e-6,
              f"got {baselines_after.get('ETH')}")
        check("PEPE avg préservée après load",
              abs(baselines_after["PEPE"] - 50.0) < 1e-6,
              f"got {baselines_after.get('PEPE')}")
        # La chain arbitrum n'a pas été persistée → store2 n'en a rien
        arb_baselines = store2.baselines_for_chain("arbitrum")
        check("Chain arbitrum vide dans store2 (pas chargée)",
              len(arb_baselines) == 0, f"got {arb_baselines}")


def test_baselines_load_missing_file():
    print("\n▶ Baselines — load fichier inexistant")
    store = cockpit_worker._BaselineStore()
    with tempfile.TemporaryDirectory() as tmp:
        n = store.load("ethereum", os.path.join(tmp, "does_not_exist.json"))
    check("load retourne 0 si fichier absent", n == 0, f"got {n}")
    check("store reste vide",
          len(store.baselines_for_chain("ethereum")) == 0)


def test_baselines_load_corrupted_file():
    print("\n▶ Baselines — load fichier corrompu/JSON invalide")
    store = cockpit_worker._BaselineStore()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "corrupt.json")
        with open(path, "w") as f:
            f.write("not valid json {{{")
        n = store.load("ethereum", path)
    check("load retourne 0 si JSON invalide", n == 0, f"got {n}")


def test_baselines_load_wrong_schema():
    print("\n▶ Baselines — load fichier avec mauvaise schema version")
    store = cockpit_worker._BaselineStore()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "wrong_schema.json")
        import json as _json
        with open(path, "w") as f:
            _json.dump({"schema": 999, "tokens": {"ETH": {"values": [100], "last_updated_ts": 0}}}, f)
        n = store.load("ethereum", path)
    check("load retourne 0 si schema mismatch", n == 0, f"got {n}")


def test_baselines_prune_stale_tokens():
    print("\n▶ Baselines — purge tokens trop vieux (>TTL)")
    store = cockpit_worker._BaselineStore(max_ticks=5)
    # On manipule directement _buf pour simuler des timestamps anciens
    import time as _time
    now = _time.time()
    very_old = now - (cockpit_worker.BASELINE_PRUNE_AFTER_SEC + 1000)
    recent = now - 60
    store._buf[("ethereum", "FRESH")] = {
        "buf": __import__("collections").deque([100.0], maxlen=5),
        "last_updated_ts": recent,
    }
    store._buf[("ethereum", "STALE")] = {
        "buf": __import__("collections").deque([100.0], maxlen=5),
        "last_updated_ts": very_old,
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "baselines.json")
        n_saved = store.save("ethereum", path, now=now)
    check("STALE token droppé au save", n_saved == 1, f"got {n_saved}")
    # Vérif au load aussi : si on injecte un fichier avec un token stale,
    # il ne devrait pas être chargé
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.json")
        import json as _json
        with open(path, "w") as f:
            _json.dump({
                "schema": cockpit_worker.BASELINE_FILE_SCHEMA,
                "tokens": {
                    "STALE": {"values": [100], "last_updated_ts": very_old},
                    "FRESH": {"values": [200], "last_updated_ts": recent},
                },
            }, f)
        store2 = cockpit_worker._BaselineStore()
        n_loaded = store2.load("ethereum", path, now=now)
    check("STALE token purgé au load (TTL dépassé)", n_loaded == 1, f"got {n_loaded}")


def test_liquidity_penalty_tiers():
    print("\n▶ Pénalité liquidité par tier")
    check("baseline=None → 0",     cockpit.liquidity_penalty(None) == 0.0)
    check("baseline=0 → 0",        cockpit.liquidity_penalty(0) == 0.0)
    check("baseline >= 100K → 0",  cockpit.liquidity_penalty(100_000) == 0.0)
    check("baseline = 80K → 0.05", cockpit.liquidity_penalty(80_000) == 0.05)
    check("baseline = 30K → 0.15", cockpit.liquidity_penalty(30_000) == 0.15)
    check("baseline = 5K → 0.25",  cockpit.liquidity_penalty(5_000) == 0.25)


def test_concentration_penalty_tiers():
    print("\n▶ Pénalité concentration wallets par tier")
    check("{} → 0", cockpit.concentration_penalty({}) == 0.0)
    # 50/50 = max_share 0.5 → 0
    check("équilibré (50/50) → 0",
          cockpit.concentration_penalty({"a": 100, "b": 100}) == 0.0)
    # 60/40 = max 0.6 → 0.05
    check("60/40 → 0.05",
          cockpit.concentration_penalty({"a": 60, "b": 40}) == 0.05)
    # 80/20 = max 0.8 → 0.15
    check("80/20 → 0.15",
          cockpit.concentration_penalty({"a": 80, "b": 20}) == 0.15)
    # 95/5 = max 0.95 → 0.20
    check("95/5 → 0.20",
          cockpit.concentration_penalty({"a": 95, "b": 5}) == 0.20)
    # Single wallet = 100% → 0.20 max
    check("1 seul wallet → 0.20",
          cockpit.concentration_penalty({"a": 100}) == 0.20)


def test_apply_penalties_combined():
    print("\n▶ Pénalités combinées sur confidence")
    # Score brut 100, baseline 5K (-25%) ET concentration single wallet (-20%)
    score, breakdown = cockpit.apply_penalties(100, baseline_usd=5_000,
                                                wallet_volumes={"a": 100})
    # Multiplicateur = 0.75 × 0.80 = 0.60 → score 60
    check("score 100 avec 2 pénalités max → 60",
          abs(score - 60.0) < 1e-6, f"got {score}")
    check("breakdown liquidity = 0.25",
          breakdown["liquidity"] == 0.25)
    check("breakdown concentration = 0.20",
          breakdown["concentration"] == 0.20)
    check("breakdown combined = 0.6",
          abs(breakdown["combined_multiplier"] - 0.6) < 1e-6,
          f"got {breakdown['combined_multiplier']}")


def test_confidence_with_penalties_visible():
    print("\n▶ confidence_index expose le breakdown pénalités")
    sub = {"convergence": 100, "wallet_quality": 100, "net_flow": 100,
           "acceleration": 100, "hl_perp": 100}
    ci = cockpit.confidence_index(sub, age_min=0, half_life_min=20,
                                   baseline_usd=200_000,
                                   wallet_volumes={"a": 50, "b": 50})
    check("penalties dict présent", "penalties" in ci)
    check("Pas de pénalité → score 100",
          ci["confidence"] == 100, f"got {ci['confidence']}")
    check("liquidity=0", ci["penalties"]["liquidity"] == 0.0)
    check("concentration=0", ci["penalties"]["concentration"] == 0.0)
    # Avec pénalités max
    ci2 = cockpit.confidence_index(sub, age_min=0, half_life_min=20,
                                    baseline_usd=5_000,
                                    wallet_volumes={"a": 100})
    check("2 pénalités max → score 60",
          ci2["confidence"] == 60, f"got {ci2['confidence']}")
    # Pas d'inputs pénalités → comportement legacy (pas de pénalité)
    ci3 = cockpit.confidence_index(sub, age_min=0, half_life_min=20)
    check("backward-compat (pas de baseline ni wallet_volumes) → 100",
          ci3["confidence"] == 100, f"got {ci3['confidence']}")


def test_aggregate_tracks_wallet_volumes():
    print("\n▶ aggregate_by_token expose wallet_volumes")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    feed = [
        {"addr": "0xa", "token": "X", "side": "buy", "usd": 80000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xb", "token": "X", "side": "buy", "usd": 20000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xa", "token": "X", "side": "sell", "usd": 5000, "block_time": "2026-06-09T13:55:00Z"},
    ]
    agg = cockpit.aggregate_by_token(feed, {}, now=now)
    x = agg.get("X", {})
    wv = x.get("wallet_volumes") or {}
    check("wallet_volumes a 2 entrées", len(wv) == 2, f"got {wv}")
    check("0xa total = 85K (buy 80 + sell 5)", wv.get("0xa") == 85000.0,
          f"got {wv.get('0xa')}")
    check("0xb total = 20K", wv.get("0xb") == 20000.0, f"got {wv.get('0xb')}")


def test_signals_and_hot_carry_hl_perp_symbol():
    print("\n▶ hl_perp_symbol attaché aux signals et hot tokens (P1 action 1-clic)")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    feed = [
        # WETH → mappé sur ETH côté HL
        {"addr": "0xa", "token": "WETH", "side": "buy", "usd": 50000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xb", "token": "WETH", "side": "buy", "usd": 50000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xc", "token": "WETH", "side": "buy", "usd": 50000, "block_time": "2026-06-09T13:55:00Z"},
        # Token random sans perp HL → mapping = None
        {"addr": "0xa", "token": "NOPERPCOIN", "side": "buy", "usd": 80000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xb", "token": "NOPERPCOIN", "side": "buy", "usd": 80000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xc", "token": "NOPERPCOIN", "side": "buy", "usd": 80000, "block_time": "2026-06-09T13:55:00Z"},
    ]
    scores = {"0xa": 70, "0xb": 70, "0xc": 70}
    agg = cockpit.aggregate_by_token(feed, scores, now=now)
    signals = cockpit.build_signals(agg, baselines_1h={"WETH": 10000, "NOPERPCOIN": 10000},
                                    hl_asset_ctxs={})
    by_token = {s["token"]: s for s in signals}
    check("Signal WETH a hl_perp_symbol = 'ETH'",
          by_token.get("WETH", {}).get("hl_perp_symbol") == "ETH",
          f"got {by_token.get('WETH', {}).get('hl_perp_symbol')}")
    check("Signal NOPERPCOIN a hl_perp_symbol = None",
          by_token.get("NOPERPCOIN", {}).get("hl_perp_symbol") is None,
          f"got {by_token.get('NOPERPCOIN', {}).get('hl_perp_symbol')}")
    # Idem pour hot tokens
    hot = cockpit.build_hot_tokens(agg, baselines_1h={"WETH": 10000, "NOPERPCOIN": 10000},
                                    min_accel_ratio=1.0, min_inflow_usd=1000)
    by_token_h = {h["token"]: h for h in hot}
    check("Hot WETH a hl_perp_symbol = 'ETH'",
          by_token_h.get("WETH", {}).get("hl_perp_symbol") == "ETH",
          f"got {by_token_h.get('WETH', {}).get('hl_perp_symbol')}")
    check("Hot NOPERPCOIN a hl_perp_symbol = None",
          by_token_h.get("NOPERPCOIN", {}).get("hl_perp_symbol") is None,
          f"got {by_token_h.get('NOPERPCOIN', {}).get('hl_perp_symbol')}")


def test_alert_url_validation():
    print("\n▶ Validation URL webhook")
    ok, _ = alert_dispatcher.validate_webhook_url("https://hooks.slack.com/services/T/B/X")
    check("https Slack-like → OK", ok)
    ok, reason = alert_dispatcher.validate_webhook_url("http://hooks.slack.com/x")
    check("http:// refusé en prod", not ok, f"got ok={ok} reason={reason}")
    ok, reason = alert_dispatcher.validate_webhook_url("https://127.0.0.1/x")
    check("IP loopback 127.0.0.1 refusée", not ok, f"got ok={ok} reason={reason}")
    ok, reason = alert_dispatcher.validate_webhook_url("https://192.168.1.1/x")
    check("IP privée 192.168 refusée", not ok, f"got ok={ok} reason={reason}")
    ok, reason = alert_dispatcher.validate_webhook_url("https://10.0.0.5/x")
    check("IP privée 10.0 refusée", not ok, f"got ok={ok} reason={reason}")
    ok, reason = alert_dispatcher.validate_webhook_url("https://169.254.169.254/x")
    check("IP link-local (AWS metadata) refusée", not ok, f"got ok={ok} reason={reason}")
    ok, reason = alert_dispatcher.validate_webhook_url("ftp://example.com/x")
    check("schéma non-https refusé", not ok, f"got ok={ok} reason={reason}")
    ok, reason = alert_dispatcher.validate_webhook_url("")
    check("URL vide refusée", not ok)
    ok, reason = alert_dispatcher.validate_webhook_url(None)
    check("URL None refusée", not ok)


def test_subscription_store_crud():
    print("\n▶ SubscriptionStore CRUD")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "subs.json")
        store = alert_dispatcher.SubscriptionStore(path)
        check("liste vide à l'init", store.list_all() == [])
        sub = store.create("webhook",
                           "https://hooks.slack.com/services/A/B/C",
                           threshold=70, chain="ethereum", label="my-discord")
        check("create renvoie id", "id" in sub)
        check("create renvoie target", sub["target"].startswith("https://"))
        check("create renvoie threshold normalisé",
              sub["threshold"] == 70)
        # Persistance : nouveau store → load
        store2 = alert_dispatcher.SubscriptionStore(path)
        check("subs persistés après reload",
              len(store2.list_all()) == 1)
        # Dédup
        try:
            store.create("webhook", "https://hooks.slack.com/services/A/B/C",
                         threshold=70, chain="ethereum")
            check("dédup détectée", False, "duplicate accepté !")
        except ValueError:
            check("dédup détectée", True)
        # Filter by chain
        store.create("webhook", "https://hooks.discord.com/api/webhooks/X/Y",
                     threshold=80, chain="*")
        eth_subs = store.for_chain("ethereum")
        check("for_chain ethereum trouve les 2 (1 ethereum + 1 wildcard)",
              len(eth_subs) == 2, f"got {len(eth_subs)}")
        arb_subs = store.for_chain("arbitrum")
        check("for_chain arbitrum trouve uniquement le wildcard",
              len(arb_subs) == 1, f"got {len(arb_subs)}")
        # Delete
        deleted = store.delete(sub["id"])
        check("delete retourne True", deleted)
        check("delete inexistant retourne False",
              store.delete("does-not-exist") is False)
        check("après delete il en reste 1", len(store.list_all()) == 1)
        # Create avec URL invalide
        try:
            store.create("webhook", "http://192.168.0.1/", threshold=70)
            check("URL invalide rejetée", False, "URL invalide acceptée !")
        except ValueError:
            check("URL invalide rejetée", True)


def test_dispatch_history_anti_spam():
    print("\n▶ DispatchHistory anti-spam (dedup par tier+jour)")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "history.json")
        h = alert_dispatcher.DispatchHistory(path)
        # Premier dispatch : doit passer
        check("première fois → dispatch OK",
              h.should_dispatch("sub1", "ethereum", "ETH", "Fort", now_dt=now))
        h.mark_dispatched("sub1", "ethereum", "ETH", "Fort", now_dt=now)
        # Même chose → bloqué
        check("même tier même jour → skip",
              not h.should_dispatch("sub1", "ethereum", "ETH", "Fort", now_dt=now))
        # Tier plus bas → bloqué
        check("tier qui descend → skip",
              not h.should_dispatch("sub1", "ethereum", "ETH", "Modéré", now_dt=now))
        # Tier qui monte → ré-alerte
        check("tier qui monte → dispatch",
              h.should_dispatch("sub1", "ethereum", "ETH", "Très fort", now_dt=now))
        # Autre token → pas affecté
        check("autre token → dispatch",
              h.should_dispatch("sub1", "ethereum", "PEPE", "Fort", now_dt=now))
        # Autre sub → pas affecté
        check("autre sub → dispatch (isolation)",
              h.should_dispatch("sub2", "ethereum", "ETH", "Fort", now_dt=now))
        # Persistance
        h.mark_dispatched("sub1", "ethereum", "ETH", "Très fort", now_dt=now)
        h2 = alert_dispatcher.DispatchHistory(path)
        check("history persisté → bloque toujours après reload",
              not h2.should_dispatch("sub1", "ethereum", "ETH", "Fort", now_dt=now))


def test_alert_payload_build():
    print("\n▶ Construction du payload webhook")
    signal = {
        "token": "WETH", "confidence": 82, "tier": "Très fort",
        "net_side": "buy", "n_wallets": 5, "inflow_usd": 920000,
        "age_min": 5.0, "hl_perp_symbol": "ETH",
    }
    p = alert_dispatcher._build_payload("ethereum", signal, sub_id="abc")
    check("payload event = cockpit.signal",
          p.get("event") == "cockpit.signal")
    check("payload contient token",
          p.get("token") == "WETH")
    check("payload contient confidence",
          p.get("confidence") == 82)
    check("payload contient subscription_id",
          p.get("subscription_id") == "abc")
    check("trade_url construit avec encodeURIComponent",
          p.get("trade_url") == "https://app.hyperliquid.xyz/trade/ETH",
          f"got {p.get('trade_url')}")
    check("text contient le tier", "Très fort" in (p.get("text") or ""))
    # Signal sans perp HL → trade_url = None
    sig2 = {**signal, "hl_perp_symbol": None}
    p2 = alert_dispatcher._build_payload("ethereum", sig2, sub_id="abc")
    check("trade_url None si pas de perp HL",
          p2.get("trade_url") is None, f"got {p2.get('trade_url')}")


def test_alert_tick_respects_threshold():
    print("\n▶ tick() : seuil de confidence respecté")
    with tempfile.TemporaryDirectory() as tmp:
        # Override des paths globaux pour ce test
        subs_path = os.path.join(tmp, "subs.json")
        hist_path = os.path.join(tmp, "history.json")
        original_subs = alert_dispatcher._subs_store
        original_hist = alert_dispatcher._dispatch_history
        alert_dispatcher._subs_store = alert_dispatcher.SubscriptionStore(subs_path)
        alert_dispatcher._dispatch_history = alert_dispatcher.DispatchHistory(hist_path)
        try:
            # Patch send_webhook pour ne pas faire de vrai HTTP
            calls = []
            original_send = alert_dispatcher.send_webhook
            alert_dispatcher.send_webhook = lambda url, payload: (calls.append((url, payload)), (True, "stub"))[1]
            try:
                # Sub avec threshold 70
                alert_dispatcher._subs_store.create(
                    "webhook", "https://hooks.slack.com/x/y/z",
                    threshold=70, chain="ethereum",
                )
                payload = {
                    "signals": [
                        {"token": "BIG", "confidence": 85, "tier": "Très fort",
                         "net_side": "buy", "n_wallets": 5, "inflow_usd": 100000,
                         "age_min": 5, "hl_perp_symbol": "BTC"},
                        {"token": "MID", "confidence": 65, "tier": "Fort",
                         "net_side": "buy", "n_wallets": 3, "inflow_usd": 50000,
                         "age_min": 8, "hl_perp_symbol": None},
                        # Sous le seuil → ignoré
                        {"token": "LOW", "confidence": 50, "tier": "Modéré",
                         "net_side": "buy", "n_wallets": 3, "inflow_usd": 30000,
                         "age_min": 10, "hl_perp_symbol": None},
                    ],
                }
                n_sent, n_skipped, n_errors = alert_dispatcher.tick("ethereum", payload)
                check("1 signal envoyé (BIG, conf 85 ≥ 70)",
                      n_sent == 1, f"got n_sent={n_sent} skipped={n_skipped} errors={n_errors}")
                check("call envoyé pour BIG uniquement",
                      len(calls) == 1 and calls[0][1]["token"] == "BIG",
                      f"got {[(u, p.get('token')) for u, p in calls]}")
                # Second tick : pas de ré-envoi (anti-spam)
                calls.clear()
                n_sent2, n_skipped2, _ = alert_dispatcher.tick("ethereum", payload)
                check("second tick → 0 sent (anti-spam)", n_sent2 == 0, f"got {n_sent2}")
                check("second tick → 1 skipped", n_skipped2 == 1, f"got {n_skipped2}")
                # Chain qui ne match aucune sub
                n_sent3, _, _ = alert_dispatcher.tick("arbitrum", payload)
                check("chain non-matchée → 0 sent", n_sent3 == 0)
            finally:
                alert_dispatcher.send_webhook = original_send
        finally:
            alert_dispatcher._subs_store = original_subs
            alert_dispatcher._dispatch_history = original_hist


def test_baselines_load_replaces_chain_not_merges():
    print("\n▶ Baselines — load remplace la chain (idempotence)")
    store = cockpit_worker._BaselineStore()
    store.push("ethereum", "ETH", 999.0)
    store.push("ethereum", "GHOST", 1.0)  # ne sera plus dans le fichier
    store.push("arbitrum", "ARB", 5.0)    # chain non touchée par le load
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.json")
        import json as _json, time as _time
        now = _time.time()
        with open(path, "w") as f:
            _json.dump({
                "schema": cockpit_worker.BASELINE_FILE_SCHEMA,
                "tokens": {
                    "ETH":  {"values": [100, 200], "last_updated_ts": now},
                    "PEPE": {"values": [50], "last_updated_ts": now},
                },
            }, f)
        store.load("ethereum", path, now=now)
    eth_baselines = store.baselines_for_chain("ethereum")
    arb_baselines = store.baselines_for_chain("arbitrum")
    check("ETH écrasé par valeur du fichier (150)",
          abs(eth_baselines.get("ETH", 0) - 150.0) < 1e-6,
          f"got {eth_baselines.get('ETH')}")
    check("GHOST retiré (était en mémoire, absent du fichier)",
          "GHOST" not in eth_baselines, f"got {eth_baselines}")
    check("PEPE ajouté depuis fichier", "PEPE" in eth_baselines)
    check("Chain arbitrum intacte (load ne touche pas l'autre chain)",
          abs(arb_baselines.get("ARB", 0) - 5.0) < 1e-6,
          f"got {arb_baselines}")


# ── Token pricer (Etherscan feed migration) ────────────────────────────────
def test_pricer_stables():
    print("\n▶ token_pricer : whitelist stables → $1")
    token_pricer.clear_cache()
    for s in ("USDC", "USDT", "DAI", "USDC.E", "FDUSD", "FRAX", "BUSD"):
        p = token_pricer.get_price_usd(None, "ethereum", symbol=s)
        check(f"{s:8s} → 1.0", p == 1.0, f"got {p}")


def test_pricer_native_uses_coingecko_mock(monkeypatch_func=None):
    print("\n▶ token_pricer : natifs via CoinGecko (mocké)")
    token_pricer.clear_cache()
    # Force le cache CoinGecko avec un dict pré-rempli
    token_pricer._CG_NATIVE_CACHE["data"] = {
        "ethereum": 1700.0, "bitcoin": 62000.0, "binancecoin": 600.0,
    }
    token_pricer._CG_NATIVE_CACHE["ts"] = time.time()
    check("WETH → 1700",  token_pricer.get_price_usd(None, "ethereum", "WETH") == 1700.0)
    check("ETH  → 1700",  token_pricer.get_price_usd(None, "ethereum", "ETH") == 1700.0)
    check("WBTC → 62000", token_pricer.get_price_usd(None, "ethereum", "WBTC") == 62000.0)
    check("WBNB → 600",   token_pricer.get_price_usd(None, "bnb", "WBNB") == 600.0)
    check("UNKNOWN_NATIVE → None (pas dans whitelist)",
          token_pricer.get_price_usd(None, "ethereum", "FOO") is None)


def test_pricer_dexscreener_mock():
    print("\n▶ token_pricer : DexScreener (HTTP mocké)")
    token_pricer.clear_cache()
    # Patch _get_json pour simuler la réponse DexScreener
    original_get_json = token_pricer._get_json
    pepe_addr = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
    # Mock : 3 pools, on doit prendre celui avec la plus grosse liquidité
    def mock_get_json(url, timeout=None):
        if "tokens" in url:
            return {"pairs": [
                {"chainId": "ethereum", "priceUsd": "0.0000025",
                 "liquidity": {"usd": 50_000}},
                {"chainId": "ethereum", "priceUsd": "0.0000030",
                 "liquidity": {"usd": 5_000_000}},   # ← winner
                {"chainId": "ethereum", "priceUsd": "0.99",  # pool manipulé
                 "liquidity": {"usd": 500}},          # trop faible, exclu
                {"chainId": "bsc", "priceUsd": "0.0000028",
                 "liquidity": {"usd": 3_000_000}},   # autre chain
            ]}
        return {}
    token_pricer._get_json = mock_get_json
    try:
        p = token_pricer.get_price_usd(pepe_addr, "ethereum")
        check("Pool le plus liquide choisi", p == 0.0000030,
              f"got {p} (attendu 0.0000030 du pool 5M$)")
        # Vérif cache : 2e appel ne re-fetche pas
        token_pricer._get_json = lambda url, timeout=None: (_ for _ in ()).throw(Exception("should not be called"))
        p2 = token_pricer.get_price_usd(pepe_addr, "ethereum")
        check("2e appel : cache hit (pas de fetch)", p2 == 0.0000030)
        # Chain BSC : prend le pool BSC
        token_pricer._get_json = mock_get_json
        token_pricer.clear_cache()
        p_bsc = token_pricer.get_price_usd(pepe_addr, "bnb")
        check("BSC pool (3M$) trouvé", p_bsc == 0.0000028, f"got {p_bsc}")
    finally:
        token_pricer._get_json = original_get_json


def test_pricer_dexscreener_no_pool():
    print("\n▶ token_pricer : DexScreener sans pool éligible → None")
    token_pricer.clear_cache()
    original_get_json = token_pricer._get_json
    token_pricer._get_json = lambda url, timeout=None: {"pairs": []}
    try:
        p = token_pricer.get_price_usd(
            "0x0000000000000000000000000000000000000001", "ethereum")
        check("token sans pool → None", p is None, f"got {p}")
    finally:
        token_pricer._get_json = original_get_json


# ── Etherscan cockpit feed ─────────────────────────────────────────────────
def _make_transfer(tx_hash, from_addr, to_addr, sym, decimals, raw_amount,
                   contract_addr, ts):
    return {
        "hash": tx_hash, "from": from_addr, "to": to_addr,
        "tokenSymbol": sym, "tokenDecimal": str(decimals),
        "value": str(raw_amount), "contractAddress": contract_addr,
        "timeStamp": str(int(ts)),
    }


def test_etherscan_feed_stable_for_crypto_buy():
    print("\n▶ Etherscan feed : stable→crypto (BUY crypto)")
    wallet = "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13"
    router = "0x1111111111111111111111111111111111111111"
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    now = time.time() - 60  # 1 min ago
    transfers = [
        # Wallet envoie 5000 USDC → router
        _make_transfer("0xTX1", wallet, router, "USDC", 6, "5000000000", usdc, now),
        # Router envoie 3 WETH → wallet
        _make_transfer("0xTX1", router, wallet, "WETH", 18, "3000000000000000000", weth, now),
    ]
    original = etherscan_cockpit_feed.get_token_transfers
    original_price = token_pricer.get_price_usd
    etherscan_cockpit_feed.get_token_transfers = lambda *a, **k: transfers
    token_pricer.get_price_usd = lambda addr, chain, symbol=None: (
        1.0 if (symbol or "").upper() == "USDC" else
        1700.0 if (symbol or "").upper() == "WETH" else None
    )
    try:
        feed = etherscan_cockpit_feed.fetch_feed([wallet], window_min=60,
                                                  chain="ethereum")
        check("1 entry produite", len(feed) == 1, f"got {len(feed)}")
        if feed:
            e = feed[0]
            check("token = WETH", e["token"] == "WETH", f"got {e['token']}")
            check("side = buy", e["side"] == "buy", f"got {e['side']}")
            check("usd ≈ 3 × 1700 = 5100", abs(e["usd"] - 5100.0) < 0.01,
                  f"got {e['usd']}")
            check("addr = wallet lowercase", e["addr"] == wallet.lower())
            check("project = etherscan", e["project"] == "etherscan")
            check("block_time iso", e["block_time"].endswith("Z"))
    finally:
        etherscan_cockpit_feed.get_token_transfers = original
        token_pricer.get_price_usd = original_price


def test_etherscan_feed_crypto_to_crypto_two_entries():
    print("\n▶ Etherscan feed : crypto↔crypto (2 entries opposées)")
    wallet = "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13"
    router = "0x1111111111111111111111111111111111111111"
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    wbtc = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
    now = time.time() - 60
    transfers = [
        _make_transfer("0xTX2", wallet, router, "WETH", 18, "10000000000000000000", weth, now),  # 10 WETH
        _make_transfer("0xTX2", router, wallet, "WBTC", 8, "26000000", wbtc, now),               # 0.26 WBTC
    ]
    original = etherscan_cockpit_feed.get_token_transfers
    original_price = token_pricer.get_price_usd
    etherscan_cockpit_feed.get_token_transfers = lambda *a, **k: transfers
    token_pricer.get_price_usd = lambda addr, chain, symbol=None: (
        1700.0 if (symbol or "").upper() == "WETH" else
        62000.0 if (symbol or "").upper() == "WBTC" else None
    )
    try:
        feed = etherscan_cockpit_feed.fetch_feed([wallet], window_min=60,
                                                  chain="ethereum")
        check("2 entries produites", len(feed) == 2, f"got {len(feed)}")
        sides = {(e["token"], e["side"]) for e in feed}
        check("WBTC en BUY", ("WBTC", "buy") in sides, f"got {sides}")
        check("WETH en SELL", ("WETH", "sell") in sides, f"got {sides}")
    finally:
        etherscan_cockpit_feed.get_token_transfers = original
        token_pricer.get_price_usd = original_price


def test_etherscan_feed_skip_stable_stable():
    print("\n▶ Etherscan feed : stable↔stable → skip")
    wallet = "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13"
    router = "0x1111111111111111111111111111111111111111"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    now = time.time() - 60
    transfers = [
        _make_transfer("0xTX3", wallet, router, "USDC", 6, "1000000000", usdc, now),
        _make_transfer("0xTX3", router, wallet, "USDT", 6, "1000000000", usdt, now),
    ]
    original = etherscan_cockpit_feed.get_token_transfers
    etherscan_cockpit_feed.get_token_transfers = lambda *a, **k: transfers
    try:
        feed = etherscan_cockpit_feed.fetch_feed([wallet], window_min=60,
                                                  chain="ethereum")
        check("USDC↔USDT → 0 entries", len(feed) == 0, f"got {len(feed)}")
    finally:
        etherscan_cockpit_feed.get_token_transfers = original


def test_etherscan_feed_skip_one_sided():
    print("\n▶ Etherscan feed : transfert seul (claim/airdrop) → skip")
    wallet = "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13"
    sender = "0x9999999999999999999999999999999999999999"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    now = time.time() - 60
    transfers = [
        # Wallet reçoit USDC mais n'envoie rien dans cette tx
        _make_transfer("0xTX4", sender, wallet, "USDC", 6, "1000000000", usdc, now),
    ]
    original = etherscan_cockpit_feed.get_token_transfers
    etherscan_cockpit_feed.get_token_transfers = lambda *a, **k: transfers
    try:
        feed = etherscan_cockpit_feed.fetch_feed([wallet], window_min=60,
                                                  chain="ethereum")
        check("Transfert simple (pas swap) → 0 entries", len(feed) == 0,
              f"got {len(feed)}")
    finally:
        etherscan_cockpit_feed.get_token_transfers = original


def test_etherscan_feed_skip_old_trades():
    print("\n▶ Etherscan feed : trades hors fenêtre → skip")
    wallet = "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13"
    router = "0x1111111111111111111111111111111111111111"
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    # 2h ago, donc HORS de la fenêtre 60min
    old_ts = time.time() - 7200
    transfers = [
        _make_transfer("0xTX5", wallet, router, "USDC", 6, "5000000000", usdc, old_ts),
        _make_transfer("0xTX5", router, wallet, "WETH", 18, "3000000000000000000", weth, old_ts),
    ]
    original = etherscan_cockpit_feed.get_token_transfers
    etherscan_cockpit_feed.get_token_transfers = lambda *a, **k: transfers
    try:
        feed = etherscan_cockpit_feed.fetch_feed([wallet], window_min=60,
                                                  chain="ethereum")
        check("Trade > 60min ago → filtré", len(feed) == 0, f"got {len(feed)}")
    finally:
        etherscan_cockpit_feed.get_token_transfers = original


def test_etherscan_feed_skip_below_min_usd():
    print("\n▶ Etherscan feed : trade sous MIN_TRADE_USD → skip")
    wallet = "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13"
    router = "0x1111111111111111111111111111111111111111"
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    now = time.time() - 60
    # 100 USDC → trade $100, sous le MIN_TRADE_USD=1000
    transfers = [
        _make_transfer("0xTX6", wallet, router, "USDC", 6, "100000000", usdc, now),
        _make_transfer("0xTX6", router, wallet, "WETH", 18, "60000000000000000", weth, now),
    ]
    original = etherscan_cockpit_feed.get_token_transfers
    original_price = token_pricer.get_price_usd
    etherscan_cockpit_feed.get_token_transfers = lambda *a, **k: transfers
    token_pricer.get_price_usd = lambda addr, chain, symbol=None: (
        1.0 if (symbol or "").upper() == "USDC" else
        1700.0 if (symbol or "").upper() == "WETH" else None
    )
    try:
        feed = etherscan_cockpit_feed.fetch_feed([wallet], window_min=60,
                                                  chain="ethereum")
        check("Trade $102 < MIN_TRADE_USD → filtré", len(feed) == 0,
              f"got {len(feed)} entries (usd={feed[0]['usd'] if feed else 'N/A'})")
    finally:
        etherscan_cockpit_feed.get_token_transfers = original
        token_pricer.get_price_usd = original_price


def test_etherscan_feed_skip_when_pricer_returns_none():
    print("\n▶ Etherscan feed : pricer None → trade ignoré")
    wallet = "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13"
    router = "0x1111111111111111111111111111111111111111"
    unknown = "0xdeadbeef00000000000000000000000000000000"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    now = time.time() - 60
    transfers = [
        _make_transfer("0xTX7", wallet, router, "USDC", 6, "5000000000", usdc, now),
        _make_transfer("0xTX7", router, wallet, "UNKNOWN", 18, "1000000000000000000", unknown, now),
    ]
    original = etherscan_cockpit_feed.get_token_transfers
    original_price = token_pricer.get_price_usd
    etherscan_cockpit_feed.get_token_transfers = lambda *a, **k: transfers
    token_pricer.get_price_usd = lambda addr, chain, symbol=None: (
        1.0 if (symbol or "").upper() == "USDC" else None
    )
    try:
        feed = etherscan_cockpit_feed.fetch_feed([wallet], window_min=60,
                                                  chain="ethereum")
        check("Pricer None pour UNKNOWN → trade ignoré", len(feed) == 0,
              f"got {len(feed)}")
    finally:
        etherscan_cockpit_feed.get_token_transfers = original
        token_pricer.get_price_usd = original_price


def test_etherscan_feed_matches_dune_format():
    print("\n▶ Etherscan feed : format de sortie IDENTIQUE au feed Dune")
    wallet = "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13"
    router = "0x1111111111111111111111111111111111111111"
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    now = time.time() - 60
    transfers = [
        _make_transfer("0xTX8", wallet, router, "USDC", 6, "5000000000", usdc, now),
        _make_transfer("0xTX8", router, wallet, "WETH", 18, "3000000000000000000", weth, now),
    ]
    original = etherscan_cockpit_feed.get_token_transfers
    original_price = token_pricer.get_price_usd
    etherscan_cockpit_feed.get_token_transfers = lambda *a, **k: transfers
    token_pricer.get_price_usd = lambda addr, chain, symbol=None: (
        1.0 if (symbol or "").upper() == "USDC" else
        1700.0 if (symbol or "").upper() == "WETH" else None
    )
    try:
        feed = etherscan_cockpit_feed.fetch_feed([wallet], window_min=60,
                                                  chain="ethereum")
        check("1 entry attendue", len(feed) == 1)
        if feed:
            entry = feed[0]
            expected_keys = {"addr", "token", "side", "usd", "project", "block_time"}
            actual = set(entry.keys())
            check("clés IDENTIQUES au feed Dune",
                  actual == expected_keys,
                  f"manquantes={expected_keys - actual} en plus={actual - expected_keys}")
            check("types corrects",
                  isinstance(entry["addr"], str)
                  and isinstance(entry["token"], str)
                  and entry["side"] in ("buy", "sell")
                  and isinstance(entry["usd"], (int, float))
                  and isinstance(entry["project"], str)
                  and isinstance(entry["block_time"], str))
    finally:
        etherscan_cockpit_feed.get_token_transfers = original
        token_pricer.get_price_usd = original_price


def test_etherscan_feed_consumed_by_aggregate_by_token():
    print("\n▶ Etherscan feed → aggregate_by_token sans modif")
    wallet1 = "0xae2fc483527b8ef99eb5d9b44875f005ba1fae13"
    wallet2 = "0xbb2fc483527b8ef99eb5d9b44875f005ba1fae13"
    router = "0x1111111111111111111111111111111111111111"
    weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    now = time.time() - 60
    # 2 wallets achètent WETH
    fixtures = {
        wallet1: [
            _make_transfer("0xA", wallet1, router, "USDC", 6, "5000000000", usdc, now),
            _make_transfer("0xA", router, wallet1, "WETH", 18, "3000000000000000000", weth, now),
        ],
        wallet2: [
            _make_transfer("0xB", wallet2, router, "USDC", 6, "10000000000", usdc, now),
            _make_transfer("0xB", router, wallet2, "WETH", 18, "6000000000000000000", weth, now),
        ],
    }
    original = etherscan_cockpit_feed.get_token_transfers
    original_price = token_pricer.get_price_usd
    etherscan_cockpit_feed.get_token_transfers = lambda addr, **k: fixtures.get(addr.lower(), [])
    token_pricer.get_price_usd = lambda addr, chain, symbol=None: (
        1.0 if (symbol or "").upper() == "USDC" else
        1700.0 if (symbol or "").upper() == "WETH" else None
    )
    try:
        feed = etherscan_cockpit_feed.fetch_feed([wallet1, wallet2], window_min=60,
                                                  chain="ethereum")
        check("2 entries (1 par wallet)", len(feed) == 2, f"got {len(feed)}")
        # Vérifie que cockpit.aggregate_by_token peut consommer ce feed sans erreur
        scores = {wallet1: 75, wallet2: 70}
        agg = cockpit.aggregate_by_token(feed, scores)
        weth_agg = agg.get("WETH")
        check("WETH agrégé", weth_agg is not None)
        check("WETH 2 wallets distincts (full)",
              len(weth_agg.get("wallets") or []) == 2,
              f"got {weth_agg.get('wallets')}")
        check("WETH buy_usd > 0",
              (weth_agg.get("buy_usd") or 0) > 0, f"got {weth_agg.get('buy_usd')}")
    finally:
        etherscan_cockpit_feed.get_token_transfers = original
        token_pricer.get_price_usd = original_price


# ── Market Maker detector (P0 fix) ─────────────────────────────────────────
def _ts_iso(epoch):
    """Helper : convertit epoch en ISO 8601 UTC pour les block_time des fixtures."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def test_mm_detects_same_cluster_within_window():
    print("\n▶ MM detector : BUY + SELL même cluster < 60s → flag")
    now = 1_770_000_000
    feed = [
        {"addr": "0xa", "token": "WBTC",  "side": "buy",  "usd": 10000,
         "block_time": _ts_iso(now - 90)},
        {"addr": "0xa", "token": "cbBTC", "side": "sell", "usd": 10000,
         "block_time": _ts_iso(now - 60)},  # 30s plus tard
    ]
    mm = market_maker_detector.detect_market_makers(feed, window_sec=60, enabled=True)
    check("0xa flaggé MM (WBTC+cbBTC à 30s, même cluster BTC)",
          "0xa" in mm, f"got {mm}")


def test_mm_does_not_flag_different_clusters():
    print("\n▶ MM detector : BUY + SELL clusters différents → PAS flag")
    now = 1_770_000_000
    feed = [
        {"addr": "0xb", "token": "WBTC", "side": "buy",  "usd": 10000,
         "block_time": _ts_iso(now - 90)},
        {"addr": "0xb", "token": "ETH",  "side": "sell", "usd": 10000,
         "block_time": _ts_iso(now - 60)},  # 30s plus tard mais cluster ETH
    ]
    mm = market_maker_detector.detect_market_makers(feed, window_sec=60, enabled=True)
    check("0xb non flaggé (WBTC BTC vs ETH ETH)",
          "0xb" not in mm, f"got {mm}")


def test_mm_respects_window():
    print("\n▶ MM detector : trades hors fenêtre → PAS flag")
    now = 1_770_000_000
    feed = [
        {"addr": "0xc", "token": "WBTC",  "side": "buy",  "usd": 10000,
         "block_time": _ts_iso(now - 180)},
        {"addr": "0xc", "token": "cbBTC", "side": "sell", "usd": 10000,
         "block_time": _ts_iso(now - 60)},  # 120s plus tard, hors 60s
    ]
    mm = market_maker_detector.detect_market_makers(feed, window_sec=60, enabled=True)
    check("0xc non flaggé (BUY+SELL à 120s > fenêtre 60s)",
          "0xc" not in mm, f"got {mm}")


def test_mm_isolation_between_wallets():
    print("\n▶ MM detector : un MM ne fait pas flagger un autre wallet")
    now = 1_770_000_000
    feed = [
        # Wallet MM
        {"addr": "0xmm", "token": "WBTC",  "side": "buy",  "usd": 10000,
         "block_time": _ts_iso(now - 90)},
        {"addr": "0xmm", "token": "cbBTC", "side": "sell", "usd": 10000,
         "block_time": _ts_iso(now - 60)},
        # Wallet directionnel pur (buy only)
        {"addr": "0xclean", "token": "WBTC", "side": "buy", "usd": 50000,
         "block_time": _ts_iso(now - 60)},
    ]
    mm = market_maker_detector.detect_market_makers(feed, window_sec=60, enabled=True)
    check("0xmm flaggé", "0xmm" in mm, f"got {mm}")
    check("0xclean NON flaggé (buy only)", "0xclean" not in mm, f"got {mm}")


def test_mm_disabled_returns_empty():
    print("\n▶ MM detector : feature OFF → set vide")
    now = 1_770_000_000
    feed = [
        {"addr": "0xa", "token": "WBTC",  "side": "buy",  "usd": 10000,
         "block_time": _ts_iso(now - 90)},
        {"addr": "0xa", "token": "cbBTC", "side": "sell", "usd": 10000,
         "block_time": _ts_iso(now - 60)},
    ]
    mm = market_maker_detector.detect_market_makers(feed, window_sec=60, enabled=False)
    check("enabled=False → set() vide", mm == set(), f"got {mm}")


def test_mm_token_not_in_cluster_ignored():
    print("\n▶ MM detector : token hors clusters → ignoré pour détection")
    now = 1_770_000_000
    feed = [
        # PEPE n'est pas dans ASSET_CLUSTERS → ne peut pas trigger MM
        {"addr": "0xa", "token": "PEPE", "side": "buy",  "usd": 10000,
         "block_time": _ts_iso(now - 90)},
        {"addr": "0xa", "token": "PEPE", "side": "sell", "usd": 10000,
         "block_time": _ts_iso(now - 60)},
    ]
    mm = market_maker_detector.detect_market_makers(feed, window_sec=60, enabled=True)
    check("PEPE non clusterisé → 0xa non flaggé MM",
          "0xa" not in mm, f"got {mm}")


def test_mm_eth_cluster_lst_variants():
    print("\n▶ MM detector : variantes ETH (WETH/stETH/cbETH) dans même cluster")
    now = 1_770_000_000
    feed = [
        {"addr": "0xeth", "token": "WETH",  "side": "buy",  "usd": 10000,
         "block_time": _ts_iso(now - 90)},
        {"addr": "0xeth", "token": "stETH", "side": "sell", "usd": 10000,
         "block_time": _ts_iso(now - 60)},
    ]
    mm = market_maker_detector.detect_market_makers(feed, window_sec=60, enabled=True)
    check("WETH+stETH à 30s → MM (cluster ETH)",
          "0xeth" in mm, f"got {mm}")


def test_aggregate_excludes_mm_from_distinct_count():
    print("\n▶ aggregate_by_token : MM exclus de n_wallets_distinct")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    feed = [
        # 3 wallets buy WETH dans la fenêtre conv
        {"addr": "0xa", "token": "WETH", "side": "buy", "usd": 10000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xb", "token": "WETH", "side": "buy", "usd": 10000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xc", "token": "WETH", "side": "buy", "usd": 10000, "block_time": "2026-06-09T13:55:00Z"},
    ]
    scores = {"0xa": 70, "0xb": 70, "0xc": 70}
    # Sans MM, 3 distincts
    agg_no_mm = cockpit.aggregate_by_token(feed, scores, now=now)
    check("Sans MM : n_wallets_distinct=3",
          agg_no_mm["WETH"]["n_wallets_distinct"] == 3,
          f"got {agg_no_mm['WETH']['n_wallets_distinct']}")
    # Avec 0xb flaggé MM → 2 distincts
    agg_with_mm = cockpit.aggregate_by_token(feed, scores, now=now,
                                              market_makers={"0xb"})
    check("Avec 0xb MM : n_wallets_distinct=2",
          agg_with_mm["WETH"]["n_wallets_distinct"] == 2,
          f"got {agg_with_mm['WETH']['n_wallets_distinct']}")
    check("wallets garde 3 (audit, incl. MM)",
          len(agg_with_mm["WETH"]["wallets"]) == 3,
          f"got {agg_with_mm['WETH']['wallets']}")
    check("wallets_market_makers contient 0xb",
          agg_with_mm["WETH"]["wallets_market_makers"] == ["0xb"],
          f"got {agg_with_mm['WETH']['wallets_market_makers']}")
    check("buy_usd inchangé (MM trades comptent dans le flow)",
          agg_with_mm["WETH"]["buy_usd"] == agg_no_mm["WETH"]["buy_usd"],
          f"with_mm={agg_with_mm['WETH']['buy_usd']} no_mm={agg_no_mm['WETH']['buy_usd']}")
    check("wallets_smart_scores : MM exclu",
          len(agg_with_mm["WETH"]["wallets_smart_scores"]) == 2,
          f"got {agg_with_mm['WETH']['wallets_smart_scores']}")


# ── Run all ────────────────────────────────────────────────────────────────
def main():
    test_decay()
    test_hl_redistribution()
    test_hl_kept_when_available()
    test_convergence_sigmoid()
    test_tiers()
    test_net_flow()
    test_acceleration()
    test_wallet_quality()
    test_hl_mapping()
    test_aggregate_by_token()
    test_build_signals_filter()
    test_select_smart_wallets()
    test_hot_tokens_filtering()
    test_hot_tokens_sort_and_topn()
    test_hot_tokens_empty_when_no_baseline()
    test_baselines_save_load_roundtrip()
    test_baselines_load_missing_file()
    test_baselines_load_corrupted_file()
    test_baselines_load_wrong_schema()
    test_baselines_prune_stale_tokens()
    test_baselines_load_replaces_chain_not_merges()
    test_signals_and_hot_carry_hl_perp_symbol()
    test_liquidity_penalty_tiers()
    test_concentration_penalty_tiers()
    test_apply_penalties_combined()
    test_confidence_with_penalties_visible()
    test_aggregate_tracks_wallet_volumes()
    test_alert_url_validation()
    test_subscription_store_crud()
    test_dispatch_history_anti_spam()
    test_alert_payload_build()
    test_alert_tick_respects_threshold()
    test_pricer_stables()
    test_pricer_native_uses_coingecko_mock()
    test_pricer_dexscreener_mock()
    test_pricer_dexscreener_no_pool()
    # Les tests Etherscan feed mockent get_token_transfers donc n'ont pas
    # besoin d'une vraie clé. On en pose une factice pour passer le guard
    # de fetch_feed qui raise si ETHERSCAN_API_KEY est vide.
    if not os.environ.get("ETHERSCAN_API_KEY"):
        os.environ["ETHERSCAN_API_KEY"] = "test-key-for-mocked-tests"
        etherscan_cockpit_feed.ETHERSCAN_API_KEY = "test-key-for-mocked-tests"
    test_etherscan_feed_stable_for_crypto_buy()
    test_etherscan_feed_crypto_to_crypto_two_entries()
    test_etherscan_feed_skip_stable_stable()
    test_etherscan_feed_skip_one_sided()
    test_etherscan_feed_skip_old_trades()
    test_etherscan_feed_skip_below_min_usd()
    test_etherscan_feed_skip_when_pricer_returns_none()
    test_etherscan_feed_matches_dune_format()
    test_etherscan_feed_consumed_by_aggregate_by_token()
    test_mm_detects_same_cluster_within_window()
    test_mm_does_not_flag_different_clusters()
    test_mm_respects_window()
    test_mm_isolation_between_wallets()
    test_mm_disabled_returns_empty()
    test_mm_token_not_in_cluster_ignored()
    test_mm_eth_cluster_lst_variants()
    test_aggregate_excludes_mm_from_distinct_count()

    print("\n" + "=" * 60)
    if _failures:
        print(f"❌ {len(_failures)} test(s) failed:")
        for f in _failures:
            print(f"   - {f}")
        sys.exit(1)
    else:
        print("✅ All cockpit tests passed.")


if __name__ == "__main__":
    main()
