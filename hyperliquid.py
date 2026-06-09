# hyperliquid.py
# Client minimal Hyperliquid Info API pour le module Cockpit (v1).
#
# Lecture seule, pas de signing : POST /info avec body JSON {"type": "..."}.
# v1 utilise uniquement `metaAndAssetCtxs` (funding rate + open interest + mark
# par perp) — léger, 1 seul appel pour tous les univers.
#
# Conception :
#   - Cache mémoire 60s (TTL), thread-safe, similaire au _CG_CACHE de server.py.
#   - Backoff exponentiel sur 429 (jamais de retry aveugle) : 1s, 2s, 4s, 8s max.
#   - Mapping token on-chain → symbole perp HL via whitelist statique v1.
#     Couvre les ~30 perps les plus liquides. Les wrapped (WETH, WBTC, …)
#     sont mappés vers leur underlying. Mismatch → renvoie None → cockpit.py
#     neutralisera le composant HL (voir Confidence Index spec).
#
# Notes Hyperliquid :
#   - Arbitrum n'est que le rail de funding (Bridge2). Les données de trading
#     vivent sur HyperCore (L1 HL), donc on ne va PAS chercher ces données
#     on-chain Arbitrum.
#   - Pas de testnet perp complet — mainnet en lecture seule sans risque.
#   - Rate limits Info > Exchange. Toute réponse cachée 60s réduit drastiquement
#     les chances de déclencher un 429 quand plusieurs chains tournent.
import json
import threading
import time
import urllib.error
import urllib.request

INFO_URL = "https://api.hyperliquid.xyz/info"

# Cache mémoire : key=(type,) → {"data": ..., "ts": float}
_CACHE = {}
_LOCK = threading.Lock()
_DEFAULT_TTL = 60      # secondes
_HTTP_TIMEOUT = 10     # secondes par requête
_MAX_BACKOFF = 8       # secondes
_USER_AGENT = "WhaleWatch/1.0 (+https://whalewatchapp.io)"


# ── Whitelist mapping token on-chain → perp HL ─────────────────────────────
# v1 statique : ce sont les perps HL les plus liquides qui matchent des tokens
# DEX tracés on-chain. Le symbole perp HL est en majuscules ASCII (BTC, ETH, …)
# ou préfixé `k` pour les memecoins / "1000x" (kPEPE = 1000 PEPE).
#
# Règle de priorité dans to_hl_perp() :
#   1. wrapped ou native → underlying (WETH→ETH, stETH→ETH, WBTC→BTC, …)
#   2. memecoin avec perp k-version → kSYM (PEPE→kPEPE)
#   3. match direct si dans WHITELIST
#   4. None
_NATIVE_ETH = {"WETH", "ETH", "STETH", "WSTETH", "RETH", "CBETH"}
_NATIVE_BTC = {"WBTC", "BTC", "BTCB", "TBTC"}
_NATIVE_BNB = {"WBNB", "BNB"}
_NATIVE_AVAX = {"WAVAX", "AVAX"}
_NATIVE_MATIC = {"WMATIC", "MATIC", "POL"}
_NATIVE_SOL = {"WSOL", "SOL"}

# Memecoins qui sont sur HL en k-version (1000×). Le côté on-chain trade
# le token natif, mais sur HL c'est la version kSYM qui est cotée.
_MEME_K = {"PEPE", "BONK", "FLOKI", "SHIB"}

# Perps HL les plus liquides (juin 2026, top OI). Pour le mapping direct
# token-symbol → perp-symbol, sans transformation.
_WHITELIST = {
    "BTC", "ETH", "SOL", "ARB", "OP", "AVAX", "BNB", "MATIC",
    "LINK", "UNI", "AAVE", "DOGE", "XRP", "ADA", "DOT", "ATOM",
    "NEAR", "APT", "SUI", "INJ", "TIA", "SEI", "JTO", "JUP",
    "WIF", "WLD", "ORDI", "RENDER", "FET", "TAO", "LDO", "PENDLE",
    "ENA", "STRK", "PYTH", "DYDX",
    # k-versions
    "kPEPE", "kBONK", "kFLOKI", "kSHIB",
}


