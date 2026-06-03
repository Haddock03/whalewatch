# combine_and_rank.py
# Fusionne Etherscan + Dune et génère le classement final

import requests
import pandas as pd
import time
import sys
import os
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from etherscan_scraper import analyze_wallet_volume

ETHERSCAN_API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
CACHE_FILE = os.path.join(os.path.dirname(__file__), "cache", "results.json")

KNOWN_LABELS = {
    "0xae2Fc483527B8EF99EB5D9B44875F005ba1FaE13": "Jaredfromsubway (MEV bot)",
    "0xDef1C0ded9bec7F1a1670819833240f027b25EfF": "0x Protocol",
    "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD": "Uniswap Universal Router",
    "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45": "Uniswap v3 Router 2",
    "0x1111111254EEB25477B68fb85Ed929f73A960582": "1inch v5 Aggregator",
    "0x6b75d8AF000000e20B7a7DDf000Ba900b4009Ee": "MEV Bot (anonyme)",
    "0xE592427A0AEce92De3Edee1F18E0157C05861564": "Uniswap v3 SwapRouter",
    "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D": "Uniswap v2 Router",
    "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F": "SushiSwap Router",
    "0x00000000003b3cc22aF3aE1EAc0440BcEe416B40": "MEV Bot (Sandwich)",
    "0xA69babEF1cA67A37Ffaf7a485DfFF3382056e78C": "Wintermute (MM)",
    "0x56178a0d5F301bAf6CF3e1Cd53d9863437345Bf9": "Jump Trading",
}


def categorize_label(label, mev_score=0, is_contract=False, nb_trades=0):
    """Catégorise un wallet selon son label et ses métriques"""
    if label and label != "Unknown":
        lw = label.lower()
        if "mev" in lw or "sandwich" in lw or "jared" in lw or "bot" in lw or "flashbot" in lw:
            return "MEV Bot"
        if any(x in lw for x in ["uniswap", "sushiswap", "1inch", "0x protocol", "curve", "balancer", "paraswap"]):
            return "DEX Protocol"
        if any(x in lw for x in ["trading", "market maker", "wintermute", "jump", "alameda", "citadel"]):
            return "Market Maker"
        return "Other"

    # Heuristiques pour wallets Unknown
    if mev_score >= 2:
        return "MEV Bot"
    if is_contract and nb_trades and nb_trades > 10000:
        return "MEV Bot"  # contrat avec énorme volume = très probablement MEV/arb bot
    if is_contract and nb_trades and nb_trades > 1000:
        return "Smart Contract"
    return "Unknown"


def get_eth_price_usd():
    """Prix ETH en temps réel via CoinGecko"""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=10
        )
        price = r.json()["ethereum"]["usd"]
        print(f"Prix ETH actuel: ${price:,.0f}")
        return float(price)
    except Exception as e:
        print(f"CoinGecko indisponible ({e}), fallback 3000 USD")
        return 3000.0


def enrich_with_etherscan(wallets, eth_price, progress_cb=None):
    """Enrichit chaque wallet avec les données Etherscan"""
    enriched = []
    total = len(wallets)

    for i, wallet in enumerate(wallets, 1):
        msg = f"[{i}/{total}] Etherscan: {wallet[:14]}..."
        print(f"  {msg}")
        if progress_cb:
            progress_cb(msg)
        try:
            data = analyze_wallet_volume(wallet)
            data["volume_usd_estimated"] = round(data.get("volume_eth_recent", 0) * eth_price, 2)
            # Label : KNOWN_LABELS prioritaire, sinon contract_name Etherscan, sinon Unknown
            forced_label = KNOWN_LABELS.get(wallet)
            if forced_label:
                data["label"] = forced_label
            elif data.get("contract_name"):
                data["label"] = data["contract_name"]
            else:
                data["label"] = "Unknown"
            enriched.append(data)
        except Exception as e:
            print(f"    Erreur {wallet[:12]}: {e}")
            enriched.append({
                "address": wallet,
                "label": KNOWN_LABELS.get(wallet, "Unknown"),
                "category": "Unknown",
                "volume_eth_recent": 0,
                "volume_usd_estimated": 0,
                "total_tx_count": 0,
                "token_transfer_count": 0,
                "unique_tokens_traded": 0,
                "current_balance_eth": 0,
                "gas_spent_eth": 0,
                "error": str(e)
            })
        time.sleep(0.22)  # ~4.5 req/s

    return pd.DataFrame(enriched)


