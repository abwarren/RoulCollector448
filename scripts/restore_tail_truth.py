#!/usr/bin/env python3
"""Restore the canonical tail (counters >= 2551) from the JOURNALD TRUTH.

Incident 2026-08-25: a recovery burst at 17:08 inflated the lobby gid
counter (+13). The old per-slot dedupe misclassified every tail entry as
new during bursts, the reconcile then BACKFILLED the inflated obs rows into
the canonical table as real spins, and the sequence rebuild cascaded
positions into the 200k range. The obs store accumulated multiple counter
eras and cannot serve as authority for this band — the only truth is the
live spin log in journald (#N: number lines).

This script:
  1. extracts the journald truth for counters >= START_COUNTER,
  2. purges canonical + obs rows with counter >= START_COUNTER (corrupt band),
  3. re-inserts the truth band into BOTH tables (canonical + obs) with
     correct counters, aware-UTC timestamps and VERIFIED status,
  4. prints a verification summary (contiguity + count).

The collector's counter re-seeds from obs MAX on restart, so run this
BEFORE restarting roulette-collector2.

Usage (worker-01):
  RC_DB_PATH=/home/wa/roulette2_spins.db PYTHONPATH=/opt/deploy/repos/RoulCollector448 \
    /opt/deploy/venv/bin/python3 scripts/restore_tail_truth.py
Idempotent: re-running after a restart re-pulls the (growing) truth band.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import schema  # noqa: E402

START_COUNTER = int(os.environ.get("RC_RESTORE_START", "2551"))
TABLE_KEY = os.environ.get("RC_LOBBY_TABLE", "48z5pjps3ntvqc1b")
SAST = ZoneInfo("Africa/Johannesburg")
SPIN_RE = re.compile(r"\[(\d\d:\d\d:\d\d)\]\s*#(\d+):\s*(\d+)")


def journald_truth():
    """(#counter, number, aware-utc iso captured_at) newest-last, from journald."""
    out = subprocess.run(
        ["journalctl", "--user", "-u", "roulette-collector2.service",
         "--since", "2026-08-25 17:05:00", "--no-pager"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    rows = []
    for line in out.splitlines():
        m = SPIN_RE.search(line)
        if not m:
            continue
        hhmmss, counter, number = m.group(1), int(m.group(2)), int(m.group(3))
        if counter < START_COUNTER:
            continue
        local = datetime.strptime(f"2026-08-25 {hhmmss}", "%Y-%m-%d %H:%M:%S")
        local = local.replace(tzinfo=SAST)
        rows.append((counter, number, local.astimezone(timezone.utc).isoformat()))
    # dedupe by counter (journald may repeat a spin across status lines)
    seen, uniq = set(), []
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        uniq.append(r)
    return uniq  # oldest -> newest


def gid(counter, number):
    return f"lobby-{TABLE_KEY}-{number}-{counter}"


def payload_hash(game_id):
    return hashlib.sha256(game_id.encode("utf-8")).hexdigest()[:32]


def purge_band(conn, counter_min):
    # counter-band purge via regex on the trailing gid segment
    cur = conn.execute("SELECT id, game_id FROM roulette_spins WHERE game_id LIKE ?",
                       (f"lobby-{TABLE_KEY}-%-%",)).fetchall()
    del_ids = [i for i, g in cur if _counter(g) is not None and _counter(g) >= counter_min]
    for i in del_ids:
        conn.execute("DELETE FROM roulette_spins WHERE id = ?", (i,))
    oc = conn.execute("SELECT id, game_id FROM spin_observations WHERE game_id LIKE ?",
                      (f"lobby-{TABLE_KEY}-%-%",)).fetchall()
    del_oids = [i for i, g in oc if _counter(g) is not None and _counter(g) >= counter_min]
    for i in del_oids:
        conn.execute("DELETE FROM spin_observations WHERE id = ?", (i,))
    return len(del_ids), len(del_oids)


def _counter(gid):
    m = re.search(r"-(\d+)$", gid or "")
    return int(m.group(1)) if m else None


def main():
    truth = journald_truth()
    if not truth:
        print("NO journald truth found — aborting (nothing to restore).")
        return 2
    print(f"journald truth: {len(truth)} spins ({truth[0][0]}..{truth[-1][0]})")
    conn = schema.connect()
    try:
        del_c, del_o = purge_band(conn, START_COUNTER)
        print(f"purged canonical={del_c}, obs={del_o}")
        ins_c = ins_o = 0
        for counter, number, ts in truth:
            g = gid(counter, number)
            color = "Green" if number == 0 else ("Red" if number % 2 else "Black")
            conn.execute(
                "INSERT OR IGNORE INTO roulette_spins "
                "(game_id, number, color, description, server_ts, captured_at, "
                " sequence_no, status, source, dedup_key, validation_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (g, number, color, f"{number} {color}", ts, ts, counter,
                 "VALID", "history", f"gid:{g}", "VERIFIED"))
            ins_c += 1
            conn.execute(
                "INSERT OR IGNORE INTO spin_observations "
                "(observed_at, source, session_id, game_id, number, server_ts, "
                " payload_hash, raw_payload, sequence_hint, validation_status) "
                "VALUES (?, 'history', 'restore-tail-truth', ?, ?, ?, ?, ?, ?, 'VERIFIED')",
                (ts, g, number, ts, payload_hash(g),
                 json.dumps({"restored_from_journald_truth": True}), counter))
            ins_o += 1
        conn.commit()
        tot = conn.execute("SELECT COUNT(*), MAX(sequence_no) FROM roulette_spins").fetchone()
        print(f"inserted canonical={ins_c}, obs={ins_o} | totals: rows={tot[0]} max_seq={tot[1]}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
