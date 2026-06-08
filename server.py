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

# Routes statiques → fichiers HTML
PAGES = {
    "/":              "index.html",       "/index.html":       "index.html",
    "/why":           "why.html",         "/why.html":         "why.html",
    "/guide":         "guide.html",       "/guide.html":       "guide.html",
    "/bot":           "bot.html",         "/bot.html":         "bot.html",
    "/methodology":   "methodology.html", "/methodology.html": "methodology.html",
    "/pro/live":      "pro_live.html",
    "/pro/backtest":  "pro_backtest.html",
    "/pro/watchlist": "pro_watchlist.html",
    "/pro/guide":     "pro_guide.html",
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
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; "
    # CoinGecko expose plusieurs sous-domaines : api.coingecko.com pour
    # les prix, coin-images.coingecko.com / assets.coingecko.com pour
    # les logos. On élargit pour éviter les violations CSP côté ticker.
    "connect-src 'self' https://api.coingecko.com https://*.coingecko.com; "
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
            # Assets statiques : long cache. Pour invalider, change le path
            # (e.g. /static/assets/whale.css?v=2).
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
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
            overall = "ok" if all(c["healthy"] for c in chains_health) else "degraded"
            status_code = 200 if overall == "ok" else 503
            return self._json({
                "status": overall,
                "uptime_seconds": None,  # could track from process start
                "timestamp": _utc_now_iso(),
                "chains": chains_health,
                "stale_threshold_hours": stale_threshold_hours,
            }, status_code)

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

        return self._json({"error": "Not found"}, 404)

    # ── OPTIONS (CORS preflight) ────────────────────────────────────────
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
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
    print(f"WhaleWatch — http://0.0.0.0:{port}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
