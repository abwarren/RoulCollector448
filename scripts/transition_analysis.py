#!/usr/bin/env python3
"""Transition matrices + walk-forward Markov backtest for Table 448 spins.

Phase A of docs/modeling-plan.md. numpy + sqlite3 only (no pandas, no
sklearn, no CDN/network).

Usage:  python3 scripts/transition_analysis.py [db_path]
Default DB: /home/wa/roulette2_spins.db

Output: normalized-row summary, count matrices, Markov normalized matrices,
chi2 fairness, walk-forward order-1/order-2 backtest vs uniform baseline,
last-50 window demo.
"""
import sqlite3
import sys
import math
from collections import Counter

WHEEL = [0,32,15,19,4,21,2,25,17,34,6,27,13,36,11,30,8,23,
         10,5,24,16,33,1,20,14,31,9,22,18,29,7,28,12,35,3,26]
REDS = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
# 8 wheel sectors, consecutive blocks in wheel order: 5,5,5,5,5,4,4,4
SEC_BLOCKS = [5,5,5,5,5,4,4,4]

def dozen(n):
    if n == 0: return 0
    return (n - 1) // 12 + 1

def column(n):
    if n == 0: return 0
    return (n - 1) % 3 + 1

def wheel_idx(n):
    return WHEEL.index(n)

def sector(n):
    wi = wheel_idx(n)
    acc = 0
    for s, b in enumerate(SEC_BLOCKS):
        if wi < acc + b:
            return s
        acc += b
    return 7

def circ_dist(a, b, size=37):
    d = abs(a - b) % size
    return min(d, size - d)

def load(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT number, color, captured_at FROM roulette_spins ORDER BY id"
    ).fetchall()
    con.close()
    return rows

