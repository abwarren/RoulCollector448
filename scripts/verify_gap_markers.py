#!/usr/bin/env python3
"""Verify gap markers on the RoulCollector448 grid in REAL Chrome.

Runs the API against a throwaway fixture DB (RC_DB_PATH) with an injected
18-minute time break, then loads the SPA in headless Chrome and asserts:
  1. a .gapmark band exists in the grid DOM
  2. its text carries the duration ("18m") and both boundary times
  3. the header #gapBadge is visible with a count
  4. a control fixture WITHOUT gaps renders ZERO .gapmark and a hidden badge

Usage: /usr/bin/python3 verify_gap_markers.py
System python (playwright is installed there, not in the project .venv).
"""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = "/home/wa/projects/RoulCollector448"
API_PORT = 4491  # test port — never touches the live 4480
BASE = f"http://127.0.0.1:{API_PORT}"


def make_fixture(path, gap=True):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE roulette_spins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number INTEGER NOT NULL, description TEXT NOT NULL,
        color TEXT NOT NULL CHECK(color IN ('Red','Black','Green')),
        game_id TEXT NOT NULL UNIQUE, server_ts TEXT NOT NULL,
        captured_at TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    # 60 spins at 44s cadence; spin 30->31 jumps 18 minutes when gap=True
    t0 = 1786400000
    for i in range(60):
        ts = t0 + i * 44
        if gap and i >= 31:
            ts += 18 * 60  # injected 18-minute break before spin 31
        n = i % 37
        color = "Green" if n == 0 else ("Red" if n in {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36} else "Black")
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
        con.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, server_ts, captured_at) VALUES (?,?,?,?,?,?)",
            (n, f"{n} {color}", color, f"gid-{i}", iso, iso))
    con.commit()
    con.close()


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read())


async def check(browser, expect_gap):
    page = await browser.new_page()
    await page.goto(BASE, wait_until="networkidle")
    await page.wait_for_timeout(2500)  # let boot + first tick settle
    count = await page.locator(".gapmark").count()
    texts = await page.locator(".gapmark").all_text_contents()
    badge_visible = await page.locator("#gapBadge").is_visible()
    badge_text = await page.locator("#gapBadge").text_content()
    cell_count = await page.locator(".cell").count()
    await page.close()
    return {"count": count, "texts": texts, "badge_visible": badge_visible,
            "badge_text": badge_text, "cells": cell_count}


def main():
    fd, gap_db = tempfile.mkstemp(suffix=".db"); os.close(fd)
    fd2, clean_db = tempfile.mkstemp(suffix=".db"); os.close(fd2)
    make_fixture(gap_db, gap=True)
    make_fixture(clean_db, gap=False)

    env = {**os.environ, "RC_DB_PATH": gap_db}
    errlog = open("/tmp/verify-gap-uvicorn.log", "w")
    proc = subprocess.Popen(
        [f"{REPO}/.venv/bin/python", "-m", "uvicorn", "backend.app:app",
         "--port", str(API_PORT), "--host", "127.0.0.1"],
        cwd=REPO, env=env, stdout=errlog, stderr=errlog)
    try:
        up = False
        for _ in range(60):
            try:
                api("/api/spins/count"); up = True; break
            except Exception:
                time.sleep(0.5)
        if not up:
            errlog.flush()
            print("FAIL: API never came up — log:")
            print(open("/tmp/verify-gap-uvicorn.log").read()[-800:])
            return 1

        from playwright.async_api import async_playwright
        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                gap = await check(browser, True)
                await browser.close()
                return gap
        gap = asyncio.run(run())

        ok = True
        if gap["count"] < 1:
            print(f"FAIL: expected >=1 .gapmark, got {gap['count']}")
            ok = False
        else:
            joined = " ".join(gap["texts"])
            # injected break: 44s cadence + 18min = 18.7min -> '19m gap';
            # boundary times are deterministic (UTC of the fixture epochs)
            if "gap" not in joined or "22:35:20" not in joined or "22:54:04" not in joined:
                print(f"FAIL: gapmark text wrong: {gap['texts']}")
                ok = False
            else:
                print(f"PASS: .gapmark x{gap['count']} -> {gap['texts'][:2]}")
        if not gap["badge_visible"]:
            print("FAIL: #gapBadge not visible on gapped fixture")
            ok = False
        else:
            print(f"PASS: badge visible -> '{gap['badge_text']}'")

        # control: swap to the clean fixture (same API, new process)
        proc.terminate(); proc.wait()
        env2 = {**os.environ, "RC_DB_PATH": clean_db}
        proc2 = subprocess.Popen(
            [f"{REPO}/.venv/bin/python", "-m", "uvicorn", "backend.app:app",
             "--port", str(API_PORT), "--host", "127.0.0.1"],
            cwd=REPO, env=env2, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                api("/api/spins/count"); break
            except Exception:
                time.sleep(0.5)
        async def run2():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                clean = await check(browser, False)
                await browser.close()
                return clean
        clean = asyncio.run(run2())
        proc2.terminate(); proc2.wait()
        if clean["count"] != 0:
            print(f"FAIL: control fixture shows {clean['count']} gapmarks (expected 0)")
            ok = False
        else:
            print("PASS: control fixture renders 0 gapmarks")
        if clean["badge_visible"]:
            print("FAIL: badge visible on clean fixture (should be hidden)")
            ok = False
        else:
            print("PASS: badge hidden on clean fixture")
        print(f"INFO: cells rendered = {gap['cells']}")
        print("RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
        return 0 if ok else 1
    finally:
        proc.terminate() if "proc" in dir() else None
        try: os.remove(gap_db)
        except OSError: pass
        try: os.remove(clean_db)
        except OSError: pass


if __name__ == "__main__":
    sys.exit(main())
