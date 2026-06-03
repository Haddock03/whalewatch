# ────────────────────────────────────────────────────────────────────────────
# WhaleWatch — Dockerfile pour Fly.io / VPS / tout hébergeur Docker
# ────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Sécurité : utilisateur non-root
RUN groupadd -r app && useradd -r -g app -d /app -s /bin/bash app

WORKDIR /app

# Dépendances (cachées via layer dédié)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code applicatif
COPY --chown=app:app . .

# Cache dir (persistant si tu montes un volume sur /app/cache)
RUN mkdir -p /app/cache && chown -R app:app /app/cache

USER app

# Port (override via $PORT en runtime)
ENV PORT=8000
EXPOSE 8000

# Démarrage
CMD ["python3", "server.py"]
