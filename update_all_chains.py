#!/usr/bin/env python3
"""
update_all_chains.py
Lance _run_analysis.py sur toutes les chains configurées en séquence.

Idéal pour cron job nocturne ou refresh manuel multi-chain :
    python3 update_all_chains.py           # toutes les chains
    python3 update_all_chains.py eth arb   # seulement les chains spécifiées
    python3 update_all_chains.py --force   # ignore le skip cache récent

Options :
    --max-age <minutes>   skip une chain si son cache a moins de N min (défaut 60)
    --force               ignore --max-age, refait tout
    --n-wallets <N>       nb wallets à analyser (défaut 100)
    --days <N>            fenêtre temporelle (défaut 7)

Exit code 0 si toutes les chains demandées ont eu un Sonar (ou skip
intentionnel) ; non-zéro si au moins une a planté.
"""
import argparse
import os
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chains import CHAINS, resolve


def _cache_age_minutes(cache_path):
    """Renvoie l'âge du cache en minutes, ou None si inexistant."""
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path) as f:
            data = json.load(f)
        ts = data.get("last_updated")
        if not ts:
            return None
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() / 60.0
    except Exception:
        return None


def run_chain(chain_key, n_wallets, days):
    """Lance _run_analysis.py pour une chain. Renvoie exit code."""
    env = {**os.environ,
           "WW_N_WALLETS": str(n_wallets),
           "WW_DAYS": str(days),
           "WW_CHAIN": chain_key}
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_run_analysis.py")
    print(f"\n{'='*60}\n  Sonar {chain_key} ({n_wallets} wallets, {days}j)\n{'='*60}", flush=True)
    proc = subprocess.Popen([sys.executable, script], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    for line in proc.stdout:
        print(f"[{chain_key}] {line.rstrip()}", flush=True)
    proc.wait()
    return proc.returncode


def main():
    ap = argparse.ArgumentParser(description="Lance les Sonars multi-chain en séquence")
    ap.add_argument("chains", nargs="*", help="Chains à lancer (default: toutes)")
    ap.add_argument("--max-age", type=int, default=60,
                    help="Skip si cache < N minutes (défaut 60)")
    ap.add_argument("--force", action="store_true",
                    help="Ignore --max-age, force le refresh")
    ap.add_argument("--n-wallets", type=int, default=100)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    # Liste cible : args.chains ou toutes
    if args.chains:
        try:
            targets = [resolve(c)["key"] for c in args.chains]
        except ValueError as e:
            print(f"Erreur : {e}", file=sys.stderr)
            sys.exit(2)
    else:
        targets = list(CHAINS.keys())

    print(f"update_all_chains : {len(targets)} chain(s) cible(s) → {targets}")
    print(f"Paramètres : n_wallets={args.n_wallets}, days={args.days}, "
          f"max_age={args.max_age}min{', force' if args.force else ''}")

    failed = []
    skipped = []
    succeeded = []

    t0 = time.time()
    for chain in targets:
        cfg = resolve(chain)
        age = _cache_age_minutes(cfg["cache_path"])
        if not args.force and age is not None and age < args.max_age:
            print(f"\n→ {chain} skip (cache {age:.1f}min < {args.max_age}min)")
            skipped.append(chain)
            continue
        rc = run_chain(chain, args.n_wallets, args.days)
        if rc == 0:
            succeeded.append(chain)
        else:
            print(f"\n⚠ {chain} : exit code {rc}")
            failed.append(chain)

    elapsed = time.time() - t0
    print(f"\n{'='*60}\n  Bilan ({elapsed:.0f}s total)\n{'='*60}")
    print(f"  ✓ Succès : {len(succeeded)}/{len(targets)} {succeeded}")
    if skipped:
        print(f"  – Skip   : {len(skipped)} {skipped}")
    if failed:
        print(f"  ✗ Échec  : {len(failed)} {failed}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
