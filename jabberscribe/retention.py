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

A call on legal hold -- and every copy of its conference -- is skipped
entirely until the hold is released (`jabberscribe unhold`).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audio import STT_GLOB
from jabberscribe.audit import (
    PURGE_FAILED_JOB,
    PURGED_AUDIO,
    PURGED_ORPHAN,
    PURGED_QUARANTINE,
    PURGED_STT_AUDIO,
    PURGED_TEXT,
    SCRUBBED_METADATA,
    AuditLog,
)
from jabberscribe.config import Config
from jabberscribe.jobs import ACTIVE, Job, JobStore
from jabberscribe.output import TEXT_FILES

log = logging.getLogger(__name__)

#: A finished or failed job deletes its STT copy itself; anything older than this was left by a crash.
STT_LEFTOVER_DAYS = 1

AUDIO_PURGED_REASON = "audio purged by retention before processing"


@dataclass(frozen=True)
class PurgeResult:
    audio_deleted: tuple[str, ...] = ()
    text_deleted: tuple[str, ...] = ()
    #: Paths of swept files: STT leftovers, quarantined pairs, inbox orphans.
    swept: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: Calls past audio retention that were kept because of a legal hold.
    held: tuple[str, ...] = ()


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


def _delete_text(job: Job, work: Path) -> tuple[list[str], list[str]]:
    """Delete a job's text outputs and work dir. Returns (what was removed, errors).

    A failure never hides what was already removed, so the caller can audit it.
    """
    removed: list[str] = []
    errors: list[str] = []
    for name in TEXT_FILES:
        path = job.out_dir / name
        try:
            if path.is_file():
                path.unlink()
                removed.append(name)
        except OSError as exc:
            errors.append(f"{job.job_key}: cannot delete {name}: {exc}")
    try:
        if work.is_dir():
            shutil.rmtree(work)
            removed.append("work")
    except OSError as exc:
        errors.append(f"{job.job_key}: cannot delete work dir: {exc}")
    return removed, errors


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
        try:
            audit.record(key_of(path), action, str(path))
        except sqlite3.Error as exc:
            errors.append(f"{path}: deleted but not audited: {exc}")
    return swept, errors


def purge(cfg: Config, store: JobStore, audit: AuditLog, now: datetime) -> PurgeResult:
    """Delete aged audio and text, sweep leftovers, scrub old metadata. Every deletion is audited.

    A job still in the pipeline (QUEUED, RUNNING, WAITING) or on legal hold keeps its work dir and STT copy.
    An active job's audio is the one exception: retention wins, and the job is failed because it can no
    longer be processed.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    audio_deleted: list[str] = []
    text_deleted: list[str] = []
    errors: list[str] = []
    held: list[str] = []
    jobs = store.list_all()
    active_keys = {j.job_key for j in jobs if j.status in ACTIVE}

    for job in jobs:
        age = _age_days(job, now)
        if age is None:
            errors.append(f"{job.job_key}: cannot parse started_at {job.started_at!r}, skipping")
            continue
        active = job.job_key in active_keys

        try:
            if store.is_held(job.job_key):
                # Checked per job, right before deleting, so a hold set during a purge still counts.
                # An active held job is not failed for its audio either: the audio is kept.
                if age > cfg.retention.audio_days:
                    held.append(job.job_key)
                continue
            if age > cfg.retention.audio_days and job.audio_path.is_file():
                try:
                    job.audio_path.unlink()
                except OSError as exc:
                    errors.append(f"{job.job_key}: cannot delete audio: {exc}")
                else:
                    audio_deleted.append(job.job_key)
                    audit.record(job.job_key, PURGED_AUDIO, str(job.audio_path))
                    if active and store.fail(job.job_key, AUDIO_PURGED_REASON):
                        log.warning("%s: audio purged by retention before processing; job failed", job.job_key)
                        audit.record(job.job_key, PURGE_FAILED_JOB, AUDIO_PURGED_REASON)

            if age > cfg.retention.text_days and not active:
                removed, text_errors = _delete_text(job, cfg.paths.work_dir / job.job_key)
                errors += text_errors
                if removed:
                    text_deleted.append(job.job_key)
                    audit.record(job.job_key, PURGED_TEXT, ", ".join(removed))
                if text_errors:
                    continue
                if store.scrub_sidecar(job.job_key):
                    audit.record(job.job_key, SCRUBBED_METADATA, "sidecar_json")
                try:
                    job.out_dir.rmdir()
                except OSError:
                    pass  # not empty (audio kept longer than text) or already gone
        except sqlite3.Error as exc:
            errors.append(f"{job.job_key}: database error: {exc}")

    stt_copies = [
        p
        for d in cfg.paths.work_dir.glob("*")
        if d.name not in active_keys and not store.is_held(d.name)
        for p in d.glob(STT_GLOB)
    ]
    swept, sweep_errors = _sweep(stt_copies, STT_LEFTOVER_DAYS, now, PURGED_STT_AUDIO, audit, lambda p: p.parent.name)
    errors += sweep_errors
    for folder, action in ((cfg.paths.quarantine, PURGED_QUARANTINE), (cfg.paths.inbox, PURGED_ORPHAN)):
        more, sweep_errors = _sweep(list(folder.glob("*")), cfg.retention.audio_days, now, action, audit, _stem)
        swept += more
        errors += sweep_errors

    log.info(
        "purge deleted %d audio file(s), text for %d call(s), %d leftover file(s); %d call(s) kept on legal hold",
        len(audio_deleted),
        len(text_deleted),
        len(swept),
        len(held),
    )
    return PurgeResult(tuple(audio_deleted), tuple(text_deleted), tuple(swept), tuple(errors), tuple(held))


def _stem(path: Path) -> str:
    return path.name.split(".", 1)[0]
