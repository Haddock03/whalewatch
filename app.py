# app.py
# Backend FastAPI - Ethereum Wallet Tracker

import os
import sys
import json
import threading
from datetime import datetime

from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

sys.path.insert(0, os.path.dirname(__file__))
from dune_top_wallets import fetch_top_wallets
from combine_and_rank import merge_and_rank, KNOWN_LABELS, CACHE_FILE
from dune_wallet_trades import get_wallet_trade_summary
from dune_patterns import analyze_patterns, CACHE_FILE as PATTERNS_CACHE

app = FastAPI(title="Ethereum Wallet Tracker")

# Montage des fichiers statiques
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

# État global de l'analyse
_state = {
    "status": "idle",       # idle | running | completed | error
    "progress": "",
    "last_run": None,
    "error": None,
    "started_at": None,
}
_lock = threading.Lock()


def set_state(**kwargs):
    with _lock:
        _state.update(kwargs)


def run_analysis_job():
    """Job principal : Dune → Etherscan → merge → cache JSON"""
    set_state(status="running", progress="Démarrage...", error=None,
              started_at=datetime.utcnow().isoformat() + "Z")

    def cb(msg):
        set_state(progress=msg)

    try:
        cb("Récupération des données Dune Analytics...")
        df_dune = fetch_top_wallets(progress_cb=cb)

        cb("Fusion Etherscan + Dune en cours...")
        merge_and_rank(
            df_dune=df_dune,
            additional_wallets=list(KNOWN_LABELS.keys()),
            progress_cb=cb,
        )

        set_state(
            status="completed",
            progress="Analyse terminée.",
            last_run=datetime.utcnow().isoformat() + "Z",
        )
    except Exception as e:
        set_state(status="error", progress="", error=str(e))
        print(f"[ERROR] run_analysis_job: {e}")


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.get("/api/status")
def api_status():
    with _lock:
        return dict(_state)


@app.get("/api/wallets")
def api_wallets():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {
        "wallets": [],
        "eth_price": 0,
        "last_updated": None,
        "total_wallets": 0,
        "total_volume_usd": 0,
    }


@app.post("/api/refresh")
def api_refresh(background_tasks: BackgroundTasks):
    with _lock:
        if _state["status"] == "running":
            return JSONResponse({"message": "Analyse déjà en cours", "status": "running"})

    background_tasks.add_task(run_analysis_job)
    return {"message": "Analyse lancée", "status": "running"}


@app.get("/api/wallet/{address}")
def api_wallet_detail(address: str):
    """Détails d'un wallet spécifique depuis le cache"""
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            data = json.load(f)
        for w in data.get("wallets", []):
            if w.get("address", "").lower() == address.lower():
                return w
    return JSONResponse({"error": "Wallet non trouvé"}, status_code=404)


@app.get("/api/patterns")
def api_patterns():
    """Patterns depuis le cache (ou 404 si pas encore calculé)"""
    if os.path.exists(PATTERNS_CACHE):
        with open(PATTERNS_CACHE) as f:
            return json.load(f)
    return JSONResponse({"error": "Patterns non calculés — lancez /api/patterns/refresh"}, status_code=404)


@app.post("/api/patterns/refresh")
def api_patterns_refresh(background_tasks: BackgroundTasks, n: int = 100, days: int = 7):
    """Lance le calcul des patterns en background (n=nombre de wallets, days=fenêtre)"""
    def _run():
        try:
            analyze_patterns(n_wallets=n, days=days)
        except Exception as e:
            print(f"[patterns] error: {e}")
    background_tasks.add_task(_run)
    return {"message": f"Calcul patterns lancé ({n} wallets, {days}j)", "status": "running"}


@app.get("/api/wallet/{address}/trades")
def api_wallet_trades(address: str, days: int = 7):
    """Résumé des trades DEX d'un wallet via Dune (requête live)"""
    try:
        result = get_wallet_trade_summary(address, days=days)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    print("Démarrage du serveur sur http://localhost:8000")
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
