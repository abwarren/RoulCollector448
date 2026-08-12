#!/usr/bin/env python3
"""
Auto Roulette spin collector — 24/7 operation.
Table 448 — Auto Roulette (Auto-Roulette 2 - 400K) via Sunbet / Evolution Gaming.

Capture: CDP WebSocket interception (catches ALL frames) + DOM polling fallback.
Persistence: JSON state + CSV export + SQLite (INSERT OR IGNORE, dedup by gameId).

v2 — 2026-08-12 reliability ladder (fixes recurring time gaps in the dataset):
  * Stall threshold raised 44s -> 120s. Table 448 legit cadence is 40-57s
    (median 44s), so the old 44s detector false-fired ~once a minute on a
    healthy table. Each false-fire re-armed CDP; that listener churn is what
    destabilised the WS stream and CAUSED the recurring ~35-min stream deaths.
    120s matches the dashboard GAP_S and never false-fires on a healthy table.
  * Every CDP call wrapped in asyncio.wait_for (15s). On 2026-08-11 a hung
    CDP session send froze the whole asyncio loop for ~26 min -> the 32.5-min
    gap. That can no longer happen.
  * Recovery ladder instead of a 4x-reload spiral:
      rung 0: passive wait 30s (stream often self-heals)
      rung 1: re-arm CDP only, wait 30s
      rung 2: click game refresh button (best effort), wait 30s
      rung 3: page reload, verify frames resume within 90s
      rung 4: full browser restart (fresh login), verify within 90s
      fail   : abandon session -> main loop starts a fresh session
    After every rung the loop VERIFIES a new spin arrived before escalating.
    The old path did 4 reloads x ~60s + abandon + re-login = ~5.8 min dead
    time per incident; the ladder resolves most stalls in under a minute.
  * DOM candidate dump when the refresh selector misses, so the next stall
    logs the real Evolution refresh control instead of guessing blindly.

Credentials are read from env vars SUNBET_USER/SUNBET_PASS, falling back to
~/.config/roulette2_collector.env (KEY=VALUE lines). NEVER committed — this
repo is public.
"""
import json, time, os, sys, asyncio, sqlite3
from playwright.async_api import async_playwright
from datetime import datetime

# ---- config ----
GAME_URL = "https://www.sunbet.co.za/slots-games/launch-game/?gameId=997043039547559967&openTable=448"
STATE_FILE = "/home/wa/roulette2_spins.json"
CSV_FILE = "/home/wa/roulette2_spins.csv"
DB_FILE = "/home/wa/roulette2_spins.db"
CRED_FILE = os.path.expanduser("~/.config/roulette2_collector.env")

STALL_THRESHOLD_S = 120   # matches dashboard GAP_S; max legit cadence ~57s
RUNG_WAIT_S = 30          # wait for a new spin after rungs 0-2
RELOAD_VERIFY_S = 90      # frames must resume within this after a reload
RESTART_VERIFY_S = 90     # frames must resume within this after browser restart
CDP_TIMEOUT_S = 15        # hard timeout for every CDP call
SESSION_DURATION = 6 * 3600


def load_credentials():
    creds = {
        "SUNBET_USER": os.environ.get("SUNBET_USER"),
        "SUNBET_PASS": os.environ.get("SUNBET_PASS"),
    }
    if all(creds.values()):
        return creds
    try:
        with open(CRED_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    if not all(creds.values()):
        sys.exit(f"FATAL: Sunbet credentials missing. Set SUNBET_USER/SUNBET_PASS "
                 f"env vars or create {CRED_FILE}")
    return creds


CREDS = load_credentials()
SUNBET_USER = CREDS["SUNBET_USER"]
SUNBET_PASS = CREDS["SUNBET_PASS"]

# European roulette number -> color
REDS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}


def num_to_color(n):
    if n == 0:
        return "Green"
    return "Red" if n in REDS else "Black"


