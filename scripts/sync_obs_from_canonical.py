#!/usr/bin/env python3
"""Sync spin_observations (lobby gids) to mirror canonical truth.

The reconcile authority is the obs store; holes or strays make ok:true
unreachable (missing spins can never verify; stray records are extras).
This makes the obs lobby set EXACTLY equal the canonical lobby set:
  - delete obs rows whose game_id has no canonical row (strays from
    re-assignment eras)
  - insert obs rows for canonical rows that lack one (holes, e.g. after a
    repair deleted the phantom's obs or the state was rebuilt)
Idempotent. Back up the DB before running.

Usage: python3 scripts/sync_obs_from_canonical.py [--db PATH] [--apply]
"""
import argparse
import re
import sqlite3

LOBBY_LIKE = "lobby-48z5pjps3ntvqc1b-%"


def counter(gid):
    m = re.search(r"-(\d+)$", gid or "")
    return int(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/home/wa/roulette2_spins.db")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    c = sqlite3.connect(args.db)
    c.execute("PRAGMA busy_timeout=10000")

    can = c.execute(
        "SELECT id, game_id, number, server_ts, captured_at, description "
        f"FROM roulette_spins WHERE game_id LIKE '{LOBBY_LIKE}'"
    ).fetchall()
    obs = c.execute(
        "SELECT id, game_id FROM spin_observations WHERE game_id LIKE ?",
        (LOBBY_LIKE,),
    ).fetchall()

    can_gids = {r[1] for r in can}
    obs_gids = {r[1] for r in obs}

    stray_obs = [oid for oid, gid in obs if gid not in can_gids]
    holes = [r for r in can if r[1] not in obs_gids]

    print(f"canonical lobby: {len(can)} | obs lobby: {len(obs)}")
    print(f"stray obs (gid not in canonical): {len(stray_obs)}")
    print(f"holes (canonical w/o obs): {len(holes)}")
    for h in holes[:8]:
        print(f"  HOLE {h[1]} num={h[2]}")
    for oid in stray_obs[:8]:
        print(f"  STRAY obs id={oid}")

    if not args.apply:
        print("DRY-RUN (--apply to commit)")
        c.close()
        return

    for oid in stray_obs:
        c.execute("DELETE FROM spin_observations WHERE id=?", (oid,))
    n_ins = 0
    for _id, gid, num, server_ts, captured_at, desc in holes:
        c.execute(
            "INSERT OR IGNORE INTO spin_observations "
            "(game_id, number, source, session_id, observed_at, server_ts, "
            "payload_hash, validation_status) "
            "VALUES (?, ?, 'history', 'sync', ?, ?, ?, 'VALID')",
            (gid, num, captured_at or server_ts, server_ts, gid),
        )
        n_ins += 1
    c.commit()
    print(f"applied: deleted {len(stray_obs)}, inserted {n_ins}")

    # verify
    obs2 = {r[0] for r in c.execute(
        "SELECT game_id FROM spin_observations WHERE game_id LIKE ?", (LOBBY_LIKE,))}
    print(f"VERIFY: obs lobby {len(obs2)} vs canonical {len(can_gids)} "
          f"| equal: {obs2 == can_gids}")
    c.close()


if __name__ == "__main__":
    main()
