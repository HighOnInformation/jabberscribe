"""Retention purge.

A record-everything policy without a delete policy is a liability. Two
independent clocks: audio is bulky and sensitive so it goes early; the text
outputs live longer because they are the business record.

The rule is "older than N days", so the boundary day itself survives -- and a
row whose start date cannot be parsed is never deleted. Refusing to act on a
date we could not read is the only safe default when the action is deletion.
A call's age counts from the later of its start and its arrival, so a recorder
clock stuck in the past cannot get a fresh call deleted.

Besides the job rows, the purge sweeps every other place a voice can linger:
leftover STT copies in work/, quarantined pairs, and inbox orphans. Past the
text retention a row keeps no call metadata. Every deletion is audited.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audio import STT_FILENAME
from jabberscribe.audit import (
    PURGED_AUDIO,
    PURGED_ORPHAN,
    PURGED_QUARANTINE,
    PURGED_STT_AUDIO,
    PURGED_TEXT,
    SCRUBBED_METADATA,
    AuditLog,
)
from jabberscribe.config import Config
from jabberscribe.jobs import Job, JobStore
from jabberscribe.output import TEXT_FILES

log = logging.getLogger(__name__)

#: A finished or failed job deletes its STT copy itself; anything older than this was left by a crash.
STT_LEFTOVER_DAYS = 1


@dataclass(frozen=True)
class PurgeResult:
    audio_deleted: tuple[str, ...] = ()
    text_deleted: tuple[str, ...] = ()
    #: Paths of swept files: STT leftovers, quarantined pairs, inbox orphans.
    swept: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _age_days(job: Job, now: datetime) -> float | None:
    try:
        started = datetime.fromisoformat(job.started_at)
        created = datetime.fromisoformat(job.created_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (now - max(started, created)) / timedelta(days=1)


def _file_age_days(path: Path, now: datetime) -> float:
    return (now - datetime.fromtimestamp(path.stat().st_mtime, UTC)) / timedelta(days=1)


def _delete_text(job: Job, work: Path) -> list[str]:
    removed: list[str] = []
    for name in TEXT_FILES:
        path = job.out_dir / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    if work.is_dir():
        shutil.rmtree(work)
        removed.append("work")
    return removed


def _sweep(
    paths: list[Path], max_days: float, now: datetime, action: str, audit: AuditLog, key_of: Callable[[Path], str]
) -> tuple[list[str], list[str]]:
    """Delete the files in `paths` older than `max_days`. Returns (deleted paths, errors)."""
    swept: list[str] = []
    errors: list[str] = []
    for path in paths:
        try:
            if not path.is_file() or _file_age_days(path, now) <= max_days:
                continue
            path.unlink()
        except OSError as exc:
            errors.append(f"{path}: cannot delete: {exc}")
            continue
        swept.append(str(path))
        audit.record(key_of(path), action, str(path))
    return swept, errors


def purge(cfg: Config, store: JobStore, audit: AuditLog, now: datetime) -> PurgeResult:
    """Delete aged audio and text, sweep leftovers, scrub old metadata. Every deletion is audited."""
    audio_deleted: list[str] = []
    text_deleted: list[str] = []
    errors: list[str] = []

    for job in store.list_all():
        age = _age_days(job, now)
        if age is None:
            errors.append(f"{job.job_key}: cannot parse started_at {job.started_at!r}, skipping")
            continue

        if age > cfg.retention.audio_days and job.audio_path.is_file():
            try:
                job.audio_path.unlink()
            except OSError as exc:
                errors.append(f"{job.job_key}: cannot delete audio: {exc}")
            else:
                audio_deleted.append(job.job_key)
                audit.record(job.job_key, PURGED_AUDIO, str(job.audio_path))

        if age > cfg.retention.text_days:
            try:
                removed = _delete_text(job, cfg.paths.work_dir / job.job_key)
            except OSError as exc:
                errors.append(f"{job.job_key}: cannot delete text: {exc}")
                continue
            if removed:
                text_deleted.append(job.job_key)
                audit.record(job.job_key, PURGED_TEXT, ", ".join(removed))
            if store.scrub_sidecar(job.job_key):
                audit.record(job.job_key, SCRUBBED_METADATA, "sidecar_json")
            try:
                job.out_dir.rmdir()
            except OSError:
                pass  # not empty (audio kept longer than text) or already gone

    stt_copies = [p for d in cfg.paths.work_dir.glob("*") for p in d.glob(f"{STT_FILENAME}*")]
    swept, sweep_errors = _sweep(stt_copies, STT_LEFTOVER_DAYS, now, PURGED_STT_AUDIO, audit, lambda p: p.parent.name)
    errors += sweep_errors
    for folder, action in ((cfg.paths.quarantine, PURGED_QUARANTINE), (cfg.paths.inbox, PURGED_ORPHAN)):
        more, sweep_errors = _sweep(list(folder.glob("*")), cfg.retention.audio_days, now, action, audit, _stem)
        swept += more
        errors += sweep_errors

    log.info(
        "purge deleted %d audio file(s), text for %d call(s), %d leftover file(s)",
        len(audio_deleted),
        len(text_deleted),
        len(swept),
    )
    return PurgeResult(tuple(audio_deleted), tuple(text_deleted), tuple(swept), tuple(errors))


def _stem(path: Path) -> str:
    return path.name.split(".", 1)[0]
