"""PRD §29 — collector watchdog upgrade: PROCESS DEAD is a first-class,
immediate signal.

The old watchdog checked the HEARTBEAT first and returned "ok" whenever it
was fresh — a collector that died seconds ago (heartbeat still fresh) was
reported healthy until the 12-minute silence window elapsed. The upgraded
watchdog checks the OS process table FIRST: dead -> restart immediately.

The decision is the pure function scripts/watchdog_win.decide(alive,
hb_age, proc_age) — these tests pin every branch without invoking
PowerShell.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

from watchdog_win import (  # noqa: E402
    BOOT_GRACE_S,
    NO_OUTPUT_S,
    SILENCE_S,
    decide,
)


def test_process_dead_with_fresh_heartbeat_restarts():
    """THE §29 case: the process died seconds ago but the heartbeat is still
    fresh — must restart IMMEDIATELY, not wait for staleness."""
    action, restart = decide(alive=False, hb_age=5)   # heartbeat 5s old
    assert action == "dead"
    assert restart is True


def test_process_dead_with_no_heartbeat_restarts():
    """Process dead + heartbeat missing (crash before first write, or
    never started) -> dead, restart."""
    action, restart = decide(alive=False, hb_age=None)
    assert action == "dead" and restart is True


def test_process_dead_ignores_stale_heartbeat():
    """Even a very stale heartbeat doesn't change the dead verdict."""
    action, restart = decide(alive=False, hb_age=60 * 60)
    assert action == "dead" and restart is True


def test_healthy_alive_fresh_heartbeat():
    """Process alive + fresh heartbeat -> ok, no restart."""
    action, restart = decide(alive=True, hb_age=30)
    assert action == "ok" and restart is False


def test_hung_alive_stale_heartbeat():
    """Process alive but heartbeat stale (> SILENCE_S) -> hung, restart."""
    action, restart = decide(alive=True, hb_age=SILENCE_S + 1)
    assert action == "hung" and restart is True


def test_hung_five_minute_stale_heartbeat():
    """PRD §29: the Windows heartbeat is written every 5s, so a 5-minute
    stale heartbeat (60 missed beats) is definitively hung — long before
    the old 12-minute Linux-aligned window."""
    action, restart = decide(alive=True, hb_age=5 * 60)
    assert action == "hung" and restart is True


def test_booting_alive_no_heartbeat_young_process():
    """Process alive, no heartbeat yet, started < BOOT_GRACE_S ago (browser
    is still launching) -> starting, leave alone (no kill loop)."""
    action, restart = decide(alive=True, hb_age=None,
                             proc_age=BOOT_GRACE_S - 10)
    assert action == "starting" and restart is False


def test_hung_alive_no_heartbeat_old_process():
    """Process alive, no heartbeat, past the boot grace -> hung, restart."""
    action, restart = decide(alive=True, hb_age=None,
                             proc_age=BOOT_GRACE_S + 60)
    assert action == "hung" and restart is True


def test_hung_alive_no_heartbeat_unknown_age():
    """Process alive, no heartbeat, age unknown -> conservative: hung."""
    action, restart = decide(alive=True, hb_age=None, proc_age=None)
    assert action == "hung" and restart is True


def test_alive_stale_heartbeat_but_boot_grace():
    """Heartbeat stale but process young -> still hung (a booting collector
    should have written its first heartbeat within the grace)."""
    action, restart = decide(alive=True, hb_age=SILENCE_S + 1,
                             proc_age=30)
    assert action == "hung" and restart is True


def test_decide_never_returns_restart_on_ok_or_starting():
    for args in [dict(alive=True, hb_age=1),
                 dict(alive=True, hb_age=None, proc_age=1)]:
        action, restart = decide(**args)
        assert action in ("ok", "starting")
        assert restart is False


# ---------------------------------------------------------------------------
# PRD §29 "no output" — alive + fresh heartbeat but no spins captured
# ---------------------------------------------------------------------------
def test_no_output_stale_last_spin():
    """Alive + fresh heartbeat + last spin > NO_OUTPUT_S ago -> no_output,
    restart (the silently-dead CDP/WS stream the ladder failed to revive)."""
    action, restart = decide(alive=True, hb_age=30, last_spin_age=NO_OUTPUT_S + 60)
    assert action == "no_output" and restart is True


def test_no_output_abandoned_status():
    """Alive + fresh heartbeat + the collector declared ABANDONED -> the
    ladder gave up; the lingering process is useless, restart."""
    action, restart = decide(alive=True, hb_age=30, hb_status="ABANDONED")
    assert action == "no_output" and restart is True


def test_no_output_recent_spin_is_ok():
    """Alive + fresh heartbeat + last spin recent -> ok, even mid-STALLED
    (the ladder is working, leave it alone)."""
    action, restart = decide(alive=True, hb_age=30, hb_status="STALLED",
                             last_spin_age=60)
    assert action == "ok" and restart is False


def test_no_output_not_when_hung():
    """A stale heartbeat takes precedence: alive + stale hb -> hung, not
    no_output (the process is frozen, not just silent)."""
    action, restart = decide(alive=True, hb_age=SILENCE_S + 1,
                             last_spin_age=NO_OUTPUT_S + 60)
    assert action == "hung" and restart is True


def test_no_output_fresh_spin_ok():
    action, restart = decide(alive=True, hb_age=30, last_spin_age=5)
    assert action == "ok" and restart is False


def test_no_output_unknown_last_spin_ok():
    """No last_spin info yet (fresh boot, no spins) -> ok (not no_output) —
    the no-output verdict requires the SPIN silence to be provable."""
    action, restart = decide(alive=True, hb_age=30, last_spin_age=None)
    assert action == "ok" and restart is False


