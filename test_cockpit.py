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

import cockpit
import cockpit_worker
import hyperliquid


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
    print("\n▶ build_signals filtre convergence (seuil N=3)")
    now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    feed = [
        # 2 wallets distincts seulement sur DOGE → sous le seuil
        {"addr": "0xa", "token": "DOGE", "side": "buy", "usd": 5000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xb", "token": "DOGE", "side": "buy", "usd": 5000, "block_time": "2026-06-09T13:55:00Z"},
        # 3 wallets distincts sur SOL → au seuil
        {"addr": "0xa", "token": "SOL", "side": "buy", "usd": 5000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xb", "token": "SOL", "side": "buy", "usd": 5000, "block_time": "2026-06-09T13:55:00Z"},
        {"addr": "0xc", "token": "SOL", "side": "buy", "usd": 5000, "block_time": "2026-06-09T13:55:00Z"},
    ]
    scores = {"0xa": 70, "0xb": 70, "0xc": 70}
    agg = cockpit.aggregate_by_token(feed, scores, now=now)
    signals = cockpit.build_signals(agg, baselines_1h={}, hl_asset_ctxs={})
    tokens = {s["token"] for s in signals}
    check("SOL devient signal (n=3)", "SOL" in tokens, f"got {tokens}")
    check("DOGE filtré (n=2 < seuil)", "DOGE" not in tokens, f"got {tokens}")


# ── Select smart wallets ──────────────────────────────────────────────────
def test_select_smart_wallets():
    print("\n▶ Sélection des smart wallets depuis cache results")
    cache_data = {
        "wallets": [
            {"address": "0xa", "smart_score": 80, "category": "Other"},
            {"address": "0xB", "smart_score": 66, "category": "Other"},  # case insensitive
            {"address": "0xc", "smart_score": 90, "category": "MEV Bot"},  # filtré infra
            {"address": "0xd", "smart_score": 50, "category": "Other"},   # sous seuil
            {"address": "0xe", "smart_score": 70, "category": "CEX"},      # filtré infra
        ]
    }
    addrs, scores = cockpit.select_smart_wallets(cache_data, min_score=65)
    check("0xa retenu (80)",  "0xa" in addrs, f"got {addrs}")
    check("0xb retenu lowercased (66)", "0xb" in addrs, f"got {addrs}")
    check("0xc filtré (MEV Bot)",       "0xc" not in addrs, f"got {addrs}")
    check("0xd filtré (sous seuil 65)", "0xd" not in addrs, f"got {addrs}")
    check("0xe filtré (CEX)",           "0xe" not in addrs, f"got {addrs}")
    check("scores[0xa] == 80", scores.get("0xa") == 80)


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
