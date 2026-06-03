#!/bin/sh
set -e
mkdir -p /app/cache
chmod -R 777 /app/cache
export PORT="${PORT:-8000}"
export DUNE_API_KEY="${DUNE_API_KEY:-}"
export ETHERSCAN_API_KEY="${ETHERSCAN_API_KEY:-}"
exec python3 /app/server.py
