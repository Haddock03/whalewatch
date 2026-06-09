#!/usr/bin/env python3
"""
Serveur HTTP standalone WhaleWatch — stdlib uniquement, zéro dépendance externe.
Sert le frontend (static/) et expose l'API.
L'analyse (Dune + pandas) tourne en subprocess séparé.
"""
import gzip
import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CACHE_DIR  = os.path.join(BASE_DIR, "cache")

# Permet aux modules d'analyse d'être importés depuis n'importe quel endpoint
sys.path.insert(0, BASE_DIR)

# Multi-chain (chains.py est dans BASE_DIR maintenant que sys.path est patché)
from chains import resolve as resolve_chain, DEFAULT_CHAIN, CHAINS as CHAIN_CONFIGS


def _chain_paths(chain_param):
    """Renvoie (chain_key, cache_path, patterns_path) en gérant les erreurs."""
    try:
        cfg = resolve_chain(chain_param or DEFAULT_CHAIN)
    except ValueError:
        cfg = resolve_chain(DEFAULT_CHAIN)
    return cfg["key"], cfg["cache_path"], cfg["patterns_path"]


# Seuil de fraîcheur du cache Cockpit. Au-delà de 3× l'intervalle de refresh,
# on considère le cache « stale » et on alerte le frontend. La var d'env est
# la même que celle utilisée par cockpit_worker — modifier l'un modifie
# automatiquement le seuil de l'autre.
_COCKPIT_REFRESH_SEC = int(os.environ.get("COCKPIT_REFRESH_INTERVAL_SEC", "60"))
_COCKPIT_STALE_AFTER_SEC = max(180, 3 * _COCKPIT_REFRESH_SEC)


