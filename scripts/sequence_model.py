#!/usr/bin/env python3
"""Phase C — tiny GRU sequence model on roulette spin sequences (numpy only).

The user's hypothesis: the neighbor-to-neighbor sequence carries learnable
structure beyond the fair-wheel baseline. This is the direct test — a
recurrent model that sees the last `SEQ` spins (as numbers) and predicts
the next, trained walk-forward on the first 70% and evaluated on the last
30% (chronological, never shuffled).

No torch: the CPU wheel comes from a CDN (user denies CDN downloads) and
at 22K rows numpy is plenty. A GRU is ~30 lines of BPTT in numpy.

Baselines reported alongside (test block):
  uniform    2.70%  top-1 /  8.11% top-3 / 13.51% top-5 / logloss 3.6109
  order-1 Markov (from scripts/transition_analysis.py)
  order-2 Markov (pair-state, the pure "neighbor-pair" learner)

Run:  ~/projects/RoulCollector448/.venv-ml/bin/python scripts/sequence_model.py
"""
import sqlite3
import sys
import time

import numpy as np

DB = "/home/wa/roulette2_spins.db"
OUT = "/home/wa/projects/RoulCollector448/ml"

SEQ = 8        # window length
HID = 48       # GRU hidden size
LR = 2e-3
EPOCHS = 12
BATCH = 128
CLIP = 5.0
SEED = 7


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def load():
    con = sqlite3.connect(DB)
    nums = np.array(
        [r[0] for r in con.execute("SELECT number FROM roulette_spins ORDER BY id")],
        dtype=int,
    )
    con.close()
    return nums


def make_batches(xs, ys, batch):
    n = len(xs)
    perm = np.arange(n)
    rng = np.random.default_rng(SEED)
    rng.shuffle(perm)
    for i in range(0, n, batch):
        idx = perm[i:i + batch]
        yield xs[idx], ys[idx]


