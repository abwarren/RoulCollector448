"""Stats computations over the collector DB (all read-only)."""

import sqlite3

from .wheel import WHEEL, color_of, nn_cluster

P = 1 / 37  # fair per-number probability on a European wheel

_WHEEL_POS = {n: i for i, n in enumerate(WHEEL)}


def z_score(hits: int, n: int) -> float:
    """Standard deviations an observed count sits from fair expectation."""
    if n <= 0:
        return 0.0
    expected = n * P
    sd = (n * P * (1 - P)) ** 0.5
    return round((hits - expected) / sd, 2) if sd else 0.0


def _balance(rows):
    counts = {"Red": 0, "Black": 0, "Green": 0}
    for r in rows:
        counts[r["color"]] += 1
    return counts


def _dozens(rows):
    d = {"0": 0, "1-12": 0, "13-24": 0, "25-36": 0}
    for r in rows:
        n = r["number"]
        if n == 0:
            d["0"] += 1
        elif n <= 12:
            d["1-12"] += 1
        elif n <= 24:
            d["13-24"] += 1
        else:
            d["25-36"] += 1
    return d


def _parity(rows):
    d = {"Even": 0, "Odd": 0, "Zero": 0}
    for r in rows:
        n = r["number"]
        d["Zero" if n == 0 else ("Even" if n % 2 == 0 else "Odd")] += 1
    return d


def numbers_stats(conn: sqlite3.Connection, limit=None, live_spins=None) -> dict:
    """Per-number hits/expected/z (+ last-100 hits for all-time).

    `live_spins` = chronological uncommitted journald spins (newer than
    anything in the DB). In the windowed branch they merge as virtual
    trailing spins so `limit` means the TRUE last `limit` spins (DB + live),
    not the last `limit` committed rows — without this, a 25-spin DB batch
    lag (~18 min at 44s cadence) makes a "last 500" window silently the last
    ~476-500 committed spins. All-time ignores live spins to stay consistent
    with /api/stats/numbers (the journald guard drops them once committed).
    """
    live_spins = live_spins or []
    if limit is None:
        n = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
        rows = conn.execute(
            "SELECT number, COUNT(*) AS hits FROM roulette_spins GROUP BY number"
        ).fetchall()
        last100 = dict(
            conn.execute(
                "SELECT number, COUNT(*) FROM (SELECT number FROM roulette_spins "
                "ORDER BY id DESC LIMIT 100) GROUP BY number"
            ).fetchall()
        )
        by_num = {r["number"]: r["hits"] for r in rows}
    else:
        db_take = max(0, limit - len(live_spins))
        rows = conn.execute(
            "SELECT number, COUNT(*) AS hits FROM (SELECT number FROM roulette_spins "
            "ORDER BY id DESC LIMIT ?) GROUP BY number",
            (db_take,),
        ).fetchall()
        by_num = {r["number"]: r["hits"] for r in rows}
        for s in live_spins:
            by_num[s["number"]] = by_num.get(s["number"], 0) + 1
        # true window size: sum(hits) = raw DB rows in the window (GROUP BY
        # collapses to <=37 rows; every spin contributes exactly one hit)
        n = sum(r["hits"] for r in rows) + len(live_spins)
        last100 = {}

    out = []
    for num in range(37):
        hits = by_num.get(num, 0)
        out.append(
            {
                "number": num,
                "color": color_of(num),
                "hits": hits,
                "expected": round(n * P, 1),
                "z": z_score(hits, n),
                "hits_100": last100.get(num, 0),
            }
        )
    out.sort(key=lambda d: abs(d["z"]), reverse=True)
    return {"total": n, "numbers": out}


