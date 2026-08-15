"""PRD §19 — capture-latency API contract.

The capture-latency panel renders the `latency` block of /api/health, which
the backend copies verbatim from the collector heartbeat file (hb_raw.latency).
This test monkeypatches backend.liveness.get_liveness so /api/health serves a
deterministic heartbeat with latency stats, and asserts the frontend-facing
shape: capture P50/P95/P99/max + n and commit P50/P95/P99/max + n, with null
when no heartbeat has been seen yet.
"""

import pytest
from fastapi.testclient import TestClient

import backend.liveness
from backend.app import app

client = TestClient(app)

FAKE_LIVENESS = {
    "ok": True,
    "source": "heartbeat",
    "age": 1.0,
    "lines": [],
    "session_marker_found": False,
    "hb_status": "RUNNING",
    "last_spin": None,
    "hb_raw": {
        "recent_spins": [],
        "latency": {
            "p50": 0.4,
            "p95": 0.8,
            "p99": 1.2,
            "max": 2.0,
            "n": 500,
            "commit_p50": 0.1,
            "commit_p95": 0.3,
            "commit_p99": 0.5,
            "commit_max": 1.0,
            "commit_n": 500,
        },
    },
}


@pytest.fixture
def fake_liveness(monkeypatch):
    monkeypatch.setattr(backend.liveness, "get_liveness", lambda force=False: FAKE_LIVENESS)
    return FAKE_LIVENESS


def test_health_serves_latency_stats(fake_liveness):
    r = client.get("/api/health")
    assert r.status_code == 200
    d = r.json()
    assert d["collector_alive"] is True
    lat = d["latency"]
    assert lat is not None
    # capture percentiles (seconds) + sample count
    assert lat["p50"] == 0.4
    assert lat["p95"] == 0.8
    assert lat["p99"] == 1.2
    assert lat["max"] == 2.0
    assert lat["n"] == 500
    # commit percentiles + sample count
    assert lat["commit_p50"] == 0.1
    assert lat["commit_p95"] == 0.3
    assert lat["commit_p99"] == 0.5
    assert lat["commit_max"] == 1.0
    assert lat["commit_n"] == 500


def test_health_latency_null_without_heartbeat(monkeypatch):
    """No heartbeat file yet -> the frontend renders '—' (contract: null, not 500)."""
    monkeypatch.setattr(
        backend.liveness,
        "get_liveness",
        lambda force=False: {
            "ok": False,
            "source": None,
            "age": None,
            "lines": [],
            "session_marker_found": False,
            "hb_status": None,
            "last_spin": None,
            "hb_raw": None,
        },
    )
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["latency"] is None
