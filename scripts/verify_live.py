"""Live smoke test against the running dashboard API (no curl pipes)."""

import json
import urllib.request

BASE = "http://127.0.0.1:4480"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


def main():
    h = get("/api/health")
    assert h["ok"] and h["collector_alive"], f"health: {h}"
    print(f"OK health: alive={h['collector_alive']} total={h['total_spins']} "
          f"live={h['live_last_spin']} live_age={h['live_age_seconds']}s "
          f"db_age={h['db_age_seconds']}s")

    n = get("/api/spins/count")
    assert n["total"] == h["total_spins"]
    print(f"OK count: {n['total']}")

    s = get("/api/spins?offset=0&limit=2000")
    assert s["returned"] == 2000 and len(s["spins"]) == 2000
    nums = [x["number"] for x in s["spins"]]
    assert all(0 <= x <= 36 for x in nums)
    print(f"OK spins: {s['returned']} returned, oldest={nums[0]} newest={nums[-1]}")

    nb = get("/api/neighbors/0")
    assert sorted(nb["cluster"]) == [0, 3, 15, 26, 32], nb
    print(f"OK neighbors(0): {sorted(nb['cluster'])} (user requirement)")

    zn = get("/api/stats/numbers")
    assert len(zn["numbers"]) == 37
    assert sum(x["hits"] for x in zn["numbers"]) == zn["total"]
    top = zn["numbers"][:3]
    print(f"OK numbers: total={zn['total']} top3=" +
          ", ".join(f"#{x['number']} z={x['z']:+}" for x in top))

    sl = get("/api/stats/sleepers")
    assert sl["sleepers"][0]["gap"] >= sl["sleepers"][-1]["gap"]
    print(f"OK sleepers: top sleeper #{sl['sleepers'][0]['number']} gap={sl['sleepers'][0]['gap']}")

    st = get("/api/stats/streaks")
    print(f"OK streaks: longest R={st['longest']['Red']} B={st['longest']['Black']} "
          f"G={st['longest']['Green']} current={st['current']['length']}x{st['current']['color']}")

    rw = get("/api/stats/rolling?window=500")
    print(f"OK rolling500: top={rw['top']} bottom={rw['bottom']} "
          f"neighbor_rate={rw['neighbor_rate']}")

    au = get("/api/audit")
    a = au["audit"]
    print(f"OK audit: window={au['window']} rotated_hot={a['rotated_hot']} "
          f"rotated_cold={a['rotated_cold']} top_drift={[(d['number'], d['delta']) for d in a['drift'][:3]]}")

    page = urllib.request.urlopen(BASE + "/", timeout=10).read().decode()
    assert "TABLE 448" in page and "app.js" in page
    js = urllib.request.urlopen(BASE + "/app.js", timeout=10).read().decode()
    assert "nnCluster" in js
    print("OK frontend served (index.html + app.js)")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
