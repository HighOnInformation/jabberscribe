"""SQLite-backed job store.

This is the queue seam. At this volume a serial worker over a SQLite table is
the right amount of machinery; scaling out means replacing this module with a
broker and changing nothing else.

A job is one recorded line's copy of a call, keyed by job_key. Every stage
checkpoints here, so a crashed worker resumes at the next incomplete stage
instead of re-transcribing. A job that failed waits in QUEUED until its
next_attempt_at; claim_next skips it until then. `attempts` counts real
failures only; outages are counted apart in `transient_failures`, so they never
spend a job's attempt budget.
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

#: Jobs still in the pipeline: not finished, failed, or handed to another copy.
ACTIVE: tuple[str, ...] = (QUEUED, RUNNING, WAITING)

STAGE_ORDER: tuple[str, ...] = ("audio", "stt", "cues", "summarize", "output")

#: The checkpoint of a job that failed in the audio or STT stage: a failure of that copy's recording.
COPY_STAGES: tuple[str, ...] = (QUEUED, "audio")

#: Stored in PRAGMA user_version. Versions listed in MIGRATIONS are upgraded in place; any other is refused.
SCHEMA_VERSION = 4

#: Columns that upgrade a database from the keyed version to the next one: (name, type and default).
#: A column that is already there is skipped, so a pre-release database that has it upgrades cleanly.
MIGRATIONS: dict[int, tuple[tuple[str, str], ...]] = {
    2: (
        ("legal_hold", "INTEGER NOT NULL DEFAULT 0"),
        ("hold_reason", "TEXT"),
    ),
    3: (
        ("output_at", "TEXT"),
        ("latency_sec", "REAL"),
    ),
}

#: What a scrubbed row keeps of its sidecar once the text retention has passed.
SCRUBBED_SIDECAR = "{}"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
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
  legal_hold      INTEGER NOT NULL DEFAULT 0,
  hold_reason     TEXT,
  output_at       TEXT,
  latency_sec     REAL,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_conference ON jobs (conference_id);
CREATE INDEX IF NOT EXISTS idx_jobs_grouped_into ON jobs (grouped_into);
"""


class SchemaError(RuntimeError):
    """The database was written by another JabberScribe version."""


def _check_version(version: int) -> None:
    """Refuse a database this version can neither use as it is nor upgrade."""
    if version != SCHEMA_VERSION and version not in MIGRATIONS:
        raise SchemaError(
            f"database schema version {version} is not {SCHEMA_VERSION}; point paths.db_path at a fresh file"
        )


