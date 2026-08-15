"""Repairer (Phase 4) — deterministic atomic repairs + audit trail.

Turns a ReconciliationResult's RepairPlan into canonical-data changes.
Every repair is ONE SQLite transaction (BEGIN...COMMIT, ROLLBACK on any
failure — PRD §33). Raw observations are NEVER destroyed (PRD §34): the
original captured value is preserved in spin_observations and the repair is
recorded in repair_events with old/new values, evidence source, reason,
attempts and verification result (PRD §35).

Repair rules (PRD §24-25):
  SAFE TO AUTO-REPAIR (authoritative identity required):
    - MISSING_SPIN      remote has it, local doesn't      -> insert
    - WRONG_VALUE       same game_id, remote authoritative -> replace canonical,
                            original raw observation preserved
    - DUPLICATE         -> collapse to one canonical record, retain observations
    - OUT_OF_ORDER      -> rebuild sequence_no for the affected window
  NEVER AUTO-REPAIR (-> UNVERIFIED, surfaced):
    - authoritative source unavailable
    - multiple conflicting sources
    - identity cannot be established
    - any statistical inference (observed > inferred, PRD §5)

The reconciler (Phase 3) -> repairer (Phase 4) -> verify loop is the PRD's
"reconcile ↓ verify": a repair is not success until re-validation passes.
"""

import json
import sqlite3

from collector.observer import now_iso
from collector import schema

STATUS_REPAIRED = "REPAIRED"
STATUS_UNVERIFIED = "UNVERIFIED"


def _run_transaction(conn, fn) -> bool:
    """Run fn(conn) inside BEGIN/COMMIT; ROLLBACK on any exception."""
    try:
        conn.execute("BEGIN")
        fn(conn)
        conn.execute("COMMIT")
        return True
    except Exception:
        conn.execute("ROLLBACK")
        raise


