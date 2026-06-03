#!/usr/bin/env python3
"""
Serveur HTTP standalone - stdlib uniquement, zéro dépendance externe.
Sert le frontend et expose l'API via des threads.
L'analyse (FastAPI + pandas) tourne en subprocess séparé.
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "cache", "results.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")

_state = {"status": "idle", "progress": "", "last_run": None, "error": None, "started_at": None}
_lock = threading.Lock()

def set_state(**kw):
    with _lock:
        _state.update(kw)

def run_analysis():
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
            set_state(status="error", error=f"Process exited with code {proc.returncode}")
    except Exception as e:
        set_state(status="error", error=str(e))


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence default logs

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_file(os.path.join(STATIC_DIR, "index.html"), "text/html")
        elif path in ("/guide", "/guide.html"):
            self._serve_file(os.path.join(STATIC_DIR, "guide.html"), "text/html")
        elif path in ("/bot", "/bot.html"):
            self._serve_file(os.path.join(STATIC_DIR, "bot.html"), "text/html")
        elif path in ("/why", "/why.html"):
            self._serve_file(os.path.join(STATIC_DIR, "why.html"), "text/html")

        elif path == "/api/status":
            with _lock:
                self._json(_state)
        elif path == "/api/wallets":
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                self._json(data)
            else:
                self._json({"wallets": [], "eth_price": 0, "last_updated": None,
                            "total_wallets": 0, "total_volume_usd": 0})
        elif path == "/api/patterns":
            pf = os.path.join(BASE_DIR, "cache", "patterns.json")
            if os.path.exists(pf):
                with open(pf) as f:
                    self._json(json.load(f))
            else:
                self._json({"error": "not_ready"}, 404)

        elif path == "/api/signal":
            # Signal whale-following (BUY/SELL/HOLD) calculé depuis patterns.json
            sys.path.insert(0, BASE_DIR)
            try:
                from whale_signal import compute_signal
                self._json(compute_signal())
            except Exception as e:
                self._json({"error": str(e)}, 500)

        elif path == "/api/patterns/refresh":
            self._json({"error": "use POST"}, 405)

        elif path.startswith("/api/wallet/") and "/trades" in path:
            addr = path.split("/api/wallet/")[1].split("/trades")[0]
            days = int(parse_qs(parsed.query).get("days", ["7"])[0])
            sys.path.insert(0, BASE_DIR)
            from dune_wallet_trades import get_wallet_trade_summary
            try:
                result = get_wallet_trade_summary(addr, days=days)
                self._json(result)
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif path.startswith("/api/wallet/"):
            addr = path.split("/api/wallet/")[1]
            if os.path.exists(CACHE_FILE):
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                for w in data.get("wallets", []):
                    if w.get("address", "").lower() == addr.lower():
                        self._json(w)
                        return
            self._json({"error": "Wallet non trouvé"}, 404)
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/patterns/refresh":
            qs = parse_qs(parsed.query)
            n = int(qs.get("n", ["100"])[0])
            days = int(qs.get("days", ["7"])[0])
            sys.path.insert(0, BASE_DIR)
            from dune_patterns import analyze_patterns
            def _run():
                try:
                    analyze_patterns(n_wallets=n, days=days)
                except Exception as e:
                    print(f"[patterns] {e}", flush=True)
            threading.Thread(target=_run, daemon=True).start()
            self._json({"message": f"Calcul patterns lancé ({n} wallets, {days}j)", "status": "running"})

        elif parsed.path == "/api/refresh":
            with _lock:
                if _state["status"] == "running":
                    self._json({"message": "Analyse déjà en cours", "status": "running"})
                    return
            t = threading.Thread(target=run_analysis, daemon=True)
            t.start()
            self._json({"message": "Analyse lancée", "status": "running"})
        else:
            self._json({"error": "Not found"}, 404)

    def _serve_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._json({"error": "File not found"}, 404)

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    os.makedirs(os.path.join(BASE_DIR, "cache"), exist_ok=True)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Serveur démarré sur http://localhost:{port}", flush=True)
    server.serve_forever()