def save_spins(spins):
    with open(STATE_FILE, "w") as f:
        json.dump(spins, f, indent=1)
    with open(CSV_FILE, "w") as f:
        f.write("number,description,gameId,timestamp,captured_at\n")
        for s in spins:
            desc = s.get("description", "").replace(",", "")
            f.write(f"{s.get('number','')},{desc},{s.get('gameId','')},{s.get('timestamp','')},{s.get('captured_at','')}\n")
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS roulette_spins (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            number      INTEGER NOT NULL,
            description TEXT NOT NULL,
            color       TEXT NOT NULL CHECK(color IN ('Red', 'Black', 'Green')),
            game_id     TEXT NOT NULL UNIQUE,
            server_ts   TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_spin_number ON roulette_spins(number);
        CREATE INDEX IF NOT EXISTS idx_spin_color  ON roulette_spins(color);
        CREATE INDEX IF NOT EXISTS idx_spin_ts     ON roulette_spins(server_ts);
    ''')
    inserted = 0
    for s in spins:
        desc = s.get("description", "")
        color = num_to_color(s['number']) if isinstance(s['number'], int) else "Green"
        try:
            c.execute('''
                INSERT OR IGNORE INTO roulette_spins
                    (number, description, color, game_id, server_ts, captured_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (int(s['number']), desc, color, s['gameId'], s.get('timestamp',''), s.get('captured_at','')))
            if c.rowcount:
                inserted += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    print(f"  Saved {len(spins)} spins to disk (+{inserted} new to DB)")


# Refresh button selectors in the Evolution game UI (checked across all frames).
# The old list (data-role/aria-label/title only) never matched the real DOM —
# widened, plus a candidate dump on miss so the next stall teaches us the real
# control instead of guessing again.
REFRESH_SELECTORS = [
    '[data-role*="refresh" i]', '[data-role*="reload" i]', '[data-role*="restart" i]',
    'button[aria-label*="refresh" i]', 'button[aria-label*="reload" i]',
    '[title*="refresh" i]', '[title*="reload" i]',
    '[class*="refresh" i]', '[class*="reload" i]',
    'svg[title*="refresh" i]', 'svg[title*="reload" i]',
    'img[alt*="refresh" i]', 'img[alt*="reload" i]',
]


async def click_refresh_button(page):
    """Find the game's refresh/reload button in any frame and real-click it.
    Returns True if clicked, False if none found."""
    frames = [page] + list(page.frames)
    for f in frames:
        for sel in REFRESH_SELECTORS:
            try:
                loc = f.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1000):
                    await loc.click(timeout=3000)
                    print(f"  [REFRESH] Clicked {sel}")
                    return True
            except Exception:
                pass
    return False


async def dump_refresh_candidates(page):
    """Log classes/aria/title of likely refresh controls for the next stall —
    self-learning diagnostics instead of blind guessing."""
    frames = [page] + list(page.frames)
    seen = set()
    for f in frames:
        for sel in ('[class*="refresh" i]', '[class*="reload" i]',
                    '[aria-label*="refresh" i]', '[aria-label*="reload" i]'):
            try:
                els = await f.query_selector_all(sel)
                for el in els[:4]:
                    info = await el.evaluate(
                        "e => ({tag: e.tagName, cls: (e.className||'').toString().slice(0,60), "
                        "aria: e.getAttribute('aria-label'), title: e.getAttribute('title'), "
                        "txt: (e.innerText||'').trim().slice(0,40)})")
                    key = str(info)
                    if key not in seen:
                        seen.add(key)
                        print(f"    candidate: {info}")
            except Exception:
                pass


# CDP WS interception state — re-armable across page reloads
CDP = {"session": None, "enabled": False}


async def cdp(fut, label, timeout_s=CDP_TIMEOUT_S):
    """Run a CDP call under a hard timeout. A hung send() once froze the whole
    loop for 26 min — every CDP call goes through here."""
    try:
        return await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        print(f"  [CDP] {label}: TIMEOUT after {timeout_s}s")
        return None
    except Exception as e:
        print(f"  [CDP] {label}: failed: {e}")
        return None


async def setup_cdp(page, context, on_frame):
    """(Re)attach CDP Network interception on the current page. Never blocks."""
    try:
        if CDP["session"]:
            await cdp(CDP["session"].send("Network.disable"), "Network.disable")
            CDP["session"] = None
        sess = await cdp(context.new_cdp_session(page), "new_cdp_session")
        if sess is None:
            return False
        CDP["session"] = sess
        await cdp(sess.send("Network.enable"), "Network.enable")
        sess.on("Network.webSocketFrameReceived", on_frame)
        sess.on("Network.webSocketFrameSent", on_frame)
        CDP["enabled"] = True
        return True
    except Exception as e:
        CDP["enabled"] = False
        print(f"  [CDP] Setup failed: {e}")
        return False


