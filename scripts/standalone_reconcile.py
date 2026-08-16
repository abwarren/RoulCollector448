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
            "SELECT game_id, number, server_ts, sequence_no FROM roulette_spins "
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

        # the ABSOLUTE window base. The remote window is the AUTHORITY that
        # defines positions — the base is the sequence preceding the remote
        # window's OLDEST record (found by its game_id in the canonical
        # table). Local-window-derived bases are wrong when a hole makes
        # the local (sequence-based) and remote (id-based) windows cover
        # different spans.
        base = None
        if remote:
            oldest_remote = remote[-1]          # newest-first -> oldest
            if oldest_remote.game_id:
                try:
                    row = conn.execute(
                        "SELECT sequence_no FROM roulette_spins "
                        "WHERE game_id=?", (oldest_remote.game_id,)
                    ).fetchone()
                    if row and row[0] is not None:
                        base = max(0, int(row[0]) - 1)
                except Exception:
                    base = None
        if base is None:
            # fallback: the oldest loaded local row's sequence - 1
            base = (max(0, int(rows[-1]["sequence_no"]) - 1)
                    if rows and rows[-1]["sequence_no"] is not None else 0)

        result = reconciler.reconcile(local_oldest,
                                      history.StaticHistoryProvider(remote),
                                      window=window, base=base)
        details = _details(result)

        # deterministic repairs only when the authority carries identity
        # (PRD §24/§25); failures are logged, never fatal to the loop.
        repaired_any = False
        if result.plan.repairable and not result.ok:
            try:
                rep = repairer.Repairer(conn)
                rep.apply_plan(result.plan)
                repaired_any = True
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

        # PRD §13/§26-new — RE-VERIFY: after a repair, re-run the reconcile
        # so the pass reports the window as verified (or still failing).
        if repaired_any or not result.ok:
            try:
                rows2 = conn.execute(
                    "SELECT game_id, number, server_ts FROM roulette_spins "
                    "ORDER BY sequence_no IS NULL, sequence_no DESC, id DESC "
                    "LIMIT ?", (window,)).fetchall()
                if rows2:
                    local2 = [{"game_id": r[0], "number": r[1],
                               "server_ts": r[2]} for r in rows2]
                    remote2 = history.DBHistoryProvider().fetch_recent_history(
                        limit=window)
                    result2 = reconciler.reconcile(
                        list(reversed(local2)),
                        history.StaticHistoryProvider(remote2),
                        window=window)
                    if result2.ok or result2.plan.repairable:
                        result = result2
                        details = _details(result)
                        repaired_any = True   # the pass verified after repair
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
            local_rows = conn.execute(
                "SELECT game_id, number, server_ts, sequence_no "
                "FROM roulette_spins "
                "ORDER BY sequence_no DESC LIMIT ?", (window,)).fetchall()
            local = [{"game_id": r[0], "number": r[1], "server_ts": r[2]}
                     for r in local_rows]
            rows2 = conn.execute(
                "SELECT game_id, number, server_ts FROM spin_observations "
                "WHERE source='history' AND game_id IS NOT NULL "
                "ORDER BY id DESC LIMIT ?", (window,)
            ).fetchall()
            remote = [history.HistoryRecord(game_id=r[0], number=r[1],
                                             server_ts=r[2]) for r in rows2]
            # absolute base from the REMOTE window's oldest record (the
            # authority); fallback to the oldest loaded local row
            base = None
            if remote:
                oldest_remote = remote[-1]
                if oldest_remote.game_id:
                    row = conn.execute(
                        "SELECT sequence_no FROM roulette_spins "
                        "WHERE game_id=?", (oldest_remote.game_id,)
                    ).fetchone()
                    if row and row[0] is not None:
                        base = max(0, int(row[0]) - 1)
            if base is None:
                oldest_seq = local_rows[-1][3] if local_rows else None
                base = max(0, int(oldest_seq) - 1) if oldest_seq is not None else 0
            result = reconciler.reconcile(
                list(reversed(local)),
                history.StaticHistoryProvider(remote), window=window,
                base=base)
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


DEEP_SWEEP_S = 5 * 60    # every 5 minutes: the full-window deep integrity
                         # sweep (reconcile + data-health + gap recovery),
                         # decoupled from the collector's own 30/60s loop so
                         # it runs even when the collector is down.


