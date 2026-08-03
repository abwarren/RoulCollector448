"""API tests against the deterministic fixture DB (1110 spins, 30/cycle)."""

from fastapi.testclient import TestClient

from backend.app import app
from backend.wheel import nn_cluster

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["total_spins"] == 1110
    assert d["collector_alive"] is True
    assert d["last_spin"]["number"] == 36
    assert d["db_age_seconds"] < 180


def test_spins_count():
    assert client.get("/api/spins/count").json() == {"total": 1110}


def test_spins_window_newest_first():
    d = client.get("/api/spins?offset=0&limit=2000").json()
    assert d["total"] == 1110
    assert d["returned"] == 1110
    nums = [s["number"] for s in d["spins"]]
    assert nums[0] == 0 and nums[-1] == 36  # chronological


def test_spins_offset_skips_newest():
    d = client.get("/api/spins?offset=100&limit=50").json()
    nums = [s["number"] for s in d["spins"]]
    # newest 50 spins are 87..36 (cycle tail); offset 100 skips them
    assert len(nums) == 50
    assert nums[-1] == 36 - 100 % 37  # sanity: last of window


def test_neighbors_0_is_user_example():
    # User requirement: clicking 0 highlights {0, 3, 15, 32, 26}
    d = client.get("/api/neighbors/0").json()
    assert sorted(d["cluster"]) == [0, 3, 15, 26, 32]


def test_neighbors_10():
    d = client.get("/api/neighbors/10").json()
    assert sorted(d["cluster"]) == [5, 8, 10, 23, 24]


def test_neighbors_invalid():
    assert client.get("/api/neighbors/37").status_code == 400


def test_stats_numbers_all_time():
    d = client.get("/api/stats/numbers").json()
    assert d["total"] == 1110
    assert len(d["numbers"]) == 37
    assert sum(x["hits"] for x in d["numbers"]) == 1110
    assert all(x["hits"] == 30 for x in d["numbers"])  # deterministic fixture
    # sorted by |z| desc
    zs = [abs(x["z"]) for x in d["numbers"]]
    assert zs == sorted(zs, reverse=True)


def test_stats_numbers_last100():
    d = client.get("/api/stats/numbers?limit=100").json()
    assert d["total"] == 100
    assert sum(x["hits"] for x in d["numbers"]) == 100


def test_stats_sleepers_sorted():
    d = client.get("/api/stats/sleepers").json()
    assert d["total"] == 1110
    gaps = [x["gap"] for x in d["sleepers"]]
    assert gaps == sorted(gaps, reverse=True)
    # cycling 0..36: number 0 last hit 36 spins ago, 36 hit last spin
    by_num = {x["number"]: x["gap"] for x in d["sleepers"]}
    assert by_num[0] == 36
    assert by_num[36] == 0


def test_sleepers_live_merge():
    # 5 uncommitted journald spins after the fixture's last DB spin (36):
    # positions 1111..1115 -> 12, 7, 12, 3, 9 (12's LAST hit is at 1113).
    from backend.db import connect
    from backend.stats import sleepers

    conn = connect()
    try:
        live = [
            {"number": 12, "time": "23:59:58"},
            {"number": 7, "time": "00:00:42"},
            {"number": 12, "time": "00:01:26"},
            {"number": 3, "time": "00:02:10"},
            {"number": 9, "time": "00:02:54"},
        ]
        merged = sleepers(conn, live_spins=live)
        m = {x["number"]: x for x in merged["sleepers"]}
        assert merged["total"] == 1110  # DB count unchanged
        assert merged["live"] == 5
        # live hits: gap counts only spins AFTER their last live occurrence
        assert m[9]["gap"] == 0    # hit last spin (1115)
        assert m[3]["gap"] == 1    # hit 1114
        assert m[12]["gap"] == 2   # last hit 1113 (also hit 1111)
        assert m[7]["gap"] == 3    # hit 1112
        # DB-only numbers: 5 virtual spins push their gaps up by 5
        assert m[36]["gap"] == 5   # was 0
        assert m[0]["gap"] == 41   # was 36
        # live last_hit_at carries the live time (today/rollover-safe ISO)
        assert m[9]["last_hit_at"].endswith("T00:02:54")
        # empty live list == DB-only behavior
        base = sleepers(conn)
        assert base == sleepers(conn, live_spins=[])
    finally:
        conn.close()


def test_stats_streaks_shape():
    # Fixture cycles 0..36 by NUMBER, so color runs come from number-color
    # adjacency (e.g. 17,18,19 are all Red; 28,29 both Black). Compute the
    # exact expected runs from the deterministic sequence and compare.
    REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
    colors = ["Green" if n == 0 else ("Red" if n in REDS else "Black")
              for n in ((i % 37) for i in range(1110))]
    longest = {"Red": 0, "Black": 0, "Green": 0}
    run, prev = 1, colors[0]
    for c in colors[1:]:
        if c == prev:
            run += 1
        else:
            longest[prev] = max(longest[prev], run)
            run, prev = 1, c
    longest[prev] = max(longest[prev], run)

    d = client.get("/api/stats/streaks").json()
    assert d["total_spins"] == 1110
    assert d["longest"] == longest
    assert d["current"]["length"] >= 1
    assert d["longest_number_repeat"] == 1  # cycling never repeats
    assert d["repeat_counts"]["doubles"] == 0


def test_stats_rolling_500():
    d = client.get("/api/stats/rolling?window=500").json()
    assert d["total"] == 500
    assert len(d["numbers"]) == 37
    assert sum(x["hits"] for x in d["numbers"]) == 500
    cb = d["color_balance"]
    assert cb["Red"] + cb["Black"] + cb["Green"] == 500
    # neighbor rate: compare against the exact fixture expectation
    # (last 500 spins = indices 610..1109 of the 1110-spin cycle)
    from backend.wheel import nn_cluster
    seq = [((610 + i) % 37) for i in range(500)]
    exp = sum(1 for a, b in zip(seq, seq[1:]) if b in nn_cluster(a)) / 499
    assert abs(d["neighbor_rate"] - exp) < 1e-4  # API rounds to 4dp


def test_stats_rolling_validation():
    assert client.get("/api/stats/rolling?window=10").status_code == 422


def test_audit_shape():
    d = client.get("/api/audit").json()
    assert d["window"] == 500
    a = d["audit"]
    assert a["all_time"]["total"] == 1110
    assert a["last_window"]["total"] == 500
    assert len(a["drift"]) == 10
    assert isinstance(a["rotated_hot"], list)
    assert isinstance(a["rotated_cold"], list)
    # deterministic fixture: last 500 is also near-uniform -> small drift
    assert max(abs(x["delta"]) for x in a["drift"]) < 2.0


def test_frontend_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "RoulCollector448" in r.text or "TABLE 448" in r.text


def test_nn_cluster_math():
    # Nn(0) = N + 2 left + 2 right on the wheel (user example: 0,3,15,32,26)
    assert sorted(nn_cluster(0)) == [0, 3, 15, 26, 32]
    assert len(nn_cluster(15)) == 5
    assert 15 in nn_cluster(15)
    # wheel is a closed ring: 0's left neighbours wrap to the wheel tail
    c = nn_cluster(0)
    assert 3 in c and 26 in c  # two left neighbours (wrap-around)
    assert 32 in c and 15 in c  # two right neighbours
