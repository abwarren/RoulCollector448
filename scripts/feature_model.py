#!/usr/bin/env python3
"""
Train LightGBM models on roulette spin data (Table 448, /home/wa/roulette2_spins.db).

Targets:
  - next number (37-class multiclass)
  - next color (3-class)

Validation: walk-forward 70/30 chronological split. Baselines reported alongside:
  uniform 1/37, majority class, Markov transition-matrix argmax, Nn-cluster follow rate.

Features per spin (predicting spin i+1 from history up to i):
  categorical: last number, last color, last dozen, last zone, last sector, hot number (last 50)
  numeric:     wheel pos, wheel distance to prev, repeat flags, rolling counts
               (5/10/25/50 of last number/color/dozen/zone), Nn-cluster activity,
               streak lengths, spins-since-last-seen, last-10 entropy,
               Markov score (train-frozen transition matrix estimate)
"""
import sqlite3
import sys
from collections import Counter

import numpy as np
import pandas as pd
import lightgbm as lgb

DB = "/home/wa/roulette2_spins.db"
OUT = "/home/wa/projects/RoulCollector448/ml"

WHEEL = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23,
         10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26]
POS = {n: i for i, n in enumerate(WHEEL)}
REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def color_of(n):
    if n == 0:
        return 2  # 0=Red, 1=Black, 2=Green
    return 0 if n in REDS else 1


def dozen_of(n):
    return 0 if n == 0 else (n - 1) // 12 + 1  # 0,1,2,3


def zone_of(n):
    return POS[n] // 3  # 13 zones of 3 wheel positions


def sector_of(n):
    return POS[n] // 13  # 3 wheel thirds (13/13/11)


def wheel_dist(a, b):
    d = abs(POS[a] - POS[b])
    return min(d, 37 - d)


def nn_cluster(n):
    p = POS[n]
    return {WHEEL[(p + k) % 37] for k in (-2, -1, 0, 1, 2)}


def entropy(xs):
    c = Counter(xs)
    n = len(xs)
    return -sum((v / n) * np.log2(v / n) for v in c.values())


def load():
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT number FROM roulette_spins ORDER BY id"
    ).fetchall()
    con.close()
    return np.array([r[0] for r in rows], dtype=int)


