"""PRD §38 (metrics endpoints) + §39 (alert conditions).

§38 exposes four integrity endpoints:
  /api/integrity            aggregate (score, verified, status, alerts, ...)
  /api/integrity/window     rolling-500 breakdown (§26/§27)
  /api/integrity/incidents  incident feed (§37)
  /api/integrity/repairs    repair queue history (§23)

§39 generates alerts from DB + heartbeat state:
  CRITICAL: two consecutive reconciliation failures, unverified > 0,
            conflicting game IDs, rolling-500 verification < 100%
  WARNING:  capture latency increasing, DOM/WS disagreement, repeated
            recovery events, repair frequency increasing
"""

import json
import os
import sqlite3

import pytest

from collector import observer, schema

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "alerts.db")
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    yield c
    c.close()


def _spin(c, gid, num, seq, status="VALID"):
    color = "Green" if num == 0 else ("Red" if num in REDS else "Black")
    c.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at, sequence_no, status) VALUES (?,?,?,?,?,?,?,?)",
        (num, f"{num} {color}", color, gid, f"t{seq}", f"t{seq}", seq, status),
    )
    c.commit()


def _clean_500(c):
    for i in range(1, 501):
        _spin(c, f"g{i}", i % 37, i)


# ---------------------------------------------------------------------------
# §39 — alert conditions
# ---------------------------------------------------------------------------
def test_clean_dataset_no_alerts(conn):
    from backend.app import evaluate_alerts
    _clean_500(conn)
    observer.log_event(conn, "RECONCILIATION", severity="INFO",
                       details={"ok": True, "score": 100},
                       root_cause="RECONCILIATION")
    alerts = evaluate_alerts(conn)
    assert alerts == []


def test_two_consecutive_reconciliation_failures_critical(conn):
    from backend.app import evaluate_alerts
    _clean_500(conn)
    observer.log_event(conn, "RECONCILIATION", severity="CRITICAL",
                       details={"ok": False}, root_cause="RECONCILIATION")
    observer.log_event(conn, "RECONCILIATION", severity="CRITICAL",
                       details={"ok": False}, root_cause="RECONCILIATION")
    alerts = evaluate_alerts(conn)
    conds = [a["condition"] for a in alerts]
    assert "two_consecutive_reconciliation_failures" in conds
    crit = [a for a in alerts
            if a["condition"] == "two_consecutive_reconciliation_failures"][0]
    assert crit["severity"] == "CRITICAL"


def test_unverified_records_critical(conn):
    from backend.app import evaluate_alerts
    for i in range(1, 500):
        _spin(conn, f"g{i}", i % 37, i)
    _spin(conn, "g500", 7, 500, status="UNVERIFIED")
    alerts = evaluate_alerts(conn)
    conds = [a["condition"] for a in alerts]
    assert "unverified_records" in conds
    assert "rolling_500_verification_lt_100" in conds