def to_hl_perp(token_sym, chain=None):
    """Mappe un symbole token on-chain vers un symbole perp HL.

    Renvoie None si le token n'a pas d'équivalent HL connu. cockpit.py
    interprète None comme « N/A » et redistribue les poids (cf. spec §4).
    `chain` est accepté pour future-proofing mais pas utilisé v1.
    """
    if not token_sym:
        return None
    sym = str(token_sym).strip().upper()
    if not sym:
        return None
    # 1. Wrapped/natives → underlying
    if sym in _NATIVE_ETH:
        return "ETH"
    if sym in _NATIVE_BTC:
        return "BTC"
    if sym in _NATIVE_BNB:
        return "BNB"
    if sym in _NATIVE_AVAX:
        return "AVAX"
    if sym in _NATIVE_MATIC:
        return "MATIC"
    if sym in _NATIVE_SOL:
        return "SOL"
    # 2. Memecoins k-version
    if sym in _MEME_K:
        candidate = f"k{sym}"  # respecte la convention HL (minuscule k)
        if candidate in _WHITELIST:
            return candidate
    # 3. Match direct
    if sym in _WHITELIST:
        return sym
    return None


# ── Client HTTP ─────────────────────────────────────────────────────────────
def _post_info(payload, timeout=_HTTP_TIMEOUT):
    """POST /info avec body JSON. Renvoie (data, error_str). error_str non-None
    encode un 429 ou autre échec — le caller décide du backoff."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        INFO_URL, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return json.loads(raw), None
    except urllib.error.HTTPError as e:
        # 429 propagé tel quel pour que _fetch_with_cache déclenche le backoff
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def _fetch_with_cache(cache_key, payload, ttl=_DEFAULT_TTL, max_retries=4):
    """Fetch + cache + backoff exponentiel sur 429.

    cache_key : string unique pour ce type de requête (ex. "metaAndAssetCtxs").
    max_retries : nombre total de tentatives avant abandon. Délais : 1,2,4,8s
    (capped à _MAX_BACKOFF). Si stale entry existe en cache (>ttl mais <600s),
    on la sert plutôt que None pour éviter le clignotement.
    """
    now = time.time()
    with _LOCK:
        entry = _CACHE.get(cache_key)
        if entry and (now - entry["ts"]) < ttl:
            return entry["data"], None

    delay = 1.0
    last_err = None
    for attempt in range(max_retries):
        data, err = _post_info(payload)
        if data is not None:
            with _LOCK:
                _CACHE[cache_key] = {"data": data, "ts": time.time()}
            return data, None
        last_err = err
        # Backoff uniquement sur 429 ; autre erreur → tente une fois de plus
        # et stop.
        if err and "429" in err and attempt < max_retries - 1:
            time.sleep(min(delay, _MAX_BACKOFF))
            delay *= 2
            continue
        break

    # Fallback : si on a une entrée cache « stale mais récente » (<10 min),
    # mieux vaut la servir que renvoyer None et casser le breakdown UI.
    with _LOCK:
        entry = _CACHE.get(cache_key)
        if entry and (now - entry["ts"]) < 600:
            return entry["data"], None
    return None, last_err or "unavailable"


# ── API publique ────────────────────────────────────────────────────────────
def get_asset_ctxs(ttl=_DEFAULT_TTL):
    """Renvoie un dict {perp_sym: {funding, open_interest, mark_px, day_volume}}.

    Source : POST /info type=metaAndAssetCtxs.
    Réponse HL = liste de 2 éléments : [meta, assetCtxs].
      meta.universe = [{name:"BTC",szDecimals:5,...}, {name:"ETH",...}, ...]
      assetCtxs[i]  = {funding, openInterest, markPx, dayNtlVlm, ...}
    L'ordre meta.universe[i] correspond à assetCtxs[i].

    Renvoie ({}, err_str) si l'API est inaccessible — caller traite comme N/A
    pour tous les perps (le composant HL du Confidence Index est neutralisé).
    """
    raw, err = _fetch_with_cache("metaAndAssetCtxs",
                                 {"type": "metaAndAssetCtxs"}, ttl=ttl)
    if err or not raw:
        return {}, err
    try:
        meta, ctxs = raw[0], raw[1]
        universe = meta.get("universe") or []
    except (IndexError, KeyError, TypeError, AttributeError):
        return {}, "malformed response"

    out = {}
    for i, asset in enumerate(universe):
        name = asset.get("name")
        if not name or i >= len(ctxs):
            continue
        ctx = ctxs[i] or {}
        try:
            out[name] = {
                "funding": float(ctx.get("funding") or 0.0),
                "open_interest": float(ctx.get("openInterest") or 0.0),
                "mark_px": float(ctx.get("markPx") or 0.0),
                "day_ntl_vlm": float(ctx.get("dayNtlVlm") or 0.0),
            }
        except (TypeError, ValueError):
            continue
    return out, None


def align_score(token_sym, on_chain_side, asset_ctxs=None):
    """Score d'alignement 0-100 entre le sens du flux on-chain et le
    positionnement perp HL pour ce token.

    on_chain_side : "buy" | "sell" (déduit du net flow agrégé).
    asset_ctxs    : sortie de get_asset_ctxs() — passé en arg pour ne pas
                    re-fetcher dans une boucle de N signaux.

    Heuristique v1 — funding sign + OI direction :
      - funding > 0  → longs payent shorts (perp en pression long majoritaire)
      - funding < 0  → shorts payent longs (perp en pression short majoritaire)
      - Pondéré par |funding| (clampé) pour la confiance.
      - OI sert de pondération de liquidité (perp illiquide → score plus neutre).

    Renvoie un float dans [0, 100], ou None si pas de perp correspondant
    (cockpit.py redistribue alors le poids du composant HL).
    """
    perp = to_hl_perp(token_sym)
    if perp is None:
        return None
    if asset_ctxs is None:
        asset_ctxs, err = get_asset_ctxs()
        if err:
            return None
    ctx = asset_ctxs.get(perp)
    if not ctx:
        return None

    funding = ctx.get("funding") or 0.0
    oi = ctx.get("open_interest") or 0.0
    side = (on_chain_side or "").lower()
    if side not in ("buy", "sell"):
        return 50.0  # neutre par défaut

    # Convention HL : funding positif = perp en pression LONG.
    # Si on a un BUY on-chain ET un funding positif → alignement.
    # Si on a un SELL on-chain ET un funding négatif → alignement.
    # Sinon → opposition. funding ≈ 0 → neutre.
    #
    # |funding| typique hourly : 0.00001 à 0.0005 (0.001%/h à 0.05%/h).
    # On clamp à 0.0005 pour la pondération [0, 1].
    f_strength = min(1.0, abs(funding) / 0.0005)

    if funding > 0:
        score = 100.0 if side == "buy" else 0.0
    elif funding < 0:
        score = 0.0 if side == "buy" else 100.0
    else:
        return 50.0

    # Tirer vers le neutre (50) selon |funding| : plus le funding est faible,
    # plus le signal est ambigu donc plus on rapproche de 50.
    score = 50.0 + (score - 50.0) * f_strength

    # OI faible → encore plus ramener vers 50. Seuil de référence : 1M$ d'OI.
    # En dessous, on dilue de moitié.
    if oi < 1_000_000:
        score = 50.0 + (score - 50.0) * 0.5

    return round(max(0.0, min(100.0, score)), 1)


if __name__ == "__main__":
    print("→ Fetch metaAndAssetCtxs…")
    ctxs, err = get_asset_ctxs()
    if err:
        print(f"  ERREUR: {err}")
    else:
        print(f"  {len(ctxs)} perps cachés")
        for sym in ("BTC", "ETH", "SOL", "kPEPE"):
            c = ctxs.get(sym)
            if c:
                print(f"  {sym:8s} funding={c['funding']:+.6f}  OI=${c['open_interest']:>14,.0f}  mark=${c['mark_px']:>10,.2f}")

    print("\n→ Test mapping :")
    for tok in ("WETH", "ETH", "PEPE", "WBTC", "USDC", "RANDOM"):
        print(f"  {tok:10s} → {to_hl_perp(tok)}")

    print("\n→ Test align_score :")
    if ctxs:
        for tok, side in [("WETH", "buy"), ("WBTC", "sell"), ("PEPE", "buy"), ("USDC", "buy")]:
            s = align_score(tok, side, asset_ctxs=ctxs)
            print(f"  {tok:6s} {side:5s} → align={s}")