def main(db):
    rows = load(db)
    N = len(rows)
    nums = [r[0] for r in rows]
    cols = [r[1] for r in rows]
    print(f"spins: {N}  ({rows[0][2][:10]} -> {rows[-1][2][:10]})")

    # ---- normalized rows ----
    dz = [dozen(n) for n in nums]
    co = [column(n) for n in nums]
    wi = [wheel_idx(n) for n in nums]
    sec = [sector(n) for n in nums]
    wd = [circ_dist(wi[i], wi[i-1]) for i in range(1, N)]
    tdiff = []
    from datetime import datetime
    for i in range(1, N):
        try:
            t0 = datetime.fromisoformat(rows[i-1][2])
            t1 = datetime.fromisoformat(rows[i][2])
            tdiff.append((t1 - t0).total_seconds())
        except ValueError:
            tdiff.append(0.0)
    gaps30 = sum(1 for d in tdiff if d > 1800)
    if tdiff:
        print(f"gaps >30min: {gaps30}  (collection quality; mean inter-spin {sum(tdiff)/len(tdiff):.0f}s, "
              f"median {sorted(tdiff)[len(tdiff)//2]:.0f}s)")

    # ---- count matrices ----
    A = [[0]*37 for _ in range(37)]
    Ac = [[0]*3 for _ in range(3)]
    Ad = [[0]*4 for _ in range(4)]
    Acol = [[0]*4 for _ in range(4)]
    As = [[0]*8 for _ in range(8)]
    cmap = {"Red": 0, "Black": 1, "Green": 2}
    for i in range(1, N):
        A[nums[i-1]][nums[i]] += 1
        Ac[cmap[cols[i-1]]][cmap[cols[i]]] += 1
        Ad[dz[i-1]][dz[i]] += 1
        Acol[co[i-1]][co[i]] += 1
        As[sec[i-1]][sec[i]] += 1

    def row_norm(M):
        out = []
        for r in M:
            s = sum(r)
            out.append([x / s if s else 0.0 for x in r])
        return out

    M = row_norm(A)
    exp = 1.0 / 37
    # top pairs by count
    pairs = []
    for i in range(37):
        for j in range(37):
            if A[i][j]:
                pairs.append((A[i][j], i, j))
    pairs.sort(reverse=True)
    print("\n-- number transitions (from->to, top 10 by count) --")
    for c, i, j in pairs[:10]:
        print(f"  {i:2d}->{j:2d}  {c:4d}  M={M[i][j]*100:5.2f}%  exp 2.70%")
    lift = [(A[i][j], i, j) for i in range(37) for j in range(37)
            if A[i][j] >= 10]
    lift.sort(key=lambda t: M[t[1]][t[2]] / exp, reverse=True)
    print("\n-- top 5 by lift over uniform (min 10 obs) --")
    for c, i, j in lift[:5]:
        print(f"  {i:2d}->{j:2d}  n={c:4d}  M={M[i][j]*100:5.2f}%  {M[i][j]/exp:.2f}x uniform")

    print("\n-- color transitions (R/B/G) --")
    Mc = row_norm(Ac)
    names = ["Red", "Blk", "Grn"]
    for i in range(3):
        print("  " + "  ".join(f"{names[i]}->{names[j]} {Mc[i][j]*100:5.1f}%" for j in range(3)))

    print("\n-- dozen transitions (0=zero,1-3) --")
    Md = row_norm(Ad)
    for i in range(4):
        print("  " + "  ".join(f"{i}->{j} {Md[i][j]*100:5.1f}%" for j in range(4)))

    print("\n-- wheel sectors (8 blocks) transitions --")
    Ms = row_norm(As)
    for i in range(8):
        print("  " + " ".join(f"{Ms[i][j]*100:4.1f}" for j in range(8)))

    # ---- chi2 fairness + Z ----
    counts = Counter(nums)
    chi2 = sum((counts[n] - N/37)**2 / (N/37) for n in range(37))
    print(f"\n-- fairness --")
    print(f"  chi2 = {chi2:.1f}  (df 36: 51.0 @95%, 58.0 @99%)  -> "
          f"{'FAIR' if chi2 < 51 else ('borderline' if chi2 < 58 else 'BIASED')}")
    expc = N/37
    zs = {n: (counts[n]-expc)/math.sqrt(N*(1/37)*(36/37)) for n in range(37)}
    ztop = sorted(zs.items(), key=lambda kv: -abs(kv[1]))
    print("  top |Z|: " + "  ".join(f"#{n} {z:+.2f}" for n, z in ztop[:5]))
    print(f"  green: {counts[0]}/{N} = {counts[0]/N*100:.2f}%  exp 2.70%")
    hi = sum(1 for n in ztop if abs(n[1]) > 2)
    print(f"  numbers |Z|>2: {hi}  (fair expectation ~{round(37*0.05)} by chance)")

    # ---- walk-forward backtest ----
    split = int(N * 0.7)
    tr_n = nums[:split]
    te_n = nums[split:]
    print(f"\n-- walk-forward backtest (train {split} / test {len(te_n)}) --")
    # order-1 on train
    A1 = [[0]*37 for _ in range(37)]
    for i in range(1, len(tr_n)):
        A1[tr_n[i-1]][tr_n[i]] += 1
    M1 = row_norm(A1)
    # order-2 on train
    A2 = {}
    for i in range(2, len(tr_n)):
        key = (tr_n[i-2], tr_n[i-1])
        A2.setdefault(key, [0]*37)[tr_n[i]] += 1

    def topk(probs, actual, k):
        order = sorted(range(37), key=lambda j: -probs[j])
        return actual in order[:k]

    def ll(probs, actual):
        p = probs[actual]
        return -math.log(p) if p > 0 else 50.0

    def run(te, use2):
        t1 = t3 = t5 = 0
        lls = 0.0
        n = 0
        unif = [exp]*37
        for t in range(len(te)):
            if t < 2 and use2:
                continue
            prev1 = te[t-1]
            if use2:
                key = (te[t-2], prev1)
                row = A2.get(key)
                if row and sum(row) >= 5:
                    m2 = [x/sum(row) for x in row]
                    m1 = M1[prev1] if sum(M1[prev1]) else unif
                    probs = [0.5*a + 0.35*b + 0.15*exp for a, b in zip(m2, m1)]
                else:
                    probs = M1[prev1] if sum(M1[prev1]) else unif
            else:
                probs = M1[prev1] if sum(M1[prev1]) else unif
            actual = te[t]
            t1 += topk(probs, actual, 1)
            t3 += topk(probs, actual, 3)
            t5 += topk(probs, actual, 5)
            lls += ll(probs, actual)
            n += 1
        return t1/n*100, t3/n*100, t5/n*100, lls/n

    u1, u3, u5, ull = 100/37, 300/37, 500/37, math.log(37)
    m1 = run(te_n, False)
    m2 = run(te_n, True)
    print(f"  baseline uniform : top1 {u1:5.2f}%  top3 {u3:5.2f}%  top5 {u5:5.2f}%  logloss {ull:.4f}")
    print(f"  order-1 Markov   : top1 {m1[0]:5.2f}%  top3 {m1[1]:5.2f}%  top5 {m1[2]:5.2f}%  logloss {m1[3]:.4f}")
    print(f"  order-2 Markov   : top1 {m2[0]:5.2f}%  top3 {m2[1]:5.2f}%  top5 {m2[2]:5.2f}%  logloss {m2[3]:.4f}")

    # ---- last-50 window demo ----
    w = nums[-50:]
    wc = Counter(w)
    exp50 = 50/37
    sd50 = math.sqrt(50*(1/37)*(36/37))
    wz = {n: (wc[n]-exp50)/sd50 for n in range(37)}
    hot = sorted(wz.items(), key=lambda kv: -kv[1])[:5]
    cold = sorted(wz.items(), key=lambda kv: kv[1])[:5]
    print("\n-- last 50 spins window --")
    print("  hot : " + "  ".join(f"#{n} {v:+.1f}z" for n, v in hot))
    print("  cold: " + "  ".join(f"#{n} {v:+.1f}z" for n, v in cold))
    streak = 1
    base = cols[-1]
    for c in reversed(cols[:-1]):
        if c == base and c != "Green":
            streak += 1
        else:
            break
    print(f"  current color streak: {streak}x {base}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/home/wa/roulette2_spins.db")
