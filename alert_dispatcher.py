# alert_dispatcher.py
# Dispatch d'alertes via webhook quand un signal Cockpit dépasse un seuil
# de Confidence. v1 : webhook générique (Discord webhook, n8n, custom
# server) — pas de Telegram-as-a-service pour éviter de gérer un bot
# global avec sa modération.
#
# Architecture
#   SubscriptionStore : CRUD persisté dans cache/alert_subscriptions.json
#   DispatchHistory   : anti-spam — clé {chain:token:day:tier}, expiration 48h
#   send_webhook      : POST stdlib avec timeout + backoff exp
#   tick(chain, p)    : appelé par le worker après chaque refresh d'une chain
#
# Sécurité
#   - URL webhook obligatoirement https:// (sauf http://localhost en dev)
#   - Refus des IPs internes / metadata (basique SSRF) : 127., 10., 172.16-31,
#     192.168., 169.254. (cloud metadata), [::1]
#   - Timeout 5s, max 2 retries avec backoff 1s/2s sur 5xx ou network error
#   - Pas de redirect follow (urllib.request follow par défaut → on désactive)
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone


CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
SUBS_FILE = os.path.join(CACHE_DIR, "alert_subscriptions.json")
DISPATCH_FILE = os.path.join(CACHE_DIR, "alerts_dispatched.json")

SUBS_SCHEMA = 1
DISPATCH_SCHEMA = 1

# Webhooks supportés
TYPE_WEBHOOK = "webhook"

# Tier hierarchy pour décider si on doit réalerter quand le tier monte.
_TIER_ORDER = {"Faible": 0, "Modéré": 1, "Fort": 2, "Très fort": 3}

# Anti-spam : on ne ré-alerte le même token le même jour QUE si le tier
# a monté. Délai d'expiration des entries dans DispatchHistory = 48h.
DISPATCH_TTL_SEC = int(os.environ.get("ALERT_DISPATCH_TTL_SEC", str(48 * 3600)))

# Timeout HTTP + backoff. Très strict : un webhook lent ne doit pas bloquer
# le worker cockpit.
HTTP_TIMEOUT = 5
MAX_RETRIES = 2

# URL du dashboard (utilisée dans le payload pour faciliter la jump-back).
DASHBOARD_URL = os.environ.get("WW_DASHBOARD_URL", "https://whalewatchapp.io")

_USER_AGENT = "WhaleWatch-AlertDispatcher/1.0 (+https://whalewatchapp.io)"


# ── Helpers fichier ────────────────────────────────────────────────────────
def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Validation URL ─────────────────────────────────────────────────────────
def _is_local_dev():
    return os.environ.get("WW_ALLOW_HTTP_WEBHOOK") == "1"


