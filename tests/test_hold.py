import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from jabberscribe.audit import LEGAL_HOLD_RELEASED, LEGAL_HOLD_SET, PURGED_AUDIO, SUPERSEDED, UNHOLD_FAILED, AuditLog
from jabberscribe.cli import main
from jabberscribe.group import settle
from jabberscribe.jobs import DONE, SCRUBBED_SIDECAR, JobStore
from jabberscribe.output import RESULT_FILE, TEXT_FILES, TRANSCRIPT_FILE
from jabberscribe.retention import purge
from jabberscribe.watcher import scan_once

# Matches the make_sidecar default started_at.
STARTED = datetime(2026, 10, 7, 14, 3, 11, tzinfo=timezone(timedelta(hours=3)))


def _job(store, key: str, *, conference_id: str | None = None) -> None:
    store.create(
        job_key=key,
        call_id=key.split("_")[0],
        conference_id=conference_id,
        audio_path=Path(f"/out/{key}/recording.wav"),
        out_dir=Path(f"/out/{key}"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )


def _set_created(cfg, key: str, created: datetime) -> None:
    conn = sqlite3.connect(cfg.paths.db_path)
    conn.execute("UPDATE jobs SET created_at = ? WHERE job_key = ?", (created.isoformat(timespec="seconds"), key))
    conn.commit()
    conn.close()


def _ingested_call(cfg, store, audit, make_wav, make_sidecar):
    make_wav(cfg.paths.inbox / "r.wav")
    make_sidecar(cfg.paths.inbox / "r.json", call_id="r1")
    scan_once(cfg, store, audit, min_age_seconds=0)
    _set_created(cfg, "r1_1042", STARTED)
    # A finished call: purge keeps the text of a job still in the pipeline (ACTIVE) whatever its age.
    store.set_status("r1_1042", DONE)
    job = store.get("r1_1042")
    for name in TEXT_FILES:
        (job.out_dir / name).write_text("x", encoding="utf-8")
    return job


def test_hold_and_release(store) -> None:
    _job(store, "a_1")

    assert store.hold("a_1", "תביעה 2026-17") is True
    job = store.get("a_1")
    assert (job.legal_hold, job.hold_reason) == (True, "תביעה 2026-17")
    assert store.is_held("a_1")
    assert [j.job_key for j in store.held_jobs()] == ["a_1"]

    assert store.release_hold("a_1") is True
    job = store.get("a_1")
    assert (job.legal_hold, job.hold_reason) == (False, None)
    assert not store.is_held("a_1")


def test_unknown_or_unheld_jobs_are_refused(store) -> None:
    assert store.hold("nope_1", "x") is False
    _job(store, "a_1")

    assert store.release_hold("a_1") is False


def test_a_hold_on_any_copy_covers_the_whole_conference(store) -> None:
    for key in ("p_1", "m_2", "n_3"):
        _job(store, key, conference_id="conf-1")
    _job(store, "x_4")
    store.group_into("m_2", "p_1")
    store.group_into("n_3", "p_1")

    store.hold("m_2", "member held")
    assert [store.is_held(k) for k in ("p_1", "m_2", "n_3", "x_4")] == [True, True, True, False]

    store.release_hold("m_2")
    store.hold("p_1", "primary held")
    assert [store.is_held(k) for k in ("p_1", "m_2", "n_3", "x_4")] == [True, True, True, False]


def test_purge_keeps_a_held_call_past_both_retention_windows(cfg, store, audit, make_wav, make_sidecar) -> None:
    job = _ingested_call(cfg, store, audit, make_wav, make_sidecar)
    store.hold(job.job_key, "litigation")

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=400))

    assert result.held == (job.job_key,)
    assert (result.audio_deleted, result.text_deleted) == ((), ())
    assert job.audio_path.is_file()
    assert (job.out_dir / RESULT_FILE).is_file()
    assert store.get(job.job_key).sidecar_json != SCRUBBED_SIDECAR


def test_purge_resumes_once_the_hold_is_released(cfg, store, audit, make_wav, make_sidecar) -> None:
    job = _ingested_call(cfg, store, audit, make_wav, make_sidecar)
    store.hold(job.job_key, "litigation")
    purge(cfg, store, audit, now=STARTED + timedelta(days=400))
    store.release_hold(job.job_key)

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=400))

    assert (result.audio_deleted, result.text_deleted, result.held) == ((job.job_key,), (job.job_key,), ())
    assert PURGED_AUDIO in [e.action for e in audit.entries(job.job_key)]


def _drop(cfg, store, audit, make_wav, make_sidecar, leg: str, ext: str, **extra) -> str:
    make_wav(cfg.paths.inbox / f"{leg}.wav")
    make_sidecar(cfg.paths.inbox / f"{leg}.json", call_id=leg, extension=ext, conference_id="conf-1", **extra)
    scan_once(cfg, store, audit, min_age_seconds=0)
    return f"{leg}_{ext}"


