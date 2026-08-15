#!/usr/bin/env python3
"""Standalone reconcile worker — the collector's reconcile loop WITHOUT the
collector process.

The collector's reconcile_task only runs while the browser session is alive:
if the collector is down or between sessions, repairs never happen. This
worker decouples reconciliation from capture so the rolling window keeps
self-auditing (PRD §13/§53) even when the collector is not running.

Every RECONCILE_LIGHT_S (30s):
  1. open the DB read-write via collector.schema.connect()
  2. load the latest RECONCILE_WINDOW (500) canonical spins
     (SELECT ... ORDER BY id DESC LIMIT 500 — newest first)
  3. load authoritative history via DBHistoryProvider (spin_observations
     rows with source='history', persisted by the collector from WS history
     frames — the authority survives restarts)
  4. call reconciler.reconcile(oldest-first local, provider) exactly like
     the collector does (reconcile expects OLDEST-first local and reverses
     internally)
  5. when result.plan.repairable and not result.ok, apply repairs via
     Repairer.apply_plan, logging REPAIR_FAILED on failure
  6. log a RECONCILIATION integrity event with the pass details
  7. sleep RECONCILE_LIGHT_S, loop forever (KeyboardInterrupt exits cleanly)

The cadence/window constants mirror collector/roulette2_collector.py (the
source of truth); they are deliberately NOT imported from it — that module
drags in playwright and the Sunbet credential guard, which this worker must
not depend on.
"""

import os
import sqlite3
import sys
import time

# Resolve the `collector` package from the repo root whether invoked as
# `python scripts/standalone_reconcile.py` or `python -m scripts.standalone_reconcile`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import history, observer, reconciler, repairer, schema  # noqa: E402

# Mirror collector/roulette2_collector.py (module-level constants there).
RECONCILE_LIGHT_S = 30
RECONCILE_WINDOW = 500


def _details(result) -> dict:
    """Map a ReconciliationResult onto the logged RECONCILIATION details."""
    return {
        "ok": result.ok,
        "window": result.window,
        "missing": result.missing_count,
        "corrections": result.correction_count,
        "duplicates": result.duplicate_count,
        "reordered": result.reorder_count,
        "extras": result.extra_count,
        "repairable": result.repairable,
        "message": result.message,
    }


def run_once(conn=None, window: int = RECONCILE_WINDOW) -> dict:
    """Run ONE reconcile pass against the DB; return the details dict.

    conn: optional read-write connection (tests pass their own tmp-DB
    connection). When None, the worker opens one via collector.schema.connect()
    (RC_DB_PATH env / schema default resolved at import) and closes it before
    returning. The pass itself:

      load latest canonical spins -> authoritative history observations ->
      reconcile (oldest-first) -> repair if repairable -> RECONCILIATION event
    """
    owns_conn = conn is None
    if owns_conn:
        conn = schema.connect()
    try:
        # canonical-ordering reconstruction FIRST (idempotent: a consistent
        # dataset changes nothing). Normalizes collisions / order violations
        # to the chronological evidence order (server_ts -> captured_at) so
        # the reconcile pass below reads a well-ordered canonical sequence —
        # and any windowed game_id-verified repair from apply_plan then
        # takes precedence over the ts-based sweep.
        try:
            recon = repairer.Repairer(conn).reconstruct_ordering()
        except Exception:
            recon = None

        # canonical spins, newest first — in CANONICAL SEQUENCE order (the
        # reconstructed ordering), NULL sequence last, id tie-break.
        rows = conn.execute(
            "SELECT game_id, number, server_ts FROM roulette_spins "
            "ORDER BY sequence_no IS NULL, sequence_no DESC, id DESC LIMIT ?",
            (window,),
        ).fetchall()
        if not rows:
            details = {"ok": True, "window": 0, "missing": 0, "corrections": 0,
                       "duplicates": 0, "reordered": 0, "extras": 0,
                       "repairable": False,
                       "message": "no canonical spins yet — nothing to reconcile"}
            observer.log_event(conn, "RECONCILIATION", severity="INFO",
                               details=details, root_cause="DATA_INTEGRITY")
            return details

        local_newest = [{"game_id": r[0], "number": r[1], "server_ts": r[2]}
                        for r in rows]
        # reconcile() expects OLDEST-first local (it reverses internally) —
        # the query above returned newest-first, so reverse it back.
        local_oldest = list(reversed(local_newest))

        # authoritative history: durable source='history' observations
        # (survives collector restarts; read-only probe of the same DB)
        remote = history.DBHistoryProvider().fetch_recent_history(limit=window)
        result = reconciler.reconcile(local_oldest,
                                      history.StaticHistoryProvider(remote),
                                      window=window)
        details = _details(result)

        # deterministic repairs only when the authority carries identity
        # (PRD §24/§25); failures are logged, never fatal to the loop.
        if result.plan.repairable and not result.ok:
            try:
                rep = repairer.Repairer(conn)
                rep.apply_plan(result.plan)
            except repairer.RepairRefused as e:
                # PRD §25: a refused repair is surfaced, never silent — the
                # reason + UNVERIFIED marking are already in the queue/rows.
                observer.log_event(conn, "REPAIR_REFUSED", severity="WARNING",
                                   details={"reason": str(e)},
                                   root_cause="DATA_INTEGRITY")
            except Exception as e:
                try:
                    observer.log_event(conn, "REPAIR_FAILED",
                                       severity="CRITICAL",
                                       details={"error": str(e)},
                                       root_cause="DATA_INTEGRITY")
                except Exception:
                    pass

        sev = "CRITICAL" if not result.ok and result.plan.authoritative else "INFO"
        if recon and recon.get("reordered"):
            details["reconstruct"] = recon   # the sweep changed ordering
        # §26-new: gap recovery loop — detect -> attempt -> repair -> verify.
        # Each gap becomes a GAP repair event resolved REPAIRED or UNVERIFIED.
        try:
            gap_outcomes = recover_gaps(conn, window=window)
            if gap_outcomes:
                details["gaps"] = gap_outcomes
        except Exception:
            pass
        observer.log_event(conn, "RECONCILIATION", severity=sev,
                           details=details, root_cause="DATA_INTEGRITY")
        return details
    finally:
        if owns_conn:
            conn.close()


