# Glossary — RoulCollector448

Shared language for this project. Keep updated as new terms emerge.

| Term | Definition |
|------|------------|
| **Spin** | One roulette result: number, color, description, unique game_id, server timestamp, capture timestamp. |
| **Table 448** | Auto Roulette 2 - 400K on Sunbet (Evolution). Sunbet gameId `997043039547559967`, openTable `448`. Data in `/home/wa/roulette2_spins.db`, table `roulette_spins`. |
| **Collector** | `roulette2_collector.py` — 24/7 systemd daemon that intercepts the game WebSocket and persists every spin to SQLite. Never modified by the dashboard project. |
| **Nn** | The 5-number wheel cluster of N: N itself + 2 neighbours left + 2 neighbours right on the European wheel. Example: Nn(0) = {0, 3, 15, 32, 26}. |
| **European wheel** | Clockwise: 0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26. |
| **Z-score** | (observed − expected) / sqrt(N × p × (1−p)). How many standard deviations an observed count sits from fair expectation. |
| **Sleeper** | A number currently in a long drought (gap since its last hit well above average ≈ 37 spins). |
| **Streak** | Consecutive same-color run (e.g. 12x Black). Also number repeats (doubles/triples). |
| **Rolling window** | The last N spins as a slice (50 / 100 / 200 / 500 / 1000). Stats recomputed per window to detect drift. |
| **Hot / cold** | Numbers with the highest / lowest hit counts within a window. |
| **Reverse black** | User's term for the dark theme: black page background. Red numbers pop; black numbers need a light border/outline to stay visible. |
| **Row of 50** | 50 spins laid out in one horizontal row, oldest → newest left-to-right, rows stacked top-to-bottom. |
| **"Show more"** | Control that appends the next older batch of rows without a page reload. |
