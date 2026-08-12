import json
from pathlib import Path

from jabberscribe.jobs import (
    DONE,
    FAILED,
    QUEUED,
    RUNNING,
    JobStore,
    next_stage,
)

PIPELINE = ("audio", "stt")


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "js.db")
    store.init_schema()
    return store


def _create(store: JobStore, call_id: str = "c1") -> bool:
    return store.create(
        call_id=call_id,
        audio_path=Path(f"/inbox/{call_id}.wav"),
        sidecar_json=json.dumps({"call_id": call_id}),
        kind="call",
        started_at="2026-08-12T14:03:11+03:00",
        duration_sec=812,
    )


def test_create_then_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert _create(store) is True

    job = store.get("c1")
    assert job is not None
    assert job.status == QUEUED
    assert job.stage == QUEUED
    assert job.duration_sec == 812
    assert job.attempts == 0
    assert job.transcript_path is None


def test_create_is_idempotent_on_call_id(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert _create(store) is True
    assert _create(store) is False

    assert len(store.list_by_status(QUEUED)) == 1


def test_claim_next_marks_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    job = store.claim_next()

    assert job is not None
    assert job.call_id == "c1"
    assert store.get("c1").status == RUNNING


def test_claim_next_returns_none_when_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).claim_next() is None


def test_claim_next_ignores_done_and_failed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "done_one")
    _create(store, "failed_one")
    store.set_status("done_one", DONE)
    store.set_status("failed_one", FAILED, last_error="boom")

    assert store.claim_next() is None


def test_crash_mid_pipeline_resumes_at_next_stage(tmp_path: Path) -> None:
    """The core guarantee: a restart must not re-transcribe completed work."""
    db = tmp_path / "js.db"
    store = JobStore(db)
    store.init_schema()
    _create(store)
    store.claim_next()
    store.complete_stage("c1", "audio")
    store.close()  # simulate the process dying here

    reopened = JobStore(db)
    job = reopened.claim_next()

    assert job is not None
    assert job.stage == "audio"
    assert next_stage(job.stage, PIPELINE) == "stt"


def test_next_stage_returns_none_at_end_of_pipeline() -> None:
    assert next_stage(QUEUED, PIPELINE) == "audio"
    assert next_stage("audio", PIPELINE) == "stt"
    assert next_stage("stt", PIPELINE) is None


def test_next_stage_skips_stages_outside_the_pipeline() -> None:
    assert next_stage("audio", ("audio", "render")) == "render"


def test_record_attempt_increments_and_stores_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    assert store.record_attempt("c1", "ffmpeg exploded") == 1
    assert store.record_attempt("c1", "ffmpeg exploded again") == 2

    job = store.get("c1")
    assert job.attempts == 2
    assert job.last_error == "ffmpeg exploded again"


def test_set_transcript_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    store.set_transcript_path("c1", Path("/work/c1/transcript.json"))

    assert store.get("c1").transcript_path == Path("/work/c1/transcript.json")


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    JobStore(db).init_schema()
    store = JobStore(db)
    store.init_schema()

    assert _create(store) is True
