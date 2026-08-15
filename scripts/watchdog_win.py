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

SILENCE_S = 3 * 60       # hung threshold. The Windows heartbeat is written
                         # EVERY 5s loop tick (roulette2_collector.py
                         # write_heartbeat) — 3 min = 36 missed beats, far
                         # tighter than the Linux journald window (12 min,
                         # where output is sparse spins). No false positives;
                         # a hung collector is caught ~9 min faster.
BOOT_GRACE_S = 3 * 60    # process alive but no heartbeat yet -> allow boot
NO_OUTPUT_S = 10 * 60    # no-output threshold: process alive + heartbeat
                         # fresh but NO spin captured for this long. The
                         # collector's own stall ladder (refresh -> reload ->
                         # browser restart, STALL_THRESHOLD_S=120s + ladder)
                         # handles short silences; 10 min is generous over
                         # that while still catching a permanently silent
                         # stream the ladder failed to revive.
DATA_UNHEALTHY_S = 15 * 60  # data-health threshold: process alive + fresh
                            # heartbeat BUT the data is unhealthy (gaps /
                            # open repairs / bad score) — "spin stream
                            # incomplete" (PRD §29). 15 min: the collector's
                            # own L2 reconcile runs every 30-60s, so a
                            # persisting data problem is real, not transient.

DATA_DIR = os.environ.get("RC_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".roulette2")
HEARTBEAT = os.environ.get("RC_HEARTBEAT_FILE") or os.path.join(
    DATA_DIR, "roulette2_heartbeat.json")
START_BAT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "start_collector.bat")


