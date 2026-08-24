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
    # PRD §16 duplicate classification:
    #   EXACT       — game_id + number + server_ts all identical
    #   CONFLICT    — game_id identical, number DIFFERS  (CRITICAL incident)
    #   TS_MISMATCH — game_id + number identical, server_ts differs
    duplicate_kinds: dict = field(default_factory=dict)  # gid -> kind
    reorder: list = field(default_factory=list)       # game_ids whose sequence_no must be rebuilt
    extras: list = field(default_factory=list)        # local rows with no remote counterpart (flag only, never delete)
    window_achieved: int = 0
    authoritative: bool = False
    # Repair authority (Signal C): True only when the remote history carries
    # identity (game_id or server_ts). DOM text (numbers only) can DETECT
    # discrepancies but never drive repairs — identity is never manufactured
    # (PRD §5, §14, §25).
    repairable: bool = False
    # Sequence realignment (PRD §13 — "repair the entire affected suffix"):
    #   renumber    — [(game_id, sequence_no)] RELATIVE (1 = oldest of the
    #                 remote window); reconcile() adds the window base.
    #                 Covers both insertion shifts (a missed middle spin
    #                 misaligns every newer record) and same-set reorders.
    #   missing_seq — {game_id: relative sequence_no} for records to
    #                 backfill at their exact authoritative position.
    renumber: list = field(default_factory=list)
    missing_seq: dict = field(default_factory=dict)
    # absolute sequence_no to start renumbering at (legacy field, kept for
    # the reorder_window path; renumber supersedes it)
    reorder_start: int = 1
    # Strongest PRD §14 matching level that actually held in this pass
    # (1=game_id .. 4=ts+neighbours; 0 = no identity). Drives what repairs
    # are permitted: game_id (1) is the only level that grants repair
    # authority — ts+number (2) may align positions but never manufactures
    # identity (PRD §5).
    match_level: int = 0


@dataclass
class ReconciliationResult:
    ok: bool
    plan: RepairPlan
    missing_count: int = 0
    correction_count: int = 0
    duplicate_count: int = 0
    reorder_count: int = 0
    extra_count: int = 0
    window: int = 0
    verified_count: int = 0
    repairable: bool = False
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


# PRD §14 matching hierarchy, level 4: timestamp + neighbouring results.
# A record matches if it has the same number as the candidate AND sits in the
# same position relative to its neighbour (previous remote record's number,
# or next remote record's number when first). This is the weakest identity —
# used only when neither game_id nor ts+number is available.
def _neighbor_key(recs, i):
    """(prev_number, next_number) around position i — the record's own
    number is EXCLUDED: level 4 matches on timestamp + neighbouring results,
    so the neighbour context must be comparable even when the record's value
    differs. Handles both HistoryRecord objects and plain dicts."""
    if not recs:
        return None
    def _num(x):
        return x.number if hasattr(x, "number") else x.get("number")
    prev = _num(recs[i - 1]) if i > 0 else None
    nxt = _num(recs[i + 1]) if i + 1 < len(recs) else None
    return (prev, nxt)


