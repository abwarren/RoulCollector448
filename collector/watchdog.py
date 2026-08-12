#!/usr/bin/env python3
"""
Roulette 2 collector watchdog — restart the systemd unit if it stops
producing output (hung loop, frozen CDP, dead process).

Checks:
1. Unit active (systemctl --user is-active)
2. journald produced output in the last 12 minutes — the collector prints a
   line per spin (~82s cadence) plus a status line every 60s, so silence
   means trouble. (DB freshness can't be used: writes batch every 25 spins,
   ~34 min.)

Install (systemd user units ship in this repo under collector/):
    cp collector/roulette2-watchdog.service collector/roulette2-watchdog.timer \
       ~/.config/systemd/user/
    cp collector/watchdog.py ~/.hermes/scripts/roulette2_watchdog.py
    systemctl --user daemon-reload
    systemctl --user enable --now roulette2-watchdog.timer
"""
import subprocess
import sys

UNIT = "roulette-collector2.service"
SILENCE_WINDOW = "12 minutes ago"


def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def main():
    active = run(f"systemctl --user is-active {UNIT}").stdout.strip()
    if active != "active":
        print(f"WATCHDOG: {UNIT} not active ('{active}') — restarting")
        run(f"systemctl --user restart {UNIT}")
        return 1

    out = run(f"journalctl --user -u {UNIT} --since '{SILENCE_WINDOW}' --no-pager").stdout.strip()
    if not out:
        print(f"WATCHDOG: {UNIT} active but silent for {SILENCE_WINDOW} — restarting")
        run(f"systemctl --user restart {UNIT}")
        return 1

    print("WATCHDOG: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
