# Déploiement WhaleWatch

Trois chemins possibles, du plus simple au plus contrôlé.

## Pré-requis (tous chemins)

1. **Clés API** dans des variables d'environnement (jamais commitées) :
   - `DUNE_API_KEY` — [dune.com/settings/api](https://dune.com/settings/api)
   - `ETHERSCAN_API_KEY` — [etherscan.io/myapikey](https://etherscan.io/myapikey)
2. **Repo Git** poussé sur GitHub (recommandé pour auto-deploy)
3. Vérifier en local : `cp .env.example .env`, remplir, puis `python3 server.py`

---

## Option 1 — Railway (recommandé, ~10 min)

**Pourquoi** : auto-deploy Git, disque persistant pour le cache, ~$5/mo après crédit gratuit.

1. Pousser sur GitHub :
   ```bash
   cd "/Users/claude/Desktop/Claude-workspace/Whale Watch"
   git init && git add . && git commit -m "Initial deploy"
   gh repo create whalewatch --public --source=. --push
   ```
2. Sur [railway.app](https://railway.app) :
   - **New Project** → **Deploy from GitHub repo** → choisis `whalewatch`
   - **Variables** → ajoute `DUNE_API_KEY` et `ETHERSCAN_API_KEY`
   - **Settings → Networking → Generate Domain** → tu obtiens `whalewatch.up.railway.app`
3. (Optionnel) Volume persistant pour le cache :
   - **Settings → Volumes → New Volume** → mount path `/app/cache`
   - Le cache survit aux redéploiements

Le `Procfile` est détecté automatiquement (`web: python3 server.py`).

---

## Option 2 — Render (free tier généreux)

1. [render.com](https://render.com) → **New Web Service** → connecte le repo GitHub
2. Settings :
   - **Build Command** : `pip install -r requirements.txt`
   - **Start Command** : `python3 server.py`
   - **Environment** : ajoute `DUNE_API_KEY`, `ETHERSCAN_API_KEY`
3. Free tier : l'app s'endort après 15 min d'inactivité, premier hit lent (~30s)

---

## Option 3 — Fly.io (Docker, ~$2/mo)

Plus rapide, déploie depuis le `Dockerfile` à la racine.

1. Installer le CLI : `brew install flyctl`
2. Login : `fly auth login`
3. Lancer (depuis le dossier) :
   ```bash
   fly launch --no-deploy   # génère fly.toml
   fly secrets set DUNE_API_KEY=... ETHERSCAN_API_KEY=...
   fly volumes create whalewatch_cache --region cdg --size 1
   fly deploy
   ```
4. Edite `fly.toml` pour monter le volume :
   ```toml
   [mounts]
     source = "whalewatch_cache"
     destination = "/app/cache"
   ```

---

## Option 4 — VPS (Hetzner CX11, ~€4/mo, contrôle total)

1. Créer un Ubuntu 24.04 chez [hetzner.com/cloud](https://hetzner.com/cloud) ou DigitalOcean
2. SSH dans le serveur :
   ```bash
   apt update && apt install -y python3-pip git nginx certbot python3-certbot-nginx
   git clone https://github.com/<toi>/whalewatch.git /opt/whalewatch
   cd /opt/whalewatch && pip install -r requirements.txt
   ```
3. Service systemd `/etc/systemd/system/whalewatch.service` :
   ```ini
   [Unit]
   Description=WhaleWatch
   After=network.target

   [Service]
   WorkingDirectory=/opt/whalewatch
   Environment="DUNE_API_KEY=..."
   Environment="ETHERSCAN_API_KEY=..."
   Environment="PORT=8000"
   ExecStart=/usr/bin/python3 server.py
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```
4. `systemctl enable --now whalewatch && systemctl status whalewatch`
5. Reverse proxy Nginx + HTTPS via Certbot (gratuit Let's Encrypt)

---

## Option 5 — Self-host via Cloudflare Tunnel (gratuit, Mac allumé)

Si tu veux ZÉRO frais et que ton Mac peut rester allumé :

1. Installer cloudflared : `brew install cloudflared`
2. `cloudflared tunnel login` (lien Cloudflare account)
3. `cloudflared tunnel create whalewatch`
4. `cloudflared tunnel route dns whalewatch whalewatch.toi.com`
5. Run : `cloudflared tunnel --url http://localhost:8000 run whalewatch`
6. Lancer en parallèle ton serveur local : `python3 server.py`

---

## Domaine perso (optionnel, ~$10/an)

1. Achete sur Namecheap/OVH (ex. `whalewatch.io`)
2. Sur ton hébergeur (Railway/Render/Fly) → ajoute le custom domain
3. Configure CNAME chez ton registrar selon les instructions de l'hébergeur
4. HTTPS auto en quelques minutes

---

## Checklist post-déploiement

- [ ] Les pages chargent : `/`, `/why`, `/guide`, `/bot`
- [ ] `/api/wallets` renvoie du JSON
- [ ] `/api/status` répond
- [ ] Bouton **Sonar** (Réanalyser) déclenche l'analyse Dune sans timeout
- [ ] Les patterns se rafraîchissent
- [ ] Les prix ETH/BTC live (CoinGecko) s'affichent dans le header
- [ ] La zone "Trading Zones" affiche live + signal BUY/SELL

## Coûts mensuels estimés

| Plateforme | Free | Payant | Note |
|---|---|---|---|
| Railway | $5 crédit | ~$5/mo après | Le plus simple |
| Render | Oui (sleep) | $7/mo | Bon free tier |
| Fly.io | 3 VMs free | ~$2/mo | Docker, rapide |
| Hetzner VPS | — | €4/mo | Tout inclus, contrôle |
| Cloudflare Tunnel | Oui | — | Mac doit rester allumé |
| Domaine .io/.com | — | ~$10/an | Optionnel |
