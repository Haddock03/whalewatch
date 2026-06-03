#!/bin/bash
# run.sh — Lance le serveur Ethereum Wallet Tracker
# Usage: ./run.sh [--port 8000]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${2:-8000}"

echo "=== Ethereum Wallet Tracker ==="
echo "Répertoire: $SCRIPT_DIR"

# Vérifier Python 3
if ! command -v python3 &>/dev/null; then
  echo "ERREUR: python3 non trouvé"
  exit 1
fi

# Installer les dépendances si nécessaire
if ! python3 -c "import fastapi, uvicorn, pandas, requests" 2>/dev/null; then
  echo "Installation des dépendances..."
  pip3 install -r "$SCRIPT_DIR/requirements.txt" --quiet
fi

# Créer les dossiers nécessaires
mkdir -p "$SCRIPT_DIR/cache" "$SCRIPT_DIR/static"

# Tuer un serveur existant sur ce port
lsof -ti :$PORT 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 0.5

echo "Démarrage sur http://localhost:$PORT"
echo "Appuyez sur Ctrl+C pour arrêter"
echo ""

cd "$SCRIPT_DIR"
python3 app.py
