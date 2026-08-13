"""Append-only audit trail.

A record-everything recording policy guarantees somebody will eventually ask who
saw a given call. That answer has to exist, so every publish, mail, and deletion
writes a row here. Rows are never updated or deleted.
"""

from __future__ import annotations

import getpass
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PUBLISHED = "published"
UPDATED = "updated"
MAILED = "mailed"
PURGED_AUDIO = "purged_audio"
PURGED_PAGE = "purged_page"
QUARANTINED = "quarantined"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL,
  action  TEXT NOT NULL,
  detail  TEXT,
  actor   TEXT NOT NULL,
  at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_call ON audit_log (call_id, id);
"""


@dataclass(frozen=True)
class AuditEntry:
    call_id: str
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

    def record(self, call_id: str, action: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO audit_log (call_id, action, detail, actor, at) VALUES (?, ?, ?, ?, ?)",
            (call_id, action, detail, self._actor, datetime.now(UTC).isoformat(timespec="seconds")),
        )

    def entries(self, call_id: str | None = None) -> list[AuditEntry]:
        if call_id is None:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM audit_log WHERE call_id = ? ORDER BY id", (call_id,)).fetchall()
        return [AuditEntry(r["call_id"], r["action"], r["detail"] or "", r["actor"], r["at"]) for r in rows]
