"""Append-only audit trail.

A record-everything recording policy guarantees somebody will eventually ask
what happened to a given call. That answer has to exist, so every deletion and
every quarantine writes a row here. Rows are never updated or deleted.

`job_key` names the call when there is one. For files that never became a job
(quarantined pairs, inbox orphans) it is the file name.
"""

from __future__ import annotations

import getpass
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

QUARANTINED = "quarantined"
DISCARDED_DUPLICATE = "discarded_duplicate"
SUPERSEDED = "superseded"
PURGED_AUDIO = "purged_audio"
PURGED_TEXT = "purged_text"
PURGED_STT_AUDIO = "purged_stt_audio"
PURGED_QUARANTINE = "purged_quarantine"
PURGED_ORPHAN = "purged_orphan"
SCRUBBED_METADATA = "scrubbed_metadata"
PURGE_FAILED_JOB = "purge_failed_job"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT NOT NULL,
  action  TEXT NOT NULL,
  detail  TEXT,
  actor   TEXT NOT NULL,
  at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_log (job_key, id);
"""


@dataclass(frozen=True)
class AuditEntry:
    job_key: str
    action: str
    detail: str
    actor: str
    at: str


def _default_actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - unusual service accounts
        return "unknown"


class AuditLog:
    def __init__(self, db_path: Path, actor: str | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._actor = actor or _default_actor()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def record(self, job_key: str, action: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO audit_log (job_key, action, detail, actor, at) VALUES (?, ?, ?, ?, ?)",
            (job_key, action, detail, self._actor, datetime.now(UTC).isoformat(timespec="seconds")),
        )

    def entries(self, job_key: str | None = None) -> list[AuditEntry]:
        if job_key is None:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM audit_log WHERE job_key = ? ORDER BY id", (job_key,)).fetchall()
        return [AuditEntry(r["job_key"], r["action"], r["detail"] or "", r["actor"], r["at"]) for r in rows]