def sleepers(conn: sqlite3.Connection, live_spins=None) -> dict:
    """Current drought per number: spins since its last hit.

    NOTE: uses positional rank, not raw id — the collector's AUTOINCREMENT
    id accumulates across restarts (14k rows can have 6M+ ids).

    `live_spins` = chronological list of {"number", "time"} from the
    collector journald feed that are NOT yet committed to the DB (it
    commits in 25-spin batches ≈ 18 min at 44s cadence). They are merged
    as virtual trailing spins so a number that hit live stops showing as a
    sleeper immediately instead of after the next DB commit.
    """
    live_spins = live_spins or []
    total = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
    rows = conn.execute(
        "WITH ranked AS (SELECT id, number, captured_at, "
        "ROW_NUMBER() OVER (ORDER BY id) AS pos FROM roulette_spins) "
        "SELECT number, MAX(pos) AS last_pos, MAX(captured_at) AS last_at "
        "FROM ranked GROUP BY number"
    ).fetchall()

    # last live-window occurrence per number (0-based, chronological order)
    live_by_num = {}
    for i, s in enumerate(live_spins):
        live_by_num[s["number"]] = i

    grand_total = total + len(live_spins)
    out = []
    for r in rows:
        num = r["number"]
        if num in live_by_num:
            gap = grand_total - (total + live_by_num[num] + 1)
            last_at = _live_ts(live_spins[live_by_num[num]]["time"], r["last_at"])
        else:
            gap = grand_total - r["last_pos"]
            last_at = r["last_at"]
        out.append(
            {
                "number": num,
                "color": color_of(num),
                "gap": gap,
                "last_hit_at": last_at,
            }
        )
    out.sort(key=lambda d: d["gap"], reverse=True)
    return {"total": total, "live": len(live_spins), "sleepers": out}


def _live_ts(hms: str, db_last_at) -> str:
    """Journald 'HH:MM:SS' -> ISO 'YYYY-MM-DDTHH:MM:SS', guarding rollover.

    Live spins are always newer than the newest DB row; if today's date
    would put the live time BEFORE the DB's last hit, the spin is from
    yesterday (midnight rollover).
    """
    import datetime

    day = datetime.date.today()
    ts = f"{day.isoformat()}T{hms}"
    try:
        if datetime.datetime.fromisoformat(ts) < datetime.datetime.fromisoformat(
            db_last_at
        ) - datetime.timedelta(hours=12):
            ts = f"{(day - datetime.timedelta(days=1)).isoformat()}T{hms}"
    except (TypeError, ValueError):
        pass
    return ts


def streaks(conn: sqlite3.Connection) -> dict:
    """Longest color runs, current run, number-repeat runs."""
    rows = conn.execute(
        "SELECT id, number, color FROM roulette_spins ORDER BY id ASC"
    ).fetchall()
    if not rows:
        return {}

    longest = {"Red": 0, "Black": 0, "Green": 0}
    longest_span = {}
    run_color, run_len, run_start = rows[0]["color"], 1, rows[0]["id"]
    for prev, cur in zip(rows, rows[1:]):
        if cur["color"] == prev["color"]:
            run_len += 1
        else:
            if run_len > longest[run_color]:
                longest[run_color] = run_len
                longest_span[run_color] = (run_start, prev["id"])
            run_color, run_len, run_start = cur["color"], 1, cur["id"]
    if run_len > longest[run_color]:
        longest[run_color] = run_len
        longest_span[run_color] = (run_start, rows[-1]["id"])

    # current running streak (from the end)
    cur_run = {"color": rows[-1]["color"], "length": 1}
    for prev, cur in zip(reversed(rows), reversed(rows[:-1])):
        if cur["color"] == prev["color"]:
            cur_run["length"] += 1
        else:
            break

    # number repeats
    run_lens = []
    rl = 1
    for prev, cur in zip(rows, rows[1:]):
        if cur["number"] == prev["number"]:
            rl += 1
        else:
            run_lens.append(rl)
            rl = 1
    run_lens.append(rl)
    longest_num = max(run_lens) if run_lens else 1
    counts = {
        "doubles": sum(1 for l in run_lens if l == 2),
        "triples": sum(1 for l in run_lens if l == 3),
        "quads_plus": sum(1 for l in run_lens if l >= 4),
    }

    return {
        "longest": {c: longest[c] for c in ("Red", "Black", "Green")},
        "longest_span": {c: list(longest_span[c]) for c in longest_span},
        "current": cur_run,
        "longest_number_repeat": longest_num,
        "repeat_counts": counts,
        "total_spins": len(rows),
    }


