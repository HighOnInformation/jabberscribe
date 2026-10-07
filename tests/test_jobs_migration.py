import sqlite3
from pathlib import Path

import pytest

from jabberscribe.jobs import SCHEMA_VERSION, JobStore, SchemaError

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
