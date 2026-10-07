from pathlib import Path

from jabberscribe.audit import MAILED, PUBLISHED, AuditLog


def _log(tmp_path: Path) -> AuditLog:
    log = AuditLog(tmp_path / "js.db", actor="svc-jabberscribe")
    log.init_schema()
    return log


def test_record_and_read_back(tmp_path: Path) -> None:
    log = _log(tmp_path)

    log.record("c1", PUBLISHED, "page 998")

    entries = log.entries("c1")
    assert len(entries) == 1
    assert entries[0].action == PUBLISHED
    assert entries[0].detail == "page 998"
    assert entries[0].actor == "svc-jabberscribe"
    assert entries[0].at


def test_entries_are_scoped_by_call(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.record("c1", PUBLISHED, "")
    log.record("c2", MAILED, "meir@corp.local")

    assert [e.call_id for e in log.entries("c2")] == ["c2"]
    assert len(log.entries()) == 2


def test_log_is_append_only_and_ordered(tmp_path: Path) -> None:
    log = _log(tmp_path)
    for i in range(3):
        log.record("c1", PUBLISHED, f"attempt {i}")

    assert [e.detail for e in log.entries("c1")] == ["attempt 0", "attempt 1", "attempt 2"]


def test_actor_defaults_to_os_account(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "js.db")
    log.init_schema()

    log.record("c1", PUBLISHED, "")

    assert log.entries("c1")[0].actor


def test_shares_a_database_with_the_job_store(tmp_path: Path) -> None:
    """Audit rows and job rows must live in one file so they cannot diverge."""
    from jabberscribe.jobs import JobStore

    db = tmp_path / "js.db"
    store = JobStore(db)
    store.init_schema()
    log = AuditLog(db)
    log.init_schema()

    store.create(
        job_key="c1",
        call_id="c1",
        conference_id=None,
        audio_path=Path("/a.wav"),
        out_dir=Path("/out/c1"),
        sidecar_json="{}",
        started_at="2026-08-12T14:03:11+03:00",
        duration_sec=5,
    )
    log.record("c1", PUBLISHED, "page 1")

    assert store.get("c1") is not None
    assert len(log.entries("c1")) == 1


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    AuditLog(db).init_schema()
    log = AuditLog(db)
    log.init_schema()

    log.record("c1", PUBLISHED, "")
    assert len(log.entries("c1")) == 1
