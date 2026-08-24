#!/usr/bin/env python3
"""One-off: dedupe roulette_spins rows that are the SAME physical spin
inserted multiple times by the reconcile backfill (the 19:10:04 x6 bug).

The backfill inserted the same lobby-tail spins repeatedly with fresh
synthesized game_ids. Physical identity available in canonical rows:
(number, captured_at) — backfill set captured_at = server_ts = its own
'now', so a group of rows sharing (number, captured_at) is one physical
spin. Keep the row with the SMALLEST id (first inserted = the original),
delete the rest. Also fix sequence_no to be contiguous 1..N after delete.

Run with a DB path arg, e.g.:
    python3 dedupe_spins.py /home/wa/roulette2_spins.db   (--dry-run to preview)
"""
import sqlite3
import sys

def main(path: str, dry: bool = True):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        # find groups: same (number, captured_at) appearing more than once
        rows = conn.execute(
            "SELECT id, number, captured_at, game_id, server_ts, source "
            "FROM roulette_spins ORDER BY id"
        ).fetchall()
        groups = {}
        for r in rows:
            key = (r["number"], r["captured_at"])
            groups.setdefault(key, []).append(r)
        dupes = {k: v for k, v in groups.items() if len(v) > 1}
        total_dupes = sum(len(v) - 1 for v in dupes.values())
        print(f"total rows: {len(rows)}, duplicate groups: {len(dupes)}, "
              f"rows to delete: {total_dupes}")

        if dry:
            for key, grp in list(dupes.items())[:10]:
                keep = grp[0]
                print(f"  {key}: keep id={keep['id']} {keep['game_id']}, "
                      f"delete ids={[r['id'] for r in grp[1:]]}")
            if len(dupes) > 10:
                print(f"  ... and {len(dupes)-10} more groups")
            return

        deleted = 0
        for key, grp in dupes.items():
            keep_id = grp[0]["id"]
            for r in grp[1:]:
                conn.execute("DELETE FROM roulette_spins WHERE id = ?", (r["id"],))
                deleted += 1
        # renumber sequence_no contiguously (keep id order = canonical)
        conn.execute("UPDATE roulette_spins SET sequence_no = id WHERE sequence_no IS NOT NULL")
        conn.commit()
        print(f"deleted {deleted} duplicate rows; sequence_no renumbered")
        # verify
        left = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
        print(f"remaining rows: {left}")
    finally:
        conn.close()

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "/home/wa/roulette2_spins.db"
    main(path, dry=dry)
