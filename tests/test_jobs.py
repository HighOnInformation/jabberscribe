from pathlib import Path

import pytest

from jabberscribe.jobs import DONE, GROUPED, QUEUED, RUNNING, STAGE_ORDER, WAITING, JobStore, next_stage


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "js.db")
    store.init_schema()
    return store


def _create(store: JobStore, key: str = "c1_1042", *, conference_id: str | None = None) -> bool:
    return store.create(
        job_key=key,
        call_id=key.split("_")[0],
        conference_id=conference_id,
        audio_path=Path(f"/out/{key}/recording.wav"),
        out_dir=Path(f"/out/{key}"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=812,
    )


def test_create_then_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert _create(store) is True

    job = store.get("c1_1042")
    assert job is not None
    assert job.call_id == "c1"
    assert job.status == QUEUED
    assert job.stage == QUEUED
    assert job.audio_path == Path("/out/c1_1042/recording.wav")
    assert job.out_dir == Path("/out/c1_1042")
    assert job.conference_id is None
    assert job.grouped_into is None
    assert job.attempts == 0
    assert job.created_at


def test_duplicate_job_key_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    assert _create(store) is False
    assert len(store.list_all()) == 1


def test_conference_copy_starts_waiting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, conference_id="conf-1")

    assert store.get("c1_1042").status == WAITING


def test_claim_next_marks_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1")
    _create(store, "b_2")

    job = store.claim_next()

    assert job.job_key == "a_1"
    assert job.status == RUNNING


def test_claim_next_skips_waiting_grouped_and_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "w_1", conference_id="conf-1")
    _create(store, "p_2", conference_id="conf-1")
    store.set_status("p_2", QUEUED)
    store.group_into("w_1", "p_2")
    store.set_status("p_2", DONE)

    assert store.claim_next() is None


def test_running_job_is_reclaimed_after_crash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.claim_next()
    store.close()

    reopened = JobStore(tmp_path / "js.db")

    assert reopened.claim_next().job_key == "c1_1042"


def test_complete_stage_records_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    store.complete_stage("c1_1042", "stt")

    assert store.get("c1_1042").stage == "stt"


def test_record_attempt_counts_and_keeps_last_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    assert store.record_attempt("c1_1042", "first") == 1
    assert store.record_attempt("c1_1042", "second") == 2
    assert store.get("c1_1042").last_error == "second"


def test_record_attempt_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        _store(tmp_path).record_attempt("nope", "x")


def test_next_stage_walks_fixed_order() -> None:
    walked = []
    stage = QUEUED
    while (stage := next_stage(stage)) is not None:
        walked.append(stage)

    assert tuple(walked) == STAGE_ORDER == ("audio", "stt", "summarize", "output")


def test_next_stage_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="publish"):
        next_stage("publish")


def test_group_into_and_members(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")
    store.set_status("a_1", QUEUED)

    store.group_into("b_2", "a_1")

    member = store.get("b_2")
    assert member.status == GROUPED
    assert member.grouped_into == "a_1"
    assert [m.job_key for m in store.members("a_1")] == ["b_2"]


def test_waiting_conference_ids_are_distinct(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")
    _create(store, "c_3", conference_id="conf-2")
    _create(store, "d_4")

    assert store.waiting_conference_ids() == ["conf-1", "conf-2"]


def test_conference_jobs_in_arrival_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")

    assert [j.job_key for j in store.conference_jobs("conf-1")] == ["a_1", "b_2"]
