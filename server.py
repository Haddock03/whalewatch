#!/usr/bin/env python3
"""
Serveur HTTP standalone WhaleWatch — stdlib uniquement, zéro dépendance externe.
Sert le frontend (static/) et expose l'API.
L'analyse (Dune + pandas) tourne en subprocess séparé.
"""
import http.server
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from urllib.parse import urlparse, parse_qs

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CACHE_DIR  = os.path.join(BASE_DIR, "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "results.json")
PATTERNS_FILE = os.path.join(CACHE_DIR, "patterns.json")

# Permet aux modules d'analyse d'être importés depuis n'importe quel endpoint
sys.path.insert(0, BASE_DIR)

# Routes statiques → fichiers HTML
PAGES = {
    "/":            "index.html", "/index.html": "index.html",
    "/why":         "why.html",   "/why.html":   "why.html",
    "/guide":       "guide.html", "/guide.html": "guide.html",
    "/bot":         "bot.html",   "/bot.html":   "bot.html",
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


def run_analysis():
    """Lance _run_analysis.py en subprocess en streamant le progress."""
    set_state(status="running", progress="Lancement de l'analyse...", error=None,
              started_at=datetime.utcnow().isoformat() + "Z")
    try:
        script = os.path.join(BASE_DIR, "_run_analysis.py")
        proc = subprocess.Popen(
            [sys.executable, script],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, cwd=BASE_DIR
        )
        for line in proc.stdout:
            line = line.strip()
            if line:
                print(f"[analysis] {line}", flush=True)
                set_state(progress=line)
        proc.wait()
        if proc.returncode == 0:
            set_state(status="completed", progress="Analyse terminée.",
                      last_run=datetime.utcnow().isoformat() + "Z")
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


class Handler(http.server.SimpleHTTPRequestHandler):
    # ── Silence les logs verbeux par défaut ─────────────────────────────
    def log_message(self, fmt, *args):
        pass

    # ── Helpers de réponse ──────────────────────────────────────────────
    def _send(self, body: bytes, content_type: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200):
        self._send(json.dumps(data).encode(), "application/json", status)

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
        if path == "/api/status":
            with _lock:
                return self._json(_state)

        if path == "/api/wallets":
            return self._json(load_json(CACHE_FILE, EMPTY_WALLETS))

        if path == "/api/patterns":
            # Renvoie 200 + null si pas encore prêt → le frontend gère la fallback
            return self._json(load_json(PATTERNS_FILE))

        if path.startswith("/api/wallet/") and path.endswith("/trades"):
            addr = path[len("/api/wallet/"):-len("/trades")]
            days = int(parse_qs(parsed.query).get("days", ["7"])[0])
            try:
                from dune_wallet_trades import get_wallet_trade_summary
                return self._json(get_wallet_trade_summary(addr, days=days))
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if path.startswith("/api/wallet/"):
            addr = path[len("/api/wallet/"):].lower()
            data = load_json(CACHE_FILE, EMPTY_WALLETS)
            for w in data.get("wallets", []):
                if w.get("address", "").lower() == addr:
                    return self._json(w)
            return self._json({"error": "Wallet non trouvé"}, 404)

        if path == "/api/patterns/refresh":
            return self._json({"error": "use POST"}, 405)

        return self._json({"error": "Not found"}, 404)

    # ── POST ────────────────────────────────────────────────────────────
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/refresh":
            with _lock:
                running = _state["status"] == "running"
            if running:
                return self._json({"message": "Analyse déjà en cours", "status": "running"})
            threading.Thread(target=run_analysis, daemon=True).start()
            return self._json({"message": "Analyse lancée", "status": "running"})

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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


if __name__ == "__main__":
    os.makedirs(CACHE_DIR, exist_ok=True)
    port = int(os.environ.get("PORT", 8000))
    print(f"WhaleWatch — http://0.0.0.0:{port}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
