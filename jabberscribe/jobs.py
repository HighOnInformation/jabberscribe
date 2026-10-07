"""SQLite-backed job store.

This is the queue seam. At this volume a serial worker over a SQLite table is
the right amount of machinery; scaling out means replacing this module with a
broker and changing nothing else.

A job is one recorded line's copy of a call, keyed by job_key. Every stage
checkpoints here, so a crashed worker resumes at the next incomplete stage
instead of re-transcribing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
#: A conference copy held until its group settles (see group.py).
WAITING = "waiting"
#: A conference copy whose meeting is processed by another job, its primary.
GROUPED = "grouped"

STAGE_ORDER: tuple[str, ...] = ("audio", "stt", "summarize", "output")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_key       TEXT PRIMARY KEY,
  call_id       TEXT NOT NULL,
  conference_id TEXT,
  status        TEXT NOT NULL,
  stage         TEXT NOT NULL,
  audio_path    TEXT NOT NULL,
  out_dir       TEXT NOT NULL,
  sidecar_json  TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  duration_sec  INTEGER NOT NULL,
  grouped_into  TEXT REFERENCES jobs (job_key),
  attempts      INTEGER NOT NULL DEFAULT 0,
  last_error    TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_conference ON jobs (conference_id);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def next_stage(stage: str) -> str | None:
    """Return the stage to run after `stage`, or None when the job is finished."""
    if stage == QUEUED:
        return STAGE_ORDER[0]
    if stage not in STAGE_ORDER:
        raise ValueError(f"unknown stage {stage!r}; stages are {STAGE_ORDER}")
    index = STAGE_ORDER.index(stage)
    return STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else None


@dataclass(frozen=True)
class Job:
    job_key: str
    call_id: str
    conference_id: str | None
    status: str
    stage: str
    audio_path: Path
    out_dir: Path
    sidecar_json: str
    started_at: str
    duration_sec: int
    grouped_into: str | None
    attempts: int
    last_error: str | None
    created_at: str


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_key=row["job_key"],
        call_id=row["call_id"],
        conference_id=row["conference_id"],
        status=row["status"],
        stage=row["stage"],
        audio_path=Path(row["audio_path"]),
        out_dir=Path(row["out_dir"]),
        sidecar_json=row["sidecar_json"],
        started_at=row["started_at"],
        duration_sec=row["duration_sec"],
        grouped_into=row["grouped_into"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=row["created_at"],
    )


class JobStore:
    """Data access only. No business logic lives here."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def create(
        self,
        *,
        job_key: str,
        call_id: str,
        conference_id: str | None,
        audio_path: Path,
        out_dir: Path,
        sidecar_json: str,
        started_at: str,
        duration_sec: int,
    ) -> bool:
        """Insert a new job. Returns False if `job_key` is already known.

        This is the deduplication point: a recorder that drops the same line's
        copy twice produces one job and one output. Conference copies start
        WAITING so grouping can pick one of them; everything else is QUEUED.
        """
        now = utcnow()
        status = WAITING if conference_id else QUEUED
        try:
            self._conn.execute(
                "INSERT INTO jobs (job_key, call_id, conference_id, status, stage, audio_path, out_dir,"
                " sidecar_json, started_at, duration_sec, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    job_key,
                    call_id,
                    conference_id,
                    status,
                    QUEUED,
                    str(audio_path),
                    str(out_dir),
                    sidecar_json,
                    started_at,
                    duration_sec,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get(self, job_key: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE job_key = ?", (job_key,)).fetchone()
        return _row_to_job(row) if row else None

    def claim_next(self) -> Job | None:
        """Claim the oldest runnable job and mark it running.

        `running` rows are claimable because a row left running belongs to a
        crashed worker; its checkpointed stage tells us where to resume. This is
        safe under the single-worker deployment this design specifies.
        """
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY created_at, rowid LIMIT 1",
            (QUEUED, RUNNING),
        ).fetchone()
        if row is None:
            return None
        self.set_status(row["job_key"], RUNNING)
        return self.get(row["job_key"])

    def complete_stage(self, job_key: str, stage: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET stage = ?, updated_at = ? WHERE job_key = ?",
            (stage, utcnow(), job_key),
        )

    def set_status(self, job_key: str, status: str, last_error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, last_error = COALESCE(?, last_error), updated_at = ? WHERE job_key = ?",
            (status, last_error, utcnow(), job_key),
        )

    def record_attempt(self, job_key: str, error: str) -> int:
        cur = self._conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, last_error = ?, updated_at = ?"
            " WHERE job_key = ? RETURNING attempts",
            (error, utcnow(), job_key),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown job_key: {job_key}")
        return int(row["attempts"])

    def list_all(self) -> list[Job]:
        rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at, rowid").fetchall()
        return [_row_to_job(r) for r in rows]

    def list_by_status(self, status: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at, rowid", (status,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def waiting_conference_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT conference_id FROM jobs WHERE status = ? ORDER BY conference_id", (WAITING,)
        ).fetchall()
        return [r["conference_id"] for r in rows]

    def conference_jobs(self, conference_id: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE conference_id = ? ORDER BY created_at, rowid", (conference_id,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def group_into(self, job_key: str, primary_key: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, grouped_into = ?, updated_at = ? WHERE job_key = ?",
            (GROUPED, primary_key, utcnow(), job_key),
        )

    def members(self, primary_key: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE grouped_into = ? ORDER BY created_at, rowid", (primary_key,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]
