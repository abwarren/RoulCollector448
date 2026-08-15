#!/usr/bin/env python3
"""Windows watchdog for the RoulCollector448 collector — the equivalent of
the Linux systemd timer (collector/roulette2-watchdog.timer, 5 min).

Reads the collector's heartbeat file (~/.roulette2/roulette2_heartbeat.json,
written every ~5s). If it's missing or stale (> 12 minutes — matches the
Linux SILENCE_WINDOW) the collector is presumed dead/hung and gets
restarted via start_collector.bat (same mechanism the user would use).

Install: register_tasks.ps1 registers RoulCollector448-Watchdog to run this
every 5 minutes. Never raises; exit 0 = healthy, 1 = action taken.
"""

import json
import os
import subprocess
import sys
from datetime import datetime

SILENCE_S = 12 * 60  # matches the Linux watchdog (12 minutes)

DATA_DIR = os.environ.get("RC_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".roulette2")
HEARTBEAT = os.environ.get("RC_HEARTBEAT_FILE") or os.path.join(
    DATA_DIR, "roulette2_heartbeat.json")
START_BAT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "start_collector.bat")


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


def _collector_pids():
    """PIDs of any python process whose command line mentions the collector."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
          "Where-Object { $_.CommandLine -like '*roulette2_collector*' } | "
          "Select-Object -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


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
    age = _hb_age()
    if age is None:
        print(f"WATCHDOG: no heartbeat ({HEARTBEAT})")
    elif age < SILENCE_S:
        print(f"WATCHDOG: ok (heartbeat {int(age)}s old)")
        return 0
    else:
        print(f"WATCHDOG: heartbeat stale ({int(age)}s > {SILENCE_S}s)")

    pids = _collector_pids()
    if not pids:
        print("WATCHDOG: collector not running — starting")
        _start_collector()
        return 1
    print(f"WATCHDOG: collector running but silent (pids {pids}) — restarting")
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
    _start_collector()
    return 1


if __name__ == "__main__":
    sys.exit(main())
