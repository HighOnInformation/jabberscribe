import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jabberscribe.audit import (
    PURGED_AUDIO,
    PURGED_ORPHAN,
    PURGED_QUARANTINE,
    PURGED_STT_AUDIO,
    PURGED_TEXT,
    SCRUBBED_METADATA,
)
from jabberscribe.jobs import SCRUBBED_SIDECAR
from jabberscribe.output import RESULT_FILE, TEXT_FILES
from jabberscribe.retention import purge
from jabberscribe.watcher import scan_once

# Matches the make_sidecar default started_at.
STARTED = datetime(2026, 10, 7, 14, 3, 11, tzinfo=timezone(timedelta(hours=3)))


def _set_created(cfg, key: str, created: datetime) -> None:
    conn = sqlite3.connect(cfg.paths.db_path)
    conn.execute("UPDATE jobs SET created_at = ? WHERE job_key = ?", (created.isoformat(timespec="seconds"), key))
    conn.commit()
    conn.close()


def _touch(path: Path, when: datetime, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def _setup(cfg, store, audit, make_wav, make_sidecar, **sidecar_fields):
    make_wav(cfg.paths.inbox / "r.wav")
    make_sidecar(cfg.paths.inbox / "r.json", call_id="r1", **sidecar_fields)
    scan_once(cfg, store, audit, min_age_seconds=0)
    _set_created(cfg, "r1_1042", STARTED)
    job = store.get("r1_1042")
    for name in TEXT_FILES:
        (job.out_dir / name).write_text("x", encoding="utf-8")
    work = cfg.paths.work_dir / job.job_key
    work.mkdir(parents=True)
    (work / "segments.json").write_text("[]", encoding="utf-8")
    return job, work


def test_young_call_is_untouched(cfg, store, audit, make_wav, make_sidecar) -> None:
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar)

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=10))

    assert (result.audio_deleted, result.text_deleted, result.swept, result.errors) == ((), (), (), ())
    assert job.audio_path.is_file()
    assert (job.out_dir / RESULT_FILE).is_file()


def test_audio_goes_after_90_days_and_text_stays(cfg, store, audit, make_wav, make_sidecar) -> None:
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar)

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=91))

    assert result.audio_deleted == ("r1_1042",)
    assert not job.audio_path.exists()
    assert (job.out_dir / RESULT_FILE).is_file()
    assert [e.action for e in audit.entries("r1_1042")] == [PURGED_AUDIO]


def test_boundary_day_survives(cfg, store, audit, make_wav, make_sidecar) -> None:
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar)

    purge(cfg, store, audit, now=STARTED + timedelta(days=90))

    assert job.audio_path.is_file()


def test_text_goes_after_365_days_and_metadata_is_scrubbed(cfg, store, audit, make_wav, make_sidecar) -> None:
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar)

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=366))

    assert result.text_deleted == ("r1_1042",)
    assert not job.out_dir.exists()
    assert not work.exists()
    assert store.get("r1_1042").sidecar_json == SCRUBBED_SIDECAR
    actions = [e.action for e in audit.entries("r1_1042")]
    assert actions == [PURGED_AUDIO, PURGED_TEXT, SCRUBBED_METADATA]
    text_entry = audit.entries("r1_1042")[1]
    assert RESULT_FILE in text_entry.detail
    assert "work" in text_entry.detail


def test_purge_is_idempotent(cfg, store, audit, make_wav, make_sidecar) -> None:
    _setup(cfg, store, audit, make_wav, make_sidecar)
    later = STARTED + timedelta(days=366)
    purge(cfg, store, audit, now=later)
    entries = len(audit.entries())

    result = purge(cfg, store, audit, now=later)

    assert (result.audio_deleted, result.text_deleted) == ((), ())
    assert len(audit.entries()) == entries


def test_a_recorder_clock_in_the_past_does_not_delete_a_fresh_call(cfg, store, audit, make_wav, make_sidecar) -> None:
    """Age counts from the later of started_at and arrival."""
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar, started_at="2024-01-01T00:00:00+00:00")

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=1))

    assert result.audio_deleted == ()
    assert job.audio_path.is_file()


def test_unparseable_start_is_never_deleted(cfg, store, audit, make_wav, make_sidecar) -> None:
    _setup(cfg, store, audit, make_wav, make_sidecar)
    store.create(
        job_key="bad_1",
        call_id="bad",
        conference_id=None,
        audio_path=Path(cfg.paths.out_root / "bad" / "recording.wav"),
        out_dir=Path(cfg.paths.out_root / "bad"),
        sidecar_json="{}",
        started_at="garbage",
        duration_sec=1,
    )

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=1000))

    assert any("bad_1" in e and "cannot parse" in e for e in result.errors)


def test_leftover_stt_copies_are_swept_after_a_day(cfg, store, audit) -> None:
    now = STARTED + timedelta(days=10)
    stale = _touch(cfg.paths.work_dir / "k1_1042" / "stt.ogg", now - timedelta(days=2))
    stale_part = _touch(cfg.paths.work_dir / "k1_1042" / "stt.ogg.part", now - timedelta(days=2))
    fresh = _touch(cfg.paths.work_dir / "k2_1042" / "stt.ogg", now - timedelta(hours=2))
    segments = _touch(cfg.paths.work_dir / "k1_1042" / "segments.json", now - timedelta(days=2))

    result = purge(cfg, store, audit, now=now)

    assert not stale.exists() and not stale_part.exists()
    assert fresh.exists() and segments.exists()
    assert sorted(result.swept) == sorted([str(stale), str(stale_part)])
    assert [e.action for e in audit.entries("k1_1042")] == [PURGED_STT_AUDIO, PURGED_STT_AUDIO]


def test_quarantine_and_inbox_orphans_follow_audio_retention(cfg, store, audit) -> None:
    now = STARTED + timedelta(days=200)
    old_quarantine = _touch(cfg.paths.quarantine / "bad.wav", now - timedelta(days=91))
    old_reason = _touch(cfg.paths.quarantine / "bad.reason.txt", now - timedelta(days=91))
    young_quarantine = _touch(cfg.paths.quarantine / "new.wav", now - timedelta(days=5))
    orphan = _touch(cfg.paths.inbox / "lost.wav.part", now - timedelta(days=91))
    waiting = _touch(cfg.paths.inbox / "pending.wav", now - timedelta(days=5))

    purge(cfg, store, audit, now=now)

    assert not old_quarantine.exists() and not old_reason.exists() and not orphan.exists()
    assert young_quarantine.exists() and waiting.exists()
    assert [e.action for e in audit.entries("bad")] == [PURGED_QUARANTINE, PURGED_QUARANTINE]
    assert [e.action for e in audit.entries("lost")] == [PURGED_ORPHAN]