def main():
    nums = load()
    N = len(nums)
    print(f"spins: {N}  (seq {SEQ}, hidden {HID})")

    # sequence windows: xs[i] = last SEQ spins ending at position i, y = next
    x_idx = np.stack(
        [np.arange(i, i + SEQ) for i in range(N - SEQ)], axis=0
    )
    xs = nums[x_idx]                       # (N-SEQ, SEQ)
    ys = nums[SEQ:]                        # next spin
    split = int((N - SEQ) * 0.7)
    x_tr, y_tr = xs[:split], ys[:split]
    x_te, y_te = xs[split:], ys[split:]
    n_tr, n_te = len(x_tr), len(y_te)
    print(f"train {n_tr} / test {n_te}")

    # ---- baselines on the test block ----
    from collections import Counter
    maj = Counter(y_tr.tolist()).most_common(1)[0][0]
    maj_acc = np.mean(y_te == maj)
    unif_logloss = np.log(37)
    # order-1 Markov from TRAIN transitions only
    tmat = np.zeros((37, 37))
    for a, b in zip(nums[:split + SEQ - 1], nums[1:split + SEQ]):
        tmat[a, b] += 1
    rs = tmat.sum(axis=1)
    m1 = np.divide(tmat, rs[:, None], out=np.zeros_like(tmat), where=rs[:, None] > 0)
    m1_pred = np.array([m1[x_te[i][-1]].argmax() for i in range(n_te)])
    m1_acc = np.mean(m1_pred == y_te)
    # order-2 Markov from TRAIN pairs (key = (prev, current) → next)
    p2 = {}
    for i in range(SEQ, split + SEQ - 1):
        key = (nums[i - 1], nums[i])
        p2.setdefault(key, []).append(nums[i + 1])
    m2_hits = m2_total = 0
    for i in range(n_te):
        key = (x_te[i][-1], y_te[i - 1]) if i > 0 else (x_te[i][-1], x_te[i][-2])
        cand = p2.get(key)
        if cand:
            m2_total += 1
            m2_hits += Counter(cand).most_common(1)[0][0] == y_te[i]
    m2_acc = m2_hits / max(1, m2_total)
    print(f"baselines        | majority #{maj} {maj_acc:.4f} | uniform {1/37:.4f} "
          f"| M1 {m1_acc:.4f} | M2 {m2_acc:.4f} | logloss {unif_logloss:.4f}")

    # ---- init GRU params (Glorot-ish) ----
    def init(shape):
        return np.random.default_rng(SEED).normal(0, 0.1, size=shape).astype(np.float32)

    p = {
        "Wz": init((37, HID)), "Uz": init((HID, HID)), "bz": np.zeros(HID, np.float32),
        "Wr": init((37, HID)), "Ur": init((HID, HID)), "br": np.zeros(HID, np.float32),
        "Wh": init((37, HID)), "Uh": init((HID, HID)), "bh": np.zeros(HID, np.float32),
        "Wo": init((HID, 37)), "bo": np.zeros(37, np.float32),
    }
    m = {k: np.zeros_like(v) for k, v in p.items()}
    v = {k: np.zeros_like(v) for k, v in p.items()}

    def forward(xb):
        """xb: (B, SEQ). Returns (hs list, h_last, logits)."""
        B = xb.shape[0]
        h = np.zeros((B, HID), np.float32)
        one = np.eye(37, dtype=np.float32)[xb]           # (B, SEQ, 37)
        hs = []
        for t in range(SEQ):
            x = one[:, t]
            z = sigmoid(x @ p["Wz"] + h @ p["Uz"] + p["bz"])
            r = sigmoid(x @ p["Wr"] + h @ p["Ur"] + p["br"])
            c = np.tanh(x @ p["Wh"] + (r * h) @ p["Uh"] + p["bh"])
            h = (1 - z) * h + z * c
            hs.append(h)
        logits = h @ p["Wo"] + p["bo"]
        return hs, logits

    def backward(xb, yb, hs, logits):
        """Manual BPTT. Returns param grads."""
        one = np.eye(37, dtype=np.float32)[xb]
        B = xb.shape[0]
        g = {k: np.zeros_like(v) for k, v in p.items()}
        prob = np.exp(logits - logits.max(axis=1, keepdims=True))
        prob /= prob.sum(axis=1, keepdims=True)
        dout = prob - np.eye(37, dtype=np.float32)[yb]   # true next-spin target
        h_last = hs[-1]
        g["Wo"] = h_last.T @ dout
        g["bo"] = dout.sum(axis=0)
        dh = dout @ p["Wo"].T
        h_prev = np.zeros((B, HID), np.float32)
        for t in reversed(range(SEQ)):
            x = one[:, t]
            h_t = hs[t]
            # recompute gates for step t
            if t == 0:
                hp = h_prev
            else:
                hp = hs[t - 1]
            z = sigmoid(x @ p["Wz"] + hp @ p["Uz"] + p["bz"])
            r = sigmoid(x @ p["Wr"] + hp @ p["Ur"] + p["br"])
            c = np.tanh(x @ p["Wh"] + (r * hp) @ p["Uh"] + p["bh"])
            dz = dh * (c - hp) * z * (1 - z)
            dc = dh * z * (1 - c * c)
            drh = dc @ p["Uh"].T
            dr = (drh * hp) * r * (1 - r)
            dh_prev = dh * (1 - z) + drh * r
            g["Wz"] += x.T @ dz
            g["Uz"] += hp.T @ dz
            g["bz"] += dz.sum(axis=0)
            g["Wr"] += x.T @ dr
            g["Ur"] += hp.T @ dr
            g["br"] += dr.sum(axis=0)
            g["Wh"] += x.T @ dc
            g["Uh"] += (r * hp).T @ dc
            g["bh"] += dc.sum(axis=0)
            dh = dh_prev
        return g

    # ---- train ----
    t0 = time.time()
    for ep in range(1, EPOCHS + 1):
        losses = []
        for xb, yb in make_batches(x_tr, y_tr, BATCH):
            hs, logits = forward(xb)
            # stable CE: subtract row max, logsumexp
            lse = np.log(np.exp(logits - logits.max(axis=1, keepdims=True)).sum(axis=1))
            loss = np.mean(lse - logits[np.arange(len(yb)), yb])
            if not np.isfinite(loss) or loss < -1e-6:
                raise SystemExit(f"numeric blowup: loss {loss}")
            losses.append(loss)
            g = backward(xb, yb, hs, logits)
            for k in p:
                g[k] = np.clip(g[k], -CLIP, CLIP)
                m[k] = 0.9 * m[k] + 0.1 * g[k]
                v[k] = 0.999 * v[k] + 0.001 * g[k] * g[k]
                mh = m[k] / (1 - 0.9 ** ep)
                vh = v[k] / (1 - 0.999 ** ep)
                p[k] -= LR * mh / (np.sqrt(vh) + 1e-8)
        print(f"epoch {ep:2d}  loss {np.mean(losses):.4f}  ({time.time()-t0:.0f}s)")

    # ---- evaluate on the test block ----
    probs = []
    for i in range(0, n_te, 512):
        xb = x_te[i:i + 512]
        _, logits = forward(xb)
        pr = np.exp(logits - logits.max(axis=1, keepdims=True))
        pr /= pr.sum(axis=1, keepdims=True)
        probs.append(pr)
    pr = np.concatenate(probs)
    pred = pr.argmax(axis=1)
    acc = np.mean(pred == y_te)
    top3 = np.mean([y_te[i] in np.argsort(pr[i])[-3:] for i in range(n_te)])
    top5 = np.mean([y_te[i] in np.argsort(pr[i])[-5:] for i in range(n_te)])
    ll = -np.mean(np.log(pr[np.arange(n_te), y_te] + 1e-12))
    print(f"\nGRU test         | top1 {acc:.4f} | top3 {top3:.4f} | top5 {top5:.4f} | logloss {ll:.4f}")
    print(f"fair             | top1 {1/37:.4f} | top3 {3/37:.4f} | top5 {5/37:.4f} | logloss {unif_logloss:.4f}")

    np.savez(f"{OUT}/sequence_model.npz", **p)
    with open(f"{OUT}/sequence_report.txt", "w") as fh:
        fh.write(f"spins={N} train={n_tr} test={n_te} seq={SEQ} hidden={HID} epochs={EPOCHS}\n")
        fh.write(f"GRU   top1 {acc:.4f} top3 {top3:.4f} top5 {top5:.4f} logloss {ll:.4f}\n")
        fh.write(f"fair  top1 {1/37:.4f} top3 {3/37:.4f} top5 {5/37:.4f} logloss {unif_logloss:.4f}\n")
        fh.write(f"M1 {m1_acc:.4f} M2 {m2_acc:.4f} majority {maj_acc:.4f}\n")
    print(f"saved {OUT}/sequence_model.npz + sequence_report.txt")


if __name__ == "__main__":
    sys.exit(main())
