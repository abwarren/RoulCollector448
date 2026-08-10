#!/usr/bin/env python3
"""
RoulCollector448 — terminal grid viewer.

Mirrors the web dashboard's number grid in the terminal:
  - newest spin TOP-LEFT, older spins move RIGHT, wrap down (A1 = newest)
  - default: last 20 numbers only (one row)
  - 'm' / space: toggle showing the rest (up to 2000 DB spins + live overlay)
  - realtime: polls /api/health every 5s; live journald spins overlaid on DB
  - colors: red=red, black=default, green=green, doubles=magenta, newest=inverted

Zero external deps — Python stdlib (urllib + curses) only.

Usage:  python3 terminal_view.py [--host http://localhost:4480] [--rows 20]
Keys:   q / Esc quit   m / space toggle more/less   r force refresh
"""
import argparse
import curses
import json
import sys
import time
import urllib.request

POLL_SECS = 5          # flat 5s realtime, matches web dashboard
DB_BATCH = 2000        # spins fetched from /api/spins when expanded
DEFAULT_ROW = 20       # numbers per row AND default visible count (one row)

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def fetch_json(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Source:
    """Holds DB slice + live overlay; mirrors frontend merge (app.js)."""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.spins = []      # chronological (oldest->newest within newest batch)
        self.live_spins = []  # newest-first, journald
        self.health = {}
        self.error = None
        self.last_poll = 0.0
        self.initial = False

    @staticmethod
    def _time_match(db_ts, live_t):
        # DB "2026-08-06T18:22:49.337407" vs live "18:22:49"
        try:
            return (db_ts or "")[11:19] == (live_t or "")
        except Exception:
            return False

    def refresh(self, force=False):
        now = time.time()
        if not force and now - self.last_poll < POLL_SECS - 0.2:
            return
        self.last_poll = now
        try:
            h = fetch_json(f"{self.base}/api/health")
            self.health = h
            live = h.get("live_spins") or []
            self.live_spins = live
            if not self.initial or force:
                d = fetch_json(f"{self.base}/api/spins?limit={DB_BATCH}&offset=0")
                self.spins = d.get("spins") or []
                self.initial = True
            self.error = None
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"

    def newest(self):
        """Newest-first list: live overlay + reversed DB slice, deduped."""
        newest = list(reversed(self.spins))
        if self.live_spins:
            known = {
                (s.get("number"), s.get("captured_at", "")[11:19])
                for s in newest[:50]
            }
            overlay = [
                ls for ls in self.live_spins
                if (ls.get("number"), ls.get("time")) not in known
            ]
            if overlay:
                newest = overlay + newest
        return newest


def cell_for(spin, is_newest, is_double):
    n = spin.get("number")
    if n is None:
        return " ? ", 3, curses.A_DIM
    color = "Green" if n == 0 else ("Red" if n in REDS else "Black")
    attrs = 0
    if is_double:
        attrs |= curses.A_BOLD | curses.A_UNDERLINE  # magenta set below
    if is_newest:
        attrs |= curses.A_REVERSE
    pair = {"Red": 1, "Green": 2, "Black": 3}.get(color, 3)
    return f"{n:>3}", pair, attrs


def draw(stdscr, src, show_all, row_len=DEFAULT_ROW):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, -1, -1)
    curses.init_pair(4, curses.COLOR_MAGENTA, -1)
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.clear()

    h, w = stdscr.getmaxyx()
    newest = src.newest()
    limit = None if show_all else row_len
    view = newest[:limit]

    # --- header ---
    hd = src.health or {}
    live = hd.get("live_last_spin") or {}
    alive = bool(hd.get("collector_alive"))
    label = "LIVE" if alive else "STALLED"
    dot = "●" if alive else "○"
    age = hd.get("live_age_seconds")
    age_s = f"{age:.0f}s" if isinstance(age, (int, float)) else "?"
    total = hd.get("total_spins", "?")
    last = f"{live.get('number', '?')} {live.get('color', '')}" if live else "?"
    header = (f"{dot} {label}  last:{last}  live-age:{age_s}  "
              f"db:{total}  showing:{len(view)} "
              f"({'+more' if not show_all else '-less'}: m/space)  q:quit")
    try:
        stdscr.addstr(0, 0, header[:w - 1], curses.A_BOLD | (0 if alive else curses.A_DIM))
    except curses.error:
        pass

    if src.error:
        try:
            stdscr.addstr(2, 0, f"API error: {src.error}", curses.A_DIM)
        except curses.error:
            pass

    # --- grid: newest top-left, rightward, wrap down ---
    row = 2
    col = 0
    for i, spin in enumerate(view):
        is_newest = (i == 0)
        is_double = bool(view and i + 1 < len(view) and
                         view[i].get("number") == view[i + 1].get("number"))
        text, pair, attrs = cell_for(spin, is_newest, is_double)
        if is_double:
            attrs |= curses.color_pair(4)
        else:
            attrs |= curses.color_pair(pair)
        if col + 5 > w - 1:
            col = 0
            row += 1
            if row > h - 2:
                break
        try:
            stdscr.addstr(row, col, f" {text} ", attrs)
        except curses.error:
            pass
        col += 5

    # --- footer ---
    n_live = len(src.live_spins)
    try:
        stdscr.addstr(h - 1, 0,
                      f"live overlay:{n_live}  total:{len(newest)}  "
                      f"{'[all shown]' if show_all else '[last 20 — m for all]'}",
                      curses.A_DIM)
    except curses.error:
        pass
    stdscr.refresh()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://localhost:4480")
    ap.add_argument("--rows", type=int, default=DEFAULT_ROW)
    args = ap.parse_args()

    src = Source(args.host)

    def run(stdscr):
        show_all = False
        src.refresh(force=True)
        last_redraw = 0.0
        while True:
            now = time.time()
            src.refresh(force=False)
            if now - last_redraw >= 0.5:
                draw(stdscr, src, show_all, row_len=args.rows)
                last_redraw = now
            try:
                k = stdscr.get_wch()
            except Exception:
                k = None
            if k is not None:
                if k in ("q", "Q", "\x1b"):
                    break
                if k in ("m", "M", " "):
                    show_all = not show_all
                    draw(stdscr, src, show_all, row_len=args.rows)
                if k in ("r", "R"):
                    src.refresh(force=True)
            time.sleep(0.1)

    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
