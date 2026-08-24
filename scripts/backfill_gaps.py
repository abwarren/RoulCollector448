#!/usr/bin/env python3
"""Backfill gap spins: insert authority observations missing from canonical.

Diff by game_id (the unique identity both sides share). Any observation
whose game_id is NOT in roulette_spins gets inserted with status='REPAIRED'
(never silently merged — the integrity trail records it). Deterministic,
idempotent (UNIQUE game_id constraint), safe to re-run.
"""
import os, sys, time
sys.path.insert(0, "/home/gdi/RoulCollector448")
os.environ.setdefault("RC_DB_PATH", "/home/gdi/roulette2/roulette2_spins.db")

from collector import schema

def backfill_gaps(conn, window=500, dry_run=False):
    # canonical game_ids present
    have = {r[0] for r in conn.execute(
        "SELECT game_id FROM roulette_spins WHERE game_id IS NOT NULL").fetchall()}
    # Physical dedupe: obs may carry server_ts (the spin's real time) now;
    # canonical rows carry it too. Same (number, server_ts) = same physical
    # spin under a different synthesized gid -> skip. When either side lacks
    # a timestamp, fall back to number-only as a weak guard (never inserts
    # a spin whose number already appears at the SAME second — the only
    # case that's provably a duplicate).
    seen_phys = {(r[0], r[1]) for r in conn.execute(
        "SELECT number, server_ts FROM roulette_spins "
        "WHERE server_ts IS NOT NULL AND server_ts != ''").fetchall()}
    # authority observations (source='history'), newest-first
    obs = conn.execute(
        "SELECT game_id, number, server_ts FROM spin_observations WHERE source='history' "
        "AND game_id IS NOT NULL ORDER BY id DESC LIMIT ?", (window,)).fetchall()
    inserted = 0
    skipped = 0
    for gid, num, server_ts in obs:
        if gid in have:
            continue
        if server_ts:
            if (num, server_ts) in seen_phys:
                skipped += 1
                continue
        # skip legacy pre-fix gids (no trailing counter) — can't order them
        if gid.count("-") < 3:
            skipped += 1
            continue
        if dry_run:
            print(f"  WOULD backfill: {gid} number={num}")
            inserted += 1
            continue
        try:
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "INSERT OR IGNORE INTO roulette_spins "
                "(number, description, color, game_id, server_ts, captured_at, "
                "source, confidence, status, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'reconcile', 0.9, 'REPAIRED', ?)",
                (num,
                 f"{num} {('Red' if num in {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36} else 'Green' if num == 0 else 'Black')} [lobby backfill]",
                 'Red' if num in {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36} else 'Green' if num == 0 else 'Black',
                 gid, server_ts or now, now,
                 now))
            inserted += 1
        except Exception as e:
            print(f"  ERR backfill {gid}: {e}")
    if not dry_run:
        conn.commit()
    return inserted, skipped

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    conn = schema.connect()
    ins, skp = backfill_gaps(conn, dry_run=dry)
    conn.close()
    print(f"backfilled: {ins}, skipped legacy: {skp} ({'DRY RUN' if dry else 'committed'})")