def rolling(conn: sqlite3.Connection, window: int) -> dict:
    """Summary for the last `window` spins (hits, balances, neighbor rate)."""
    rows = conn.execute(
        "SELECT number, color FROM roulette_spins ORDER BY id DESC LIMIT ?",
        (window,),
    ).fetchall()
    rows = rows[::-1]  # chronological
    n = len(rows)
    if n == 0:
        return {"window": window, "total": 0}

    by_num = {}
    for r in rows:
        by_num[r["number"]] = by_num.get(r["number"], 0) + 1
    per_num = [
        {
            "number": num,
            "color": color_of(num),
            "hits": by_num.get(num, 0),
            "expected": round(n * P, 1),
            "z": z_score(by_num.get(num, 0), n),
        }
        for num in range(37)
    ]

    neighbor_hits = 0
    for a, b in zip(rows, rows[1:]):
        if b["number"] in nn_cluster(a["number"]):
            neighbor_hits += 1

    ordered = sorted(per_num, key=lambda d: d["hits"], reverse=True)
    return {
        "window": window,
        "total": n,
        "numbers": per_num,
        "top": [d["number"] for d in ordered[:5]],
        "bottom": [d["number"] for d in ordered[-5:]],
        "color_balance": _balance(rows),
        "dozens": _dozens(rows),
        "parity": _parity(rows),
        "neighbor_rate": round(neighbor_hits / max(1, n - 1), 4),
    }


def audit(conn: sqlite3.Connection, window: int = 500, live_spins=None) -> dict:
    """Hourly audit: current (all-time) stats vs the true last `window` spins.

    `live_spins` (uncommitted journald spins) merge into the last-window side
    so the window is the TRUE last `window` spins — same live-merge as
    sleepers. All-time stays DB-only (consistent with /api/stats/numbers).
    """
    live_spins = live_spins or []
    all_stats = numbers_stats(conn)
    w_stats = numbers_stats(conn, limit=window, live_spins=live_spins)

    z_all = {d["number"]: d["z"] for d in all_stats["numbers"]}
    z_w = {d["number"]: d["z"] for d in w_stats["numbers"]}

    drift = [
        {
            "number": num,
            "all_z": z_all[num],
            "last500_z": z_w[num],
            "delta": round(z_w[num] - z_all[num], 2),
        }
        for num in range(37)
    ]
    drift.sort(key=lambda d: abs(d["delta"]), reverse=True)

    top_all = set(d["number"] for d in all_stats["numbers"][:5])
    top_w = set(d["number"] for d in w_stats["numbers"][:5])

    return {
        "window": window,
        "live": len(live_spins),
        "all_time": {
            "total": all_stats["total"],
            "top": sorted(top_all),
            "bottom": sorted(d["number"] for d in all_stats["numbers"][-5:]),
        },
        "last_window": {
            "total": w_stats["total"],
            "top": sorted(top_w),
            "bottom": sorted(d["number"] for d in w_stats["numbers"][-5:]),
        },
        "drift": drift[:10],
        "rotated_hot": sorted(top_w - top_all),
        "rotated_cold": sorted(top_all - top_w),
    }