def deep_sweep(conn=None, window: int = RECONCILE_WINDOW) -> dict:
    """PRD §31 'every 5 min' — the full-window deep integrity sweep.

    Runs independent of the collector: full-window reconciliation against
    authoritative history + gap recovery + the six data-health signals.
    Decoupled from the collector's 30/60s loop so it runs even when the
    collector process is down.

    Returns {"reconciliation": {...}, "gaps": [...], "data_health": {...},
             "healthy": bool}.
    """
    owns_conn = conn is None
    if owns_conn:
        conn = schema.connect()
    try:
        details = run_once(conn, window=window)          # reconcile + repair
        outcomes = recover_gaps(conn, window=window)     # gap lifecycle
        dh = _data_health(conn)                          # six signals
        healthy = bool(details.get("ok")) and dh["healthy"] \
            and not any(o["resolution"] == "UNVERIFIED" for o in outcomes)
        return {
            "reconciliation": details,
            "gaps": outcomes,
            "data_health": dh,
            "healthy": healthy,
        }
    finally:
        if owns_conn:
            conn.close()


def _data_health(conn) -> dict:
    """Six-signal data-health evaluation (PRD §29) over an open conn."""
    import json as _json
    out = {}
    # sequence health — gaps in the latest window
    try:
        rows = conn.execute(
            "SELECT sequence_no FROM roulette_spins "
            "WHERE sequence_no IS NOT NULL ORDER BY sequence_no DESC LIMIT ?",
            (RECONCILE_WINDOW,)).fetchall()
        seqs = sorted(r[0] for r in rows)
        gaps = 0
        if seqs:
            present = set(seqs)
            gaps = sum(1 for i in range(seqs[0], seqs[-1] + 1)
                       if i not in present)
    except Exception:
        gaps = 0
    out["sequence_health"] = gaps == 0
    # reconciliation health + score from the latest event — but the
    # standalone worker's run_once doesn't compute the §22 health score
    # (only the collector's reconcile_task does). Compute it here from the
    # pass details when the score is absent, so the sweep always reports it.
    try:
        row = conn.execute(
            "SELECT details FROM integrity_events "
            "WHERE event_type IN ('RECONCILIATION','RECONCILIATION_LIGHT') "
            "ORDER BY id DESC LIMIT 1").fetchone()
        rec = _json.loads(row["details"]) if row and row["details"] else {}
        out["reconciliation_health"] = bool(rec.get("ok", False))
        score = rec.get("score")
        if score is None:
            try:
                from collector.integrity_state import DataHealthScore
                score = DataHealthScore().compute(
                    reconciliation=1.0 if out["reconciliation_health"] else 0.0,
                    sequence=1.0 if out["sequence_health"] else 0.0,
                    # source_agreement unknown here (no WS-vs-DOM pairs) —
                    # keep it neutral (1.0 = nothing to contradict)
                    source_agreement=1.0,
                )
            except Exception:
                score = None
        out["data_health_score"] = score
    except Exception:
        out["reconciliation_health"] = False
        out["data_health_score"] = None
    # repair queue
    try:
        out["repair_queue"] = conn.execute(
            "SELECT COUNT(*) FROM repair_events "
            "WHERE status IN ('OPEN','FAILED','UNVERIFIED')").fetchone()[0]
    except Exception:
        out["repair_queue"] = 0
    score = out["data_health_score"]
    out["healthy"] = (out["sequence_health"] and out["reconciliation_health"]
                      and out["repair_queue"] == 0
                      and (score is None or score >= 75))
    return out


def main() -> None:
    print(f"[{observer.now_iso()}] standalone reconcile worker — "
          f"light pass every {RECONCILE_LIGHT_S}s, window {RECONCILE_WINDOW}, "
          f"deep sweep every {DEEP_SWEEP_S // 60}min")
    last_deep = 0.0
    while True:
        try:
            # light pass (30s cadence, PRD §31)
            details = run_once()
            print(f"[{observer.now_iso()}] RECONCILIATION ok={details['ok']} "
                  f"window={details['window']} missing={details['missing']} "
                  f"corrections={details['corrections']} "
                  f"duplicates={details['duplicates']} "
                  f"reordered={details['reordered']} extras={details['extras']} "
                  f"repairable={details['repairable']} — {details['message']}")
            # every 5 minutes: the deep sweep (full window + data health)
            if time.time() - last_deep >= DEEP_SWEEP_S:
                last_deep = time.time()
                sweep = deep_sweep()
                print(f"[{observer.now_iso()}] DEEP SWEEP healthy="
                      f"{sweep['healthy']} "
                      f"gaps={len(sweep['gaps'])} "
                      f"seq={sweep['data_health']['sequence_health']} "
                      f"recon={sweep['data_health']['reconciliation_health']} "
                      f"repairs={sweep['data_health']['repair_queue']} "
                      f"score={sweep['data_health']['data_health_score']}")
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