def test_a_held_primary_keeps_its_outputs_when_a_longer_copy_replaces_it(
    cfg, store, audit, make_wav, make_sidecar
) -> None:
    start = "2026-10-07T14:00:00+03:00"
    leaver = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at=start, duration_sec=480)
    _set_created(cfg, leaver, datetime.now(UTC) - timedelta(seconds=61))
    settle(cfg, store, audit, datetime.now(UTC))
    out_dir = store.get(leaver).out_dir
    (out_dir / RESULT_FILE).write_text(json.dumps({"owners": []}), encoding="utf-8")
    (out_dir / TRANSCRIPT_FILE).write_text("x", encoding="utf-8")
    store.set_status(leaver, DONE)
    store.hold(leaver, "litigation")

    host = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at=start, duration_sec=3600)
    result = settle(cfg, store, audit, datetime.now(UTC))

    assert result.superseded == (leaver,)
    assert result.released == (host,)
    assert (out_dir / RESULT_FILE).is_file()
    assert (out_dir / TRANSCRIPT_FILE).is_file()
    entry = audit.entries(leaver)[-1]
    assert entry.action == SUPERSEDED
    assert "legal hold" in entry.detail


def test_hold_command_sets_the_hold_and_audits_the_actor(cfg_file, store, tmp_path, capsys) -> None:
    _job(store, "a_1")

    assert main(["--config", str(cfg_file), "hold", "a_1", "--reason", "תביעה 17"]) == 0

    assert "a_1: on legal hold" in capsys.readouterr().out
    assert store.get("a_1").legal_hold is True
    entry = AuditLog(tmp_path / "js.db").entries("a_1")[-1]
    assert (entry.action, entry.detail) == (LEGAL_HOLD_SET, "תביעה 17")
    assert entry.actor


def test_unhold_command_releases_and_audits(cfg_file, store, tmp_path, capsys) -> None:
    _job(store, "a_1")
    main(["--config", str(cfg_file), "hold", "a_1", "--reason", "x"])

    assert main(["--config", str(cfg_file), "unhold", "a_1", "--reason", "case closed"]) == 0

    assert store.get("a_1").legal_hold is False
    entry = AuditLog(tmp_path / "js.db").entries("a_1")[-1]
    assert (entry.action, entry.detail) == (LEGAL_HOLD_RELEASED, "case closed")
    assert main(["--config", str(cfg_file), "unhold", "a_1"]) == 1
    assert "not on legal hold" in capsys.readouterr().err


def test_hold_refuses_an_unknown_job_and_a_blank_reason(cfg_file, store, capsys) -> None:
    _job(store, "a_1")

    assert main(["--config", str(cfg_file), "hold", "nope_1", "--reason", "x"]) == 1
    assert main(["--config", str(cfg_file), "hold", "a_1", "--reason", "   "]) == 1

    err = capsys.readouterr().err
    assert "unknown job" in err
    assert "reason is required" in err
    assert store.get("a_1").legal_hold is False


def test_status_lists_held_calls(cfg_file, store, capsys) -> None:
    _job(store, "a_1")
    store.hold("a_1", "litigation")

    assert main(["--config", str(cfg_file), "status"]) == 0

    assert "h a_1: on legal hold: litigation" in capsys.readouterr().out


def _failing_record(monkeypatch, failing_action: str) -> None:
    real = AuditLog.record

    def record(self, job_key: str, action: str, detail: str = "") -> None:
        if action == failing_action:
            raise sqlite3.OperationalError("disk I/O error")
        real(self, job_key, action, detail)

    monkeypatch.setattr(AuditLog, "record", record)


def test_unhold_keeps_the_hold_when_the_audit_row_cannot_be_written(cfg_file, store, monkeypatch, capsys) -> None:
    _job(store, "a_1")
    store.hold("a_1", "litigation")
    _failing_record(monkeypatch, LEGAL_HOLD_RELEASED)

    assert main(["--config", str(cfg_file), "unhold", "a_1", "--reason", "case closed"]) == 1

    assert store.get("a_1").legal_hold is True
    assert "hold kept" in capsys.readouterr().err


def test_unhold_audits_a_release_that_failed(cfg_file, store, tmp_path, monkeypatch, capsys) -> None:
    _job(store, "a_1")
    store.hold("a_1", "litigation")

    def broken_release(self, job_key: str) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(JobStore, "release_hold", broken_release)

    assert main(["--config", str(cfg_file), "unhold", "a_1", "--reason", "case closed"]) == 1

    assert store.get("a_1").legal_hold is True
    actions = [e.action for e in AuditLog(tmp_path / "js.db").entries("a_1")]
    assert actions[-2:] == [LEGAL_HOLD_RELEASED, UNHOLD_FAILED]
    assert "database is locked" in capsys.readouterr().err


def _conference(store) -> None:
    for key in ("p_1", "m_2", "n_3"):
        _job(store, key, conference_id="conf-1")
    store.group_into("m_2", "p_1")
    store.group_into("n_3", "p_1")


def test_unhold_names_the_copy_that_holds_the_call(cfg_file, store, capsys) -> None:
    _conference(store)
    store.hold("p_1", "litigation")

    assert main(["--config", str(cfg_file), "unhold", "m_2"]) == 1

    err = capsys.readouterr().err
    assert "m_2: not on legal hold itself" in err
    assert "p_1" in err
    assert store.get("p_1").legal_hold is True


def test_unhold_says_when_other_copies_remain_held(cfg_file, store, capsys) -> None:
    _conference(store)
    store.hold("p_1", "litigation")
    store.hold("n_3", "litigation")

    assert main(["--config", str(cfg_file), "unhold", "n_3", "--reason", "x"]) == 0

    out = capsys.readouterr().out
    assert "n_3: legal hold released" in out
    assert "still held by p_1" in out
