"""PRD §26 — Rolling 500 Verification (core trust indicator) + §27 —
"Perfect Dataset" definition.

The dashboard must show, for the latest 500 canonical spins:
  500 / 500 verified · Missing / Duplicates / Conflicts / Unverified /
  Repaired / Current gap — and the dataset counts as VERIFIED (§27) only
  when the latest 500 have: no unexplained gaps, no duplicate game_ids, no
  conflicting ids, no invalid numbers, nothing UNVERIFIED.
"""

import os
import sqlite3

import pytest

from backend.app import rolling_verify

REDS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


@pytest.fixture()
def conn(tmp_path):
    from collector import schema
    c = sqlite3.connect(tmp_path / "v.db")
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


# ---------------------------------------------------------------------------
# §27 — perfect dataset
# ---------------------------------------------------------------------------
def test_perfect_dataset(conn):
    """500 consecutive spins, all VALID, unique game_ids -> verified 500/500,
    every counter 0, perfect True (the §27 definition)."""
    for i in range(1, 501):
        _spin(conn, f"g{i}", i % 37, i)
    r = rolling_verify(conn)
    assert r["checked"] == 500
    assert r["verified"] == 500
    assert r["verified_label"] == "500 / 500 verified"
    assert r["missing"] == 0
    assert r["duplicates"] == 0
    assert r["conflicts"] == 0
    assert r["unverified"] == 0
    assert r["repaired"] == 0
    assert r["current_gap"] == 0
    assert r["invalid_numbers"] == 0
    assert r["perfect"] is True


def test_window_caps_at_500(conn):
    """600 spins -> only the latest 500 (by sequence) are checked."""
    for i in range(1, 601):
        _spin(conn, f"g{i}", i % 37, i)
    r = rolling_verify(conn)
    assert r["checked"] == 500
    assert r["verified"] == 500


def test_missing_gap_detected(conn):
    """Sequence 1..501 with #250 missing -> missing 1, current_gap 1, not
    perfect (an unexplained gap — §27)."""
    for i in range(1, 502):
        if i == 250:
            continue
        _spin(conn, f"g{i}", i % 37, i)
    r = rolling_verify(conn)
    assert r["missing"] == 1
    assert r["current_gap"] == 1
    assert r["perfect"] is False


def test_largest_gap_run(conn):
    """Two consecutive holes (#300, #301 missing) -> current_gap 2 (the
    largest single run), missing 2."""
    for i in range(1, 503):
        if i in (300, 301):
            continue
        _spin(conn, f"g{i}", i % 37, i)
    r = rolling_verify(conn)
    assert r["missing"] == 2
    assert r["current_gap"] == 2
    assert r["perfect"] is False