def compare_windows(local, remote) -> RepairPlan:
    """Core algorithm: walk newest-first, find the first divergence, build a
    deterministic repair plan.

    Matching hierarchy (PRD §14):
      1. game_id                          (strongest — also grants repair authority)
      2. server_ts + number
      3. ordered sequence position
      4. server_ts + neighbouring results (weakest)
    The walk uses the strongest identity available on BOTH sides at each
    index; a record matches if ANY level holds.

    local  — list of canonical spins (newest-first), each {game_id, number, server_ts}
    remote — list of HistoryRecords (newest-first)

    Returns a RepairPlan; `authoritative=True` means remote is trusted enough
    to drive repairs. `repairable` (Signal C) additionally requires identity
    (game_id or server_ts) — numbers alone can detect, never repair.
    """
    plan = RepairPlan()
    if not remote:
        plan.authoritative = False
        return plan
    plan.authoritative = True
    # Repair authority requires game_id identity to have actually matched
    # (PRD §14 level 1). ts+number (2) or weaker may align positions but
    # never manufactures identity (PRD §5). match_level is computed in the
    # walk below; repairable defaults False until a level-1 match proves it.
    plan.repairable = False

    def _level(l, r, i):
        """Return the strongest PRD §14 level at which l and r match, or 0.
        -1 (hard mismatch) only when BOTH carry DIFFERENT game_ids at an
        index where they'd be expected to align. Same game_id but different
        value still matches at level 1 — the value difference is a
        correction (PRD §15), not an identity mismatch."""
        if l.get("game_id") and r.game_id:
            if l["game_id"] == r.game_id:
                return 1
            return -1
        if l.get("server_ts") and r.server_ts:
            if l["server_ts"] == r.server_ts and l.get("number") == r.number:
                return 2
        # level 3: ordered position (same index) + same number
        if l.get("number") == r.number:
            return 3
        # level 4: timestamp + neighbouring results
        if l.get("server_ts") and r.server_ts and l["server_ts"] == r.server_ts:
            lk = _neighbor_key(local, i)
            rk = _neighbor_key(remote, i)
            if lk and lk == rk:
                return 4
        return 0

    # --- walk from the newest backwards until identities diverge ---
    matched = 0
    max_level = 0
    game_id_matched = False
    for i in range(min(len(local), len(remote))):
        lv = _level(local[i], remote[i], i)
        if lv > 0:
            matched += 1
            if lv > max_level:
                max_level = lv
            if lv == 1:
                game_id_matched = True
            continue
        break
    plan.match_level = max_level
    # Repair authority: the REMOTE history carries game_id identity (level-1
    # capable). We do NOT require an index to have matched — a reorder/swap
    # is precisely the case where indices DON'T match (E vs D at i=0), yet
    # the game_ids on both sides still make the remote authoritative. A
    # number-only remote (DOM) stays detection-only (identity never
    # manufactured, PRD §5).
    plan.repairable = any(r.game_id for r in remote)

    # everything beyond the matched prefix needs attention
    divergent_local = local[matched:]
    divergent_remote = remote[matched:]

    # identity maps used by the classification below
    rmap = {}   # game_id -> HistoryRecord (remote)
    for r in remote:
        if r.game_id:
            rmap.setdefault(r.game_id, r)
    lset = {}   # game_id -> list of local canonical rows (may be >1 on duplicates)
    for l in local:
        gid = l.get("game_id")
        if gid:
            lset.setdefault(gid, []).append(l)

    # --- 3) classify ---
    # wrong values: same game_id (or ts+number), different number — within
    # the matched prefix too (the walk matches by identity, not value)
    for i in range(matched):
        l = local[i]
        r = remote[i]
        if l.get("game_id") and r.game_id and l["game_id"] == r.game_id:
            if l.get("number") != r.number:
                plan.corrections.append((l["game_id"], l.get("number"), r.number))
        elif (l.get("server_ts") and r.server_ts
              and l["server_ts"] == r.server_ts and l.get("number") != r.number):
            plan.corrections.append((f"ts:{l['server_ts']}", l.get("number"), r.number))

    # missing: in remote, not in local (by game_id, else ts+number)
    r_ids = {r.game_id for r in divergent_remote if r.game_id}
    l_ids = {l.get("game_id") for l in divergent_local if l.get("game_id")}
    for r in divergent_remote:
        if r.game_id and r.game_id not in lset:
            plan.missing.append(r)
        elif not r.game_id:
            # no identity — check by number+ts against local (PRD §14 level 2)
            if not any(l.get("number") == r.number and
                       (l.get("server_ts") == r.server_ts or r.server_ts is None)
                       for l in divergent_local):
                plan.missing.append(r)

    # wrong values: same game_id, different number
    for r in divergent_remote:
        if r.game_id and r.game_id in lset:
            for l in lset[r.game_id]:
                if l.get("number") != r.number:
                    plan.corrections.append((r.game_id, l.get("number"), r.number))

    # duplicates / conflicts in local canonical (PRD §16)
    #   EXACT duplicate    — game_id + number + server_ts ALL identical
    #   CONFLICT duplicate — game_id identical, number DIFFERS (CRITICAL)
    #   TS_MISMATCH        — game_id + number identical, server_ts differs
    seen = {}          # gid -> first row dict
    plan.duplicate_kinds = {}
    for l in local:
        gid = l.get("game_id")
        if not gid:
            continue
        if gid in seen:
            first = seen[gid]
            if l.get("number") != first.get("number"):
                kind = "CONFLICT"          # critical incident
            elif l.get("server_ts") == first.get("server_ts"):
                kind = "EXACT"             # true duplicate row
            else:
                kind = "TS_MISMATCH"       # same identity, inconsistent ts
            if gid not in plan.duplicate_kinds:
                plan.duplicate_kinds[gid] = kind
            if gid not in plan.duplicates:
                plan.duplicates.append(gid)
        else:
            seen[gid] = l

    # extras: local rows in the divergent region with NO counterpart in the
    # authoritative history (by game_id, else ts+number — PRD §14 level 2).
    # Flagged, never auto-deleted — the PRD forbids destroying data; an
    # extra ages out of the window on its own.
    r_tsnum = {(r.server_ts, r.number) for r in divergent_remote if r.server_ts}
    for l in divergent_local:
        gid = l.get("game_id")
        if gid:
            if gid not in rmap:
                plan.extras.append(gid)
        else:
            num = l.get("number")
            ts = l.get("server_ts")
            if not any(
                r.number == num and (r.server_ts == ts or r.server_ts is None)
                for r in divergent_remote
            ):
                plan.extras.append(f"pos:{num}@{ts}")

    # --- sequence alignment (Signal C, PRD §13) ---
    # With game_id identity, compute each local spin's authoritative
    # position (relative sequence, 1 = oldest of the remote window):
    #   * insertion shift: a uniform positive delta on a contiguous newest
    #     suffix means records were missed BELOW it — the suffix is
    #     misaligned and must be renumbered to its authoritative positions
    #     while the missing records are backfilled at theirs.
    #   * same-set reorder: the divergent region has the same game_id set
    #     in a different order -> renumber to the authoritative order.
    # (anchored by a matched prefix so it can't false-fire on a wholly
    # different history)
    plan.renumber = []
    plan.missing_seq = {}
    if plan.repairable:
        rpos = {}
        for i, r in enumerate(remote):
            if r.game_id:
                rpos.setdefault(r.game_id, i)

        # every remote-only record's authoritative relative sequence
        # (identity: game_id, else ts+number — PRD §14 levels 1-2)
        for r in remote:
            if r.game_id and r.game_id not in lset:
                plan.missing_seq[r.game_id] = len(remote) - rpos[r.game_id]
            elif not r.game_id and r.server_ts:
                if not any(l.get("server_ts") == r.server_ts
                           and l.get("number") == r.number for l in local):
                    plan.missing_seq[f"ts:{r.server_ts}:{r.number}"] = (
                        len(remote) - rpos.get(r.server_ts,
                                               len(remote) - len(remote)))

        # insertion / deletion shift — only with game_id identity (the
        # authoritative position of a game_id is unambiguous; a ts+number
        # position would be inferred)
        deltas = {}
        for i, l in enumerate(local):
            gid = l.get("game_id")
            if gid and gid in rpos:
                local_age = len(local) - 1 - i
                remote_age = len(remote) - 1 - rpos[gid]
                d = remote_age - local_age
                if d:
                    deltas[gid] = d
        if deltas:
            vals = set(deltas.values())
            d = next(iter(vals)) if len(vals) == 1 else 0
            shifted = {g for g, dd in deltas.items()
                       if dd == d and g not in plan.duplicates}
            rpos_shifted = sorted(rpos[g] for g in shifted)
            if d > 0 and rpos_shifted == list(range(len(shifted))):
                oldest_first = sorted(shifted, key=lambda g: rpos[g])
                plan.renumber = [(g, len(remote) - rpos[g]) for g in oldest_first]

        # same-set reorder — no anchor required: the divergent region has the
        # SAME game_id set in a different order (a swap at the newest
        # position breaks the walk at i=0 with matched=0, but set equality
        # ALREADY proves it's the same history, so it can't false-fire).
        # Only the MINIMAL out-of-order suffix is renumbered: the longest
        # common prefix (oldest-first) is already correct and untouched.
        if (not plan.renumber
                and len(divergent_local) == len(divergent_remote) > 0):
            r_gids = [r.game_id for r in divergent_remote if r.game_id]
            l_gids = [l.get("game_id") for l in divergent_local if l.get("game_id")]
            if (r_gids and l_gids and len(r_gids) == len(divergent_remote)
                    and len(l_gids) == len(divergent_local)
                    and set(r_gids) == set(l_gids) and r_gids != l_gids):
                # oldest-first orders (divergent lists are newest-first)
                ro = list(reversed(r_gids))   # remote, oldest-first
                lo = list(reversed(l_gids))   # local, oldest-first
                # longest common prefix is ALREADY correct -> exclude it
                common = 0
                while (common < len(ro) and common < len(lo)
                       and ro[common] == lo[common]):
                    common += 1
                suffix = ro[common:]          # the minimal out-of-order set
                if not suffix:
                    pass
                # absolute positions: each suffix game_id's authoritative
                # position IS its remote index (len(remote) - rpos) — correct
                # for a newest swap AND a middle swap (anchored records keep
                # their slots; the suffix takes its true remote positions).
                plan.reorder = suffix         # oldest-first
                plan.renumber = [(g, len(remote) - rpos[g])
                                 for g in suffix]
                plan.reorder_start = plan.renumber[0][1] if plan.renumber else 1

    plan.window_achieved = len(remote)
    return plan


