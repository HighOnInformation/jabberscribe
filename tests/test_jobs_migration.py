import sqlite3
from pathlib import Path

import pytest

from jabberscribe.jobs import MIGRATIONS, SCHEMA_VERSION, JobStore, SchemaError

#: The jobs table exactly as schema version 2 (the hardened MVP, feat/v2-pipeline e49c0cb) created it.
V2_SCHEMA = """
CREATE TABLE jobs (
  job_key         TEXT PRIMARY KEY,
  call_id         TEXT NOT NULL,
  conference_id   TEXT,
  status          TEXT NOT NULL,
  stage           TEXT NOT NULL,
  audio_path      TEXT NOT NULL,
  out_dir         TEXT NOT NULL,
  sidecar_json    TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  duration_sec    INTEGER NOT NULL,
  grouped_into    TEXT REFERENCES jobs (job_key),
  attempts        INTEGER NOT NULL DEFAULT 0,
  transient_failures INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  next_attempt_at TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
"""


def _v2_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V2_SCHEMA)
    conn.execute(
        "INSERT INTO jobs (job_key, call_id, status, stage, audio_path, out_dir, sidecar_json, started_at,"
        " duration_sec, created_at, updated_at) VALUES ('old_1042', 'old', 'done', 'output', '/a.wav', '/out',"
        " '{}', '2026-10-07T14:03:11+03:00', 5, '2026-10-07T11:03:11+00:00', '2026-10-07T11:20:00+00:00')"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_a_v2_database_is_upgraded_in_place(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)

    store = JobStore(db)
    store.init_schema()

    job = store.get("old_1042")
    assert (job.status, job.legal_hold, job.hold_reason, job.transient_failures) == ("done", False, None, 0)
    assert _user_version(db) == SCHEMA_VERSION


def test_a_pre_release_v2_database_without_transient_failures_is_upgraded_too(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    conn = sqlite3.connect(db)
    conn.executescript(V2_SCHEMA.replace("  transient_failures INTEGER NOT NULL DEFAULT 0,\n", ""))
    conn.execute("PRAGMA user_version = 2")
    conn.close()

    store = JobStore(db)
    store.init_schema()

    assert _user_version(db) == SCHEMA_VERSION
    conn = sqlite3.connect(db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    conn.close()
    assert {"transient_failures", "legal_hold", "hold_reason"} <= columns


def test_an_upgraded_database_opens_again(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)
    JobStore(db).init_schema()

    store = JobStore(db)
    store.init_schema()

    assert store.get("old_1042") is not None


def test_a_newer_database_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    JobStore(db).init_schema()
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaError, match="fresh file"):
        JobStore(db).init_schema()


def test_upgrade_adds_the_latency_columns(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)

    store = JobStore(db)
    store.init_schema()

    job = store.get("old_1042")
    assert (job.output_at, job.latency_sec) == (None, None)


def _schema(path: Path) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    finally:
        conn.close()


def _columns(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    finally:
        conn.close()


def test_a_refused_newer_database_is_left_untouched(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    before = _schema(db)

    with pytest.raises(SchemaError):
        JobStore(db).init_schema()

    assert _user_version(db) == SCHEMA_VERSION + 1
    assert _schema(db) == before


def test_a_version_below_the_migration_range_is_refused_untouched(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {min(MIGRATIONS) - 1}")
    conn.commit()
    conn.close()
    before = _schema(db)

    with pytest.raises(SchemaError):
        JobStore(db).init_schema()

    assert _user_version(db) == min(MIGRATIONS) - 1
    assert _schema(db) == before


def test_a_step_failing_mid_way_rolls_back(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)
    # The step's first column is added, then the second statement fails.
    first = MIGRATIONS[2][0]
    monkeypatch.setitem(MIGRATIONS, 2, (first, ("hold_reason", "TEXT CHECK (")))

    with pytest.raises(sqlite3.Error):
        JobStore(db).init_schema()

    assert _user_version(db) == 2
    assert first[0] not in _columns(db)
    monkeypatch.undo()
    store = JobStore(db)
    store.init_schema()
    assert store.get("old_1042").legal_hold is False


class _RacingConnection:
    """A connection proxy: another process finishes the upgrade just before this one's BEGIN IMMEDIATE."""

    def __init__(self, conn: sqlite3.Connection, db: Path) -> None:
        self._conn, self._db, self.statements = conn, db, []

    def execute(self, sql: str, *params):
        self.statements.append(sql)
        if sql == "BEGIN IMMEDIATE" and self.statements.count(sql) == 1:
            other = JobStore(self._db)
            other.init_schema()
            other.close()
        return self._conn.execute(sql, *params)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def test_the_version_is_read_again_inside_the_migration_transaction(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)
    store = JobStore(db)
    racing = _RacingConnection(store._conn, db)
    store._conn = racing

    store.init_schema()

    assert _user_version(db) == SCHEMA_VERSION
    assert not any(s.startswith("ALTER TABLE") for s in racing.statements)
    assert "PRAGMA user_version = 3" not in racing.statements
