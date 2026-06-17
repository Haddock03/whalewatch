# infra_blacklist.py
# Blacklist hardcodée d'adresses connues d'infrastructure DEX.
# Couvre routers, bridges, hot wallets CEX, et entités connues qui ne
# peuvent JAMAIS être considérées comme "smart money discrétionnaire".
#
# Pourquoi hardcodé : wallet_classifier.py détecte déjà via regex sur les
# labels Etherscan, mais (a) les labels manquent sur les chains récentes,
# (b) certains routers n'ont pas de label public, (c) on veut une garantie
# stricte : si une adresse est dans cette liste, AUCUNE chance qu'elle
# émette un signal.
#
# Sources : addresses publiques documentées (Uniswap docs, 1inch docs,
# Etherscan tags officiels, bridges officiels).
# Toutes en lowercase pour comparaison directe.

# ── Routers DEX ────────────────────────────────────────────────────────────
_ROUTERS = {
    # Uniswap
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",  # Uniswap V3 SwapRouter02
    "0xe592427a0aece92de3edee1f18e0157c05861564",  # Uniswap V3 SwapRouter (legacy)
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",  # Uniswap V2 Router02
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Uniswap Universal Router (ETH)
    "0x643770e279d5d0733f21d6dc03a8efbabf3255b4",  # Uniswap Universal Router V1.2
    "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b",  # Uniswap Universal Router V2
    # 1inch
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch Aggregator V5
    "0x1111111254fb6c44bac0bed2854e76f90643097d",  # 1inch Aggregator V4
    "0x111111125421ca6dc452d289314280a0f8842a65",  # 1inch Aggregation Router V6
    # 0x Protocol / Matcha
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff",  # 0x ExchangeProxy / Matcha
    "0xdef1abe32c034e558cdd535791643c58a13acc10",  # 0x Allowance Target
    "0x70bf6634ee8cb27d04478f184b9b8bb13e5f4710",  # 0x Settler
    # Paraswap / Augustus
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57",  # Augustus V5
    "0x6a000f20005980200259b80c5102003040001068",  # Augustus V6
    # Odos
    "0xcf5540fffcdc3d510b18bfca6d2b9987b0772559",  # Odos Router V2 (ETH)
    "0xa669e7a0d4b3e4fa48af2de86bd4cd7126be4e13",  # Odos Router V2 (ARB)
    # CoW Protocol / Cowswap
    "0x9008d19f58aabd9ed0d60971565aa8510560ab41",  # CoW Settlement
    # Kyber
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",  # KyberSwap Aggregator
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b6",  # Kyber Meta Aggregator
    # MetaMask Swap
    "0x881d40237659c251811cec9c364ef91dc08d300c",  # MetaMask Swap Router
    # Maestro / Banana sniping bots
    "0x80a64c6d7f12c47b7c66c5b4e20e72bc1fcd5d9e",  # Maestro Router
    # LI.FI
    "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae",  # LI.FI Diamond
    # ParaSwap V6 router on Arbitrum/multi
    "0x6a000f20005980200259b80c5102003040001068",
}

# ── Bridges (Arbitrum + cross-chain populaires) ───────────────────────────
_BRIDGES = {
    # Arbitrum officiel
    "0x8315177ab297ba92a06054ce80a67ed4dbd7ed3a",  # Arbitrum L1 Inbox
    "0x4dbd4fc535ac27206064b68ffcf827b0a60bab3f",  # Arbitrum L1 Bridge
    "0xa10c7ce4b876998858b1a9e12b10092229c40a0a",  # Arbitrum DAI Gateway
    "0xcee284f754e854890e311e3280b767f80797180d",  # Arbitrum L1 ERC20 Gateway
    # Stargate
    "0x8731d54e9d02c286767d56ac03e8037c07e01e98",  # Stargate Router
    "0x150f94b44927f078737562f0fcf3c95c01cc2376",  # Stargate Router (ARB)
    # Across
    "0x4d9079bb4165aeb4084c526a32695dcfd2f77381",  # Across SpokePool (ETH)
    "0xe35e9842fceaca96570b734083f4a58e8f7c5f2a",  # Across SpokePool (ARB)
    # Hop Protocol
    "0xb8901acb165ed027e32754e0ffe830802919727f",  # Hop ETH Bridge
    # Wormhole / Portal
    "0x3ee18b2214aff97000d974cf647e54b5d0b0b1e5",  # Wormhole Token Bridge
    # Synapse
    "0x2796317b0ff8538f253012862c06787adfb8ceb6",  # Synapse Bridge
    # deBridge
    "0x43de2d77bf8027e25dbd179b491e8d64f38398aa",  # deBridge Gate
    # Orbiter
    "0x80c67432656d59144ceff962e8faf8926599bcf8",  # Orbiter Router (ETH)
}

