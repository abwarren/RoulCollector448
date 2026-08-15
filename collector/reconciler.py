"""Reconciler (Phase 3) — the reconcile -> verify loop (PRD §13, §53).

This is the CORE of the self-auditing pipeline. Every pass:

  1. load latest local canonical spins (effective window)
  2. obtain authoritative recent history from a HistoryProvider
  3. normalize + match by strongest identity (game_id -> ts+number ->
     ordered position -> ts+neighbours)
  4. walk from the newest tail backwards to the first mismatch
  5. classify: missing / wrong-value / extra-local / ordering
  6. emit a repair plan (deterministic corrections only)
  7. apply via repairer (Phase 4), re-run validation, mark window verified

The pipeline NEVER guesses: if the authoritative source is unavailable the
window is left UNVERIFIED, and 'observed > inferred' (PRD §5) is respected
absolutely. Reconcile -> verify is never skipped — that is what makes the
dataset self-auditing (PRD §54: detect early, reconcile while the site's
rolling history is still available, repair deterministically, verify, only
then escalate browser/session recovery).
"""

import datetime
from dataclasses import dataclass, field


@dataclass
class HistoryRecord:
    """One authoritative spin from the site's recent history."""
    game_id: str | None
    number: int
    server_ts: str | None = None
    # position from the newest (0 = most recent) when the source orders them
    order_hint: int | None = None


@dataclass
class RepairPlan:
    """Deterministic corrections to apply to the canonical dataset."""
    missing: list = field(default_factory=list)       # HistoryRecord to backfill
    corrections: list = field(default_factory=list)   # (game_id, old_number, new_number)
    duplicates: list = field(default_factory=list)    # game_ids to collapse
    reorder: list = field(default_factory=list)       # game_ids whose sequence_no must be rebuilt
    window_achieved: int = 0
    authoritative: bool = False


@dataclass
class ReconciliationResult:
    ok: bool
    plan: RepairPlan
    missing_count: int = 0
    correction_count: int = 0
    duplicate_count: int = 0
    reorder_count: int = 0
    window: int = 0
    verified_count: int = 0
    message: str = ""


class HistoryProvider:
    """Abstraction over whatever authoritative recent-history source exists.

    The site's DOM history panel currently shows ~25 results; a WS
    join-snapshot may expose more; a REST endpoint would be best. Probe order
    is implementation-defined (probe_all() returns the best available).
    """

    def fetch_recent_history(self, limit: int = 500) -> list[HistoryRecord]:
        """Newest-first authoritative records. Raise on failure; return []
        when genuinely empty. Subclass or mock in tests."""
        raise NotImplementedError

    @property
    def max_window(self) -> int:
        """How many records this provider can actually produce (0 = unknown)."""
        return 0


def normalize_record(rec) -> HistoryRecord:
    """Coerce whatever shape a provider returns into a HistoryRecord."""
    if isinstance(rec, HistoryRecord):
        return rec
    return HistoryRecord(
        game_id=rec.get("game_id"),
        number=int(rec.get("number")),
        server_ts=rec.get("server_ts"),
        order_hint=rec.get("order_hint"),
    )


def match_key(rec) -> tuple | None:
    """Strongest identity available for a record (PRD §14 hierarchy):
    game_id if present, else server_ts+number, else None."""
    if rec.game_id:
        return ("game_id", rec.game_id)
    if rec.server_ts:
        return ("ts+number", rec.server_ts, rec.number)
    return None


def compare_windows(local, remote) -> RepairPlan:
    """Core algorithm: walk newest-first, find the first divergence, build a
    deterministic repair plan.

    local  — list of canonical spins (newest-first), each {game_id, number, server_ts}
    remote — list of HistoryRecords (newest-first)

    Returns a RepairPlan; `authoritative=True` means remote is trusted enough
    to drive repairs.
    """
    plan = RepairPlan()
    if not remote:
        plan.authoritative = False
        return plan
    plan.authoritative = True

    rmap = {}   # game_id -> HistoryRecord (remote)
    for r in remote:
        if r.game_id:
            rmap.setdefault(r.game_id, r)

    lset = {}   # game_id -> local canonical
    for l in local:
        if l.get("game_id"):
            lset[l["game_id"]] = l

    # --- 1) walk from the newest backwards until identities diverge ---
    # Match on strongest identity first (game_id), falling back to
    # ts+number, then ordered position.
    matched = 0
    for i in range(min(len(local), len(remote))):
        l = local[i]
        r = remote[i]
        if l.get("game_id") and r.game_id:
            if l["game_id"] == r.game_id:
                matched += 1
                continue
            else:
                # game ids diverge — everything from here is misaligned
                break
        else:
            # weak identity — fall back to ts+number or position
            lk = (l.get("server_ts"), l.get("number"))
            rk = (r.server_ts, r.number)
            if lk == rk or (not l.get("server_ts") and l.get("number") == r.number):
                matched += 1
                continue
            else:
                break

    # --- 2) everything beyond the matched prefix needs attention ---
    divergent_local = local[matched:]
    divergent_remote = remote[matched:]

    # --- 3) classify ---
    # missing: in remote, not in local (by game_id or ts+number)
    r_ids = {r.game_id for r in divergent_remote if r.game_id}
    l_ids = {l.get("game_id") for l in divergent_local if l.get("game_id")}
    for r in divergent_remote:
        if r.game_id and r.game_id not in lset:
            plan.missing.append(r)
        elif not r.game_id:
            # no identity — check by number+ts against local
            if not any(l.get("number") == r.number and
                       (l.get("server_ts") == r.server_ts or r.server_ts is None)
                       for l in divergent_local):
                plan.missing.append(r)

    # wrong values: same game_id, different number
    for r in divergent_remote:
        if r.game_id and r.game_id in lset:
            l = lset[r.game_id]
            if l.get("number") != r.number:
                plan.corrections.append((r.game_id, l.get("number"), r.number))

    # duplicates / conflicts in local canonical
    seen = {}
    for l in local:
        gid = l.get("game_id")
        if not gid:
            continue
        if gid in seen and seen[gid] != l.get("number"):
            plan.duplicates.append(gid)
        seen[gid] = l.get("number")

    plan.window_achieved = len(remote)
    return plan


def reconcile(local_spins, history_provider, window: int = 500) -> ReconciliationResult:
    """Run one reconcile pass. Returns a result with a RepairPlan.

    local_spins: canonical spins, OLDEST-first (as stored) — reversed here.
    """
    local = list(reversed(local_spins))  # newest-first for the walk
    try:
        remote = [normalize_record(r) for r in
                  history_provider.fetch_recent_history(limit=window)]
    except Exception as e:
        return ReconciliationResult(
            ok=False, plan=RepairPlan(authoritative=False),
            window=window, message=f"history unavailable: {e}",
        )

    plan = compare_windows(local, remote)
    if not plan.authoritative:
        return ReconciliationResult(
            ok=False, plan=plan, window=window,
            message="no authoritative history — window UNVERIFIED",
        )

    return ReconciliationResult(
        ok=not (plan.missing or plan.corrections or plan.duplicates or plan.reorder),
        plan=plan,
        missing_count=len(plan.missing),
        correction_count=len(plan.corrections),
        duplicate_count=len(plan.duplicates),
        reorder_count=len(plan.reorder),
        window=len(remote),
        verified_count=len(remote) - len(plan.missing) - len(plan.corrections),
        message="verified" if not (plan.missing or plan.corrections) else "repair plan generated",
    )