def make_on_ws_frame(state):
    """Build the WS frame handler bound to a mutable state dict."""
    async def on_ws_frame(params):
        s = state
        for key in ["response", "request"]:
            frame = params.get(key, {})
            payload = frame.get("payloadData", "")
            if not payload or len(payload) < 20:
                continue
            try:
                data = json.loads(payload)
                # Auto Roulette message format:
                # {"args":{"code":1,"description":"1 Red","gameId":"...","timestamp":"..."}}
                args = data if isinstance(data, dict) and "code" in data else data.get("args", data)
                code = args.get("code", "")
                desc = args.get("description", "")
                game_id = args.get("gameId", "")
                ts = args.get("timestamp", args.get("time", ""))

                if game_id and code and game_id != s["last_game_id"]:
                    s["last_game_id"] = game_id
                    try:
                        number = int(code)
                    except Exception:
                        number = code
                    desc_full = desc if desc else f"{number} {num_to_color(number)}"
                    desc_full = " ".join(desc_full.split())
                    s["spins"].append({
                        "number": number,
                        "description": desc_full,
                        "gameId": game_id,
                        "timestamp": ts,
                        "captured_at": datetime.now().isoformat()
                    })
                    s["new_since_save"] += 1
                    s["ws_captured"] += 1
                    total = len(s["spins"])
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] #{total}: {desc_full}")

                    if s["new_since_save"] >= 25:
                        save_spins(s["spins"])
                        s["new_since_save"] = 0
            except Exception:
                pass
    return on_ws_frame


