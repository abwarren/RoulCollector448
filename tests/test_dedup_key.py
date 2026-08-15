"""Storage-level uniqueness (dedup_key) — the PRD's game_id-uniqueness gap.

game_id UNIQUE is good but insufficient if malformed/missing ids occur:
  * the same spin arriving under an empty/different game_id would duplicate
  * an empty-string game_id would collide with EVERY other empty-id spin

The fix: dedup_key — 'gid:<game_id>' when valid, else 'tsn:<server_ts>|<n>',
enforced by a partial unique index. Rows with no establishable identity are
never inserted canonically (kept as observations, surfaced).

Env must be set BEFORE importing the collector (module-level paths are
resolved at import).
"""

import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["RC_DB_PATH"] = os.path.join(_tmp, "d.db")
os.environ["RC_STATE_FILE"] = os.path.join(_tmp, "s.json")
os.environ["RC_CSV_FILE"] = os.path.join(_tmp, "s.csv")
os.environ.setdefault("SUNBET_USER", "test")
os.environ.setdefault("SUNBET_PASS", "test")

import pytest  # noqa: E402

from collector import schema  # noqa: E402
import collector.roulette2_collector as rc  # noqa: E402


# ---------------------------------------------------------------------------
# canonical_dedup_key
# ---------------------------------------------------------------------------
def test_dedup_key_game_id_normalized():
    assert schema.canonical_dedup_key("  g1  ") == "gid:g1"
    assert schema.canonical_dedup_key("GAME123") == "gid:GAME123"


def test_dedup_key_falls_back_to_tsn():
    assert schema.canonical_dedup_key("", "2026-08-15T23:00:00Z", 17) == \
        "tsn:2026-08-15T23:00:00Z|17"
    assert schema.canonical_dedup_key("  ", "2026-08-15T23:00:00Z", 0) == \
        "tsn:2026-08-15T23:00:00Z|0"


def test_dedup_key_none_when_no_identity():
    assert schema.canonical_dedup_key(None) is None
    assert schema.canonical_dedup_key("") is None
    assert schema.canonical_dedup_key("", None, None) is None
    assert schema.canonical_dedup_key(None, "ts", None) is None


# ---------------------------------------------------------------------------
# migration: legacy table gains dedup_key + unique index + backfill
# ---------------------------------------------------------------------------
def test_migration_adds_dedup_key_and_backfills(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    # legacy table WITHOUT dedup_key (the pre-fix shape)
    conn.execute(
        """CREATE TABLE roulette_spins (
            id INTEGER PRIMARY KEY AUTOINCREMENT, number INTEGER NOT NULL,
            description TEXT NOT NULL, color TEXT NOT NULL,
            game_id TEXT NOT NULL UNIQUE, server_ts TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
    )
    conn.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at) VALUES (17,'17 Black','Black','g1','t','t')"
    )
    conn.execute(
        "INSERT INTO roulette_spins (number, description, color, game_id, "
        "server_ts, captured_at) VALUES (0,'0 Green','Green','g2','t','t')"
    )
    conn.commit()

    schema.ensure_schema(conn)
    rows = conn.execute(
        "SELECT game_id, dedup_key FROM roulette_spins ORDER BY game_id"
    ).fetchall()
    assert [(r["game_id"], r["dedup_key"]) for r in rows] == \
        [("g1", "gid:g1"), ("g2", "gid:g2")]  # backfilled

    # the unique index exists and is enforced
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO roulette_spins (number, description, color, game_id, "
            "server_ts, captured_at, dedup_key) "
            "VALUES (17,'17 Black','Black','g3','t','t','gid:g1')"
        )
    conn.close()


# ---------------------------------------------------------------------------
# save_spins: malformed game_id dedupes by ts+number
# ---------------------------------------------------------------------------
def _spin(number, gid, ts):
    return {"number": number, "description": f"{number} X", "gameId": gid,
            "timestamp": ts, "captured_at": ts}


def test_save_spins_dedup_by_tsn_when_game_id_malformed(monkeypatch):
    db = os.path.join(_tmp, "save1.db")
    monkeypatch.setattr(rc, "DB_FILE", db)
    monkeypatch.setattr(rc, "STATE_FILE", os.path.join(_tmp, "s1.json"))
    monkeypatch.setattr(rc, "CSV_FILE", os.path.join(_tmp, "s1.csv"))
    ts = "2026-08-15T23:00:00Z"
    # same spin, EMPTY game_id both times (the malformed case) — same
    # ts+number -> dedup_key collides -> ONE canonical row
    rc.save_spins([_spin(17, "", ts), _spin(17, "", ts)])
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
    assert n == 1
    conn.close()


def test_save_spins_skips_identity_less(monkeypatch):
    db = os.path.join(_tmp, "save2.db")
    monkeypatch.setattr(rc, "DB_FILE", db)
    monkeypatch.setattr(rc, "STATE_FILE", os.path.join(_tmp, "s2.json"))
    monkeypatch.setattr(rc, "CSV_FILE", os.path.join(_tmp, "s2.csv"))
    # no game_id AND no timestamp -> no identity -> NOT canonical
    rc.save_spins([{"number": 17, "description": "17 X", "gameId": None,
                    "timestamp": None, "captured_at": "t"}])
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
    assert n == 0
    conn.close()


def test_save_spins_normal_game_id_dedup(monkeypatch):
    db = os.path.join(_tmp, "save3.db")
    monkeypatch.setattr(rc, "DB_FILE", db)
    monkeypatch.setattr(rc, "STATE_FILE", os.path.join(_tmp, "s3.json"))
    monkeypatch.setattr(rc, "CSV_FILE", os.path.join(_tmp, "s3.csv"))
    ts = "2026-08-15T23:00:00Z"
    # same game_id -> deduped (existing game_id UNIQUE behavior preserved)
    rc.save_spins([_spin(17, "g1", ts), _spin(17, "g1", ts)])
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM roulette_spins").fetchone()[0]
    assert n == 1
    conn.close()
