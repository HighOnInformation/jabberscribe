import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jabberscribe.jobs import (
    _SCHEMA,
    DONE,
    FAILED,
    GROUPED,
    QUEUED,
    RUNNING,
    SCRUBBED_SIDECAR,
    STAGE_ORDER,
    WAITING,
    JobStore,
    SchemaError,
    next_stage,
)


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
    assert job.transient_failures == 0
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


def test_record_transient_counts_apart_from_attempts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    assert store.record_transient("c1_1042", "HTTP 503") == 1
    assert store.record_transient("c1_1042", "HTTP 502") == 2

    job = store.get("c1_1042")
    assert (job.attempts, job.transient_failures, job.last_error) == (0, 2, "HTTP 502")


def test_record_transient_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        _store(tmp_path).record_transient("nope", "x")


def test_finish_applies_only_to_a_running_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1")
    _create(store, "b_2")
    store.claim("a_1")

    assert store.finish("a_1", DONE) is True
    assert store.finish("b_2", DONE) is False

    assert (store.get("a_1").status, store.get("b_2").status) == (DONE, QUEUED)


def test_finish_accepts_only_terminal_statuses(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.claim("c1_1042")

    with pytest.raises(ValueError, match="queued"):
        store.finish("c1_1042", QUEUED)
    assert store.get("c1_1042").status == RUNNING


def test_retry_later_applies_only_to_a_running_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")
    store.set_status("a_1", QUEUED)
    store.claim("a_1")
    store.group_into("b_2", "a_1")

    assert store.retry_later("a_1", NOW) is True
    assert store.retry_later("b_2", NOW) is False

    assert (store.get("a_1").status, store.get("a_1").next_attempt_at) == (QUEUED, "2026-10-07T12:00:00+00:00")
    assert (store.get("b_2").status, store.get("b_2").next_attempt_at) == (GROUPED, None)


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


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def test_failed_job_is_not_claimed_before_it_is_due(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1")
    _create(store, "b_2")
    store.schedule_retry("a_1", NOW + timedelta(seconds=30))

    claimed = store.claim_next(NOW)

    assert claimed.job_key == "b_2"
    assert store.get("a_1").status == QUEUED
    assert store.get("a_1").next_attempt_at == "2026-10-07T12:00:30+00:00"


def test_failed_job_is_claimed_once_due(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1")
    store.schedule_retry("a_1", NOW + timedelta(seconds=30))

    assert store.claim_next(NOW + timedelta(seconds=30)).job_key == "a_1"


def test_claim_takes_a_specific_job_even_if_not_due(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1")
    _create(store, "b_2")
    store.schedule_retry("b_2", NOW + timedelta(hours=1))

    assert store.claim("b_2").status == RUNNING
    assert store.get("a_1").status == QUEUED


def test_claim_refuses_a_finished_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.set_status("c1_1042", DONE)

    assert store.claim("c1_1042") is None


def test_requeue_resets_attempts_of_a_failed_primary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.record_attempt("c1_1042", "boom")
    store.schedule_retry("c1_1042", NOW + timedelta(hours=1))
    store.set_status("c1_1042", FAILED)

    assert store.requeue("c1_1042") is True

    job = store.get("c1_1042")
    assert (job.status, job.attempts, job.next_attempt_at) == (QUEUED, 0, None)


def test_requeue_also_resets_transient_failures(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.record_transient("c1_1042", "HTTP 503")
    store.set_status("c1_1042", FAILED)

    assert store.requeue("c1_1042") is True

    assert store.get("c1_1042").transient_failures == 0


def test_requeue_refuses_jobs_that_did_not_fail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    assert store.requeue("c1_1042") is False
    assert store.requeue("missing") is False


def test_reset_job_starts_a_copy_from_scratch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")
    store.group_into("b_2", "a_1")
    store.complete_stage("b_2", "output")
    store.record_attempt("b_2", "x")
    store.record_transient("b_2", "y")

    store.reset_job("b_2")

    job = store.get("b_2")
    assert (job.status, job.stage, job.grouped_into, job.attempts, job.last_error) == (QUEUED, QUEUED, None, 0, None)
    assert job.transient_failures == 0


def test_hand_over_moves_members_and_demotes_the_old_primary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for key in ("a_1", "b_2", "c_3"):
        _create(store, key, conference_id="conf-1")
    store.set_status("a_1", DONE)
    store.group_into("b_2", "a_1")
    store.group_into("c_3", "a_1")

    store.hand_over("a_1", "c_3")
    store.reset_job("c_3")

    assert store.get("a_1").status == GROUPED
    assert {m.job_key for m in store.members("c_3")} == {"a_1", "b_2"}
    assert store.get("c_3").grouped_into is None


def test_hand_over_keeps_a_failed_primary_failed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")
    store.set_status("a_1", FAILED)

    store.hand_over("a_1", "b_2")

    assert (store.get("a_1").status, store.get("a_1").grouped_into) == (FAILED, "b_2")
    assert store.requeue("a_1") is False


def test_conference_ids_to_settle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "w_1", conference_id="waiting")
    _create(store, "f_1", conference_id="failed")
    _create(store, "m_2", conference_id="failed")
    store.set_status("f_1", FAILED)
    store.group_into("m_2", "f_1")
    _create(store, "x_1", conference_id="failed-alone")
    store.set_status("x_1", FAILED)
    _create(store, "d_1", conference_id="done")
    store.set_status("d_1", DONE)

    assert store.conference_ids_to_settle() == ["failed", "waiting"]


def test_scrub_sidecar_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(
        job_key="c1_1042",
        call_id="c1",
        conference_id=None,
        audio_path=Path("/out/c1_1042/recording.wav"),
        out_dir=Path("/out/c1_1042"),
        sidecar_json='{"call_id": "c1", "parties": [{"display_name": "דנה"}]}',
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=812,
    )

    assert store.scrub_sidecar("c1_1042") is True
    assert store.scrub_sidecar("c1_1042") is False
    assert store.get("c1_1042").sidecar_json == SCRUBBED_SIDECAR


def test_init_schema_refuses_an_older_database(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "js.db")
    conn.execute("CREATE TABLE jobs (call_id TEXT PRIMARY KEY)")
    conn.close()

    with pytest.raises(SchemaError, match="fresh file"):
        JobStore(tmp_path / "js.db").init_schema()


def test_init_schema_adds_transient_failures_to_an_early_v2_database(tmp_path: Path) -> None:
    early = _SCHEMA.replace("  transient_failures INTEGER NOT NULL DEFAULT 0,\n", "")
    assert early != _SCHEMA
    conn = sqlite3.connect(tmp_path / "js.db")
    conn.executescript(early)
    conn.execute("PRAGMA user_version = 2")
    conn.close()

    store = _store(tmp_path)
    _create(store)

    assert store.record_transient("c1_1042", "HTTP 503") == 1
    assert store.get("c1_1042").transient_failures == 1


def test_init_schema_accepts_its_own_database_again(tmp_path: Path) -> None:
    _store(tmp_path).close()

    _store(tmp_path)