def build_features(nums):
    N = len(nums)
    n = N - 1
    cols = {k: np.zeros(n, dtype=np.float32) for k in [
        "last_num", "last_color", "last_dozen", "last_zone", "last_sector",
        "hot50", "wheel_pos", "wheel_dist", "same_num", "same_color",
        "same_dozen", "c5_num", "c10_num", "c25_num", "c50_num",
        "c5_col", "c10_col", "c25_col", "c50_col", "c5_doz", "c10_doz",
        "c25_doz", "c50_doz", "c5_zone", "c10_zone", "c25_zone", "c50_zone",
        "nn5", "nn10", "num_streak", "col_streak", "gap_last", "ent10",
        "markov_pred", "markov_conf",
    ]}
    y_num = np.zeros(n, dtype=int)
    y_col = np.zeros(n, dtype=int)

    colors = np.array([color_of(x) for x in nums], dtype=np.int8)
    dozens = np.array([dozen_of(x) for x in nums], dtype=np.int8)
    zones = np.array([zone_of(x) for x in nums], dtype=np.int8)
    sectors = np.array([sector_of(x) for x in nums], dtype=np.int8)
    positions = np.array([POS[x] for x in nums], dtype=np.int8)
    col_by_num = np.array([color_of(n) for n in range(37)], dtype=np.int8)
    doz_by_num = np.array([dozen_of(n) for n in range(37)], dtype=np.int8)
    zone_by_num = np.array([zone_of(n) for n in range(37)], dtype=np.int8)

    last_seen = {}
    num_streak = 1
    col_streak = 1
    # running histograms: hist[k] = counts of numbers in last k spins (k in 5/10/25/50)
    hist5, hist10, hist25, hist50 = {}, {}, {}, {}

    for i in range(1, N):
        cur, prev = nums[i], nums[i - 1]
        j = i - 1
        cols["last_num"][j] = prev
        cols["last_color"][j] = colors[i - 1]
        cols["last_dozen"][j] = dozens[i - 1]
        cols["last_zone"][j] = zones[i - 1]
        cols["last_sector"][j] = sectors[i - 1]
        cols["wheel_pos"][j] = positions[i - 1]
        if i >= 2:
            d = abs(int(positions[i - 1]) - int(positions[i - 2]))
            cols["wheel_dist"][j] = min(d, 37 - d)
            cols["same_num"][j] = prev == nums[i - 2]
            cols["same_color"][j] = colors[i - 1] == colors[i - 2]
            cols["same_dozen"][j] = dozens[i - 1] == dozens[i - 2]
        else:
            cols["wheel_dist"][j] = 18
        if i >= 2 and prev == nums[i - 2]:
            num_streak += 1
        else:
            num_streak = 1
        if i >= 2 and colors[i - 1] == colors[i - 2]:
            col_streak += 1
        else:
            col_streak = 1
        cols["num_streak"][j] = min(num_streak, 20)
        cols["col_streak"][j] = min(col_streak, 20)
        last = last_seen.get(prev)
        cols["gap_last"][j] = min(i - 1 - last, 200) if last is not None else 200
        last_seen[prev] = i - 1
        # windows + running histograms
        if i > 5:
            hist5[nums[i - 6]] = hist5.get(nums[i - 6], 1) - 1
        else:
            hist5 = {}
        if i > 10:
            hist10[nums[i - 11]] = hist10.get(nums[i - 11], 1) - 1
        if i > 25:
            hist25[nums[i - 26]] = hist25.get(nums[i - 26], 1) - 1
        if i > 50:
            hist50[nums[i - 51]] = hist50.get(nums[i - 51], 1) - 1
        for h in (hist5, hist10, hist25, hist50):
            h[prev] = h.get(prev, 0) + 1
        cols["c5_num"][j] = hist5.get(prev, 0)
        cols["c10_num"][j] = hist10.get(prev, 0)
        cols["c25_num"][j] = hist25.get(prev, 0)
        cols["c50_num"][j] = hist50.get(prev, 0)
        pc = colors[i - 1]
        cols["c5_col"][j] = sum(v for k, v in hist5.items() if col_by_num[k] == pc)
        cols["c10_col"][j] = sum(v for k, v in hist10.items() if col_by_num[k] == pc)
        cols["c25_col"][j] = sum(v for k, v in hist25.items() if col_by_num[k] == pc)
        cols["c50_col"][j] = sum(v for k, v in hist50.items() if col_by_num[k] == pc)
        pd_ = dozens[i - 1]
        cols["c5_doz"][j] = sum(v for k, v in hist5.items() if doz_by_num[k] == pd_)
        cols["c10_doz"][j] = sum(v for k, v in hist10.items() if doz_by_num[k] == pd_)
        cols["c25_doz"][j] = sum(v for k, v in hist25.items() if doz_by_num[k] == pd_)
        cols["c50_doz"][j] = sum(v for k, v in hist50.items() if doz_by_num[k] == pd_)
        pz = zones[i - 1]
        cols["c5_zone"][j] = sum(v for k, v in hist5.items() if zone_by_num[k] == pz)
        cols["c10_zone"][j] = sum(v for k, v in hist10.items() if zone_by_num[k] == pz)
        cols["c25_zone"][j] = sum(v for k, v in hist25.items() if zone_by_num[k] == pz)
        cols["c50_zone"][j] = sum(v for k, v in hist50.items() if zone_by_num[k] == pz)
        cl = nn_cluster(prev)
        cols["nn5"][j] = sum(v for k, v in hist5.items() if k in cl)
        cols["nn10"][j] = sum(v for k, v in hist10.items() if k in cl)
        if i > 50:
            hot = max(hist50, key=hist50.get)
        else:
            hot = prev
        cols["hot50"][j] = hot
        if i >= 10:
            cols["ent10"][j] = -sum((v / 10) * np.log2(v / 10) for v in hist10.values() if v > 0)
        y_num[j] = cur
        y_col[j] = colors[i]

    df = pd.DataFrame(cols)
    df["y_num"] = y_num
    df["y_col"] = y_col
    return df