def test_conflicting_game_ids_critical(conn):
    from backend.app import evaluate_alerts
    from collector import schema as _s
    c = conn
    # a legacy no-UNIQUE table is the only place conflicts can exist
    c.execute("DROP TABLE roulette_spins")
    c.execute(
        """CREATE TABLE roulette_spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER NOT NULL,
            description TEXT NOT NULL, color TEXT NOT NULL,
            game_id TEXT NOT NULL, server_ts TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    _s.ensure_schema(c)
    for i in range(1, 499):
        _spin(c, f"g{i}", i % 37, i)
    _spin(c, "gX", 17, 499)
    _spin(c, "gX", 23, 500)   # same id, different number -> conflict
    alerts = evaluate_alerts(c)
    conds = [a["condition"] for a in alerts]
    assert "conflicting_game_ids" in conds
    # (the legacy table has no status column -> all rows count verified,
    # so lt_100 does NOT fire here; the conflict itself is the alert)
    crit = [a for a in alerts
            if a["condition"] == "conflicting_game_ids"][0]
    assert crit["severity"] == "CRITICAL"


def test_dom_ws_disagreement_warning(conn):
    from backend.app import evaluate_alerts
    _clean_500(conn)
    observer.log_event(conn, "SOURCE_DISAGREEMENT", severity="WARNING",
                       details={"ws": 17, "dom": 18},
                       root_cause="DATA_INTEGRITY")
    alerts = evaluate_alerts(conn)
    conds = [a["condition"] for a in alerts]
    assert "dom_ws_disagreement" in conds
    warn = [a for a in alerts if a["condition"] == "dom_ws_disagreement"][0]
    assert warn["severity"] == "WARNING"


def test_repair_frequency_increasing_warning(conn):
    from backend.app import evaluate_alerts
    from collector.repairer import Repairer
    _clean_500(conn)
    # 2 repairs an hour ago, 5 in the current hour -> 5 > 2*2 -> warning
    r = Repairer(conn)
    import datetime
    for i in range(5):
        ev = r.record_gap(start_seq=1000 + i, end_seq=1000 + i, size=1)
        r.resolve_gap(ev, status="RESOLVED", resolution="REPAIRED")
    conn.execute("UPDATE repair_events SET created_at = "
                 "datetime('now', '-5 minutes')")
    for i in range(2):
        ev = r.record_gap(start_seq=2000 + i, end_seq=2000 + i, size=1)
        r.resolve_gap(ev, status="RESOLVED", resolution="REPAIRED")
    conn.execute("UPDATE repair_events SET created_at = "
                 "datetime('now', '-90 minutes') WHERE id IN "
                 "(SELECT id FROM repair_events ORDER BY id LIMIT 2)")
    conn.commit()
    alerts = evaluate_alerts(conn)
    conds = [a["condition"] for a in alerts]
    assert "repair_frequency_increasing" in conds


def test_capture_latency_increasing_warning(tmp_path, monkeypatch):
    from backend.app import evaluate_alerts
    db = tmp_path / "lat.db"
    monkeypatch.setenv("RC_DB_PATH", str(db))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    schema.ensure_schema(conn)
    _clean_500(conn)
    conn.close()
    # heartbeat with a high P99 (> 10s)
    hb = tmp_path / "hb.json"
    hb.write_text(json.dumps({
        "at": "2026-08-15T12:00:00+00:00", "status": "RUNNING",
        "latency": {"capture_p50": 1.2, "capture_p99": 25.0},
    }))
    monkeypatch.setenv("RC_HEARTBEAT_FILE", str(hb))
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        alerts = evaluate_alerts(conn)
        conds = [a["condition"] for a in alerts]
        assert "capture_latency_increasing" in conds
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# §38 — metrics endpoints
# ---------------------------------------------------------------------------
def test_api_integrity_aggregate_shape(tmp_path, monkeypatch):
    """GET /api/integrity carries score, verified, status, alerts (the §38
    example shape)."""
    import backend.db
    from fastapi.testclient import TestClient
    from backend.app import app
    db_path = tmp_path / "full.db"
    monkeypatch.setenv("RC_DB_PATH", str(db_path))
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    _clean_500(c)
    observer.log_event(c, "RECONCILIATION", severity="INFO",
                       details={"ok": True, "score": 100, "window": 500},
                       root_cause="RECONCILIATION")
    c.close()
    monkeypatch.setattr(backend.db, "DB_PATH", str(db_path))
    d = TestClient(app).get("/api/integrity").json()
    for k in ("score", "window", "verified", "missing", "duplicates",
              "conflicts", "unverified", "repaired", "last_reconciliation",
              "status", "rolling500", "alerts"):
        assert k in d
    assert d["status"] == "VERIFIED"
    assert d["alerts"] == []


def test_api_integrity_window_endpoint(tmp_path, monkeypatch):
    import backend.db
    from fastapi.testclient import TestClient
    from backend.app import app
    db_path = tmp_path / "win.db"
    monkeypatch.setenv("RC_DB_PATH", str(db_path))
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    _clean_500(c)
    c.close()
    monkeypatch.setattr(backend.db, "DB_PATH", str(db_path))
    d = TestClient(app).get("/api/integrity/window").json()
    assert d["ok"] is True
    assert d["verified_label"] == "500 / 500 verified"
    assert d["perfect"] is True
    assert d["checked"] == 500


def test_api_integrity_repairs_endpoint(tmp_path, monkeypatch):
    import backend.db
    from collector.repairer import Repairer
    from fastapi.testclient import TestClient
    from backend.app import app
    db_path = tmp_path / "rep.db"
    monkeypatch.setenv("RC_DB_PATH", str(db_path))
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    _clean_500(c)
    ev = Repairer(c).record_gap(start_seq=999, end_seq=999, size=1)
    Repairer(c).resolve_gap(ev, status="RESOLVED", resolution="REPAIRED")
    c.close()
    monkeypatch.setattr(backend.db, "DB_PATH", str(db_path))
    d = TestClient(app).get("/api/integrity/repairs").json()
    assert d["ok"] is True
    reps = d["repairs"]
    assert len(reps) == 1
    assert reps[0]["incident_type"] == "GAP"
    assert reps[0]["status"] == "RESOLVED"
    assert reps[0]["resolution"] == "REPAIRED"
    assert reps[0]["affected_count"] == 1


def test_api_integrity_incidents_endpoint(tmp_path, monkeypatch):
    import backend.db
    from fastapi.testclient import TestClient
    from backend.app import app
    db_path = tmp_path / "inc.db"
    monkeypatch.setenv("RC_DB_PATH", str(db_path))
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    observer.log_event(c, "SOURCE_DISAGREEMENT", severity="WARNING",
                       details={"ws": 17, "dom": 18},
                       root_cause="DATA_INTEGRITY")
    c.close()
    monkeypatch.setattr(backend.db, "DB_PATH", str(db_path))
    d = TestClient(app).get("/api/integrity/incidents").json()
    assert d["ok"] is True
    assert any(i["type"] == "SOURCE_DISAGREEMENT" for i in d["incidents"])


def test_api_integrity_alerts_endpoint(tmp_path, monkeypatch):
    import backend.db
    from fastapi.testclient import TestClient
    from backend.app import app
    db_path = tmp_path / "al.db"
    monkeypatch.setenv("RC_DB_PATH", str(db_path))
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    for i in range(1, 500):
        _spin(c, f"g{i}", i % 37, i)
    _spin(c, "g500", 7, 500, status="UNVERIFIED")
    c.close()
    monkeypatch.setattr(backend.db, "DB_PATH", str(db_path))
    d = TestClient(app).get("/api/integrity/alerts").json()
    assert d["ok"] is True
    conds = [a["condition"] for a in d["alerts"]]
    assert "unverified_records" in conds
    assert "rolling_500_verification_lt_100" in conds
