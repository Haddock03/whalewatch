# wallet_classifier.py
# Classification granulaire d'un wallet en :
#   eoa | contract | mev | mm | cex | bridge | router
#
# Distingue le « vrai alpha » (eoa / contract opaque) de l'« infrastructure »
# (CEX hot wallets, bridges, routers DEX, market makers, MEV bots) qui ont
# un volume énorme mais ne représentent pas une activité discrétionnaire.
#
# Cette classification est utilisée par smart_score.py pour pénaliser l'infra
# dans le Smart Money Score, et par combine_and_rank.py pour exposer le type
# au frontend.
#
# Miroir fidèle de la fonction `classifyWalletType()` côté frontend
# (static/index.html). Si tu modifies les regex ici, mets-les à jour aussi
# dans le JS pour garder la cohérence.

import re

# ── Patterns de détection ──────────────────────────────────────────────────
# L'ordre des tests dans classify_wallet() compte : MEV puis MM puis CEX
# puis Bridge puis Router. Les patterns les plus spécifiques d'abord.

_RX_MEV = re.compile(
    r"(mev|sandwich|jaredfromsubway|frontrunner|backrun|atomic[\s_-]?arb|flashbot)",
    re.IGNORECASE,
)
_RX_MM = re.compile(
    r"(market[\s_-]?maker|wintermute|jump[\s_-]?trading|jane[\s_-]?street"
    r"|amber[\s_-]?group|gsr|cumberland|flow[\s_-]?traders|alameda|citadel|virtu)",
    re.IGNORECASE,
)
_RX_CEX = re.compile(
    r"(binance|coinbase|kraken|bybit|okx|kucoin|huobi|bitfinex|gate\.io"
    r"|crypto\.com|gemini|bitstamp|mexc|bitget|exchange[:\s])",
    re.IGNORECASE,
)
_RX_BRIDGE = re.compile(
    r"(bridge|stargate|across|hop[\s_-]?protocol|wormhole|synapse"
    r"|debridge|orbiter|li[\s.\-]?fi|connext|cbridge|polybridge)",
    re.IGNORECASE,
)
_RX_ROUTER = re.compile(
    r"(router|aggregator|1inch|paraswap|0x[\s_-]?protocol|cowswap|kyber"
    r"|metamask[\s_-]?swap|universal[\s_-]?router|swaprouter|odos|matcha)",
    re.IGNORECASE,
)


# Types canoniques renvoyés par classify_wallet()
TYPE_MEV      = "mev"
TYPE_MM       = "mm"
TYPE_CEX      = "cex"
TYPE_BRIDGE   = "bridge"
TYPE_ROUTER   = "router"
TYPE_CONTRACT = "contract"
TYPE_EOA      = "eoa"

# Sous-ensemble considéré comme infrastructure (= pas d'alpha discrétionnaire)
INFRA_TYPES = {TYPE_MEV, TYPE_MM, TYPE_CEX, TYPE_BRIDGE, TYPE_ROUTER}

# Labels lisibles pour l'UI
TYPE_DISPLAY = {
    TYPE_MEV:      "MEV",
    TYPE_MM:       "MM",
    TYPE_CEX:      "CEX",
    TYPE_BRIDGE:   "Bridge",
    TYPE_ROUTER:   "Router",
    TYPE_CONTRACT: "Contract",
    TYPE_EOA:      "EOA",
}


def classify_wallet(wallet):
    """Renvoie un dict {key, label, is_infra} à partir d'un wallet record.

    `wallet` doit contenir au minimum :
        - category   : str   (catégorie Dune historique : 'MEV Bot', 'DEX Protocol',
                              'Market Maker', 'Smart Contract', 'Unknown', 'Other')
        - label      : str | None  (name tag Etherscan ou contract_name)
        - is_contract: bool | None
    Champs optionnels utilisés en heuristique :
        - contract_name : str | None
    """
    cat = (wallet.get("category") or "").strip()
    lbl = (wallet.get("label") or "") + " " + (wallet.get("contract_name") or "")
    is_contract = wallet.get("is_contract")

    # 1) MEV — la category Dune est fiable, sinon regex de secours
    if cat == "MEV Bot" or _RX_MEV.search(lbl):
        return _make(TYPE_MEV)

    # 2) Market Maker — idem
    if cat == "Market Maker" or _RX_MM.search(lbl):
        return _make(TYPE_MM)

    # 3) CEX (Dune ne nous le donne pas, regex obligatoire)
    if _RX_CEX.search(lbl):
        return _make(TYPE_CEX)

    # 4) Bridge (Dune non plus, regex obligatoire)
    if _RX_BRIDGE.search(lbl):
        return _make(TYPE_BRIDGE)

    # 5) Router DEX — soit cat Dune = "DEX Protocol", soit regex sur label
    if cat == "DEX Protocol" or _RX_ROUTER.search(lbl):
        return _make(TYPE_ROUTER)

    # 6) Smart Contract opaque — ni infra connue, mais code on-chain
    if cat == "Smart Contract" or is_contract is True:
        return _make(TYPE_CONTRACT)

    # 7) EOA — humain (ou bot non détecté). Par défaut.
    return _make(TYPE_EOA)


def _make(key):
    return {
        "key": key,
        "label": TYPE_DISPLAY[key],
        "is_infra": key in INFRA_TYPES,
    }


def is_infrastructure(wallet):
    """Raccourci : True si le wallet appartient à l'infrastructure."""
    return classify_wallet(wallet)["is_infra"]


if __name__ == "__main__":
    cases = [
        {"label": "Jaredfromsubway (MEV bot)", "category": "MEV Bot",     "is_contract": True},
        {"label": "Wintermute (MM)",           "category": "Market Maker","is_contract": False},
        {"label": "Binance 14",                "category": "Other",        "is_contract": False},
        {"label": "Stargate Bridge",           "category": "Other",        "is_contract": True},
        {"label": "1inch v5 Aggregator",       "category": "DEX Protocol", "is_contract": True},
        {"label": "Uniswap Universal Router",  "category": "DEX Protocol", "is_contract": True},
        {"label": "Random Contract",           "category": "Smart Contract","is_contract": True},
        {"label": "Unknown",                   "category": "Unknown",      "is_contract": False},
    ]
    for w in cases:
        wt = classify_wallet(w)
        infra = "INFRA" if wt["is_infra"] else "alpha"
        print(f"{w['label']:35s} → {wt['label']:10s} ({infra})")