def transitions(conn: sqlite3.Connection, limit=None, live_spins=None) -> dict:
    """37x37 next-spin transition matrix + neighbor-sequence diagnostics.

    - `matrix[f][t]` = count of number f immediately followed by number t.
    - Per-cell z vs uniform independence (p = 1/1369 per ordered pair).
    - `top_pairs` = highest-z ordered pairs (the "neighbor-to-neighbor"
      sequences the user watches).
    - `nn_follow` = per-number rate that the NEXT spin lands in Nn (5-cluster).
    - `distances` = circular wheel-gap distribution between consecutive spins
      (0 = same pocket, 1..18 = gap size). Under a fair wheel, gap 0 has
      p = 1/37 and every other gap p = 2/37 — a non-flat shape here is the
      cleanest single-number read on physical/mechanical bias.

    `live_spins` (uncommitted journald spins, chronological) merge as
    virtual trailing spins, same live-merge as sleepers/audit.
    """
    live_spins = live_spins or []
    rows = conn.execute(
        "SELECT number FROM roulette_spins ORDER BY id ASC"
    ).fetchall()
    nums = [r["number"] for r in rows]
    if live_spins:
        nums += [s["number"] for s in live_spins]
    if limit:
        nums = nums[-limit:]
    pairs = len(nums) - 1
    if pairs <= 0:
        return {"total": len(nums), "pairs": 0}

    matrix = [[0] * 37 for _ in range(37)]
    dist_counts = [0] * 19
    color_matrix = [[0] * 3 for _ in range(3)]
    col_idx = {"Red": 0, "Black": 1, "Green": 2}
    for a, b in zip(nums, nums[1:]):
        matrix[a][b] += 1
        d = abs(_WHEEL_POS[a] - _WHEEL_POS[b])
        dist_counts[min(d, 37 - d)] += 1
        color_matrix[col_idx[color_of(a)]][col_idx[color_of(b)]] += 1

    # per-cell expectation under uniform independence
    p_cell = 1 / 1369
    exp_cell = pairs * p_cell
    sd_cell = (pairs * p_cell * (1 - p_cell)) ** 0.5
    cells = []
    for f in range(37):
        for t in range(37):
            c = matrix[f][t]
            z = (c - exp_cell) / sd_cell if sd_cell else 0.0
            cells.append(
                {
                    "from": f,
                    "to": t,
                    "count": c,
                    "expected": round(exp_cell, 1),
                    "ratio": round(c / exp_cell, 2) if exp_cell else 0.0,
                    "z": round(z, 2),
                }
            )
    cells.sort(key=lambda d: -d["z"])
    top_pairs = cells[:15]
    bottom_pairs = [c for c in cells if c["count"] > 0][-5:]

    # Nn-cluster follow rate per number
    nn_rows = []
    for f in range(37):
        row = matrix[f]
        row_sum = sum(row)
        cluster = nn_cluster(f)
        hits = sum(row[t] for t in cluster)
        exp_rate = 5 / 37
        exp_hits = row_sum * exp_rate
        sd = (row_sum * exp_rate * (1 - exp_rate)) ** 0.5
        nn_rows.append(
            {
                "number": f,
                "color": color_of(f),
                "n": row_sum,
                "hits": hits,
                "expected": round(exp_hits, 1),
                "rate": round(hits / row_sum, 4) if row_sum else 0.0,
                "z": round((hits - exp_hits) / sd, 2) if sd else 0.0,
            }
        )
    nn_rows.sort(key=lambda d: -d["z"])
    nn_total_hits = sum(r["hits"] for r in nn_rows)
    nn_rate = nn_total_hits / pairs
    exp_nn_rate = 5 / 37
    # count-scale sd: z = (observed hits - expected hits) / sd
    sd_nn = (pairs * exp_nn_rate * (1 - exp_nn_rate)) ** 0.5
    nn_z = (nn_total_hits - pairs * exp_nn_rate) / sd_nn if sd_nn else 0.0

    # wheel-gap distribution
    dist_rows = []
    for d in range(19):
        exp_d = pairs * (1 / 37 if d == 0 else 2 / 37)
        sd_d = (pairs * (exp_d / pairs) * (1 - exp_d / pairs)) ** 0.5
        dist_rows.append(
            {
                "gap": d,
                "count": dist_counts[d],
                "expected": round(exp_d, 1),
                "z": round((dist_counts[d] - exp_d) / sd_d, 2) if sd_d else 0.0,
            }
        )

    return {
        "total": len(nums),
        "pairs": pairs,
        "expected_per_pair": round(exp_cell, 1),
        "matrix": matrix,
        "top_pairs": top_pairs,
        "bottom_pairs": bottom_pairs,
        "nn_follow": nn_rows,
        "nn_follow_rate": round(nn_rate, 4),
        "nn_follow_expected": exp_nn_rate,
        "nn_follow_z": round(nn_z, 2) if sd_nn else 0.0,
        "distances": dist_rows,
        "color_matrix": {
            "labels": ["Red", "Black", "Green"],
            "matrix": color_matrix,
        },
    }