# ---------------------------------------------------------------------------
# PRD §29 "spin stream incomplete" — data health evaluation
# ---------------------------------------------------------------------------
def test_data_health_healthy(tmp_path):
    """A clean DB (no gaps, reconciliation ok, no open repairs, good score)
    -> healthy with a clear reason."""
    import sqlite3
    from collector import schema
    from collector import observer
    from watchdog_win import data_health
    db = tmp_path / "healthy.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    for i in range(1, 501):
        c.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at, sequence_no, status) VALUES (?,?,?,?,?,?,?,?)",
            (i % 37, f"{i % 37} X", "Black", f"g{i}", f"t{i}", f"t{i}", i, "VALID"),
        )
    from collector import observer
    observer.log_event(c, "RECONCILIATION", severity="INFO",
                       details={"ok": True, "score": 98},
                       root_cause="DATA_INTEGRITY")
    c.commit()
    c.close()
    dh = data_health(str(db))
    assert dh["sequence_health"] is True
    assert dh["reconciliation_health"] is True
    assert dh["repair_queue"] == 0
    assert dh["data_health_score"] == 98
    assert dh["healthy"] is True
    assert dh["reason"] == "ok"


def test_data_health_gap_detected(tmp_path):
    """A sequence gap in the latest window -> data unhealthy (the §29
    "spin stream incomplete" case: process fine, data broken)."""
    import sqlite3
    from collector import schema
    from watchdog_win import data_health
    db = tmp_path / "gap.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    for i in range(1, 502):
        if i == 250:
            continue   # the gap
        c.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at, sequence_no, status) VALUES (?,?,?,?,?,?,?,?)",
            (i % 37, f"{i % 37} X", "Black", f"g{i}", f"t{i}", f"t{i}", i, "VALID"),
        )
    from collector import observer
    observer.log_event(c, "RECONCILIATION", severity="INFO",
                       details={"ok": True, "score": 98},
                       root_cause="DATA_INTEGRITY")
    c.commit()
    c.close()
    dh = data_health(str(db))
    assert dh["sequence_health"] is False
    assert dh["healthy"] is False
    assert "sequence gap" in dh["reason"]


def test_data_health_open_repairs(tmp_path):
    """Open/unresolved repair events -> unhealthy."""
    import sqlite3
    from collector import schema
    from collector import observer
    from watchdog_win import data_health
    db = tmp_path / "open.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    for i in range(1, 501):
        c.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at, sequence_no, status) VALUES (?,?,?,?,?,?,?,?)",
            (i % 37, f"{i % 37} X", "Black", f"g{i}", f"t{i}", f"t{i}", i, "VALID"),
        )
    from collector import observer
    observer.log_event(c, "RECONCILIATION", severity="INFO",
                       details={"ok": True, "score": 98},
                       root_cause="DATA_INTEGRITY")
    from collector.repairer import Repairer
    Repairer(c).record_gap(start_seq=999, end_seq=999, size=1)   # OPEN
    c.commit()
    c.close()
    dh = data_health(str(db))
    assert dh["repair_queue"] >= 1
    assert dh["healthy"] is False
    assert "unresolved repair" in dh["reason"]


def test_data_health_bad_score(tmp_path):
    """A CRITICAL data health score (< 75) -> unhealthy even with no gaps."""
    import sqlite3
    from collector import schema
    from watchdog_win import data_health
    db = tmp_path / "score.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    for i in range(1, 501):
        c.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at, sequence_no, status) VALUES (?,?,?,?,?,?,?,?)",
            (i % 37, f"{i % 37} X", "Black", f"g{i}", f"t{i}", f"t{i}", i, "VALID"),
        )
    from collector import observer
    observer.log_event(c, "RECONCILIATION", severity="CRITICAL",
                       details={"ok": False, "score": 40},
                       root_cause="DATA_INTEGRITY")
    c.commit()
    c.close()
    dh = data_health(str(db))
    assert dh["reconciliation_health"] is False
    assert dh["data_health_score"] == 40
    assert dh["healthy"] is False
    assert "score 40" in dh["reason"]


def test_data_health_missing_db(tmp_path):
    """A missing/unreadable DB -> unhealthy (the data cannot be trusted)."""
    from watchdog_win import data_health
    dh = data_health(str(tmp_path / "does_not_exist.db"))
    assert dh["healthy"] is False
    assert "db read failed" in dh["reason"]


# ---------------------------------------------------------------------------
# PRD §30 — escalation ladder order (reconcile BEFORE destructive recovery)
# ---------------------------------------------------------------------------
def test_ladder_reconcile_before_destructive():
    """The recovery ladder's rung order: L2 reconcile must precede L3
    (re-arm WS) and all destructive rungs — data first, transport second."""
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "collector", "roulette2_collector.py"),
        encoding="utf-8").read()
    # the recover() ladder prints each rung — assert L2 precedes L3+
    idx_reconcile = src.find("[RECOVERY] L2")
    idx_ream = src.find("[RECOVERY] L3")
    idx_refresh = src.find("[RECOVERY] L4")
    idx_reload = src.find("[RECOVERY] L5")
    idx_browser = src.find("[RECOVERY] L6")
    assert idx_reconcile != -1
    assert 0 < idx_reconcile < idx_ream < idx_refresh < idx_reload < idx_browser
    # the full 9-rung ladder (L0..L7 in the collector, L8 flagged by caller)
    for rung in ("L0", "L1", "L2", "L3", "L4", "L5", "L6", "L7"):
        assert src.find(f"[RECOVERY] {rung}") != -1
