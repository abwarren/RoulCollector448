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
import re
import sqlite3

from collector.observer import now_iso
from collector import schema

STATUS_REPAIRED = "REPAIRED"
STATUS_UNVERIFIED = "UNVERIFIED"


class RepairRefused(Exception):
    """Raised when a plan must NOT be auto-repaired (PRD §25).

    Carries the refusal reason (one of the §25 never-auto-repair
    conditions) so callers can surface it without losing it.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# PRD §25 — NEVER auto-repair conditions (each is an explicit, enforced gate;
# a refused plan marks the affected canonical rows UNVERIFIED and records the
# refusal in the repair queue — surfaced, never silent).
NEVER_AUTO_REPAIR = {
    "NO_AUTHORITY": "authoritative source unavailable",
    "NO_IDENTITY": "identity cannot be established (game_id or server_ts)",
    "CONFLICTING_SOURCES": "multiple conflicting sources (WS vs DOM disagreement)",
    "STATISTICAL_INFERENCE": "repair would require statistical inference (observed > inferred, PRD §5)",
}


def refuse_repair(conn, plan, reason_key: str) -> None:
    """Mark the plan's affected canonical rows UNVERIFIED and record the
    refusal in the repair queue (PRD §25: -> UNVERIFIED, surfaced).

    Marks the game_ids touched by the plan (missing/corrections/duplicates/
    reorder) as UNVERIFIED with the refusal reason in the queue. Never
    raises; the marking is best-effort (a DB failure degrades to a logged
    refusal event)."""
    reason = NEVER_AUTO_REPAIR.get(reason_key, reason_key)
    # affected game_ids from the plan
    gids = set()
    for r in plan.missing:
        if r.game_id:
            gids.add(r.game_id)
    for gid, _old, _new in plan.corrections:
        gids.add(gid)
    gids.update(plan.duplicates)
    gids.update(g for g, _ in plan.renumber)
    gids.update(plan.reorder)
    try:
        for gid in gids:
            conn.execute(
                "UPDATE roulette_spins SET status=?, last_verified_at=? "
                "WHERE game_id=? AND status != 'UNVERIFIED'",
                (STATUS_UNVERIFIED, now_iso(), gid),
            )
        conn.commit()
        # record the refusal in the repair queue (status=UNVERIFIED keeps it
        # out of the RESOLVED/FAILED retry paths)
        cur = conn.execute(
            "INSERT INTO repair_events "
            "(created_at, incident_type, start_game_id, end_game_id, "
            " affected_count, status, attempts, last_attempt_at, "
            " resolved_at, resolution, details) "
            "VALUES (?,?,?,?,?,?,1,?,NULL,?,?)",
            (now_iso(), "REPAIR_REFUSED", (sorted(gids) or [None])[0],
             (sorted(gids) or [None])[-1], len(gids),
             "UNVERIFIED", now_iso(), reason,
             json.dumps({"reason_key": reason_key, "reason": reason,
                         "game_ids": sorted(gids)})),
        )
        conn.commit()
    except Exception:
        try:
            conn.execute(
                "INSERT INTO repair_events "
                "(created_at, incident_type, status, attempts, resolution, details) "
                "VALUES (?,?,?,1,?,?)",
                (now_iso(), "REPAIR_REFUSED", "UNVERIFIED", reason,
                 json.dumps({"reason_key": reason_key, "reason": reason})),
            )
            conn.commit()
        except Exception:
            pass


def _ts_epoch(s) -> float | None:
    """Parse ISO-8601 or epoch (s/ms) timestamps to epoch seconds — the
    chronological evidence for reconstruct_ordering. None when unparseable
    (such rows sort last, after a deterministic id tie-break)."""
    if not s:
        return None
    s = str(s)
    try:
        f = float(s)
        return f if f < 1e12 else f / 1000.0
    except ValueError:
        pass
    try:
        import datetime
        return datetime.datetime.fromisoformat(
            s.replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return None


def plan_incident_types(plan) -> list:
    """Derive the PRD §23 incident types present in a RepairPlan. Returns a
    list of {"type", "start", "end", "count"} — one entry per incident class
    (MISSING_SPIN, WRONG_VALUE, DUPLICATE, CONFLICT, OUT_OF_ORDER). The
    repair queue records one event per class, not a blanket type."""
    out = []
    if plan.missing:
        gids = [r.game_id for r in plan.missing if r.game_id]
        if gids:
            out.append({"type": "MISSING_SPIN", "start": gids[0],
                        "end": gids[-1], "count": len(gids)})
    if plan.corrections:
        out.append({"type": "WRONG_VALUE", "start": plan.corrections[0][0],
                    "end": plan.corrections[-1][0],
                    "count": len(plan.corrections)})
    for gid in plan.duplicates:
        kind = plan.duplicate_kinds.get(gid, "EXACT")
        itype = "CONFLICT" if kind == "CONFLICT" else "DUPLICATE"
        out.append({"type": itype, "start": gid, "end": gid, "count": 1})
    n_seq = len(plan.renumber) or len(plan.reorder)
    if n_seq:
        gids = [g for g, _ in plan.renumber] or plan.reorder
        out.append({"type": "OUT_OF_ORDER", "start": gids[0],
                    "end": gids[-1], "count": n_seq})
    return out


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
        Returns the count renumbered.

        Lobby gids (lobby-<key>-<n>-<cnt>) carry their TRUE sequence in the
        trailing counter — window-relative positions would destroy it
        (2026-08-25: counters 3214-3243 renumbered to 136-165). Counter-
        bearing gids are set to their counter (idempotent no-op); only
        counter-less legacy rows get window positions.
        """
        n = 0
        for i, gid in enumerate(game_ids_in_order, start=start):
            m = re.search(r"-(\d+)$", gid or "")
            seq = int(m.group(1)) if m else i
            cur = self.conn.execute(
                "UPDATE roulette_spins SET sequence_no=?, last_verified_at=? "
                "WHERE game_id=?",
                (seq, now_iso(), gid),
            )
            n += cur.rowcount
        if commit:
            self.conn.commit()
        return n

    # ------------------------------------------------------------------
    # Reconstruct — rebuild the canonical ordering (sequence_no 1..N)
    # ------------------------------------------------------------------
    def reconstruct_ordering(self, commit: bool = True,
                             preserve_gaps: bool = True) -> dict:
        """Reconstruct the canonical ordering: rebuild sequence_no 1..N for
        ALL rows from the best available chronological evidence (server_ts,
        fallback captured_at, deterministic id tie-break).

        Deterministic and idempotent: a consistent dataset (1..N, matching
        the chronological evidence) changes nothing. Fixes collisions
        (duplicate sequence_no), NULL sequence_no, and order violations
        (sequence contradicting timestamps).

        preserve_gaps=True (default): sequence HOLES are preserved, never
        compressed — the gap lifecycle (recover_gaps) owns holes and must
        find them still present to attempt recovery from history. With
        preserve_gaps=False the old compression applies (1,2,4,5 ->
        1,2,3,4) for callers that want a tight 1..N (e.g. a final sweep
        AFTER recovery).

        Records a RECONSTRUCT_ORDER repair event when anything changed, with
        the before-state anomalies (collisions_found, gaps_found) in
        details. Never touches observations.

        Returns {"checked", "reordered", "gaps_found", "collisions_found"}.
        """
        rows = self.conn.execute(
            "SELECT id, game_id, server_ts, captured_at, sequence_no "
            "FROM roulette_spins"
        ).fetchall()
        n = len(rows)
        if not n:
            return {"checked": 0, "reordered": 0,
                    "gaps_found": 0, "collisions_found": 0}

        # before-state anomalies
        seqs = [r["sequence_no"] for r in rows if r["sequence_no"] is not None]
        collisions = len(seqs) - len(set(seqs))
        gaps = 0
        if seqs:
            present = set(seqs)
            gaps = sum(1 for i in range(1, max(seqs) + 1) if i not in present)

        # chronological evidence order: server_ts, fallback captured_at,
        # unparseable/absent sorts last, id tie-breaks (deterministic)
        def _key(r):
            ts = _ts_epoch(r["server_ts"])
            if ts is None:
                ts = _ts_epoch(r["captured_at"])
            return (ts is None, ts if ts is not None else 0.0, r["id"])

        ordered = sorted(rows, key=_key)

        if preserve_gaps:
            # preserve holes: assign each row (in evidence order) the k-th
            # smallest PRESENT position (k = evidence index) — holes are
            # never in `present`, so they're never filled. Collisions,
            # order violations and NULLs are fixed; holes survive for the
            # gap-recovery lifecycle. Deterministic + idempotent (a
            # consistent dataset keeps its positions).
            present_sorted = sorted(set(seqs))
            changed = 0
            for k, r in enumerate(ordered):
                if k < len(present_sorted):
                    target = present_sorted[k]
                else:
                    # beyond the present set — extend the sequence past the
                    # last present position (the row is newest, past a
                    # collision-compressed tail)
                    target = (present_sorted[-1] + (k - len(present_sorted) + 1)
                              if present_sorted else k + 1)
                if r["sequence_no"] != target:
                    changed += 1
                    self.conn.execute(
                        "UPDATE roulette_spins SET sequence_no=?, last_verified_at=? "
                        "WHERE id=?",
                        (target, now_iso(), r["id"]),
                    )
        else:
            changed = 0
            for i, r in enumerate(ordered, start=1):
                if r["sequence_no"] != i:
                    changed += 1
                    self.conn.execute(
                        "UPDATE roulette_spins SET sequence_no=?, last_verified_at=? "
                        "WHERE id=?",
                        (i, now_iso(), r["id"]),
                    )
        if commit:
            self.conn.commit()
        if changed:
            self.record_repair(
                "RECONSTRUCT_ORDER", affected_count=changed,
                status="RESOLVED", resolution="REORDERED",
                details={"checked": n, "reordered": changed,
                         "gaps_found": gaps, "collisions_found": collisions},
            )
        return {"checked": n, "reordered": changed,
                "gaps_found": gaps, "collisions_found": collisions}

    def record_gap(self, *, start_seq: int, end_seq: int, size: int,
                   status: str = "OPEN", resolution: str | None = None,
                   details: dict | None = None) -> int:
        """Record a gap in the canonical sequence as a repair event (the
        §26-new recovery lifecycle). A gap is OPEN when first detected;
        the recovery loop later resolves it RESOLVED/REPAIRED (repaired gap)
        or UNVERIFIED (permanent gap). Returns the event id.

        start/end are the sequence positions bounding the gap; size is the
        number of missing spins. Never raises."""
        try:
            now = now_iso()
            cur = self.conn.execute(
                "INSERT INTO repair_events "
                "(created_at, incident_type, start_game_id, end_game_id, "
                " affected_count, status, attempts, last_attempt_at, "
                " resolved_at, resolution, details) "
                "VALUES (?, 'GAP', ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (now, f"seq:{start_seq}", f"seq:{end_seq}", size,
                 status, now,
                 now if status in ("RESOLVED", "UNVERIFIED") else None,
                 resolution,
                 json.dumps(details) if details else None),
            )
            self.conn.commit()
            return cur.lastrowid if cur.lastrowid is not None else 0
        except Exception:
            return 0

    def resolve_gap(self, event_id: int, *, status: str = "RESOLVED",
                    resolution: str = "REPAIRED",
                    details: dict | None = None) -> None:
        """Resolve a gap event after the recovery loop: RESOLVED/REPAIRED
        (the gap was backfilled) or UNVERIFIED (permanent — could not be
        repaired). Never raises."""
        try:
            now = now_iso()
            self.conn.execute(
                "UPDATE repair_events SET status=?, resolution=?, resolved_at=?, "
                "details=?, last_attempt_at=? WHERE id=?",
                (status, resolution, now,
                 json.dumps(details) if details else None, now, event_id),
            )
            self.conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Audit — every repair writes a repair_events row (PRD §23 queue)
    # ------------------------------------------------------------------
    def record_repair(self, incident_type: str, *, start_game_id=None,
                      end_game_id=None, affected_count=0, status="RESOLVED",
                      resolution=None, details=None) -> int:
        """Record a repair attempt in the queue (PRD §23). If an unresolved
        event (OPEN/FAILED) exists for the same incident (type + start id),
        INCREMENT attempts and update last_attempt_at — retry semantics.
        Otherwise insert a new event with attempts=1. Returns the event id."""
        now = now_iso()
        row = self.conn.execute(
            "SELECT id FROM repair_events "
            "WHERE incident_type=? AND start_game_id IS ? "
            "AND status IN ('OPEN','FAILED') ORDER BY id DESC LIMIT 1",
            (incident_type, start_game_id),
        ).fetchone()
        if row:
            ev_id = row["id"]
            if status == "RESOLVED":
                self.conn.execute(
                    "UPDATE repair_events SET attempts=attempts+1, "
                    "last_attempt_at=?, status='RESOLVED', resolved_at=?, "
                    "resolution=?, affected_count=?, details=? WHERE id=?",
                    (now, now, resolution, affected_count,
                     json.dumps(details) if details else None, ev_id),
                )
            else:
                self.conn.execute(
                    "UPDATE repair_events SET attempts=attempts+1, "
                    "last_attempt_at=?, status=?, resolution=? WHERE id=?",
                    (now, status, resolution, ev_id),
                )
        else:
            cur = self.conn.execute(
                "INSERT INTO repair_events "
                "(created_at, incident_type, start_game_id, end_game_id, "
                " affected_count, status, attempts, last_attempt_at, "
                " resolved_at, resolution, details) "
                "VALUES (?,?,?,?,?,?,1,?,?,?,?)",
                (now, incident_type, start_game_id, end_game_id,
                 affected_count, status, now,
                 now if status == "RESOLVED" else None,
                 resolution, json.dumps(details) if details else None),
            )
            ev_id = cur.lastrowid if cur.lastrowid is not None else 0
        self.conn.commit()
        return ev_id

    # ------------------------------------------------------------------
    # Apply a whole RepairPlan atomically (PRD §33)
    # ------------------------------------------------------------------
    def apply_plan(self, plan, verify_fn=None, check_conflict: bool = True) -> dict:
        """Apply a ReconciliationResult's plan in ONE transaction.

        Returns {applied, backfilled, corrected, collapsed, reordered,
                 repair_event_id} — raises on any failure (transaction
        rolled back). `verify_fn(conn)` runs before COMMIT: if it raises,
        the whole repair rolls back (repair is not success until verified).
        """
        summary = {"backfilled": 0, "corrected": 0,
                   "collapsed": 0, "reordered": 0, "extras_flagged": 0}
        # PRD §25 — NEVER auto-repair gates (each refusal marks the affected
        # rows UNVERIFIED + records the reason in the queue; surfaced, never
        # silent). A conflicting-sources check runs first (the reconciler's
        # history authority + the DOM secondary channel disagreeing is a
        # refusal, not a repair).
        if check_conflict and self.conn is not None:
            try:
                from collector import source_agreement
                ag = source_agreement.verify_recent_agreement(
                    self.conn, window=50)
                # §25: refuse only when a CONFLICT involves THIS plan's
                # affected game_ids — a stale unrelated disagreement (another
                # spin) must not block a correct repair. game_ids that
                # disagree appear in verify_recent_agreement's conflicts
                # only when they carried identity; without identity the
                # conflict is positional and the affected ids are unknown,
                # so any conflict then refuses (conservative).
                affected = set()
                for r in plan.missing:
                    if r.game_id:
                        affected.add(r.game_id)
                for gid, _o, _n in plan.corrections:
                    affected.add(gid)
                affected.update(plan.duplicates)
                scoped_conflict = False
                conflict_gids = set(ag.get("conflict_game_ids") or [])
                if conflict_gids:
                    # identity-bearing conflicts: refuse only if one involves
                    # this plan's game_ids
                    if not affected or (affected & conflict_gids):
                        scoped_conflict = True
                elif ag.get("conflicts", 0) > 0:
                    # positional conflicts (no identity): conservative —
                    # refuse (the affected ids are unknown)
                    scoped_conflict = True
                if ag.get("checked") and scoped_conflict:
                    refuse_repair(self.conn, plan, "CONFLICTING_SOURCES")
                    raise RepairRefused(NEVER_AUTO_REPAIR["CONFLICTING_SOURCES"])
            except RepairRefused:
                raise
            except Exception:
                pass   # agreement check is best-effort; never blocks repair
        if not plan.authoritative:
            refuse_repair(self.conn, plan, "NO_AUTHORITY")
            raise RepairRefused(NEVER_AUTO_REPAIR["NO_AUTHORITY"])
        # Identity gate (Signal C): DOM-style number-only history can DETECT
        # but never drive repairs — identity is never manufactured (PRD §5).
        if not plan.repairable:
            refuse_repair(self.conn, plan, "NO_IDENTITY")
            raise RepairRefused(NEVER_AUTO_REPAIR["NO_IDENTITY"])

        def _apply(conn):
            # missing -> backfill at their exact authoritative position
            for rec in plan.missing:
                if rec.game_id:
                    self.backfill_missing(rec.game_id, rec.number, rec.server_ts,
                                          sequence_no=plan.missing_seq.get(rec.game_id),
                                          commit=False)
                    summary["backfilled"] += 1
            # corrections — capture old->new for the audit trail (§35)
            for gid, _old, new in plan.corrections:
                row = self.conn.execute(
                    "SELECT number FROM roulette_spins WHERE game_id=?", (gid,)
                ).fetchone()
                old = row["number"] if row else _old
                if self.correct_value(gid, new, commit=False):
                    summary["corrected"] += 1
                    corrections_detail.append((gid, old, new))
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
                    m = re.search(r"-(\d+)$", gid or "")
                    if m:  # lobby gid counter is the truth — idempotent no-op
                        seq = int(m.group(1))
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

        incidents = plan_incident_types(plan)
        corrections_detail = []  # (gid, old, new) audit trail

        # atomicity (PRD §33): one BEGIN...COMMIT; ROLLBACK on any failure —
        # never partially repair a sequence.
        self.conn.execute("BEGIN")
        try:
            _apply(self.conn)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            # a FAILED attempt still lands in the queue (retry semantics:
            # the next pass increments attempts on the same incident).
            for inc in incidents:
                self.record_repair(
                    inc["type"], start_game_id=inc["start"],
                    end_game_id=inc["end"], affected_count=inc["count"],
                    status="FAILED", resolution="REPAIR_FAILED",
                    details={**summary, "error": "verify_failed"},
                )
            raise

        ev_ids = []
        for inc in incidents:
            ev_id = self.record_repair(
                inc["type"], start_game_id=inc["start"],
                end_game_id=inc["end"], affected_count=inc["count"],
                status="RESOLVED", resolution="REPAIRED",
                details={**summary, "corrections": corrections_detail},
            )
            ev_ids.append(ev_id)
        return {**summary, "repair_event_id": ev_ids[0] if ev_ids else 0,
                "repair_event_ids": ev_ids}
