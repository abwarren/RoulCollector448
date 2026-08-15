#!/usr/bin/env python3
"""Windows watchdog for the RoulCollector448 collector — the equivalent of
the Linux systemd timer (collector/roulette2-watchdog.timer, 5 min).

Failure modes it catches (PRD §29):
  * PROCESS DEAD   — no collector process in the OS table. Detected FIRST,
                     immediately (a dead process never produces output again
                     — no reason to wait for the heartbeat to go stale).
                     Previously the heartbeat was checked first, so a process
                     that died seconds ago (heartbeat still fresh) was
                     reported "ok" until the 12-min silence window elapsed.
  * HUNG           — process alive but heartbeat missing/stale (> 12 min,
                     matches Linux SILENCE_WINDOW): kill + restart.
  * NEVER STARTED  — no process AND no heartbeat (e.g. after boot): start.
  * BOOTING        — process alive, no heartbeat yet, started < BOOT_GRACE_S
                     ago (browser launch takes time): healthy, leave alone.

The decision is a pure function (`decide`) so it is unit-testable without
invoking PowerShell. Exit 0 = healthy, 1 = action taken.

Install: register_tasks.ps1 registers RoulCollector448-Watchdog to run this
every 5 minutes. Never raises.
"""

import json
import os
import subprocess
import sys
from datetime import datetime

SILENCE_S = 12 * 60      # matches the Linux watchdog (12 minutes)
BOOT_GRACE_S = 3 * 60    # process alive but no heartbeat yet -> allow boot

DATA_DIR = os.environ.get("RC_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".roulette2")
HEARTBEAT = os.environ.get("RC_HEARTBEAT_FILE") or os.path.join(
    DATA_DIR, "roulette2_heartbeat.json")
START_BAT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "start_collector.bat")


def decide(*, alive: bool, hb_age, proc_age=None) -> tuple[str, bool]:
    """Pure watchdog decision.

    alive      — is the collector process present in the OS table?
    hb_age     — seconds since the heartbeat was written (None = missing).
    proc_age   — seconds since the collector process started (None = unknown).

    Returns (action, restart): action is one of
      'ok' | 'dead' | 'hung' | 'starting'.
    """
    if not alive:
        # PROCESS DEAD (or never started) — restart now. The heartbeat
        # freshness is IRRELEVANT: a dead process never writes again.
        return "dead", True
    if hb_age is None:
        if proc_age is not None and proc_age < BOOT_GRACE_S:
            return "starting", False     # still booting — leave it alone
        return "hung", True              # alive but never/silent since boot
    if hb_age < SILENCE_S:
        return "ok", False
    return "hung", True                  # alive but silent too long


def _hb_age():
    """Seconds since the heartbeat was written; None if unreadable/missing."""
    try:
        with open(HEARTBEAT, encoding="utf-8") as f:
            hb = json.load(f)
        at = datetime.fromisoformat(hb.get("at", ""))
        now = datetime.now().astimezone()
        if at.tzinfo is None:
            at = at.replace(tzinfo=now.tzinfo)
        return (now - at).total_seconds()
    except Exception:
        return None


def _collector_info():
    """(pids, oldest_proc_age_s) for any python process whose command line
    mentions the collector. proc_age None when no pids/unknown."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Where-Object { $_.CommandLine -like '*roulette2_collector*' } | "
          "Select-Object ProcessId, CreationDate")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20)
        pids, ages = [], []
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].strip().isdigit():
                pids.append(int(parts[0].strip()))
                try:
                    created = datetime.fromisoformat(
                        parts[1].strip().replace("Z", "+00:00"))
                    ages.append(
                        (datetime.now().astimezone() - created).total_seconds())
                except Exception:
                    pass
        oldest = min(ages) if ages else None
        return pids, oldest
    except Exception:
        return [], None


def _kill(pids):
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)
        except Exception:
            pass


def _start_collector():
    """Launch start_collector.bat detached, no console window."""
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", START_BAT],
            cwd=os.path.dirname(START_BAT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
        )
        return True
    except Exception as e:
        print(f"WATCHDOG: failed to start collector: {e}")
        return False


def main():
    pids, proc_age = _collector_info()
    age = _hb_age()
    action, restart = decide(alive=bool(pids), hb_age=age, proc_age=proc_age)

    if action == "ok":
        print(f"WATCHDOG: ok (heartbeat {int(age)}s old, pids {pids})")
        return 0
    if action == "starting":
        print(f"WATCHDOG: collector booting (pid {pids}, heartbeat not yet "
              f"written, {int(proc_age)}s old) — ok")
        return 0
    if action == "dead":
        print(f"WATCHDOG: process dead (no collector process, heartbeat "
              f"{'missing' if age is None else str(int(age)) + 's old'}) — "
              f"starting now")
        _start_collector()
        return 1
    # hung
    print(f"WATCHDOG: process alive ({pids}) but "
          f"{'no heartbeat' if age is None else 'heartbeat stale ' + str(int(age)) + 's'} "
          f"— killing and restarting")
    _kill(pids)
    _start_collector()
    return 1


if __name__ == "__main__":
    sys.exit(main())