def iso(moment: datetime) -> str:
    """The one timestamp format the store writes, so stored values compare as strings."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def utcnow() -> str:
    return iso(datetime.now(UTC))


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
    next_attempt_at: str | None
    transient_failures: int
    legal_hold: bool = False
    hold_reason: str | None = None
    #: When the outputs were last written, and how long after hang-up (for `status`).
    output_at: str | None = None
    latency_sec: float | None = None


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
        next_attempt_at=row["next_attempt_at"],
        transient_failures=row["transient_failures"],
        legal_hold=bool(row["legal_hold"]),
        hold_reason=row["hold_reason"],
        output_at=row["output_at"],
        latency_sec=row["latency_sec"],
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
        """Create the tables, upgrade a known older database in place, or refuse anything else.

        CREATE TABLE IF NOT EXISTS would silently keep an older jobs table and
        fail later with a cryptic column error, so the version is checked first.
        """
        version = self._user_version()
        has_jobs = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()
        if has_jobs and version != SCHEMA_VERSION:
            _check_version(version)
            while self._migrate():
                pass
        self._conn.executescript(_SCHEMA)
        # A v2 database created before transient_failures existed (pre-release only).
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
        if "transient_failures" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN transient_failures INTEGER NOT NULL DEFAULT 0")
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _user_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self) -> bool:
        """Upgrade by one version in one transaction. Returns False when the database is already current.

        The version is read again inside the transaction: another process may have upgraded it meanwhile.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            version = self._user_version()
            current = version == SCHEMA_VERSION
            if not current:
                _check_version(version)
                present = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
                for name, definition in MIGRATIONS[version]:
                    if name not in present:
                        self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
                self._conn.execute(f"PRAGMA user_version = {version + 1}")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return not current

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

    def claim_next(self, now: datetime | None = None) -> Job | None:
        """Claim the oldest runnable job that is due and mark it running.

        A job whose next_attempt_at is still in the future is skipped, not
        waited for, so one failing job never blocks the jobs behind it.
        `running` rows are claimable because a row left running belongs to a
        crashed worker; its checkpointed stage tells us where to resume. The
        single-instance lock (lock.py) makes that safe.
        """
        due = iso(now or datetime.now(UTC))
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            " ORDER BY created_at, rowid LIMIT 1",
            (QUEUED, RUNNING, due),
        ).fetchone()
        if row is None:
            return None
        self.set_status(row["job_key"], RUNNING)
        return self.get(row["job_key"])

    def claim(self, job_key: str) -> Job | None:
        """Claim one specific job now, due or not. Returns None unless it is QUEUED or RUNNING."""
        cur = self._conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE job_key = ? AND status IN (?, ?)",
            (RUNNING, utcnow(), job_key, QUEUED, RUNNING),
        )
        return self.get(job_key) if cur.rowcount else None

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

    def record_transient(self, job_key: str, error: str) -> int:
        """Count an outage-type failure. It never spends the attempt budget."""
        cur = self._conn.execute(
            "UPDATE jobs SET transient_failures = transient_failures + 1, last_error = ?, updated_at = ?"
            " WHERE job_key = ? RETURNING transient_failures",
            (error, utcnow(), job_key),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown job_key: {job_key}")
        return int(row["transient_failures"])

    def finish(self, job_key: str, status: str) -> bool:
        """Set a terminal status, but only on a job still RUNNING. Returns whether it applied.

        A job regrouped or reset while the worker held it keeps its new state.
        """
        if status not in (DONE, FAILED):
            raise ValueError(f"finish takes {DONE!r} or {FAILED!r}, not {status!r}")
        cur = self._conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE job_key = ? AND status = ?",
            (status, utcnow(), job_key, RUNNING),
        )
        return cur.rowcount == 1

    def fail(self, job_key: str, reason: str) -> bool:
        """Fail a job that is still active (QUEUED, RUNNING or WAITING). Returns whether it applied."""
        cur = self._conn.execute(
            "UPDATE jobs SET status = ?, last_error = ?, updated_at = ? WHERE job_key = ? AND status IN (?, ?, ?)",
            (FAILED, reason, utcnow(), job_key, *ACTIVE),
        )
        return cur.rowcount == 1

    def retry_later(self, job_key: str, at: datetime) -> bool:
        """schedule_retry, but only on a job still RUNNING. Returns whether it applied."""
        cur = self._conn.execute(
            "UPDATE jobs SET status = ?, next_attempt_at = ?, updated_at = ? WHERE job_key = ? AND status = ?",
            (QUEUED, iso(at), utcnow(), job_key, RUNNING),
        )
        return cur.rowcount == 1

    def schedule_retry(self, job_key: str, at: datetime) -> None:
        """Put a failed job back in the queue, not to be claimed before `at`."""
        self._conn.execute(
            "UPDATE jobs SET status = ?, next_attempt_at = ?, updated_at = ? WHERE job_key = ?",
            (QUEUED, iso(at), utcnow(), job_key),
        )

    def requeue(self, job_key: str) -> bool:
        """Give a FAILED job a fresh set of attempts. Returns False if it is not a FAILED primary.

        A FAILED job that was handed over to another copy (grouped_into set)
        is not requeued: its conference already has a primary.
        """
        cur = self._conn.execute(
            "UPDATE jobs SET status = ?, attempts = 0, transient_failures = 0, next_attempt_at = NULL, updated_at = ?"
            " WHERE job_key = ? AND status = ? AND grouped_into IS NULL",
            (QUEUED, utcnow(), job_key, FAILED),
        )
        return cur.rowcount == 1

    def reset_job(self, job_key: str) -> None:
        """Make a conference copy the primary from scratch: QUEUED, no stage done, no attempts."""
        self._conn.execute(
            "UPDATE jobs SET status = ?, stage = ?, grouped_into = NULL, attempts = 0, transient_failures = 0,"
            " last_error = NULL, next_attempt_at = NULL, updated_at = ? WHERE job_key = ?",
            (QUEUED, QUEUED, utcnow(), job_key),
        )

    def hand_over(self, old_primary: str, new_primary: str) -> None:
        """Move a conference from one primary to another.

        Every member follows, and the old primary becomes a member too: GROUPED,
        or still FAILED if it failed, so it is never elected again.
        """
        now = utcnow()
        self._conn.execute(
            "UPDATE jobs SET grouped_into = ?, updated_at = ? WHERE grouped_into = ? AND job_key != ?",
            (new_primary, now, old_primary, new_primary),
        )
        self._conn.execute(
            "UPDATE jobs SET grouped_into = ?, status = CASE WHEN status = ? THEN ? ELSE ? END, updated_at = ?"
            " WHERE job_key = ?",
            (new_primary, FAILED, FAILED, GROUPED, now, old_primary),
        )

    def scrub_sidecar(self, job_key: str) -> bool:
        """Drop the call metadata of a row past text retention. Returns False if already scrubbed."""
        cur = self._conn.execute(
            "UPDATE jobs SET sidecar_json = ?, updated_at = ? WHERE job_key = ? AND sidecar_json != ?",
            (SCRUBBED_SIDECAR, utcnow(), job_key, SCRUBBED_SIDECAR),
        )
        return cur.rowcount == 1

    def hold(self, job_key: str, reason: str) -> bool:
        """Put a job on legal hold, or replace the reason of its hold. Returns False for an unknown job."""
        cur = self._conn.execute(
            "UPDATE jobs SET legal_hold = 1, hold_reason = ?, updated_at = ? WHERE job_key = ?",
            (reason, utcnow(), job_key),
        )
        return cur.rowcount == 1

    def release_hold(self, job_key: str) -> bool:
        """Lift a legal hold. Returns False unless the job was on hold."""
        cur = self._conn.execute(
            "UPDATE jobs SET legal_hold = 0, hold_reason = NULL, updated_at = ? WHERE job_key = ? AND legal_hold = 1",
            (utcnow(), job_key),
        )
        return cur.rowcount == 1

    def is_held(self, job_key: str) -> bool:
        """True when this job, its primary, a fellow member, or one of its members is on hold.

        A conference is one call: a hold on any copy of it holds every copy.
        """
        return bool(self.holders(job_key))

    def holders(self, job_key: str) -> list[str]:
        """The keys of the copies whose hold covers this job (see is_held), this job included when it is held.

        hand_over keeps groups one level deep, so these four relations cover a whole group.
        """
        rows = self._conn.execute(
            "SELECT h.job_key FROM jobs h, jobs k WHERE k.job_key = ? AND h.legal_hold = 1 AND ("
            " h.job_key = k.job_key OR h.job_key = k.grouped_into OR h.grouped_into = k.job_key"
            " OR (k.grouped_into IS NOT NULL AND h.grouped_into = k.grouped_into)) ORDER BY h.created_at, h.rowid",
            (job_key,),
        ).fetchall()
        return [r["job_key"] for r in rows]

    def held_jobs(self) -> list[Job]:
        rows = self._conn.execute("SELECT * FROM jobs WHERE legal_hold = 1 ORDER BY created_at, rowid").fetchall()
        return [_row_to_job(r) for r in rows]

    def record_output(self, job_key: str, latency_sec: float) -> None:
        """Remember that the outputs were written now, `latency_sec` after the call ended."""
        now = utcnow()
        self._conn.execute(
            "UPDATE jobs SET output_at = ?, latency_sec = ?, updated_at = ? WHERE job_key = ?",
            (now, latency_sec, now, job_key),
        )

    def latencies_since(self, since: datetime) -> list[float]:
        """Hang-up-to-output latencies of the outputs written at or after `since`, ascending."""
        rows = self._conn.execute(
            "SELECT latency_sec FROM jobs WHERE output_at >= ? AND latency_sec IS NOT NULL ORDER BY latency_sec",
            (iso(since),),
        ).fetchall()
        return [float(r["latency_sec"]) for r in rows]

    def status_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def oldest_pending_created_at(self) -> str | None:
        """Arrival time of the oldest job still in the pipeline (ACTIVE: QUEUED, RUNNING or WAITING)."""
        row = self._conn.execute(
            "SELECT MIN(created_at) AS oldest FROM jobs WHERE status IN (?, ?, ?)", ACTIVE
        ).fetchone()
        return row["oldest"]

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

    def conference_ids_to_settle(self) -> list[str]:
        """Conferences with a waiting copy, or with a primary FAILED in COPY_STAGES that still has members to elect."""
        rows = self._conn.execute(
            "SELECT DISTINCT conference_id FROM jobs j WHERE conference_id IS NOT NULL AND (status = ?"
            " OR (status = ? AND grouped_into IS NULL AND stage IN (?, ?)"
            " AND EXISTS (SELECT 1 FROM jobs m WHERE m.grouped_into = j.job_key AND m.status = ?)))"
            " ORDER BY conference_id",
            (WAITING, FAILED, *COPY_STAGES, GROUPED),
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