def duplicate_incidents(plan) -> list:
    """PRD §16 incident records for logging:
    [{game_id, kind, severity}] — CONFLICT -> CRITICAL (never silent),
    EXACT / TS_MISMATCH -> WARNING. Pure, deterministic."""
    out = []
    for gid in plan.duplicates:
        kind = plan.duplicate_kinds.get(gid, "EXACT")
        sev = "CRITICAL" if kind == "CONFLICT" else "WARNING"
        out.append({"game_id": gid, "kind": kind, "severity": sev})
    return out


def reconcile(local_spins, history_provider, window: int = 500,
              base: int | None = None) -> ReconciliationResult:
    """Run one reconcile pass. Returns a result with a RepairPlan.

    local_spins: canonical spins, OLDEST-first (as stored) — reversed here.

    base: the number of dataset records that PRECEDE this window (their
    absolute sequence positions are 1..base). Default None -> derived from
    the passed list (base = len(local_spins) - len(window slice)), which is
    only correct when the FULL dataset is passed. Callers that truncate to
    the latest `window` rows MUST pass the true base (e.g. total rows -
    window) so backfill/renumber positions are absolute, not window-relative.

    Authority is decided per-pass from the STRONGEST identity that actually
    matched (PRD §14): only a game_id (level 1) match grants repairable.
    A history that matches only by ts+number / position / neighbours stays
    detection-only — identity is never manufactured (PRD §5).
    """
    local = list(reversed(local_spins))  # newest-first for the walk
    try:
        remote = [normalize_record(r) for r in
                  history_provider.fetch_recent_history(limit=window)]
    except Exception as e:
        import traceback as _tb
        return ReconciliationResult(
            ok=False, plan=RepairPlan(authoritative=False),
            window=window, message=f"history unavailable: {e} | {_tb.format_exc()[-600:]}",
        )

    plan = compare_windows(local, remote)
    if not plan.authoritative:
        return ReconciliationResult(
            ok=False, plan=plan, window=window,
            message="no authoritative history — window UNVERIFIED",
        )

    # Relative sequences (1 = oldest of the remote window) become absolute:
    # the window base is how many records precede the window in the dataset.
    # The caller passes the true base when it truncated the dataset to the
    # window (the normal case); the passed-list derivation is only correct
    # for a full-dataset call.
    if base is None:
        base = len(local_spins) - len(local)
    if plan.reorder:
        plan.reorder_start += base
    if plan.renumber:
        plan.renumber = [(g, rel + base) for g, rel in plan.renumber]
    if plan.missing_seq:
        plan.missing_seq = {g: rel + base for g, rel in plan.missing_seq.items()}

    ok = not (plan.missing or plan.corrections or plan.duplicates
              or plan.renumber or plan.extras)
    if ok:
        message = "verified"
    elif plan.repairable:
        message = "repair plan generated"
    else:
        message = f"identity-limited (match level {plan.match_level}) — detection only, window UNVERIFIED"

    return ReconciliationResult(
        ok=ok,
        plan=plan,
        missing_count=len(plan.missing),
        correction_count=len(plan.corrections),
        duplicate_count=len(plan.duplicates),
        reorder_count=len(plan.renumber),
        extra_count=len(plan.extras),
        window=len(remote),
        verified_count=max(0, len(remote) - len(plan.missing)
                           - len(plan.corrections) - len(plan.extras)),
        repairable=plan.repairable,
        message=message,
    )
