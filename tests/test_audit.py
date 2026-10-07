from pathlib import Path

from jabberscribe.audit import PURGED_AUDIO, PURGED_TEXT, AuditLog
from jabberscribe.jobs import JobStore


def _log(tmp_path: Path, actor: str | None = "svc") -> AuditLog:
    log = AuditLog(tmp_path / "js.db", actor=actor)
    log.init_schema()
    return log


def test_records_in_order(tmp_path: Path) -> None:
    log = _log(tmp_path)

    log.record("k1", PURGED_AUDIO, "a")
    log.record("k1", PURGED_TEXT, "b")

    entries = log.entries("k1")
    assert [(e.job_key, e.action, e.detail, e.actor) for e in entries] == [
        ("k1", PURGED_AUDIO, "a", "svc"),
        ("k1", PURGED_TEXT, "b", "svc"),
    ]
    assert all(e.at for e in entries)


def test_filters_by_job_key(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.record("k1", PURGED_AUDIO)
    log.record("k2", PURGED_AUDIO)

    assert [e.job_key for e in log.entries("k2")] == ["k2"]
    assert len(log.entries()) == 2


def test_actor_defaults_to_os_account(tmp_path: Path) -> None:
    log = _log(tmp_path, actor=None)

    log.record("k1", PURGED_AUDIO)

    assert log.entries("k1")[0].actor


def test_shares_a_database_with_the_job_store(tmp_path: Path) -> None:
    """Audit rows and job rows live in one file so they cannot diverge."""
    store = JobStore(tmp_path / "js.db")
    store.init_schema()
    log = _log(tmp_path)

    store.create(
        job_key="k1",
        call_id="c1",
        conference_id=None,
        audio_path=Path("/a.wav"),
        out_dir=Path("/out/k1"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )
    log.record("k1", PURGED_AUDIO)

    assert store.get("k1") is not None
    assert len(log.entries("k1")) == 1


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    _log(tmp_path)
    log = _log(tmp_path)

    log.record("k1", PURGED_AUDIO)

    assert len(log.entries("k1")) == 1