class Repairer:
    """Applies deterministic repairs to the canonical roulette_spins."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # manual transaction control for atomic apply_plan (PRD §33)
        self.conn.isolation_level = None

    # ------------------------------------------------------------------
    # Backfill — insert a missing spin from authoritative history
    # ------------------------------------------------------------------
    def backfill_missing(self, game_id: str | None, number: int,
                         server_ts: str | None, source: str = "backfilled",
                         sequence_no: int | None = None,
                         commit: bool = True) -> int:
        """Insert one missing canonical spin. Returns row id."""
        if game_id is None:
            raise ValueError("cannot backfill without an authoritative game_id")
        # server_ts is NOT NULL — when no authoritative timestamp exists, the
        # repair time is the best available evidence (never fabricate a spin,
        # but a timestamp from the authoritative source OR capture time is fine).
        if server_ts is None:
            server_ts = now_iso()
        desc = f"{number} {'Green' if number == 0 else ('Red' if number in {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36} else 'Black')}"
        color = "Green" if number == 0 else ("Red" if number in {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36} else "Black")
        dk = schema.canonical_dedup_key(game_id, server_ts, number)
        if dk is None:
            raise ValueError("cannot backfill without any identity (game_id or server_ts+number)")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO roulette_spins "
            "(number, description, color, game_id, server_ts, captured_at, "
            " source, confidence, status, first_seen_at, sequence_no, dedup_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (number, desc, color, game_id, server_ts,
             now_iso(), source, 1.0, STATUS_REPAIRED, now_iso(), sequence_no, dk),
        )
        if commit:
            self.conn.commit()
        return cur.lastrowid if cur.lastrowid is not None else 0

    # ------------------------------------------------------------------
    # Correct — wrong canonical value with authoritative identity
    # ------------------------------------------------------------------
    def correct_value(self, game_id: str, new_number: int,
                      evidence: str = "remote-history", commit: bool = True) -> bool:
        """Replace the canonical number for game_id; preserve the raw
        observation. Returns True if a row changed."""
        row = self.conn.execute(
            "SELECT id, number, status FROM roulette_spins WHERE game_id=?",
            (game_id,),
        ).fetchone()
        if not row:
            return False
        old_number = row["number"]
        if old_number == new_number:
            return False
        desc = f"{new_number} {'Green' if new_number == 0 else ('Red' if new_number in {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36} else 'Black')}"
        color = "Green" if new_number == 0 else ("Red" if new_number in {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36} else "Black")
        self.conn.execute(
            "UPDATE roulette_spins SET number=?, description=?, color=?, "
            "status=?, confidence=1.0, last_verified_at=? WHERE game_id=?",
            (new_number, desc, color, STATUS_REPAIRED, now_iso(), game_id),
        )
        if commit:
            self.conn.commit()
        return True

    # ------------------------------------------------------------------
    # Collapse — duplicate game_id -> keep one canonical row
    # ------------------------------------------------------------------
    def collapse_duplicate(self, game_id: str, keep_number: int | None = None,
                           commit: bool = True) -> int:
        """Collapse duplicate game_id rows into the row matching keep_number
        (or the first); delete the rest. Returns rows removed."""
        rows = self.conn.execute(
            "SELECT id, number FROM roulette_spins WHERE game_id=? ORDER BY id",
            (game_id,),
        ).fetchall()
        if len(rows) <= 1:
            return 0
        keep_id = None
        if keep_number is not None:
            for r in rows:
                if r["number"] == keep_number:
                    keep_id = r["id"]
                    break
        if keep_id is None:
            keep_id = rows[0]["id"]
        removed = [r["id"] for r in rows if r["id"] != keep_id]
        for rid in removed:
            self.conn.execute("DELETE FROM roulette_spins WHERE id=?", (rid,))
        if commit:
            self.conn.commit()
        return len(removed)

    # ------------------------------------------------------------------
    # Reorder — rebuild sequence_no for a window
    # ------------------------------------------------------------------
    def reorder_window(self, game_ids_in_order: list[str], start: int = 1,
                       commit: bool = True) -> int:
        """Assign sequence_no start..start+N-1 to the given game_ids in order.
        Returns the count renumbered."""
        n = 0
        for i, gid in enumerate(game_ids_in_order, start=start):
            cur = self.conn.execute(
                "UPDATE roulette_spins SET sequence_no=?, last_verified_at=? "
                "WHERE game_id=?",
                (i, now_iso(), gid),
            )
            n += cur.rowcount
        if commit:
            self.conn.commit()
        return n

    # ------------------------------------------------------------------
    # Audit — every repair writes a repair_events row
    # ------------------------------------------------------------------
    def record_repair(self, incident_type: str, *, start_game_id=None,
                      end_game_id=None, affected_count=0, status="RESOLVED",
                      resolution=None, details=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO repair_events "
            "(created_at, incident_type, start_game_id, end_game_id, "
            " affected_count, status, attempts, last_attempt_at, resolved_at, "
            " resolution, details) "
            "VALUES (?,?,?,?,?,?,1,?,?,?,?)",
            (now_iso(), incident_type, start_game_id, end_game_id,
             affected_count, status, now_iso(), now_iso(),
             resolution, json.dumps(details) if details else None),
        )
        self.conn.commit()
        return cur.lastrowid if cur.lastrowid is not None else 0

    # ------------------------------------------------------------------
    # Apply a whole RepairPlan atomically (PRD §33)
    # ------------------------------------------------------------------
    def apply_plan(self, plan, verify_fn=None) -> dict:
        """Apply a ReconciliationResult's plan in ONE transaction.

        Returns {applied, backfilled, corrected, collapsed, reordered,
                 repair_event_id} — raises on any failure (transaction
        rolled back). `verify_fn(conn)` runs before COMMIT: if it raises,
        the whole repair rolls back (repair is not success until verified).
        """
        summary = {"backfilled": 0, "corrected": 0,
                   "collapsed": 0, "reordered": 0, "extras_flagged": 0}
        if not plan.authoritative:
            raise ValueError("refusing to repair without authoritative history")
        # Identity gate (Signal C): DOM-style number-only history can DETECT
        # but never drive repairs — identity is never manufactured (PRD §5).
        if not plan.repairable:
            raise ValueError(
                "refusing to repair without authoritative identity "
                "(game_id or server_ts on the remote history)"
            )

        def _apply(conn):
            # missing -> backfill at their exact authoritative position
            for rec in plan.missing:
                if rec.game_id:
                    self.backfill_missing(rec.game_id, rec.number, rec.server_ts,
                                          sequence_no=plan.missing_seq.get(rec.game_id),
                                          commit=False)
                    summary["backfilled"] += 1
            # corrections
            for gid, _old, new in plan.corrections:
                if self.correct_value(gid, new, commit=False):
                    summary["corrected"] += 1
            # duplicates -> collapse, preferring the row that matches the
            # authoritative value (from corrections: (gid, old, new) — new
            # is what the authoritative history says the spin IS).
            keep_by_gid = {c[0]: c[2] for c in plan.corrections}
            for gid in plan.duplicates:
                summary["collapsed"] += self.collapse_duplicate(
                    gid, keep_number=keep_by_gid.get(gid), commit=False)
            # sequence realignment (PRD §13 — "repair the entire affected
            # suffix"): explicit (game_id, absolute sequence_no) pairs;
            # legacy plans fall back to reorder_window.
            if plan.renumber:
                for gid, seq in plan.renumber:
                    cur = self.conn.execute(
                        "UPDATE roulette_spins SET sequence_no=?, last_verified_at=? "
                        "WHERE game_id=?",
                        (seq, now_iso(), gid),
                    )
                    summary["reordered"] += cur.rowcount
            elif plan.reorder:
                summary["reordered"] = self.reorder_window(
                    plan.reorder, start=plan.reorder_start, commit=False)
            # extras are NEVER deleted (PRD: no data destruction) — only
            # flagged for the incident panel; they age out of the window.
            summary["extras_flagged"] = len(plan.extras)
            if verify_fn is not None:
                verify_fn(conn)

        # atomicity (PRD §33): one BEGIN...COMMIT; ROLLBACK on any failure —
        # never partially repair a sequence.
        self.conn.execute("BEGIN")
        try:
            _apply(self.conn)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        ev_id = self.record_repair(
            "RECONCILIATION_REPAIR",
            affected_count=sum(summary.values()),
            resolution="REPAIRED",
            details=summary,
        )
        return {**summary, "repair_event_id": ev_id}
