"""Retention purge.

A record-everything policy without a delete policy is a liability, so this ships
with v1 rather than after it. Two independent clocks: audio is bulky and
sensitive so it goes early; the transcript page lives longer because it is the
business record.

The rule is "older than N days", so the boundary day itself survives -- and a
row whose start date cannot be parsed is never deleted. Refusing to act on a
date we could not read is the only safe default when the action is deletion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from jabberscribe.audit import PURGED_AUDIO, PURGED_PAGE, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import Job, JobStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PurgeResult:
    audio_deleted: tuple[str, ...] = ()
    pages_deleted: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _age_days(job: Job, now: datetime) -> float | None:
    try:
        started = datetime.fromisoformat(job.started_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=now.tzinfo)
    return (now - started) / timedelta(days=1)


def purge(
    cfg: Config,
    store: JobStore,
    audit: AuditLog,
    now: datetime,
    confluence=None,
) -> PurgeResult:
    """Delete aged audio and pages. Every deletion is audited."""
    audio_deleted: list[str] = []
    pages_deleted: list[str] = []
    errors: list[str] = []

    for job in store.list_all():
        age = _age_days(job, now)
        if age is None:
            errors.append(f"{job.call_id}: cannot parse started_at {job.started_at!r}, skipping")
            continue

        if age > cfg.retention.audio_days:
            path = Path(job.audio_path)
            if path.is_file():
                try:
                    path.unlink()
                except OSError as exc:
                    errors.append(f"{job.call_id}: cannot delete audio: {exc}")
                else:
                    audio_deleted.append(job.call_id)
                    audit.record(job.call_id, PURGED_AUDIO, str(path))

        if job.confluence_page_id and age > cfg.retention.page_days and confluence is not None:
            try:
                confluence.delete_page(job.confluence_page_id)
            except Exception as exc:
                errors.append(f"{job.call_id}: cannot delete page {job.confluence_page_id}: {exc}")
            else:
                pages_deleted.append(job.call_id)
                audit.record(job.call_id, PURGED_PAGE, f"page {job.confluence_page_id}")

    log.info("purge deleted %d audio file(s) and %d page(s)", len(audio_deleted), len(pages_deleted))
    return PurgeResult(tuple(audio_deleted), tuple(pages_deleted), tuple(errors))