def freeze_markov(df_train, nums_train):
    """Fill markov column using train-only transition counts."""
    tmat = np.zeros((37, 37))
    for a, b in zip(nums_train[:-1], nums_train[1:]):
        tmat[a, b] += 1
    rowsum = tmat.sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        probs = np.where(rowsum[:, None] > 0, tmat / rowsum[:, None, np.newaxis][:, 0] if False else tmat / rowsum[:, None], 0.0)
    df_train["markov"] = [probs[a, b] for a, b in zip(nums_train[:-1], nums_train[1:])]
    return probs


def main():
    nums = load()
    N = len(nums)
    print(f"spins: {N}")
    df = build_features(nums)
    split = int(N * 0.7)
    tr, te = df.iloc[: split - 1], df.iloc[split - 1:]
    nums_tr, nums_te = nums[:split], nums[split:]

    # freeze Markov probabilities from train transitions
    tmat = np.zeros((37, 37))
    for a, b in zip(nums_tr[:-1], nums_tr[1:]):
        tmat[a, b] += 1
    rowsum = tmat.sum(axis=1)
    probs = np.divide(tmat, rowsum[:, None], out=np.zeros_like(tmat), where=rowsum[:, None] > 0)
    # Markov features WITHOUT target leakage: argmax class + its probability
    markov_pred = probs.argmax(axis=1)
    markov_conf = probs.max(axis=1)
    tr.loc[:, "markov_pred"] = [markov_pred[a] for a in nums_tr[:-1]]
    tr.loc[:, "markov_conf"] = [markov_conf[a] for a in nums_tr[:-1]]
    te.loc[:, "markov_pred"] = [markov_pred[a] for a in nums[split - 1:-1]]
    te.loc[:, "markov_conf"] = [markov_conf[a] for a in nums[split - 1:-1]]

    feat_cols = [c for c in df.columns if not c.startswith("y_")]
    cat_cols = ["last_num", "last_color", "last_dozen", "last_zone", "last_sector", "hot50", "markov_pred"]

    # ---- baselines ----
    maj_num = Counter(nums_tr.tolist()).most_common(1)[0][0]
    maj_acc = np.mean(nums_te == maj_num)
    markov_pred = np.array([int(np.argmax(probs[a])) for a in nums_te[:-1]])
    markov_acc = np.mean(markov_pred == nums_te[1:])
    nn_rate = np.mean([nums_te[i + 1] in nn_cluster(nums_te[i]) for i in range(len(nums_te) - 1)])
    col_maj = 0 if sum(color_of(x) == 0 for x in nums_te) > sum(color_of(x) == 1 for x in nums_te) else 1
    col_maj_acc = np.mean([color_of(x) == col_maj for x in nums_te])
    print(f"baselines | majority #{maj_num}: {maj_acc:.4f} | markov: {markov_acc:.4f} | "
          f"uniform: {1/37:.4f} | Nn follow: {nn_rate:.4f} (fair 13.5%) | "
          f"color majority: {col_maj_acc:.4f} (fair 18/37={18/37:.4f})")

    # ---- LightGBM: next number (37-class) ----
    X_tr, y_tr = tr[feat_cols], tr["y_num"]
    X_va, y_va = X_tr.iloc[-int(0.1 * len(X_tr)):], y_tr.iloc[-int(0.1 * len(y_tr)):]
    X_tr_f, y_tr_f = X_tr.iloc[:-int(0.1 * len(X_tr))], y_tr.iloc[:-int(0.1 * len(y_tr))]
    dtr = lgb.Dataset(X_tr_f, y_tr_f, categorical_feature=cat_cols)
    dva = lgb.Dataset(X_va, y_va, categorical_feature=cat_cols, reference=dtr)
    params = dict(objective="multiclass", num_class=37, metric="multi_logloss",
                  learning_rate=0.05, num_leaves=31, min_data_in_leaf=40,
                  feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
                  verbosity=-1, num_threads=8)
    bst = lgb.train(params, dtr, num_boost_round=600, valid_sets=[dva],
                    callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)])
    X_te = te[feat_cols]
    proba = bst.predict(X_te, num_iteration=bst.best_iteration)
    pred = proba.argmax(axis=1)
    y_te = te["y_num"].to_numpy()
    acc = np.mean(pred == y_te)
    top3 = np.mean([y_te[i] in np.argsort(proba[i])[-3:] for i in range(len(y_te))])
    top5 = np.mean([y_te[i] in np.argsort(proba[i])[-5:] for i in range(len(y_te))])
    print(f"LGB number   | acc {acc:.4f} | top3 {top3:.4f} | top5 {top5:.4f} "
          f"(best_iter {bst.best_iteration})")
    bst.save_model(f"{OUT}/model_number.txt")

    # ---- LightGBM: next color (3-class) ----
    yva_c = te["y_col"].to_numpy()
    dtr_c = lgb.Dataset(X_tr_f, tr["y_col"].iloc[:-int(0.1 * len(tr))], categorical_feature=cat_cols)
    dva_c = lgb.Dataset(X_va, tr["y_col"].iloc[-int(0.1 * len(tr)):], categorical_feature=cat_cols, reference=dtr_c)
    bst_c = lgb.train(dict(objective="multiclass", num_class=3, metric="multi_logloss",
                           learning_rate=0.05, num_leaves=31, min_data_in_leaf=40,
                           feature_fraction=0.8, bagging_fraction=0.8, bagging_freq=1,
                           verbosity=-1, num_threads=8),
                      dtr_c, num_boost_round=600, valid_sets=[dva_c],
                      callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)])
    proba_c = bst_c.predict(X_te, num_iteration=bst_c.best_iteration)
    pred_c = proba_c.argmax(axis=1)
    acc_c = np.mean(pred_c == yva_c)
    print(f"LGB color    | acc {acc_c:.4f} (fair {18/37:.4f}) | best_iter {bst_c.best_iteration}")
    bst_c.save_model(f"{OUT}/model_color.txt")

    # ---- neighbor-pair diagnostics from the transition matrix ----
    print("\ntop from->to transitions (full dataset):")
    full = np.zeros((37, 37))
    for a, b in zip(nums[:-1], nums[1:]):
        full[a, b] += 1
    order = np.dstack(np.unravel_index(np.argsort(full.ravel())[::-1], (37, 37)))[0]
    for a, b in order[:10]:
        if full[a, b] == 0:
            continue
        print(f"  {a:2d} -> {b:2d}   n={int(full[a, b])}")
    wheel_nn = np.mean([nums[i + 1] in nn_cluster(nums[i]) for i in range(N - 1)])
    print(f"wheel-neighbor follow rate: {wheel_nn:.4f} (fair 5/37={5/37:.4f})")

    # importance
    imp = sorted(zip(feat_cols, bst.feature_importance("gain")), key=lambda x: -x[1])[:12]
    print("\ntop features (gain):")
    for f, v in imp:
        print(f"  {f:12s} {v:12.0f}")

    # feature importance for color model
    imp_c = sorted(zip(feat_cols, bst_c.feature_importance("gain")), key=lambda x: -x[1])[:6]
    print("top features color (gain):")
    for f, v in imp_c:
        print(f"  {f:12s} {v:12.0f}")

    with open(f"{OUT}/train_report.txt", "w") as fh:
        fh.write(f"spins={N} train={len(tr)} test={len(te)}\n")
        fh.write(f"baseline majority {maj_acc:.4f} | markov {markov_acc:.4f} | uniform {1/37:.4f}\n")
        fh.write(f"LGB number acc {acc:.4f} top3 {top3:.4f} top5 {top5:.4f}\n")
        fh.write(f"LGB color acc {acc_c:.4f} (fair {18/37:.4f})\n")
        fh.write(f"Nn follow {nn_rate:.4f} fair {5/37:.4f}\n")
    print(f"\nmodels saved: {OUT}/model_number.txt, {OUT}/model_color.txt")


if __name__ == "__main__":
    sys.exit(main())