def _legacy_nounique(tmp_path):
    """A roulette_spins table WITHOUT the UNIQUE game_id constraint — the
    only place duplicates/conflicts can physically exist (dedup_key + UNIQUE
    block them in the canonical schema)."""
    from collector import schema
    c = sqlite3.connect(tmp_path / "dup.db")
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    c.execute("DROP TABLE roulette_spins")
    c.execute(
        """CREATE TABLE roulette_spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER NOT NULL,
            description TEXT NOT NULL, color TEXT NOT NULL,
            game_id TEXT NOT NULL, server_ts TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
    )
    schema.ensure_schema(c)
    return c


def _spin_legacy(c, gid, num, seq, status="VALID"):
    color = "Green" if num == 0 else ("Red" if num in REDS else "Black")
    c.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at, status) VALUES (?,?,?,?,?,?,?)",
        (num, f"{num} {color}", color, gid, f"t{seq}", f"t{seq}", status),
    )
    c.commit()


def test_duplicate_game_id_detected(tmp_path):
    """The same game_id twice (a dedup failure) -> duplicates 1, not perfect."""
    c = _legacy_nounique(tmp_path)
    try:
        for i in range(1, 499):
            _spin_legacy(c, f"g{i}", i % 37, i)
        _spin_legacy(c, "g10", 17, 499)     # g10 already present -> duplicate
        _spin_legacy(c, "g500", 5, 500)
        r = rolling_verify(c)
        assert r["duplicates"] == 1
        assert r["perfect"] is False
    finally:
        c.close()


def test_conflicting_ids_detected(tmp_path):
    """Same game_id with DIFFERENT numbers -> conflicts 1 (conflicting ids),
    not perfect."""
    c = _legacy_nounique(tmp_path)
    try:
        for i in range(1, 499):
            _spin_legacy(c, f"g{i}", i % 37, i)
        _spin_legacy(c, "gX", 17, 499)
        _spin_legacy(c, "gX", 23, 500)      # same id, different number
        r = rolling_verify(c)
        assert r["conflicts"] == 1
        assert r["perfect"] is False
    finally:
        c.close()


def test_unverified_and_repaired_counted(conn):
    """UNVERIFIED rows are surfaced (not perfect); REPAIRED rows are counted
    as verified AND reported in the repaired counter."""
    for i in range(1, 497):
        _spin(conn, f"g{i}", i % 37, i)
    _spin(conn, "g497", 3, 497, status="REPAIRED")
    _spin(conn, "g498", 5, 498, status="REPAIRED")
    _spin(conn, "g499", 7, 499, status="UNVERIFIED")
    _spin(conn, "g500", 9, 500)
    r = rolling_verify(conn)
    assert r["repaired"] == 2
    assert r["unverified"] == 1
    assert r["verified"] == 499       # 497 VALID + 2 REPAIRED
    assert r["verified_label"] == "499 / 500 verified"
    assert r["perfect"] is False


def test_invalid_number_detected(conn):
    """A number outside 0..36 -> invalid_numbers 1, not perfect."""
    for i in range(1, 500):
        _spin(conn, f"g{i}", i % 37, i)
    _spin(conn, "g500", 99, 500)      # invalid
    r = rolling_verify(conn)
    assert r["invalid_numbers"] == 1
    assert r["perfect"] is False


def test_legacy_table_degrades_gracefully(tmp_path):
    """A legacy table WITHOUT status/sequence_no columns doesn't crash: all
    rows counted verified, ordered by id, gaps unknown (0)."""
    c = sqlite3.connect(tmp_path / "legacy.db")
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE roulette_spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER NOT NULL,
            description TEXT NOT NULL, color TEXT NOT NULL,
            game_id TEXT NOT NULL UNIQUE, server_ts TEXT NOT NULL,
            captured_at TEXT NOT NULL)"""
    )
    for i in range(1, 501):
        c.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at) VALUES (?,?,?,?,?,?)",
            (i % 37, f"{i % 37} X", "Black", f"g{i}", "t", "t"),
        )
    c.commit()
    r = rolling_verify(c)
    assert r["checked"] == 500
    assert r["verified"] == 500
    assert r["missing"] == 0 and r["duplicates"] == 0
    c.close()


# ---------------------------------------------------------------------------
# API contract — /api/integrity carries rolling500
# ---------------------------------------------------------------------------
def test_api_integrity_includes_rolling500(tmp_path, monkeypatch):
    """GET /api/integrity -> rolling500 block with the §26 fields (the
    dashboard's core trust indicator). Uses a full-schema DB (the conftest
    fixture is a legacy table without integrity_events)."""
    import backend.db
    from collector import schema
    from fastapi.testclient import TestClient
    from backend.app import app

    db_path = tmp_path / "full.db"
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    schema.ensure_schema(c)
    for i in range(1, 501):
        num = i % 37
        color = "Green" if num == 0 else ("Red" if num in REDS else "Black")
        c.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at, sequence_no, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (num, f"{num} {color}", color, f"g{i}", f"t{i}", f"t{i}", i, "VALID"),
        )
    c.commit()
    c.close()

    monkeypatch.setattr(backend.db, "DB_PATH", str(db_path))
    client = TestClient(app)
    resp = client.get("/api/integrity")
    assert resp.status_code == 200
    d = resp.json()
    r5 = d.get("rolling500")
    assert r5 is not None
    for k in ("window", "checked", "verified", "verified_label", "missing",
              "duplicates", "conflicts", "unverified", "repaired",
              "current_gap", "invalid_numbers", "perfect"):
        assert k in r5
    assert r5["checked"] == 500
    assert r5["verified_label"] == "500 / 500 verified"
    assert r5["perfect"] is True