async def start_session(p, state, on_frame):
    """Fresh browser -> login -> game page -> CDP setup.
    Returns (browser, context, page). Raises on hard failure."""
    browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    )
    page = await context.new_page()

    print("[1] Loading Sunbet...")
    await page.goto(GAME_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)

    print("[2] Logging in...")
    try:
        username_input = await page.wait_for_selector('#loginUsername', timeout=10000)
        pw_input = await page.query_selector('#loginPassword')
        if username_input and pw_input:
            await username_input.fill(SUNBET_USER)
            await pw_input.fill(SUNBET_PASS)
            await page.click('#loginBtn')
            print("   Login submitted")
            await page.wait_for_timeout(10000)
    except Exception as e:
        print(f"   Already logged in: {e}")

    print("[3] Getting game URL...")
    await page.wait_for_timeout(3000)
    iframe_elem = await page.wait_for_selector('#gameIframe', timeout=20000)
    game_src = await iframe_elem.get_attribute('src')
    print(f"   Game src: {game_src[:100]}...")

    print("[4] Navigating to game page...")
    await page.goto(game_src, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(5000)

    print("[5] Setting up CDP WS interception...")
    ok = await setup_cdp(page, context, on_frame)
    if ok:
        print("  [CDP] WS interception enabled ✓")
    return browser, context, page


async def collect_loop():
    print(f"[{datetime.now().isoformat()}] ===== Starting Roulette 2 session =====\n")
    spins = []
    last_game_id = None
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                spins = json.load(f)
            if spins:
                last_game_id = spins[-1]["gameId"]
            print(f"  Resuming from {len(spins)} existing spins (last gameId: {last_game_id})")
        except Exception:
            spins = []

    state = {"spins": spins, "last_game_id": last_game_id,
             "new_since_save": 0, "ws_captured": 0}

    on_frame = make_on_ws_frame(state)

    async with async_playwright() as p:
        browser, context, page = await start_session(p, state, on_frame)

        # ---- DOM polling fallback (read visible result) ----
        last_dom_result = None

        async def poll_dom():
            nonlocal last_dom_result
            try:
                selectors = [
                    '.number-win', '.win-number', '.result-number',
                    '[class*="number"][class*="win"]', '[class*="result"]',
                    '.last-result .number', '.current-result .number',
                    'div[class*="roulette-result"]', 'div[class*="last-number"]',
                    'div[class*="prev-number"]', '.bubble-number',
                    'span[class*="number"][class*="big"]',
                    'div[class*="value"]:has-text("Red"):has-text("Black")',
                ]
                for sel in selectors:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            text = await el.inner_text()
                            text = text.strip()
                            if text and text != last_dom_result:
                                last_dom_result = text
                                print(f"  [DOM] Found element '{sel}': '{text}'")
                                return
                    except Exception:
                        pass
            except Exception:
                pass

        async def wait_for_new_spin(base_count, timeout_s, label):
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if len(state["spins"]) > base_count:
                    print(f"  [RECOVERY] {label}: new spin #{len(state['spins'])} — stream is back")
                    return True
                await asyncio.sleep(2)
            print(f"  [RECOVERY] {label}: no new spin within {timeout_s}s")
            return False

        async def recover(base_count):
            """Recovery ladder. Returns True if a new spin arrived."""
            nonlocal browser, context, page
            print("  [RECOVERY] rung 0/4: passive wait — stream often self-heals")
            if await wait_for_new_spin(base_count, RUNG_WAIT_S, "after passive wait"):
                return True

            print("  [RECOVERY] rung 1/4: re-arm CDP interception")
            await setup_cdp(page, context, on_frame)
            if await wait_for_new_spin(base_count, RUNG_WAIT_S, "after CDP re-arm"):
                return True

            print("  [RECOVERY] rung 2/4: click game refresh button (best effort)")
            clicked = await click_refresh_button(page)
            if not clicked:
                print("    refresh button not found — dumping DOM candidates for next time")
                await dump_refresh_candidates(page)
            if await wait_for_new_spin(base_count, RUNG_WAIT_S, "after refresh click"):
                return True

            print("  [RECOVERY] rung 3/4: page reload — verifying frames resume")
            try:
                await asyncio.wait_for(page.reload(wait_until="domcontentloaded"), timeout=60)
            except Exception as e:
                print(f"    reload failed: {e}")
            await page.wait_for_timeout(5000)
            await setup_cdp(page, context, on_frame)
            if await wait_for_new_spin(base_count, RELOAD_VERIFY_S, "after reload"):
                return True

            print("  [RECOVERY] rung 4/4: full browser restart (fresh session)")
            try:
                await browser.close()
            except Exception:
                pass
            browser, context, page = await start_session(p, state, on_frame)
            return await wait_for_new_spin(base_count, RESTART_VERIFY_S, "after browser restart")

        print(f"[6] Monitoring — {len(spins)} spins so far\n")
        start_time = time.time()
        last_spin_count = len(spins)
        dom_poll_count = 0

        while (time.time() - start_time) < SESSION_DURATION:
            await asyncio.sleep(5)
            elapsed = time.time() - start_time

            # DOM poll every 15s while the WS has never produced anything
            if state["ws_captured"] == 0 and len(spins) == 0:
                dom_poll_count += 1
                if dom_poll_count % 3 == 0:
                    await poll_dom()

            # Stall detection — silence beyond the legit cadence envelope.
            # 120s threshold: no false fires (max legit ~57s), catches real
            # stream deaths promptly. Also covers a fresh session that never
            # produced its first spin (bootstrap watchdog after 4 min).
            if spins:
                last_spin_time = spins[-1]["captured_at"]
                sec_since_last = (datetime.now() - datetime.fromisoformat(last_spin_time)).total_seconds()
            else:
                sec_since_last = elapsed
            if sec_since_last > STALL_THRESHOLD_S and (spins or elapsed > 240):
                print(f"  STALLED — {sec_since_last:.0f}s since last spin. Running recovery ladder...")
                ok = await recover(len(spins))
                if not ok:
                    print("  Recovery ladder failed — stream still silent after full restart. Abandoning session.")
                    break

            # Status every 60s
            if int(elapsed) % 60 < 6:
                rate = len(spins) / (elapsed / 3600) if elapsed > 0 else 0
                print(f"  Status: {len(spins)} spins (WS: {state['ws_captured']}), {elapsed/60:.0f}m elapsed, {rate:.1f}/hr")

            if len(spins) > last_spin_count:
                last_spin_count = len(spins)

        save_spins(spins)
        hours = (time.time() - start_time) / 3600
        print(f"  Session ended after {hours:.1f}h — {len(spins)} spins total")
        await browser.close()
        return spins


async def main():
    print(f"[{datetime.now().isoformat()}] Roulette 2 Collector v2 (reliability ladder) — 24/7 MODE")
    while True:
        try:
            await collect_loop()
            print("  Session complete, starting new session in 10s...")
            await asyncio.sleep(10)
        except Exception as e:
            print(f"  SESSION CRASHED: {e}")
            print("  Restarting in 30s...")
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