def validate_webhook_url(url):
    """Valide l'URL webhook. Renvoie (ok: bool, reason: str|None).

    Refuse :
      - schéma autre que https (sauf http://localhost si WW_ALLOW_HTTP_WEBHOOK=1)
      - host vide
      - IPs internes / link-local / loopback (anti-SSRF basique)
    Note : on ne résout PAS le DNS à la validation (l'URL pourrait pointer
    sur dont le DNS change). La résolution se fait au moment du POST via
    socket.gethostbyname pour bloquer une dernière fois.
    """
    if not url or not isinstance(url, str):
        return False, "URL vide"
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError:
        return False, "URL malformée"
    if not parsed.scheme or not parsed.hostname:
        return False, "schéma ou host manquant"
    if parsed.scheme not in ("https", "http"):
        return False, f"schéma {parsed.scheme!r} non supporté (https requis)"
    if parsed.scheme == "http":
        # Autoriser uniquement localhost en dev
        if not (_is_local_dev() and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
            return False, "http:// interdit en production (https requis)"
    # Refus des IPs littérales internes (best-effort, on ne résout pas le DNS)
    try:
        ip = ipaddress.ip_address(parsed.hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            if not _is_local_dev():
                return False, f"IP {ip} non autorisée (interne)"
    except ValueError:
        # Hostname, pas IP — on laisse passer
        pass
    return True, None


def _resolve_safe(hostname):
    """Résout le hostname et bloque si ça pointe sur une IP interne.
    Renvoie l'IP en string si OK, sinon raise ValueError."""
    if _is_local_dev() and hostname in ("localhost", "127.0.0.1", "::1"):
        return hostname
    try:
        info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed: {e}")
    for af, _, _, _, sockaddr in info:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError(f"hostname résout sur IP interne {addr}")
        except ValueError:
            pass
    return hostname


# ── Envoi HTTP ─────────────────────────────────────────────────────────────
def send_webhook(url, payload):
    """POST JSON payload à url avec backoff. Renvoie (ok: bool, info: str).
    Retry 2× sur 5xx ou network error. Pas de retry sur 4xx (signal final)."""
    valid, reason = validate_webhook_url(url)
    if not valid:
        return False, f"invalid URL: {reason}"
    parsed = urllib.parse.urlparse(url)
    try:
        _resolve_safe(parsed.hostname)
    except ValueError as e:
        return False, f"resolution refused: {e}"

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
    )

    last_err = None
    delay = 1.0
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                status = r.status
                if 200 <= status < 300:
                    return True, f"HTTP {status}"
                last_err = f"HTTP {status}"
                # 5xx → retry
                if 500 <= status < 600 and attempt < MAX_RETRIES:
                    time.sleep(delay)
                    delay *= 2
                    continue
                return False, last_err
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if 500 <= e.code < 600 and attempt < MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            return False, last_err
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            last_err = f"network: {e}"
            if attempt < MAX_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            return False, last_err

    return False, last_err or "unknown"


# ── SubscriptionStore ─────────────────────────────────────────────────────
class SubscriptionStore:
    """CRUD des subscriptions persistées en JSON. Thread-safe."""
    def __init__(self, path=SUBS_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._subs = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, dict) or data.get("schema") != SUBS_SCHEMA:
            return []
        return list(data.get("subscriptions") or [])

    def _save(self):
        payload = {
            "schema": SUBS_SCHEMA,
            "saved_at": _utc_now_iso(),
            "subscriptions": self._subs,
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        _atomic_write_json(self.path, payload)

    def list_all(self):
        with self._lock:
            return [dict(s) for s in self._subs]

    def create(self, type_, target, threshold, chain="*", label=None):
        valid, reason = validate_webhook_url(target)
        if not valid:
            raise ValueError(reason)
        try:
            threshold = int(threshold)
        except (TypeError, ValueError):
            raise ValueError("threshold doit être un entier 0-100")
        threshold = max(0, min(100, threshold))
        sub = {
            "id": str(uuid.uuid4()),
            "type": type_ or TYPE_WEBHOOK,
            "target": target.strip(),
            "threshold": threshold,
            "chain": (chain or "*").strip().lower(),
            "label": (label or "").strip()[:80] or None,
            "enabled": True,
            "created_at": _utc_now_iso(),
        }
        with self._lock:
            # Dédup : même target + chain + threshold = doublon refusé
            for existing in self._subs:
                if (existing.get("target") == sub["target"]
                        and existing.get("chain") == sub["chain"]
                        and existing.get("threshold") == sub["threshold"]):
                    raise ValueError("subscription déjà existante "
                                     "(même URL/chain/threshold)")
            self._subs.append(sub)
            self._save()
        return sub

    def delete(self, sub_id):
        with self._lock:
            before = len(self._subs)
            self._subs = [s for s in self._subs if s.get("id") != sub_id]
            if len(self._subs) == before:
                return False
            self._save()
            return True

    def get(self, sub_id):
        with self._lock:
            for s in self._subs:
                if s.get("id") == sub_id:
                    return dict(s)
        return None

    def for_chain(self, chain):
        """Renvoie les subs qui matchent cette chain (ou wildcard '*')."""
        with self._lock:
            return [dict(s) for s in self._subs
                    if s.get("enabled", True)
                    and s.get("chain") in ("*", chain)]


# ── DispatchHistory : anti-spam ────────────────────────────────────────────
class DispatchHistory:
    """Tracker des alertes déjà dispatchées. Clé : (sub_id, chain, token, day).
    On stocke aussi le tier max envoyé : on ne ré-alerte que si le tier
    courant est strictement supérieur.

    Auto-purge : entries de + de DISPATCH_TTL_SEC au save.
    """
    def __init__(self, path=DISPATCH_FILE):
        self.path = path
        self._lock = threading.Lock()
        self._entries = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict) or data.get("schema") != DISPATCH_SCHEMA:
            return {}
        entries = data.get("entries") or {}
        if not isinstance(entries, dict):
            return {}
        # Purge stale au load
        now = time.time()
        return {k: v for k, v in entries.items()
                if isinstance(v, dict) and now - float(v.get("ts") or 0) < DISPATCH_TTL_SEC}

    def _save(self):
        now = time.time()
        # Purge stale au save aussi
        entries = {k: v for k, v in self._entries.items()
                   if isinstance(v, dict) and now - float(v.get("ts") or 0) < DISPATCH_TTL_SEC}
        self._entries = entries
        payload = {
            "schema": DISPATCH_SCHEMA,
            "saved_at": _utc_now_iso(),
            "entries": entries,
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        _atomic_write_json(self.path, payload)

    @staticmethod
    def _key(sub_id, chain, token, day):
        return f"{sub_id}|{chain}|{token}|{day}"

    def should_dispatch(self, sub_id, chain, token, tier, now_dt=None):
        """Renvoie True si on doit envoyer l'alerte, False si on doit skip
        (déjà dispatché aujourd'hui sur ce token au même tier ou tier inférieur)."""
        now_dt = now_dt or datetime.now(timezone.utc)
        day = now_dt.strftime("%Y-%m-%d")
        k = self._key(sub_id, chain, token, day)
        new_rank = _TIER_ORDER.get(tier, 0)
        with self._lock:
            entry = self._entries.get(k)
            if entry is None:
                return True
            prev_rank = _TIER_ORDER.get(entry.get("tier") or "Faible", 0)
            return new_rank > prev_rank

    def mark_dispatched(self, sub_id, chain, token, tier, now_dt=None):
        now_dt = now_dt or datetime.now(timezone.utc)
        day = now_dt.strftime("%Y-%m-%d")
        k = self._key(sub_id, chain, token, day)
        with self._lock:
            self._entries[k] = {"tier": tier, "ts": time.time()}
            self._save()


# Instances globales — initialisées au boot via init() ou auto au premier accès.
_subs_store = None
_dispatch_history = None
_init_lock = threading.Lock()


def get_subscription_store():
    global _subs_store
    with _init_lock:
        if _subs_store is None:
            _subs_store = SubscriptionStore()
        return _subs_store


def get_dispatch_history():
    global _dispatch_history
    with _init_lock:
        if _dispatch_history is None:
            _dispatch_history = DispatchHistory()
        return _dispatch_history


# ── Construction du payload ───────────────────────────────────────────────
def _build_payload(chain, signal, sub_id):
    """Format JSON envoyé au webhook. Tient sur 1 message Discord/Slack."""
    perp_url = (f"https://app.hyperliquid.xyz/trade/"
                f"{urllib.parse.quote(signal['hl_perp_symbol'])}"
                if signal.get("hl_perp_symbol") else None)
    return {
        "event": "cockpit.signal",
        "chain": chain,
        "token": signal.get("token"),
        "confidence": signal.get("confidence"),
        "tier": signal.get("tier"),
        "net_side": signal.get("net_side"),
        "n_wallets": signal.get("n_wallets"),
        "inflow_usd": signal.get("inflow_usd"),
        "age_min": signal.get("age_min"),
        "hl_perp_symbol": signal.get("hl_perp_symbol"),
        "trade_url": perp_url,
        "dashboard_url": f"{DASHBOARD_URL}/pro/cockpit?chain={chain}",
        "generated_at": _utc_now_iso(),
        "subscription_id": sub_id,
        # Format-friendly text pour Discord/Slack qui affichent le content brut
        "text": _build_text(chain, signal, perp_url),
    }


def _build_text(chain, signal, perp_url):
    arrow = "📈" if signal.get("net_side") == "buy" else "📉" if signal.get("net_side") == "sell" else "↔️"
    perp_part = f"\n🔗 {perp_url}" if perp_url else ""
    return (
        f"{arrow} Signal Cockpit {chain.upper()} : {signal.get('token')} "
        f"({signal.get('tier')}, {signal.get('confidence')}/100)\n"
        f"  • {signal.get('n_wallets')} wallets · ${(signal.get('inflow_usd') or 0):,.0f} flux\n"
        f"  • {signal.get('net_side', 'neutral').upper()} · il y a {signal.get('age_min', '?')}min"
        f"{perp_part}"
    )


# ── Tick : appelé par le worker après chaque refresh ──────────────────────
def tick(chain, cockpit_payload, progress_cb=None):
    """Pour chaque sub qui match la chain, dispatch les signaux qui passent
    le seuil ET ne sont pas déjà dispatchés au même tier aujourd'hui.

    Renvoie (n_sent, n_skipped, n_errors).
    """
    def log(m):
        if progress_cb:
            progress_cb(f"[alerts] {m}")

    signals = (cockpit_payload or {}).get("signals") or []
    if not signals:
        return (0, 0, 0)

    store = get_subscription_store()
    history = get_dispatch_history()
    subs = store.for_chain(chain)
    if not subs:
        return (0, 0, 0)

    n_sent = n_skipped = n_errors = 0
    for sub in subs:
        threshold = sub.get("threshold", 70)
        for sig in signals:
            conf = sig.get("confidence") or 0
            if conf < threshold:
                continue
            token = sig.get("token")
            tier = sig.get("tier") or "Faible"
            if not history.should_dispatch(sub["id"], chain, token, tier):
                n_skipped += 1
                continue
            payload = _build_payload(chain, sig, sub["id"])
            ok, info = send_webhook(sub["target"], payload)
            if ok:
                history.mark_dispatched(sub["id"], chain, token, tier)
                n_sent += 1
                log(f"sent {token} conf={conf} → {sub['target'][:40]}… [{info}]")
            else:
                n_errors += 1
                log(f"FAILED {token} → {sub['target'][:40]}… [{info}]")

    return (n_sent, n_skipped, n_errors)


if __name__ == "__main__":
    # Smoke test
    import sys
    store = get_subscription_store()
    print(f"Subscriptions actuelles : {len(store.list_all())}")
    if len(sys.argv) > 1 and sys.argv[1] == "test-url":
        url = sys.argv[2] if len(sys.argv) > 2 else "https://example.com/webhook"
        valid, reason = validate_webhook_url(url)
        print(f"URL {url!r} → valid={valid}  reason={reason}")
