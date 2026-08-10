#!/usr/bin/env python3
"""Live next-spin prediction using the trained LightGBM models.

Reads the latest spin from /home/wa/roulette2_spins.db, rebuilds the
feature row, and prints the model's top-5 numbers + color probs against
fair-wheel baselines (1/37, 18/37).
"""
import sqlite3
import sys

import numpy as np
import pandas as pd
import lightgbm as lgb

sys.path.insert(0, "/home/wa/projects/RoulCollector448/scripts")
from feature_model import build_features, color_of  # noqa: E402

DB = "/home/wa/roulette2_spins.db"
OUT = "/home/wa/projects/RoulCollector448/ml"


def load():
    con = sqlite3.connect(DB)
    nums = np.array(
        [r[0] for r in con.execute(
            "SELECT number FROM roulette_spins ORDER BY id")], dtype=int)
    con.close()
    return nums


def main():
    nums = load()
    df = build_features(nums)

    # markov features from full-data transitions (inference mode)
    tmat = np.zeros((37, 37))
    for a, b in zip(nums[:-1], nums[1:]):
        tmat[a, b] += 1
    rowsum = tmat.sum(axis=1)
    probs = np.divide(tmat, rowsum[:, None], out=np.zeros_like(tmat),
                      where=rowsum[:, None] > 0)
    mp = probs.argmax(axis=1)
    mc = probs.max(axis=1)
    last = nums[-1]
    df.loc[df.index[-1], "markov_pred"] = int(mp[last])
    df.loc[df.index[-1], "markov_conf"] = float(np.float32(mc[last]))

    feat_cols = [c for c in df.columns if not c.startswith("y_")]
    row = df.iloc[[-1]][feat_cols]

    bst = lgb.Booster(model_file=f"{OUT}/model_number.txt")
    bst_c = lgb.Booster(model_file=f"{OUT}/model_color.txt")
    p = bst.predict(row)[0]
    pc = bst_c.predict(row)[0]

    top = np.argsort(p)[::-1][:5]
    names = ["Red", "Black", "Green"]
    cn = "Red" if color_of(last) == 0 else "Black" if color_of(last) == 1 else "Green"
    print(f"last spin: {last} {cn}  | total {len(nums)} spins")
    print("top-5 next: " + " ".join(f"{int(x)}" for x in top))
    print("  probs:    " + " ".join(f"{p[int(x)]:.4f}" for x in top) + "  (fair 0.0270)")
    print("color:      " + " ".join(f"{names[i]} {pc[i]:.4f}" for i in range(3)) +
          "  (fair R/B 0.4865, G 0.0270)")
    print(f"markov baseline: {int(mp[last])}  conf {mc[last]:.4f}")


if __name__ == "__main__":
    sys.exit(main())