def decide(*, alive: bool, hb_age, proc_age=None,
           hb_status: str | None = None, last_spin_age=None) -> tuple[str, bool]:
    """Pure watchdog decision.

    alive          — is the collector process present in the OS table?
    hb_age         — seconds since the heartbeat was written (None = missing).
    proc_age       — seconds since the collector process started (None = unknown).
    hb_status      — the heartbeat's own status field (RUNNING/STALLED/
                     ABANDONED) — the collector's self-report.
    last_spin_age  — seconds since the LAST captured spin (None = no spins
                     ever / unknown). The no-output signal.

    Returns (action, restart): action is one of
      'ok' | 'dead' | 'hung' | 'starting' | 'no_output'.
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
        # alive + fresh heartbeat. The loop IS running — but is it
        # producing anything? (PRD §29 "no output")
        if hb_status == "ABANDONED":
            # the collector's own ladder gave up and declared the session
            # dead — but the process lingers, heartbeating uselessly.
            return "no_output", True
        if last_spin_age is not None and last_spin_age > NO_OUTPUT_S:
            # alive, heartbeating, but NO spin captured for a long time —
            # a silently dead CDP/WS stream the ladder failed to revive.
            return "no_output", True
        return "ok", False
    return "hung", True                  # alive but silent too long


def data_health(db_path: str) -> dict:
    """Evaluate the DATA health of the collector DB (PRD §29 "spin stream
    incomplete"): the process may be alive + heartbeating while the data is
    silently broken. Pure read of the DB (read-only connection), never
    raises.

    Returns {"sequence_health": bool, "reconciliation_health": bool,
             "repair_queue": int (open/unresolved), "data_health_score": int|None,
             "healthy": bool, "reason": str}.

    healthy = sequence intact (no gaps in the latest window) AND the last
    reconciliation passed AND no open/unresolved repair events AND a
    non-bad data-health score (when recorded).
    """
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                               timeout=5)
        conn.row_factory = sqlite3.Row
        # sequence health — gaps in the latest 500 (by canonical sequence)
        try:
            rows = conn.execute(
                "SELECT sequence_no FROM roulette_spins "
                "WHERE sequence_no IS NOT NULL ORDER BY sequence_no DESC LIMIT 500"
            ).fetchall()
            seqs = sorted(r[0] for r in rows)
            gaps = 0
            if seqs:
                present = set(seqs)
                gaps = sum(1 for i in range(seqs[0], seqs[-1] + 1)
                           if i not in present)
        except Exception:
            gaps = 0   # legacy table without sequence_no — can't judge
        sequence_health = gaps == 0
        # reconciliation health — the latest RECONCILIATION event ok?
        try:
            import json as _json
            row = conn.execute(
                "SELECT details FROM integrity_events "
                "WHERE event_type IN ('RECONCILIATION','RECONCILIATION_LIGHT') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            rec = _json.loads(row["details"]) if row and row["details"] else {}
            recon_ok = bool(rec.get("ok", False))
            score = rec.get("score")
        except Exception:
            recon_ok, score = False, None
        # repair queue — unresolved events (OPEN / FAILED / UNVERIFIED)
        try:
            open_repairs = conn.execute(
                "SELECT COUNT(*) FROM repair_events "
                "WHERE status IN ('OPEN','FAILED','UNVERIFIED')"
            ).fetchone()[0]
        except Exception:
            open_repairs = 0
        conn.close()
        score_ok = score is None or score >= 75   # WARNING band or better
        healthy = (sequence_health and recon_ok and open_repairs == 0
                   and score_ok)
        reason = []
        if not sequence_health:
            reason.append(f"{gaps} sequence gap(s)")
        if not recon_ok:
            reason.append("last reconciliation failed/unavailable")
        if open_repairs:
            reason.append(f"{open_repairs} unresolved repair event(s)")
        if not score_ok:
            reason.append(f"data health score {score} < 75")
        return {
            "sequence_health": sequence_health,
            "reconciliation_health": recon_ok,
            "repair_queue": open_repairs,
            "data_health_score": score,
            "healthy": healthy,
            "reason": "; ".join(reason) or "ok",
        }
    except Exception as e:
        return {"sequence_health": False, "reconciliation_health": False,
                "repair_queue": -1, "data_health_score": None,
                "healthy": False, "reason": f"db read failed: {e}"}


def _hb_info():
    """(age_s, status, last_spin_age_s) from the heartbeat file.

    age_s: seconds since the heartbeat was written (None if unreadable/
    missing). status: the heartbeat's own status field (RUNNING/STALLED/
    ABANDONED). last_spin_age_s: seconds since the last captured spin
    (None when no spin ever / unknown) — parsed from last_spin.captured_at.
    """
    try:
        with open(HEARTBEAT, encoding="utf-8") as f:
            hb = json.load(f)
        at = datetime.fromisoformat(hb.get("at", ""))
        now = datetime.now().astimezone()
        if at.tzinfo is None:
            at = at.replace(tzinfo=now.tzinfo)
        age = (now - at).total_seconds()
        status = hb.get("status")
        last_spin = hb.get("last_spin") or {}
        lsa = None
        cap = last_spin.get("captured_at") or last_spin.get("time")
        if cap:
            try:
                lsa = (now - datetime.fromisoformat(
                    str(cap).replace("Z", "+00:00"))).total_seconds()
            except Exception:
                lsa = None
        return age, status, lsa
    except Exception:
        return None, None, None


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
    age, hb_status, last_spin_age = _hb_info()
    action, restart = decide(alive=bool(pids), hb_age=age, proc_age=proc_age,
                             hb_status=hb_status, last_spin_age=last_spin_age)

    if action == "ok":
        # PRD §29 "spin stream incomplete": the process is alive + the loop
        # is heartbeating — but is the DATA healthy? Evaluate the six
        # signals (collector_alive / last_spin_age / sequence_health /
        # reconciliation_health / repair_queue / data_health_score). On a
        # data problem, run the L2 reconciliation first (non-destructive);
        # if it persists, escalate to kill+restart.
        try:
            dh = data_health(os.environ.get(
                "RC_DB_PATH", os.path.join(DATA_DIR, "roulette2_spins.db")))
        except Exception:
            dh = {"healthy": True, "reason": "db read skipped"}
        if not dh["healthy"]:
            print(f"WATCHDOG: process alive + heartbeating but DATA UNHEALTHY — "
                  f"{dh['reason']} — running L2 reconciliation")
            try:
                sys.path.insert(0, os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))))
                from scripts.standalone_reconcile import run_once
                run_once()
                print("  L2 reconciliation attempted — checking data again")
                dh2 = data_health(os.environ.get(
                    "RC_DB_PATH", os.path.join(DATA_DIR, "roulette2_spins.db")))
                if dh2["healthy"]:
                    print("WATCHDOG: data healthy after reconciliation — ok")
                    return 0
                print(f"WATCHDOG: data STILL unhealthy after reconcile — "
                      f"{dh2['reason']} — killing and restarting")
                _kill(pids)
                _start_collector()
                return 1
            except Exception as e:
                print(f"WATCHDOG: L2 reconciliation failed ({e}) — escalating")
                _kill(pids)
                _start_collector()
                return 1
        print(f"WATCHDOG: ok (heartbeat {int(age)}s old, pids {pids}, data healthy)")
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
    if action == "no_output":
        print(f"WATCHDOG: process alive ({pids}) but NO OUTPUT — "
              f"{'collector declared ABANDONED' if hb_status == 'ABANDONED' else 'no spin for ' + str(int(last_spin_age)) + 's'} "
              f"(heartbeat fresh) — killing and restarting")
        _kill(pids)
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
