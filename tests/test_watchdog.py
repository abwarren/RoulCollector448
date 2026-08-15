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

from watchdog_win import BOOT_GRACE_S, SILENCE_S, decide  # noqa: E402


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