def fmt_volume(v):
    if pd.isna(v) or v == 0:
        return "$0"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    if v >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def merge_and_rank(df_dune=None, dune_csv_path=None, additional_wallets=None, progress_cb=None):
    """
    Pipeline complet de fusion Dune + Etherscan.

    Args:
        df_dune: DataFrame Dune déjà chargé (prioritaire sur dune_csv_path)
        dune_csv_path: chemin CSV Dune alternatif
        additional_wallets: adresses supplémentaires à toujours inclure
        progress_cb: callback(msg) pour les mises à jour de statut
    Returns:
        DataFrame final classé
    """
    def log(msg):
        print(msg)
        if progress_cb:
            progress_cb(msg)

    log("=== ÉTAPE 1/4 - Chargement données Dune ===")
    if df_dune is None or df_dune.empty:
        if dune_csv_path and os.path.exists(dune_csv_path):
            df_dune = pd.read_csv(dune_csv_path)
            log(f"  {len(df_dune)} wallets chargés depuis CSV Dune")
        else:
            log("  Pas de données Dune disponibles, utilisation des wallets connus uniquement")
            df_dune = pd.DataFrame(columns=["address", "total_volume_usd", "nb_trades"])

    if "wallet" in df_dune.columns:
        df_dune = df_dune.rename(columns={
            "wallet": "address",
            "total_volume_usd": "dune_volume_usd",
            "nb_trades": "dune_nb_trades"
        })
    elif "address" in df_dune.columns:
        df_dune = df_dune.rename(columns={
            "total_volume_usd": "dune_volume_usd",
            "nb_trades": "dune_nb_trades"
        })

    log("=== ÉTAPE 2/4 - Prix ETH ===")
    eth_price = get_eth_price_usd()

    log("=== ÉTAPE 3/4 - Enrichissement Etherscan ===")
    wallets_to_analyze = list(df_dune["address"].head(100)) if "address" in df_dune.columns else []
    if additional_wallets:
        for w in additional_wallets:
            if w not in wallets_to_analyze:
                wallets_to_analyze.append(w)
    wallets_to_analyze = list(dict.fromkeys(wallets_to_analyze))
    log(f"  Analyse de {len(wallets_to_analyze)} wallets...")

    df_eth = enrich_with_etherscan(wallets_to_analyze, eth_price, progress_cb=progress_cb)

    log("=== ÉTAPE 4/4 - Fusion et classement ===")
    df = pd.merge(df_dune, df_eth, on="address", how="outer")

    df["dune_volume_usd"] = pd.to_numeric(df.get("dune_volume_usd", 0), errors="coerce").fillna(0)
    df["volume_usd_estimated"] = pd.to_numeric(df.get("volume_usd_estimated", 0), errors="coerce").fillna(0)
    df["total_volume_usd"] = df["dune_volume_usd"] + df["volume_usd_estimated"]

    if "label" not in df.columns:
        df["label"] = df["address"].map(KNOWN_LABELS).fillna("Unknown")
    else:
        df["label"] = df["label"].fillna(df["address"].map(KNOWN_LABELS)).fillna("Unknown")

    def assign_category(row):
        return categorize_label(
            row.get("label"),
            mev_score=row.get("mev_score", 0) or 0,
            is_contract=bool(row.get("is_contract", False)),
            nb_trades=row.get("dune_nb_trades", 0) or 0,
        )
    df["category"] = df.apply(assign_category, axis=1)

    df = df.sort_values("total_volume_usd", ascending=False).reset_index(drop=True)
    df["rank"] = df.index + 1
    df["volume_display"] = df["total_volume_usd"].apply(fmt_volume)
    df["dune_volume_display"] = df["dune_volume_usd"].apply(fmt_volume)

    # Sauvegarde CSV
    os.makedirs("cache", exist_ok=True)
    csv_path = "top_wallets_final.csv"
    export_cols = ["rank", "address", "label", "category", "is_contract", "contract_name",
                   "total_volume_usd", "volume_display", "dune_volume_usd", "dune_nb_trades",
                   "total_tx_count", "token_transfer_count", "unique_tokens_traded",
                   "current_balance_eth", "gas_spent_eth", "volume_eth_recent", "mev_score"]
    export_available = [c for c in export_cols if c in df.columns]
    df[export_available].to_csv(csv_path, index=False)

    # Sauvegarde JSON pour l'API
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    wallets_json = []
    for _, row in df.iterrows():
        w = {}
        for col in export_available:
            val = row.get(col)
            if pd.isna(val) if not isinstance(val, str) else False:
                val = None
            elif hasattr(val, 'item'):
                val = val.item()
            w[col] = val
        wallets_json.append(w)

    cache_data = {
        "wallets": wallets_json,
        "eth_price": eth_price,
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "total_wallets": len(wallets_json),
        "total_volume_usd": float(df["total_volume_usd"].sum()),
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(cache_data, f)

    log(f"\nExporté: {csv_path} ({len(df)} wallets)")
    log(f"Cache JSON: {CACHE_FILE}")
    log(f"Prix ETH: ${eth_price:,.0f}")
    return df


if __name__ == "__main__":
    extra_wallets = list(KNOWN_LABELS.keys())
    df_final = merge_and_rank(
        dune_csv_path="dune_top_wallets.csv",
        additional_wallets=extra_wallets
    )
    display_cols = ["rank", "address", "label", "volume_display", "dune_nb_trades", "total_tx_count"]
    available = [c for c in display_cols if c in df_final.columns]
    print("\n" + "="*80)
    print("          TOP WALLETS PAR VOLUME - ETHEREUM")
    print("="*80)
    print(df_final[available].head(20).to_string(index=False))
