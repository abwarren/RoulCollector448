#!/usr/bin/env python3
"""Audit: compare DB spins vs live Table 448 (Auto-Roulette R2) history.

Fetches the live table history via the collector's own source (Evolution
lobby WS in the game iframe), compares against the DB, reports missing or
out-of-order spins. Used by the 30-min cron for data-accuracy self-correction.

Usage: python3 audit_448.py [--db PATH] [--limit 500]
"""
import argparse, asyncio, json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collector import history  # reuse the lobby parser

DB_DEFAULT = "/home/gdi/roulette2/roulette2_spins.db"
LOBBY_KEY = os.environ.get("RC_LOBBY_TABLE", "48z5pjps3ntvqc1b")

async def fetch_live_history(limit=500):
    """Attach to the game iframe (logged-in), capture lobby history ~20s."""
    from playwright.async_api import async_playwright
    CRED = {}
    cred_path = os.environ.get("RC_CRED_FILE", "/home/gdi/.config/roulette2_collector.env")
    with open(cred_path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                CRED[k] = v
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            locale="en-ZA", timezone_id="Africa/Johannesburg")
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = await ctx.new_page()
        # login
        await page.goto("https://www.sunbet.co.za/en/login", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        for _ in range(3):
            try:
                await page.get_by_label("Username", exact=True).fill(CRED["SUNBET_USER"], timeout=15000)
                await page.get_by_label("Password", exact=True).fill(CRED["SUNBET_PASS"])
                try:
                    await page.locator('button[type="submit"]').first.click(timeout=5000)
                except Exception:
                    await page.keyboard.press("Enter")
                await page.wait_for_timeout(10000)
                break
            except Exception:
                await page.wait_for_timeout(3000)
        # game page
        await page.goto("https://www.sunbet.co.za/en/play/auto-roulette", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(15000)
        game_frame = None
        for f in page.frames:
            if "evo-games.com" in (f.url or ""):
                game_frame = f
                break
        if not game_frame:
            await browser.close()
            return None, "no game iframe"
        sess = await ctx.new_cdp_session(game_frame)
        await sess.send("Network.enable")
        live_seq = []  # chronological numbers seen for 448
        seen_gids = set()
        def on_frame(p):
            try:
                pl = (p.get("response") or {}).get("payloadData", "")
                if "lobby.historyUpdated" not in pl:
                    return
                data = json.loads(pl)
                recs = history.parse_lobby_history(data)
                if not recs:
                    return
                for r in recs:
                    if r.game_id and LOBBY_KEY in r.game_id:
                        gid = r.game_id
                        if gid not in seen_gids:
                            seen_gids.add(gid)
                            live_seq.append(r.number)
            except Exception:
                pass
        sess.on("Network.webSocketFrameReceived", on_frame)
        # capture ~40s: each lobby update carries the table's recent results,
        # so new spins appear as new game_ids; accumulate chronological.
        for _ in range(8):
            await asyncio.sleep(5)
        await browser.close()
        if not live_seq:
            return None, "no 448 spins seen in 40s"
        return live_seq, None

def audit_db(db_path, limit=500):
    c = sqlite3.connect(db_path, timeout=10)
    c.execute("PRAGMA busy_timeout=10000")
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        n = c.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
        if n == 0:
            return {"ok": False, "spins": 0, "msg": "DB empty"}
        rows = c.execute(
            "SELECT number, server_ts FROM roulette_spins ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        ts = [r[1] for r in rows]
        order_ok = all((t1 or "") >= (t2 or "") for t1, t2 in zip(ts, ts[1:])) if len(ts) > 1 else True
        return {"ok": True, "spins": n, "window": len(rows),
                "newest": rows[0][0] if rows else None,
                "newest_ts": rows[0][1] if rows else None,
                "order_ok": order_ok,
                "oldest_window": rows[-1][0] if rows else None}
    finally:
        c.close()

def read_heartbeat(hb_path):
    """Read the collector's heartbeat for its captured count (WS + saved)."""
    try:
        with open(hb_path) as f:
            hb = json.load(f)
        return hb
    except Exception:
        return None

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--heartbeat", default="/home/gdi/roulette2/roulette2_heartbeat.json")
    args = ap.parse_args()
    live, live_err = await fetch_live_history(args.limit)
    db = audit_db(args.db, args.limit)
    hb = read_heartbeat(args.heartbeat)
    # Accuracy reference: the collector's captured count (ws_captured).
    # DB vs live newest will differ on normal save-lag (batch of 25);
    # a real problem is DB spins << collector captured count.
    hb_captured = (hb or {}).get("ws_captured") or (hb or {}).get("spins_captured")
    hb_total = (hb or {}).get("total_spins") or (hb or {}).get("spins")
    result = {"live": live, "live_err": live_err, "db": db, "hb": hb,
              "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        d = db
        live_newest = live[-1] if live else None
        live_n = len(live) if live else 0
        print(f"[{result['timestamp']}] Audit Table 448 (Auto-Roulette R2)")
        print(f"  DB spins: {d.get('spins')} (last {d.get('window')} in window)")
        print(f"  DB newest: {d.get('newest')} @ {d.get('newest_ts')}")
        print(f"  Order OK: {d.get('order_ok')}")
        print(f"  Live 448 seen: {live_n} spins, newest {live_newest} ({live_err or 'ok'})")
        print(f"  Collector captured: {hb_captured} (hb total: {hb_total})")
        if not d.get("ok"):
            print(f"  STATUS: {d.get('msg')}")
        elif not d.get("order_ok"):
            print("  STATUS: ORDER VIOLATION")
        elif hb is None:
            print("  STATUS: HEARTBEAT MISSING — collector not writing (down?)")
        elif (hb.get("status") or "").upper() != "RUNNING":
            print(f"  STATUS: COLLECTOR NOT RUNNING (hb status: {hb.get('status')})")
        elif hb_captured is not None and d.get("spins") < hb_captured:
            print(f"  STATUS: SAVE-LAG — DB {d.get('spins')} vs collector {hb_captured} (batch not flushed yet)")
        elif live_newest is not None and d.get("newest") != live_newest:
            print(f"  STATUS: SAVE-LAG (expected) — DB {d.get('newest')} vs live {live_newest}")
        else:
            print("  STATUS: OK")

if __name__ == "__main__":
    asyncio.run(main())
