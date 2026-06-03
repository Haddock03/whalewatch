#!/bin/sh
# Sur Railway/Fly, le volume monté à /app/cache appartient à root:root
# et écrase les permissions du Dockerfile. On corrige au boot.
set -e
mkdir -p /app/cache
chown -R app:app /app/cache 2>/dev/null || true
chmod -R u+rwX /app/cache 2>/dev/null || true
# -p (preserve-environment) : Railway injecte PORT/DUNE_API_KEY/ETHERSCAN_API_KEY
# via le subprocess env ; su sans -p peut les stripper selon PAM/distrib.
# Liste explicite ce qu'on veut absolument propager.
exec su -p -s /bin/sh app -c '
  export PORT="'"${PORT:-8000}"'"
  export DUNE_API_KEY="'"${DUNE_API_KEY:-}"'"
  export ETHERSCAN_API_KEY="'"${ETHERSCAN_API_KEY:-}"'"
  exec python3 /app/server.py
'
