#!/usr/bin/env python3
"""Probe the REAL Sunbet WS stream to teach the Signal C history adapter.

The most important mechanism in the system is the authoritative recent-history
comparison — and its only unverified assumption is the shape of Evolution's
history payloads (parse_history_frame is inferred from standard patterns).
This probe runs the real login + CDP interception for a bounded time and
prints:

  * every history frame parse_history_frame COULD parse (record count + first)
  * the shape of history-LIKE frames it MISSED (top-level keys + first
    entry's keys), so the adapter can be taught the real format

Usage (needs SUNBET_USER/SUNBET_PASS, same as the collector):
    .venv\\Scripts\\python.exe scripts/probe_history_frames.py [--seconds 90]

Exits FATAL without credentials (imports the collector, same guard).
"""

import argparse
import asyncio
import json
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, __file__.rsplit("/", 2)[0])  # repo root

from collector import history  # noqa: E402
import collector.roulette2_collector as rc  # noqa: E402


def _shape_of(data, depth=0):
    """Compact shape description of a JSON blob (top-level + first list-of-dicts)."""
    out = {"top": sorted(data.keys()) if isinstance(data, dict) else type(data).__name__}
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                out["list_entry_keys"] = sorted(v[0].keys())
                out["list_len"] = len(v)
                break
    return out


async def main(seconds: int):
    stats = {"frames": 0, "parsed_records": 0, "parsed_frames": 0, "missed_like": 0}

    def make_on_frame():
        async def on_frame(params):
            for key in ("response", "request"):
                frame = params.get(key, {})
                payload = frame.get("payloadData", "")
                if not payload or len(payload) < 20:
                    continue
                try:
                    data = json.loads(payload)
                except Exception:
                    continue
                stats["frames"] += 1
                recs = history.parse_history_frame(data)
                if recs:
                    stats["parsed_records"] += len(recs)
                    stats["parsed_frames"] += 1
                    print(f"  [HIST] parsed {len(recs)} recs, "
                          f"first={recs[0].game_id} #{recs[0].number} "
                          f"ts={recs[0].server_ts}")
                else:
                    shape = _shape_of(data)
                    if "list_entry_keys" in shape:
                        stats["missed_like"] += 1
                        print(f"  [MISS] history-LIKE frame not parsed: "
                              f"top={shape['top']} entry_keys={shape['list_entry_keys']} "
                              f"len={shape['list_len']}")
        return on_frame

    async with async_playwright() as p:
        browser, context, page = await rc.start_session(p, {}, make_on_frame())
        print(f"\nCapturing frames for {seconds}s ...")
        await asyncio.sleep(seconds)
        await browser.close()

    print(f"\n=== probe summary ===")
    print(f"  ws frames seen        : {stats['frames']}")
    print(f"  history frames parsed : {stats['parsed_frames']} "
          f"({stats['parsed_records']} records)")
    print(f"  history-LIKE missed   : {stats['missed_like']}")
    if stats["missed_like"]:
        print("  -> teach parse_history_frame the real shape (see [MISS] lines)")
    elif stats["parsed_frames"]:
        print("  -> parse_history_frame matches the real payload format")
    else:
        print("  -> no history payloads seen in this window "
              "(they may arrive on join only; rerun with more seconds)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=90)
    args = ap.parse_args()
    asyncio.run(main(args.seconds))