def _mask_url(url):
    """Masque le path d'une URL pour ne pas exposer le secret (token Discord,
    n8n key, etc.). Garde le host + 8 premiers chars du path."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        host = p.netloc
        head = (p.path or "/")[:8]
        return f"{p.scheme}://{host}{head}…"
    except Exception:
        return url[:20] + "…"


def _cockpit_cache_meta(payload):
    """Calcule age + flag stale pour un payload cockpit lu depuis le cache JSON.
    Renvoie un dict { cache_age_seconds, is_stale, stale_threshold_seconds }.
    Si pas de generated_at, considère stale=True (worker n'a jamais tourné)."""
    from datetime import datetime, timezone
    gen = payload.get("generated_at") if payload else None
    if not gen:
        return {
            "cache_age_seconds": None,
            "is_stale": True,
            "stale_threshold_seconds": _COCKPIT_STALE_AFTER_SEC,
        }
    try:
        dt = datetime.fromisoformat(gen.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, AttributeError, TypeError):
        return {"cache_age_seconds": None, "is_stale": True,
                "stale_threshold_seconds": _COCKPIT_STALE_AFTER_SEC}
    return {
        "cache_age_seconds": round(age, 1),
        "is_stale": age > _COCKPIT_STALE_AFTER_SEC,
        "stale_threshold_seconds": _COCKPIT_STALE_AFTER_SEC,
    }

# Routes statiques → fichiers HTML
PAGES = {
    "/":              "index.html",       "/index.html":       "index.html",
    "/why":           "why.html",         "/why.html":         "why.html",
    "/guide":         "guide.html",       "/guide.html":       "guide.html",
    "/bot":           "bot.html",         "/bot.html":         "bot.html",
    "/methodology":   "methodology.html", "/methodology.html": "methodology.html",
    "/pro/live":       "pro_live.html",
    "/pro/cockpit":    "pro_cockpit.html",
    "/pro/hot-tokens": "pro_hot_tokens.html",
    "/pro/backtest":   "pro_backtest.html",
    "/pro/watchlist":  "pro_watchlist.html",
    "/pro/guide":      "pro_guide.html",
}

MIME = {
    "css":  "text/css",
    "js":   "application/javascript",
    "html": "text/html; charset=utf-8",
    "json": "application/json",
    "svg":  "image/svg+xml",
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "ico":  "image/x-icon",
}

EMPTY_WALLETS  = {"wallets": [], "eth_price": 0, "last_updated": None,
                  "total_wallets": 0, "total_volume_usd": 0}

_state = {"status": "idle", "progress": "", "last_run": None,
          "error": None, "started_at": None}
_lock = threading.Lock()


def set_state(**kw):
    with _lock:
        _state.update(kw)


# ── CoinGecko proxy ─────────────────────────────────────────────────────────
# On proxify les appels CoinGecko (prix live + ticker) côté serveur pour :
#   1. Éviter les erreurs CORS / 429 dans la console du browser (qui
#      dégradent le score Best Practices Lighthouse).
#   2. Mutualiser les requêtes — 1 appel serveur sert tous les visiteurs
#      pendant la durée du cache.
# Cache mémoire 60s. Si CoinGecko renvoie une erreur, on sert la dernière
# réponse valide pour éviter le clignotement.
_CG_CACHE = {}  # url → {"data": ..., "ts": ..., "stale_ok_until": ...}
_CG_LOCK = threading.Lock()

def _cg_fetch(url, cache_seconds=60, stale_seconds=600, timeout=8):
    now = time.time()
    with _CG_LOCK:
        entry = _CG_CACHE.get(url)
        if entry and (now - entry["ts"]) < cache_seconds:
            return entry["data"], None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "WhaleWatch/1.0 (+https://whalewatchapp.io)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        data = json.loads(raw)
        with _CG_LOCK:
            _CG_CACHE[url] = {"data": data, "ts": now}
        return data, None
    except Exception as e:
        # Servir la dernière réponse valide si elle existe et n'est pas trop vieille
        with _CG_LOCK:
            entry = _CG_CACHE.get(url)
            if entry and (now - entry["ts"]) < stale_seconds:
                return entry["data"], None
        return None, str(e)


def load_json(path, default=None):
    """Charge un JSON ou renvoie le default si le fichier n'existe pas / corrompu."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def run_analysis(n_wallets: int = 100, days: int = 7, chain: str = DEFAULT_CHAIN):
    """Lance _run_analysis.py en subprocess en streamant le progress.

    n_wallets/days/chain sont passés via env vars (WW_N_WALLETS, WW_DAYS,
    WW_CHAIN) — plus simples que des args CLI pour la composition avec
    subprocess.Popen.
    """
    set_state(status="running", progress=f"Lancement de l'analyse {chain}...",
              error=None, started_at=_utc_now_iso())
    try:
        script = os.path.join(BASE_DIR, "_run_analysis.py")
        env = {**os.environ, "WW_N_WALLETS": str(n_wallets),
               "WW_DAYS": str(days), "WW_CHAIN": chain}
        proc = subprocess.Popen(
            [sys.executable, script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=BASE_DIR, env=env
        )
        for line in proc.stdout:
            line = line.strip()
            if line:
                print(f"[analysis] {line}", flush=True)
                set_state(progress=line)
        proc.wait()
        if proc.returncode == 0:
            set_state(status="completed", progress="Analyse terminée.",
                      last_run=_utc_now_iso())
        else:
            set_state(status="error",
                      error=f"Process exited with code {proc.returncode}")
    except Exception as e:
        set_state(status="error", error=str(e))


def run_patterns(n, days):
    """Lance le calcul patterns Dune en thread daemon."""
    try:
        from dune_patterns import analyze_patterns
        analyze_patterns(n_wallets=n, days=days)
    except Exception as e:
        print(f"[patterns] {e}", flush=True)


# ─── En-têtes de sécurité HTTP ──────────────────────────────────────────────
# Appliqués à toutes les réponses servies par Handler._send().
# Audit Lighthouse 08/06/2026 : 5 audits Trust & Safety à passer.
#
# CSP : pour ne pas casser l'existant, on autorise :
#   - script-src : 'self' + cdn.jsdelivr.net (Chart.js) + 'unsafe-inline'
#     (le code a beaucoup de JS inline ; passer à des nonces serait un
#     chantier dédié — à voir dans une future itération)
#   - style-src  : 'self' + fonts.googleapis + 'unsafe-inline'
#   - font-src   : 'self' + fonts.gstatic + data:
#   - img-src    : 'self' + https: + data: (logos CoinGecko sur urls variables)
#   - connect-src: 'self' + api.coingecko (XHR/fetch prix live)
#   - frame-ancestors 'none' : anti-clickjacking
#   - base-uri / form-action 'self' : hardening
# Trusted Types n'est PAS activé (`require-trusted-types-for 'script'`) car
# le code utilise innerHTML à plusieurs endroits. À planifier après refacto.
_CSP = (
    "default-src 'self'; "
    # script-src : ajout cloudflareinsights.com (analytics auto-injecté par
    # Cloudflare/Railway via le proxy). Sans ça → erreur console.
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net "
    "https://static.cloudflareinsights.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    # connect-src : ajout cdn.jsdelivr.net pour autoriser le fetch du
    # sourcemap de Chart.js par les DevTools (sinon erreur Lighthouse).
    # CoinGecko reste pour fallback ; en pratique on passe par /api/prices.
    "connect-src 'self' https://api.coingecko.com https://*.coingecko.com "
    "https://cdn.jsdelivr.net https://static.cloudflareinsights.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'"
    # Trusted Types retiré : créait des erreurs console (le default policy
    # pass-through ne couvre pas tous les cas dans app.js et certains
    # frameworks). À reprendre avec DOMPurify dans un chantier dédié.
)

# HSTS : 2 ans + preload + sous-domaines. Envoyé toujours ; en HTTP local
# le navigateur l'ignore (pas de risque). En HTTPS prod (Railway) il
# force le HTTPS pour 2 ans.
_HSTS = "max-age=63072000; includeSubDomains; preload"

_SECURITY_HEADERS = {
    "Content-Security-Policy":          _CSP,
    "Strict-Transport-Security":        _HSTS,
    "Cross-Origin-Opener-Policy":       "same-origin",
    "X-Frame-Options":                  "DENY",        # garde-fou hérité
    "X-Content-Type-Options":           "nosniff",     # anti MIME-sniffing
    "Referrer-Policy":                  "strict-origin-when-cross-origin",
    "Permissions-Policy":               "geolocation=(), microphone=(), camera=()",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    # ── Silence les logs verbeux par défaut ─────────────────────────────
    def log_message(self, fmt, *args):
        pass

    # Types MIME compressibles via gzip (audit Lighthouse P1.4 — TTFB).
    # On compresse seulement si :
    #   - body ≥ 1024 bytes (en-dessous, gzip ajoute plus d'overhead qu'il
    #     n'économise de bande passante)
    #   - le client annonce gzip dans Accept-Encoding
    #   - le type MIME est compressible (text, json, JS, CSS, SVG, XML)
    _GZIPPABLE_PREFIXES = (
        "text/", "application/json", "application/javascript",
        "application/xml", "image/svg+xml",
    )

    def _accepts_gzip(self):
        ae = (self.headers.get("Accept-Encoding") or "").lower()
        return "gzip" in ae

    def _maybe_gzip(self, body: bytes, content_type: str):
        """Compresse `body` en gzip si éligible. Renvoie (body, encoding_or_None)."""
        if len(body) < 1024 or not self._accepts_gzip():
            return body, None
        if not any(content_type.startswith(p) for p in self._GZIPPABLE_PREFIXES):
            return body, None
        buf = io.BytesIO()
        # compresslevel 6 = bon compromis ratio/CPU pour du contenu servi à la volée
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
            gz.write(body)
        return buf.getvalue(), "gzip"

    # ── Helpers de réponse ──────────────────────────────────────────────
    def _send(self, body: bytes, content_type: str, status: int = 200, extra_headers=None):
        # Gzip compression (P1.4) — réduit le TTFB et la taille des bytes
        # servis (HTML/JSON/CSS/JS/SVG).
        body, encoding = self._maybe_gzip(body, content_type)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
            # Vary indique aux caches que la réponse dépend du Accept-Encoding
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Access-Control-Allow-Origin", "*")
        # En-têtes de sécurité (audit Lighthouse Trust & Safety)
        for k, v in _SECURITY_HEADERS.items():
            self.send_header(k, v)
        # Cache control — évite que les browsers servent une version stale
        # des pages HTML après un déploiement.
        #   Pages HTML  → no-cache, must-revalidate (revalide à chaque fois)
        #   Assets /static/ → cache 1 an (servis par self.path startswith /static/)
        #   API JSON    → no-store (toujours frais)
        if content_type.startswith("text/html"):
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        elif content_type == "application/json":
            self.send_header("Cache-Control", "no-store")
        elif self.path.startswith("/static/"):
            # Assets statiques : cache 1 jour avec revalidation. Au-delà,
            # le browser fait un If-Modified-Since pour vérifier. Évite
            # le piège "ancien JS cached en immutable" après un deploy.
            # Pour un cache plus agressif : ajouter ?v=X au src dans le HTML
            # à chaque update du fichier (cache busting manuel).
            self.send_header("Cache-Control", "public, max-age=86400, must-revalidate")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200, extra_headers=None):
        self._send(json.dumps(data).encode(), "application/json", status, extra_headers)

    def _serve_file(self, path: str, content_type: str):
        try:
            with open(path, "rb") as f:
                self._send(f.read(), content_type)
        except FileNotFoundError:
            self._json({"error": "Not found"}, 404)

    # ── GET ─────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Pages
        if path in PAGES:
            return self._serve_file(os.path.join(STATIC_DIR, PAGES[path]), MIME["html"])

        # Static assets (css/js/img)
        if path.startswith("/static/"):
            ext = path.rsplit(".", 1)[-1].lower()
            mime = MIME.get(ext, "application/octet-stream")
            return self._serve_file(os.path.join(BASE_DIR, path.lstrip("/")), mime)

        # API
        if path == "/api/health":
            # Endpoint léger pour load balancer / uptime monitoring.
            # Renvoie 200 si le serveur tourne ; payload détaille la santé
            # par chain (cache existe, âge < 24h, nb wallets > 0).
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            chains_health = []
            stale_threshold_hours = 24
            for k, v in CHAIN_CONFIGS.items():
                cp = os.path.join(CACHE_DIR, v["cache_file"])
                d = load_json(cp, None)
                age_hours = None
                healthy = False
                if d and d.get("last_updated"):
                    try:
                        dt = datetime.fromisoformat(d["last_updated"].replace("Z", "+00:00"))
                        age_hours = round((now - dt).total_seconds() / 3600, 2)
                        healthy = age_hours < stale_threshold_hours and len(d.get("wallets") or []) > 0
                    except Exception:
                        pass
                chains_health.append({
                    "chain": k,
                    "has_cache": d is not None,
                    "age_hours": age_hours,
                    "wallets": len(d.get("wallets", []) if d else []),
                    "healthy": healthy,
                })
            # Santé du Cockpit : chains activées + fraîcheur de leur cache.
            # Considéré healthy si le cache existe et n'est pas stale (cf.
            # _cockpit_cache_meta).
            cockpit_health = []
            enabled = [c.strip() for c in
                       os.environ.get("COCKPIT_ENABLED_CHAINS", "ethereum,arbitrum,bnb").split(",")
                       if c.strip()]
            for ch in enabled:
                cp = os.path.join(CACHE_DIR, f"cockpit_{ch}.json")
                cdata = load_json(cp, None)
                cmeta = _cockpit_cache_meta(cdata)
                cockpit_health.append({
                    "chain": ch,
                    "has_cache": cdata is not None,
                    "age_seconds": cmeta["cache_age_seconds"],
                    "is_stale": cmeta["is_stale"],
                    "signals": len(cdata.get("signals") or []) if cdata else 0,
                })
            overall = "ok" if all(c["healthy"] for c in chains_health) else "degraded"
            status_code = 200 if overall == "ok" else 503
            return self._json({
                "status": overall,
                "uptime_seconds": None,  # could track from process start
                "timestamp": _utc_now_iso(),
                "chains": chains_health,
                "stale_threshold_hours": stale_threshold_hours,
                "cockpit": {
                    "enabled_chains": enabled,
                    "stale_threshold_seconds": _COCKPIT_STALE_AFTER_SEC,
                    "chains": cockpit_health,
                },
            }, status_code)

        if path == "/api/prices":
            # Proxy CoinGecko simple price (ETH + BTC). Cache 60s côté serveur.
            data, err = _cg_fetch(
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=ethereum,bitcoin&vs_currencies=usd",
                cache_seconds=60
            )
            if data is not None:
                return self._json(data)
            return self._json({"error": err or "unavailable"}, 503)

        if path == "/api/ticker":
            # Proxy CoinGecko top 10 markets. Cache 120s côté serveur.
            data, err = _cg_fetch(
                "https://api.coingecko.com/api/v3/coins/markets"
                "?vs_currency=usd&order=market_cap_desc&per_page=10&page=1"
                "&sparkline=false&price_change_percentage=24h",
                cache_seconds=120
            )
            if data is not None:
                return self._json(data)
            return self._json({"error": err or "unavailable"}, 503)

        if path == "/api/status":
            with _lock:
                return self._json(_state)

        if path == "/api/chains":
            # Liste des chains disponibles pour le sélecteur frontend
            return self._json([{
                "key": k,
                "label": v["label"],
                "symbol": v["symbol"],
                "chainid": v["chainid"],
                "explorer_url": v["explorer_url"],
            } for k, v in CHAIN_CONFIGS.items()])

        if path == "/api/chains/summary":
            # Agrégat de tous les caches : utile pour un widget "Vue d'ensemble"
            # qui compare les 6 chains supportées sans avoir à fetch chacune.
            summary = []
            for k, v in CHAIN_CONFIGS.items():
                cache_path = os.path.join(CACHE_DIR, v["cache_file"])
                data = load_json(cache_path, None)
                if data and data.get("wallets"):
                    wallets = data["wallets"]
                    top = max(wallets, key=lambda w: w.get("total_volume_usd") or 0, default={})
                    summary.append({
                        "key": k,
                        "label": v["label"],
                        "symbol": v["symbol"],
                        "explorer_url": v["explorer_url"],
                        "total_volume_usd": data.get("total_volume_usd") or 0,
                        "total_wallets": data.get("total_wallets") or len(wallets),
                        "last_updated": data.get("last_updated"),
                        "top_wallet_address": top.get("address"),
                        "top_wallet_volume_usd": top.get("total_volume_usd") or 0,
                        "has_data": True,
                    })
                else:
                    summary.append({
                        "key": k,
                        "label": v["label"],
                        "symbol": v["symbol"],
                        "explorer_url": v["explorer_url"],
                        "total_volume_usd": 0,
                        "total_wallets": 0,
                        "last_updated": None,
                        "top_wallet_address": None,
                        "top_wallet_volume_usd": 0,
                        "has_data": False,
                    })
            return self._json({"chains": summary})

        if path == "/api/wallets":
            chain = parse_qs(parsed.query).get("chain", [DEFAULT_CHAIN])[0]
            _, cache_path, _ = _chain_paths(chain)
            return self._json(load_json(cache_path, EMPTY_WALLETS))

        if path == "/api/patterns":
            chain = parse_qs(parsed.query).get("chain", [DEFAULT_CHAIN])[0]
            _, _, patterns_path = _chain_paths(chain)
            # Renvoie 200 + null si pas encore prêt → le frontend gère la fallback
            return self._json(load_json(patterns_path))

        if path == "/api/alerts":
            try:
                from alerts import read_alerts
                return self._json(read_alerts())
            except Exception as e:
                return self._json({"error": str(e), "alerts": []}, 200)

        # ── Webhook alert subscriptions (P2) ──────────────────────────────
        # Liste publique (mais le secret est dans l'URL webhook elle-même —
        # le créateur de la sub doit garder l'URL privée).
        if path == "/api/alerts/subscriptions":
            try:
                import alert_dispatcher
                subs = alert_dispatcher.get_subscription_store().list_all()
                # Masque partiellement l'URL pour la liste (préserve l'host
                # mais cache le path/secret typique d'un webhook Discord/Slack).
                masked = [{**s, "target_masked": _mask_url(s.get("target", ""))} for s in subs]
                return self._json({"subscriptions": masked,
                                   "stale_threshold_seconds": _COCKPIT_STALE_AFTER_SEC})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path.startswith("/api/alerts/subscriptions/") and path.endswith("/details"):
            # /api/alerts/subscriptions/{id}/details — renvoie l'URL en clair
            # (utile pour debug, mais auth REFRESH_TOKEN exigée si configuré).
            if not self._check_refresh_token():
                return
            sub_id = path[len("/api/alerts/subscriptions/"):-len("/details")]
            try:
                import alert_dispatcher
                s = alert_dispatcher.get_subscription_store().get(sub_id)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            if not s:
                return self._json({"error": "not found"}, 404)
            return self._json(s)

        # ── Cockpit (P0) ──────────────────────────────────────────────────
        # 3 endpoints qui lisent UNIQUEMENT le cache JSON écrit par le
        # worker (cockpit_worker.py). Aucune query Dune/HL ici → poll user
        # = O(1), zéro latence externe.
        #
        # Chaque endpoint enrichit la réponse avec cache_age_seconds et
        # is_stale pour que le frontend puisse alerter visuellement si le
        # worker s'est arrêté (cache > 3× l'intervalle de refresh).
        if path in ("/api/cockpit/feed", "/api/cockpit/signals",
                    "/api/cockpit/config", "/api/cockpit/hot-tokens"):
            chain = parse_qs(parsed.query).get("chain", [DEFAULT_CHAIN])[0]
            # Valide la chain (résolution lève ValueError sinon)
            try:
                resolve_chain(chain)
            except ValueError:
                return self._json({"error": f"chain inconnue: {chain}"}, 400)
            cockpit_path = os.path.join(CACHE_DIR, f"cockpit_{chain}.json")
            payload = load_json(cockpit_path, None)
            if payload is None:
                # Worker pas encore passé OU chain pas activée dans
                # COCKPIT_ENABLED_CHAINS — on renvoie un squelette vide
                # pour que le frontend affiche un état "loading" propre.
                # is_stale=True force le bandeau d'alerte UI.
                return self._json({
                    "chain": chain,
                    "generated_at": None,
                    "signals": [],
                    "convergence_radar": [],
                    "feed": [],
                    "hot_tokens": [],
                    "hl_available": False,
                    "status": "warming_up",
                    "cache_age_seconds": None,
                    "is_stale": True,
                    "stale_threshold_seconds": _COCKPIT_STALE_AFTER_SEC,
                })
            meta = _cockpit_cache_meta(payload)
            if path == "/api/cockpit/feed":
                return self._json({
                    "chain": payload["chain"],
                    "generated_at": payload["generated_at"],
                    "feed_window_min": payload.get("feed_window_min"),
                    "feed": payload.get("feed") or [],
                    "smart_wallets_count": payload.get("smart_wallets_count"),
                    "feed_trades_count": payload.get("feed_trades_count"),
                    **meta,
                })
            if path == "/api/cockpit/signals":
                return self._json({
                    "chain": payload["chain"],
                    "generated_at": payload["generated_at"],
                    "signals": payload.get("signals") or [],
                    "convergence_radar": payload.get("convergence_radar") or [],
                    "hl_available": payload.get("hl_available", False),
                    "conv_window_min": payload.get("conv_window_min"),
                    "conv_threshold": payload.get("conv_threshold"),
                    "half_life_min": payload.get("half_life_min"),
                    **meta,
                })
            if path == "/api/cockpit/hot-tokens":
                return self._json({
                    "chain": payload["chain"],
                    "generated_at": payload["generated_at"],
                    "hot_tokens": payload.get("hot_tokens") or [],
                    "min_accel_ratio": payload.get("hot_min_accel_ratio"),
                    "min_inflow_usd":  payload.get("hot_min_inflow_usd"),
                    "feed_window_min": payload.get("feed_window_min"),
                    "smart_wallets_count": payload.get("smart_wallets_count"),
                    **meta,
                })
            # /api/cockpit/config
            return self._json({
                "chain": payload["chain"],
                "feed_window_min":  payload.get("feed_window_min"),
                "conv_window_min":  payload.get("conv_window_min"),
                "conv_threshold":   payload.get("conv_threshold"),
                "half_life_min":    payload.get("half_life_min"),
                "min_smart_score":  payload.get("min_smart_score"),
                "weights":          payload.get("weights") or {},
                "hl_available":     payload.get("hl_available", False),
                "generated_at":     payload.get("generated_at"),
                **meta,
            })

        if path.startswith("/api/wallet/") and path.endswith("/trades"):
            addr = path[len("/api/wallet/"):-len("/trades")]
            qs = parse_qs(parsed.query)
            days  = int(qs.get("days",  ["7"])[0])
            chain = qs.get("chain", [DEFAULT_CHAIN])[0]
            try:
                from dune_wallet_trades import get_wallet_trade_summary
                return self._json(get_wallet_trade_summary(addr, days=days, chain=chain))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path.startswith("/api/wallet/"):
            addr = path[len("/api/wallet/"):].lower()
            chain = parse_qs(parsed.query).get("chain", [DEFAULT_CHAIN])[0]
            _, cache_path, _ = _chain_paths(chain)
            data = load_json(cache_path, EMPTY_WALLETS)
            for w in data.get("wallets", []):
                if w.get("address", "").lower() == addr:
                    return self._json(w)
            return self._json({"error": "Wallet non trouvé"}, 404)

        if path == "/api/patterns/refresh":
            return self._json({"error": "Method Not Allowed", "expected": "POST"},
                              405, {"Allow": "POST"})

        return self._json({"error": "Not found"}, 404)

    # ── POST ────────────────────────────────────────────────────────────
    def _check_refresh_token(self):
        """Exige un jeton secret pour les endpoints de refresh.
        Le jeton est lu dans la variable d'env REFRESH_TOKEN.
        Renvoie True si autorisé, sinon écrit une erreur 401 et renvoie False."""
        expected = os.environ.get("REFRESH_TOKEN")
        if not expected:
            return True  # pas de jeton configuré = pas de protection (rétro-compat)
        provided = self.headers.get("X-Refresh-Token", "")
        if provided != expected:
            self._json({"error": "Non autorisé.", "status": "unauthorized"}, 401)
            return False
        return True

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/api/refresh", "/api/patterns/refresh"):
            if not self._check_refresh_token():
                return

        if path == "/api/refresh":
            # Auth — si REFRESH_TOKEN est défini en env, exige le même token
            # dans le header X-Refresh-Token. Empêche un visiteur anonyme
            # de déclencher des Sonars Dune coûteux. En dev local sans token
            # défini, l'auth est skip.
            expected_token = os.environ.get("REFRESH_TOKEN", "").strip()
            if expected_token:
                provided = self.headers.get("X-Refresh-Token", "").strip()
                if provided != expected_token:
                    return self._json({
                        "error": "Non autorisé — token X-Refresh-Token manquant ou invalide",
                        "status": "unauthorized",
                    }, 401)
            # Validation des clés API avant lancement — évite un échec
            # silencieux dans le subprocess que l'utilisateur attend pour rien.
            missing = []
            if not os.environ.get("DUNE_API_KEY"):
                missing.append("DUNE_API_KEY")
            if not os.environ.get("ETHERSCAN_API_KEY"):
                missing.append("ETHERSCAN_API_KEY")
            if missing:
                return self._json({
                    "error": f"Clés API manquantes : {', '.join(missing)}. "
                             "Voir .env.example et le README pour la configuration.",
                    "status": "config_error",
                }, 400)
            with _lock:
                running = _state["status"] == "running"
            if running:
                return self._json({"message": "Analyse déjà en cours", "status": "running"})
            qs = parse_qs(parsed.query)
            n     = int(qs.get("n",    ["100"])[0])
            days  = int(qs.get("days", ["7"])[0])
            chain = qs.get("chain", [DEFAULT_CHAIN])[0]
            threading.Thread(target=run_analysis, args=(n, days, chain), daemon=True).start()
            return self._json({"message": f"Analyse {chain} lancée ({n} wallets, {days}j)",
                               "status": "running"})

        if path == "/api/patterns/refresh":
            qs = parse_qs(parsed.query)
            n    = int(qs.get("n",    ["100"])[0])
            days = int(qs.get("days", ["7"])[0])
            threading.Thread(target=run_patterns, args=(n, days), daemon=True).start()
            return self._json({"message": f"Calcul patterns lancé ({n} wallets, {days}j)",
                               "status": "running"})

        # ── Webhook alert subscriptions (P2) ──────────────────────────────
        if path == "/api/alerts/subscriptions":
            # Création. Auth REFRESH_TOKEN exigée si configuré (la sub donne
            # accès à un endpoint qui sera POST par le serveur — ne pas laisser
            # n'importe qui y ajouter des URLs).
            if not self._check_refresh_token():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length > 0 else b"{}"
                data = json.loads(body.decode("utf-8"))
            except (ValueError, json.JSONDecodeError) as e:
                return self._json({"error": f"body JSON invalide: {e}"}, 400)
            try:
                import alert_dispatcher
                sub = alert_dispatcher.get_subscription_store().create(
                    type_=data.get("type") or "webhook",
                    target=data.get("target") or "",
                    threshold=data.get("threshold", 70),
                    chain=data.get("chain", "*"),
                    label=data.get("label"),
                )
                return self._json({"subscription": sub,
                                   "target_masked": _mask_url(sub.get("target", ""))}, 201)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path.startswith("/api/alerts/subscriptions/") and path.endswith("/test"):
            if not self._check_refresh_token():
                return
            sub_id = path[len("/api/alerts/subscriptions/"):-len("/test")]
            try:
                import alert_dispatcher
                sub = alert_dispatcher.get_subscription_store().get(sub_id)
                if not sub:
                    return self._json({"error": "subscription not found"}, 404)
                fake_signal = {
                    "token": "TEST",
                    "confidence": 99,
                    "tier": "Très fort",
                    "net_side": "buy",
                    "n_wallets": 5,
                    "inflow_usd": 1_000_000,
                    "age_min": 0,
                    "hl_perp_symbol": "BTC",
                }
                payload = alert_dispatcher._build_payload(
                    sub.get("chain") or "ethereum", fake_signal, sub["id"]
                )
                payload["text"] = "🧪 [TEST] " + payload.get("text", "")
                ok, info = alert_dispatcher.send_webhook(sub["target"], payload)
                return self._json({"ok": ok, "info": info}, 200 if ok else 502)
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        return self._json({"error": "Not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/alerts/subscriptions/"):
            if not self._check_refresh_token():
                return
            sub_id = path[len("/api/alerts/subscriptions/"):]
            try:
                import alert_dispatcher
                deleted = alert_dispatcher.get_subscription_store().delete(sub_id)
                if not deleted:
                    return self._json({"error": "not found"}, 404)
                return self._json({"deleted": True, "id": sub_id})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        return self._json({"error": "Not found"}, 404)

    # ── OPTIONS (CORS preflight) ────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Refresh-Token")
        self.end_headers()


if __name__ == "__main__":
    os.makedirs(CACHE_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", 8000))
    # Warn au démarrage si les clés API sont absentes — utile pour les
    # premiers déploiements où l'utilisateur a oublié .env.
    missing_keys = []
    if not os.environ.get("DUNE_API_KEY"):
        missing_keys.append("DUNE_API_KEY")
    if not os.environ.get("ETHERSCAN_API_KEY"):
        missing_keys.append("ETHERSCAN_API_KEY")
    if missing_keys:
        print(f"⚠️  Clés API manquantes : {', '.join(missing_keys)}", flush=True)
        print(f"    Le serveur tourne mais /api/refresh renverra une erreur 400.", flush=True)
        print(f"    Configure-les via .env (voir .env.example et README).", flush=True)

    # ── Cockpit worker ──────────────────────────────────────────────────
    # Démarrage du thread daemon qui refresh cache/cockpit_<chain>.json
    # toutes les COCKPIT_REFRESH_INTERVAL_SEC (60s par défaut).
    # No-op si WW_DISABLE_COCKPIT=1 ou si DUNE_API_KEY est absente.
    try:
        import cockpit_worker
        cockpit_worker.start_background()
    except Exception as e:
        print(f"[cockpit] worker failed to start: {e}", flush=True)

    print(f"WhaleWatch — http://0.0.0.0:{port}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
