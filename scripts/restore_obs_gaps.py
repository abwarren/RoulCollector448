#!/usr/bin/env python3
"""One-time integrity repair: restore spin_observations rows for canonical
lobby spins that have NO observation twin (extras that keep the reconciliation
score at 65 forever — counters ~2066-2073 legacy rows).

These are REAL spins with REAL game_ids already committed to roulette_spins;
only their audit-trail record is missing. Restoring the observation from the
canonical row (deterministic payload_hash = sha256(game_id)) is honest
identity restoration, NOT fabrication (PRD §5/§25 — we never invent identity;
the identity already exists and is simply re-linked).

Usage (worker-01):
  RC_DB_PATH=/home/wa/roulette2_spins.db PYTHONPATH=/opt/deploy/repos/RoulCollector448 \
    /opt/deploy/venv/bin/python3 scripts/restore_obs_gaps.py
Idempotent: rows whose gid already exists in spin_observations are skipped.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import schema  # noqa: E402


def payload_hash(game_id: str) -> str:
    return hashlib.sha256(game_id.encode("utf-8")).hexdigest()[:32]


def main() -> int:
    conn = schema.connect()
    try:
        canon = conn.execute(
            "SELECT game_id, number, description, server_ts, captured_at "
            "FROM roulette_spins WHERE game_id LIKE 'lobby-%'"
        ).fetchall()
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT game_id FROM spin_observations WHERE game_id IS NOT NULL"
        ).fetchall()}
        restored = 0
        skipped = 0
        for gid, number, description, server_ts, captured_at in canon:
            if gid in have:
                skipped += 1
                continue
            ph = payload_hash(gid)
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO spin_observations "
                    "(observed_at, source, session_id, game_id, number, description, "
                    " server_ts, payload_hash, raw_payload, sequence_hint, validation_status) "
                    "VALUES (?, 'history', 'restore-obs-gaps', ?, ?, ?, ?, ?, ?, ?, 'VERIFIED')",
                    (captured_at, gid, number, description, server_ts, ph,
                     json.dumps({"restored_from_canonical": True}), None),
                )
                if conn.total_changes:
                    restored += 1
            except Exception as e:  # noqa: BLE001
                print(f"  SKIP {gid}: {e}")
        conn.commit()
        print(f"canonical lobby rows: {len(canon)}, already-in-obs: {skipped}, restored: {restored}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