# ── CEX hot wallets (gros joueurs) ────────────────────────────────────────
_CEX_HOT = {
    # Binance
    "0x28c6c06298d514db089934071355e5743bf21d60",  # Binance 14
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549",  # Binance 15
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d",  # Binance 16
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",  # Binance 17
    "0x9696f59e4d72e237be84ffd425dcad154bf96976",  # Binance 18
    "0x5a52e96bacdabb82fd05763e25335261b270efcb",  # Binance 20
    "0x290275e3db66394c52272398959845170e4dcb88",  # Binance Hot 21
    "0xd5c08681719445a5fdce2bda98b341a49050d821",  # Binance 23
    # Coinbase
    "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",  # Coinbase 1
    "0x503828976d22510aad0201ac7ec88293211d23da",  # Coinbase 2
    "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740",  # Coinbase 3
    "0x3cd751e6b0078be393132286c442345e5dc49699",  # Coinbase 4
    "0xb5d85cbf7cb3ee0d56b3bb207d5fc4b82f43f511",  # Coinbase 5
    "0x6b76f8b1e9e59913bfe758821887311ba1805cab",  # Coinbase 7
    # Kraken
    "0xae2d4617c862309a3d75a0ffb358c7a5009c673f",  # Kraken 10
    "0x267be1c1d684f78cb4f6a176c4911b741e4ffdc0",  # Kraken 9
    "0xfa52274dd61e1643d2205169732f29114bc240b3",  # Kraken 4
    # OKX
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b",  # OKX hot
    "0x868dab0b8e21ec0a48b726a1ccf25826c78c6d7f",  # OKX hot
    # Bybit
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40",  # Bybit hot
    # Crypto.com
    "0x6262998ced04146fa42253a5c0af90ca02dfd2a3",  # Crypto.com 1
    "0x46340b20830761efd32832a74d7169b29feb9758",  # Crypto.com 2
}

# Union complète des blacklists
BLACKLIST = (_ROUTERS | _BRIDGES | _CEX_HOT)


def is_blacklisted(address):
    """True si l'adresse appartient à la blacklist infra hardcodée.
    Insensible à la casse, accepte avec ou sans préfixe 0x."""
    if not address or not isinstance(address, str):
        return False
    addr = address.strip().lower()
    if not addr.startswith("0x"):
        addr = "0x" + addr
    return addr in BLACKLIST


def blacklist_stats():
    """Pour debug : compte par catégorie."""
    return {
        "routers": len(_ROUTERS),
        "bridges": len(_BRIDGES),
        "cex_hot": len(_CEX_HOT),
        "total": len(BLACKLIST),
    }


if __name__ == "__main__":
    print(f"Blacklist stats: {blacklist_stats()}")
    samples = [
        ("0x1111111254eeb25477b68fb85ed929f73a960582", "1inch V5"),
        ("0xDEf1C0ded9bec7F1a1670819833240f027b25EfF", "0x ExchangeProxy (mixed case)"),
        ("0x28c6c06298d514db089934071355e5743bf21d60", "Binance 14"),
        ("0xae2fc483527b8ef99eb5d9b44875f005ba1fae13", "Jared MEV (NOT in blacklist, caught by classifier)"),
        ("0xabcdef0123456789abcdef0123456789abcdef01", "Random wallet"),
    ]
    for addr, label in samples:
        print(f"  {is_blacklisted(addr):5} → {label}")