def recover_gaps(conn, window: int = RECONCILE_WINDOW) -> list:
    """PRD §26-new — gap recovery loop.

    For every gap in the latest window's canonical sequence:
      1. record it OPEN (GAP repair event)
      2. attempt recovery: query the authoritative rolling history and
         repair (backfill) if possible — the same identity-gated pipeline
         (game_id authority; §25 refusals respected)
      3. re-run validation (a fresh reconcile pass)
      4. if the gap is gone -> RESOLVED/REPAIRED (repaired gap)
         else -> UNVERIFIED (permanent/unverified gap)

    Returns a list of {start, end, size, status, resolution} outcomes —
    one per gap found. Never raises; failures degrade a gap to UNVERIFIED.
    """
    rep = repairer.Repairer(conn)
    outcomes = []
    # sequence gaps in the latest window (the §26 definition)
    rows = conn.execute(
        "SELECT sequence_no FROM roulette_spins "
        "WHERE sequence_no IS NOT NULL "
        "ORDER BY sequence_no DESC LIMIT ?",
        (window,),
    ).fetchall()
    seqs = sorted(r[0] for r in rows)
    if not seqs:
        return outcomes
    holes = []
    for i in range(seqs[0], seqs[-1] + 1):
        if i not in set(seqs):
            holes.append(i)
    # group consecutive holes into gaps
    gaps = []
    for h in holes:
        if gaps and h == gaps[-1]["end"] + 1:
            gaps[-1]["end"] = h
            gaps[-1]["size"] += 1
        else:
            gaps.append({"start": h, "end": h, "size": 1})
    for g in gaps:
        ev_id = rep.record_gap(start_seq=g["start"], end_seq=g["end"],
                               size=g["size"], status="OPEN",
                               resolution=None,
                               details={"note": "gap detected — attempting recovery"})
        # attempt recovery: query history + repair (identity-gated). The
        # authoritative history is read from THE SAME conn (spin_observations
        # with source='history' live in the same DB) so the recovery works
        # regardless of RC_DB_PATH / which DB the caller connected to.
        recovered = False
        try:
            local = [{"game_id": r[0], "number": r[1], "server_ts": r[2]}
                     for r in conn.execute(
                         "SELECT game_id, number, server_ts FROM roulette_spins "
                         "ORDER BY sequence_no DESC LIMIT ?", (window,))]
            rows = conn.execute(
                "SELECT game_id, number, server_ts FROM spin_observations "
                "WHERE source='history' AND game_id IS NOT NULL "
                "ORDER BY id DESC LIMIT ?", (window,)
            ).fetchall()
            remote = [history.HistoryRecord(game_id=r[0], number=r[1],
                                             server_ts=r[2]) for r in rows]
            result = reconciler.reconcile(
                list(reversed(local)),
                history.StaticHistoryProvider(remote), window=window)
            if result.plan.repairable and not result.ok:
                rep.apply_plan(result.plan)
            # re-run validation: is the gap gone?
            after = conn.execute(
                "SELECT COUNT(*) FROM roulette_spins WHERE sequence_no=?",
                (g["start"],)).fetchone()[0]
            recovered = after > 0
        except Exception:
            recovered = False
        if recovered:
            rep.resolve_gap(ev_id, status="RESOLVED", resolution="REPAIRED",
                            details={"start": g["start"], "end": g["end"],
                                     "size": g["size"]})
            outcomes.append({**g, "status": "RESOLVED", "resolution": "REPAIRED"})
        else:
            rep.resolve_gap(ev_id, status="UNVERIFIED", resolution="UNVERIFIED",
                            details={"start": g["start"], "end": g["end"],
                                     "size": g["size"],
                                     "note": "could not be repaired — "
                                             "permanent/unverified gap"})
            outcomes.append({**g, "status": "UNVERIFIED",
                             "resolution": "UNVERIFIED"})
    return outcomes


def main() -> None:
    print(f"[{observer.now_iso()}] standalone reconcile worker — "
          f"light pass every {RECONCILE_LIGHT_S}s, window {RECONCILE_WINDOW}")
    while True:
        try:
            details = run_once()
            print(f"[{observer.now_iso()}] RECONCILIATION ok={details['ok']} "
                  f"window={details['window']} missing={details['missing']} "
                  f"corrections={details['corrections']} "
                  f"duplicates={details['duplicates']} "
                  f"reordered={details['reordered']} extras={details['extras']} "
                  f"repairable={details['repairable']} — {details['message']}")
        except KeyboardInterrupt:
            print("\n[reconcile] KeyboardInterrupt — exiting cleanly")
            break
        except Exception as e:
            print(f"[reconcile] pass failed: {e}")
            try:
                sconn = schema.connect()
                observer.log_event(sconn, "RECONCILIATION", severity="WARNING",
                                   details={"error": str(e)},
                                   root_cause="DATA_INTEGRITY")
                sconn.close()
            except Exception:
                pass
        time.sleep(RECONCILE_LIGHT_S)


if __name__ == "__main__":
    main()
