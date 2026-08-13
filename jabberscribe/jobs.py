"""SQLite-backed job store.

This is the queue seam. At pilot volume a serial worker over a SQLite table is
the right amount of machinery; scaling out means replacing this module with a
broker and changing nothing else.

Every stage checkpoints here, so a crashed worker resumes at the next
incomplete stage instead of re-transcribing.
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
NEEDS_REVIEW = "needs_review"

STAGE_ORDER: tuple[str, ...] = ("audio", "stt", "enrich", "render", "publish", "notify")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  call_id            TEXT PRIMARY KEY,
  status             TEXT NOT NULL,
  stage              TEXT NOT NULL,
  audio_path         TEXT NOT NULL,
  sidecar_json       TEXT NOT NULL,
  kind               TEXT NOT NULL,
  started_at         TEXT NOT NULL,
  duration_sec       INTEGER NOT NULL,
  transcript_path    TEXT,
  summary_path       TEXT,
  confluence_page_id TEXT,
  notified_at        TEXT,
  attempts           INTEGER NOT NULL DEFAULT 0,
  last_error         TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def next_stage(stage: str, pipeline: tuple[str, ...]) -> str | None:
    """Return the stage to run after `stage`, or None if the pipeline is done.

    `pipeline` is the ordered subset of STAGE_ORDER this deployment runs, so a
    deployment can omit stages without the resume logic changing.
    """
    if stage == QUEUED:
        return pipeline[0] if pipeline else None
    if stage not in pipeline:
        raise ValueError(f"stage {stage!r} is not in pipeline {pipeline!r}")
    index = pipeline.index(stage)
    return pipeline[index + 1] if index + 1 < len(pipeline) else None


@dataclass(frozen=True)
class Job:
    call_id: str
    status: str
    stage: str
    audio_path: Path
    sidecar_json: str
    kind: str
    started_at: str
    duration_sec: int
    transcript_path: Path | None
    summary_path: Path | None
    confluence_page_id: str | None
    notified_at: str | None
    attempts: int
    last_error: str | None


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        call_id=row["call_id"],
        status=row["status"],
        stage=row["stage"],
        audio_path=Path(row["audio_path"]),
        sidecar_json=row["sidecar_json"],
        kind=row["kind"],
        started_at=row["started_at"],
        duration_sec=row["duration_sec"],
        transcript_path=Path(row["transcript_path"]) if row["transcript_path"] else None,
        summary_path=Path(row["summary_path"]) if row["summary_path"] else None,
        confluence_page_id=row["confluence_page_id"],
        notified_at=row["notified_at"],
        attempts=row["attempts"],
        last_error=row["last_error"],
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
        call_id: str,
        audio_path: Path,
        sidecar_json: str,
        kind: str,
        started_at: str,
        duration_sec: int,
    ) -> bool:
        """Insert a new job. Returns False if `call_id` is already known.

        This is the deduplication point: a recorder that drops the same call
        twice produces one job, one page, and one email.
        """
        now = utcnow()
        try:
            self._conn.execute(
                "INSERT INTO jobs (call_id, status, stage, audio_path, sidecar_json, kind, started_at,"
                " duration_sec, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    call_id,
                    QUEUED,
                    QUEUED,
                    str(audio_path),
                    sidecar_json,
                    kind,
                    started_at,
                    duration_sec,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get(self, call_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE call_id = ?", (call_id,)).fetchone()
        return _row_to_job(row) if row else None

    def claim_next(self) -> Job | None:
        """Claim the oldest runnable job and mark it running.

        `running` rows are claimable because a row left running belongs to a
        crashed worker; its checkpointed stage tells us where to resume. This is
        safe under the single-worker deployment this design specifies.
        """
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY created_at LIMIT 1",
            (QUEUED, RUNNING),
        ).fetchone()
        if row is None:
            return None
        self.set_status(row["call_id"], RUNNING)
        return _row_to_job(self._conn.execute("SELECT * FROM jobs WHERE call_id = ?", (row["call_id"],)).fetchone())

    def complete_stage(self, call_id: str, stage: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET stage = ?, updated_at = ? WHERE call_id = ?",
            (stage, utcnow(), call_id),
        )

    def set_status(self, call_id: str, status: str, last_error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, last_error = COALESCE(?, last_error), updated_at = ? WHERE call_id = ?",
            (status, last_error, utcnow(), call_id),
        )

    def set_transcript_path(self, call_id: str, path: Path) -> None:
        self._conn.execute(
            "UPDATE jobs SET transcript_path = ?, updated_at = ? WHERE call_id = ?",
            (str(path), utcnow(), call_id),
        )

    def set_page_id(self, call_id: str, page_id: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET confluence_page_id = ?, updated_at = ? WHERE call_id = ?",
            (page_id, utcnow(), call_id),
        )

    def set_summary_path(self, call_id: str, path: Path) -> None:
        self._conn.execute(
            "UPDATE jobs SET summary_path = ?, updated_at = ? WHERE call_id = ?",
            (str(path), utcnow(), call_id),
        )

    def mark_notified(self, call_id: str, at: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET notified_at = ?, updated_at = ? WHERE call_id = ?",
            (at, utcnow(), call_id),
        )

    def list_all(self) -> list[Job]:
        rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()
        return [_row_to_job(r) for r in rows]

    def record_attempt(self, call_id: str, error: str) -> int:
        cur = self._conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, last_error = ?, updated_at = ?"
            " WHERE call_id = ? RETURNING attempts",
            (error, utcnow(), call_id),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown call_id: {call_id}")
        return int(row["attempts"])

    def list_by_status(self, status: str) -> list[Job]:
        rows = self._conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY created_at", (status,)).fetchall()
        return [_row_to_job(r) for r in rows]
