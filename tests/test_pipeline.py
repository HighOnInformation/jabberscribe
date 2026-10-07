import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jabberscribe import pipeline
from jabberscribe.audio import STT_FILENAME
from jabberscribe.jobs import DONE, FAILED, GROUPED, QUEUED
from jabberscribe.llm import TransientError
from jabberscribe.output import ACTIONS_FILE, RESULT_FILE, SUMMARY_FILE, TRANSCRIPT_FILE
from jabberscribe.pipeline import MAX_ATTEMPTS, backoff, run_job, run_once
from jabberscribe.stt import Segment, SttError
from jabberscribe.summarize import ActionItem, Summary
from jabberscribe.watcher import scan_once

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

SUMMARY = Summary("סיכום.", (ActionItem("לשלוח את הדוח", "דנה", None, "00:00:01"),))
T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe(self, audio: Path) -> list[Segment]:
        self.calls.append(audio)
        return [Segment(0.0, 1.5, "אה, שלום"), Segment(1.5, 3.0, "נדבר מחר")]


class FailingTranscriber:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def transcribe(self, audio: Path) -> list[Segment]:
        raise self.error


class FakeSummarizer:
    def __init__(self, result: Summary | None = SUMMARY) -> None:
        self.result = result
        self.seen: list[Segment] | None = None

    def summarize(self, segments: list[Segment]) -> Summary | None:
        self.seen = segments
        return self.result


class ExplodingSummarizer:
    def summarize(self, segments: list[Segment]) -> Summary | None:
        raise RuntimeError("summarizer bug")


def _enqueue(
    cfg, store, audit, make_wav, make_sidecar, *, call_id="abc", extension="1042", tracks="mixed", **extra
) -> str:
    # Two-channel audio either way. "mixed" keeps the one-downmix route these tests were written for;
    # the dual-track speaker route has its own tests below.
    make_wav(cfg.paths.inbox / f"{call_id}{extension}.wav", channels=2)
    make_sidecar(
        cfg.paths.inbox / f"{call_id}{extension}.json", call_id=call_id, extension=extension, tracks=tracks, **extra
    )
    scan_once(cfg, store, audit, min_age_seconds=0)
    return f"{call_id}_{extension}"


def _run_at(moment: datetime, cfg, store, transcriber, summarizer) -> int:
    """One pass at `moment` whose failures also happen at `moment`."""
    return run_once(cfg, store, transcriber, summarizer, now=moment, clock=lambda: moment)


def _result(job) -> dict:
    return json.loads((job.out_dir / RESULT_FILE).read_text(encoding="utf-8"))


def test_end_to_end_writes_outputs_and_marks_done(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    assert run_once(cfg, store, FakeTranscriber(), FakeSummarizer()) == 1

    job = store.get(key)
    assert job.status == DONE
    result = _result(job)
    assert result["transcript"][0]["text"] == "אה, שלום"
    assert result["summary"] == "סיכום."
    assert result["models"] == {"stt": "whisper-he", "summary": "gemma-3"}
    for name in ("recording.wav", TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE):
        assert (job.out_dir / name).is_file()


def test_result_records_stage_timings_and_latency(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    timings = _result(store.get(key))["timings"]
    assert set(timings) == {"audio_sec", "stt_sec", "summarize_sec", "hangup_to_output_sec"}
    assert timings["hangup_to_output_sec"] > 0


def test_summarizer_gets_the_transcript(cfg, store, audit, make_wav, make_sidecar) -> None:
    _enqueue(cfg, store, audit, make_wav, make_sidecar)
    summarizer = FakeSummarizer()

    run_once(cfg, store, FakeTranscriber(), summarizer)

    assert [s.text for s in summarizer.seen] == ["אה, שלום", "נדבר מחר"]


def test_stt_audio_is_removed_after_transcription(cfg, store, audit, make_wav, make_sidecar) -> None:
    """The Opus copy is the voice too; keeping it would dodge audio retention."""
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    assert not (cfg.paths.work_dir / key / STT_FILENAME).exists()


def test_unavailable_summary_still_completes(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer(None))

    job = store.get(key)
    assert job.status == DONE
    assert _result(job)["summary_available"] is False
    assert _result(job)["summary_error"] is None


def test_degraded_summary_ships_the_transcript_and_records_why(cfg, store, audit, make_wav, make_sidecar) -> None:
    from jabberscribe.summarize import SummaryUnavailable

    class TooLong:
        def summarize(self, segments: list[Segment]) -> Summary | None:
            raise SummaryUnavailable("chat route rejected the request: HTTP 400")

    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), TooLong())

    job = store.get(key)
    assert (job.status, job.attempts) == (DONE, 0)
    assert (job.out_dir / TRANSCRIPT_FILE).is_file()
    result = _result(job)
    assert result["summary_available"] is False
    assert result["summary_error"] == "chat route rejected the request: HTTP 400"


def test_a_summary_records_no_summary_error(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    assert _result(store.get(key))["summary_error"] is None


def test_resume_after_crash_does_not_retranscribe(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    _run_at(T0, cfg, store, FakeTranscriber(), ExplodingSummarizer())
    crashed = store.get(key)
    assert (crashed.status, crashed.stage, crashed.attempts) == (QUEUED, "stt", 1)
    assert "summarize: summarizer bug" in crashed.last_error

    transcriber = FakeTranscriber()
    _run_at(T0 + timedelta(minutes=1), cfg, store, transcriber, FakeSummarizer())

    assert store.get(key).status == DONE
    assert transcriber.calls == []


def test_transient_summary_outage_resumes_at_summarize(cfg, store, audit, make_wav, make_sidecar) -> None:
    """I9: a summary outage retries the summary; it never re-transcribes or drops it."""
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    class DownSummarizer:
        def summarize(self, segments: list[Segment]) -> Summary | None:
            raise TransientError("HTTP 503")

    _run_at(T0, cfg, store, FakeTranscriber(), DownSummarizer())
    waiting = store.get(key)
    assert (waiting.status, waiting.stage, waiting.attempts, waiting.transient_failures) == (QUEUED, "stt", 0, 1)

    transcriber = FakeTranscriber()
    _run_at(T0 + timedelta(minutes=1), cfg, store, transcriber, FakeSummarizer())

    job = store.get(key)
    assert job.status == DONE
    assert _result(job)["summary"] == SUMMARY.text
    assert transcriber.calls == []


def test_failed_job_waits_for_its_backoff(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    _run_at(T0, cfg, store, FailingTranscriber(SttError("bad audio")), FakeSummarizer())

    assert store.get(key).next_attempt_at == "2026-10-07T12:00:30+00:00"
    assert _run_at(T0 + timedelta(seconds=29), cfg, store, FakeTranscriber(), FakeSummarizer()) == 0
    assert _run_at(T0 + timedelta(seconds=30), cfg, store, FakeTranscriber(), FakeSummarizer()) == 1


def test_backoff_counts_from_the_failure_not_the_pass_start(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A pass slower than the backoff must not leave the job due at once."""
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    failed_at = T0 + timedelta(minutes=5)

    run_once(cfg, store, FailingTranscriber(SttError("bad audio")), FakeSummarizer(), now=T0, clock=lambda: failed_at)

    assert store.get(key).next_attempt_at == "2026-10-07T12:05:30+00:00"
    assert _run_at(failed_at, cfg, store, FakeTranscriber(), FakeSummarizer()) == 0


def test_permanent_failure_gives_up_and_deletes_the_stt_copy(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    transcriber = FailingTranscriber(SttError("HTTP 413 too large"))

    for hour in range(MAX_ATTEMPTS):
        assert store.get(key).status == QUEUED
        _run_at(T0 + timedelta(hours=hour), cfg, store, transcriber, FakeSummarizer())
        if hour == 0:
            assert (cfg.paths.work_dir / key / STT_FILENAME).is_file()

    job = store.get(key)
    assert job.status == FAILED
    assert "413" in job.last_error
    assert not (cfg.paths.work_dir / key / STT_FILENAME).exists()


def test_transient_failure_never_fails_the_job(cfg, store, audit, make_wav, make_sidecar) -> None:
    """An hour-long LiteLLM outage delays calls; it must not lose them."""
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    transcriber = FailingTranscriber(TransientError("HTTP 503"))

    for hour in range(10):
        _run_at(T0 + timedelta(hours=hour), cfg, store, transcriber, FakeSummarizer())

    job = store.get(key)
    assert (job.status, job.attempts, job.transient_failures) == (QUEUED, 0, 10)
    _run_at(T0 + timedelta(hours=10), cfg, store, FakeTranscriber(), FakeSummarizer())
    assert store.get(key).status == DONE


def test_an_outage_does_not_spend_the_attempt_budget(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    down = FailingTranscriber(TransientError("HTTP 503"))
    broken = FailingTranscriber(SttError("bad audio"))
    for hour in range(10):
        _run_at(T0 + timedelta(hours=hour), cfg, store, down, FakeSummarizer())

    _run_at(T0 + timedelta(hours=10), cfg, store, broken, FakeSummarizer())
    job = store.get(key)
    assert (job.status, job.attempts) == (QUEUED, 1)

    for hour in (11, 12):
        _run_at(T0 + timedelta(hours=hour), cfg, store, broken, FakeSummarizer())
    assert store.get(key).status == FAILED


def test_backoff_grows_with_transient_failures_too(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    down = FailingTranscriber(TransientError("HTTP 503"))
    for hour in range(3):
        _run_at(T0 + timedelta(hours=hour), cfg, store, down, FakeSummarizer())

    assert store.get(key).next_attempt_at == "2026-10-07T14:02:00+00:00"


def test_locked_output_file_is_transient(cfg, store, audit, make_wav, make_sidecar, monkeypatch) -> None:
    """A recording held open on the share locks the file; that is not bad data."""
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    real_write_outputs = pipeline.write_outputs
    locked = {"left": 5}

    def flaky_write_outputs(*args, **kwargs):
        if locked["left"]:
            locked["left"] -= 1
            raise PermissionError(13, "The process cannot access the file")
        return real_write_outputs(*args, **kwargs)

    monkeypatch.setattr(pipeline, "write_outputs", flaky_write_outputs)
    for hour in range(5):
        _run_at(T0 + timedelta(hours=hour), cfg, store, FakeTranscriber(), FakeSummarizer())

    job = store.get(key)
    assert (job.status, job.attempts, job.transient_failures) == (QUEUED, 0, 5)
    _run_at(T0 + timedelta(hours=5), cfg, store, FakeTranscriber(), FakeSummarizer())
    assert store.get(key).status == DONE


def test_missing_segments_at_output_is_not_transient(cfg, store, audit, make_wav, make_sidecar) -> None:
    """Only the output write itself is transient; a lost checkpoint file is a real failure."""
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    class LoseSegments(FakeSummarizer):
        def summarize(self, segments: list[Segment]) -> Summary | None:
            (cfg.paths.work_dir / key / pipeline.SEGMENTS_FILE).unlink()
            return super().summarize(segments)

    _run_at(T0, cfg, store, FakeTranscriber(), LoseSegments())
    for hour in range(1, MAX_ATTEMPTS):
        _run_at(T0 + timedelta(hours=hour), cfg, store, FakeTranscriber(), FakeSummarizer())

    job = store.get(key)
    assert (job.status, job.attempts, job.transient_failures) == (FAILED, MAX_ATTEMPTS, 0)
    assert job.last_error.startswith("output:")


def test_job_handed_over_mid_run_is_not_marked_done(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    class HandOverDuringStt(FakeTranscriber):
        def transcribe(self, audio: Path) -> list[Segment]:
            store.set_status(key, GROUPED)
            return super().transcribe(audio)

    _run_at(T0, cfg, store, HandOverDuringStt(), FakeSummarizer())

    assert store.get(key).status == GROUPED


def test_job_handed_over_mid_run_is_not_requeued_or_failed(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    class HandOverThenFail:
        def transcribe(self, audio: Path) -> list[Segment]:
            store.set_status(key, GROUPED)
            raise SttError("bad audio")

    for hour in range(MAX_ATTEMPTS):
        store.set_status(key, QUEUED)
        _run_at(T0 + timedelta(hours=hour), cfg, store, HandOverThenFail(), FakeSummarizer())
        job = store.get(key)
        assert (job.status, job.next_attempt_at) == (GROUPED, None)


def test_a_failing_job_does_not_block_the_queue(cfg, store, audit, make_wav, make_sidecar) -> None:
    first = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="aaa")
    second = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="bbb")

    class FailFirst(FakeTranscriber):
        def transcribe(self, audio: Path) -> list[Segment]:
            if first in str(audio):
                raise SttError("poisoned")
            return super().transcribe(audio)

    assert _run_at(T0, cfg, store, FailFirst(), FakeSummarizer()) == 2
    assert store.get(first).status == QUEUED
    assert store.get(second).status == DONE


def test_a_job_not_yet_due_does_not_block_a_due_one(cfg, store, audit, make_wav, make_sidecar) -> None:
    first = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="aaa")
    second = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="bbb")
    store.schedule_retry(first, T0 + timedelta(hours=1))

    assert _run_at(T0, cfg, store, FakeTranscriber(), FakeSummarizer()) == 1

    assert store.get(first).status == QUEUED
    assert store.get(second).status == DONE


def test_backoff_doubles_up_to_thirty_minutes() -> None:
    assert [backoff(n).total_seconds() for n in (1, 2, 3, 6, 7, 20)] == [30, 60, 120, 960, 1800, 1800]


def test_unreadable_sidecar_counts_as_an_attempt(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    store.scrub_sidecar(key)

    for hour in range(MAX_ATTEMPTS):
        _run_at(T0 + timedelta(hours=hour), cfg, store, FakeTranscriber(), FakeSummarizer())

    assert store.get(key).status == FAILED


def test_grouped_copies_are_listed_as_owners(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _enqueue(
        cfg, store, audit, make_wav, make_sidecar, call_id="leg1", extension="1042", conference_id="conf-1"
    )
    member = _enqueue(
        cfg, store, audit, make_wav, make_sidecar, call_id="leg2", extension="3000", conference_id="conf-1"
    )
    store.set_status(primary, QUEUED)
    store.group_into(member, primary)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    assert [o["extension"] for o in _result(store.get(primary))["owners"]] == ["1042", "3000"]


def test_run_job_processes_only_that_job_even_if_not_due(cfg, store, audit, make_wav, make_sidecar) -> None:
    mine = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="mine")
    other = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="other")
    store.schedule_retry(mine, datetime.now(UTC) + timedelta(hours=1))

    assert run_job(mine, cfg, store, FakeTranscriber(), FakeSummarizer()) is True

    assert store.get(mine).status == DONE
    assert store.get(other).status == QUEUED


def test_run_job_refuses_a_finished_job(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    store.set_status(key, DONE)

    assert run_job(key, cfg, store, FakeTranscriber(), FakeSummarizer()) is False


def test_summary_rejection_fails_only_the_primary_of_a_conference(cfg, store, audit, make_wav, make_sidecar) -> None:
    """Reviewer probe: a summary 4xx must not walk re-election through every copy."""
    from jabberscribe.audit import SUPERSEDED
    from jabberscribe.group import requeue_failed, settle
    from jabberscribe.summarize import SummaryError

    class RejectingSummarizer:
        def summarize(self, segments: list[Segment]) -> Summary | None:
            raise SummaryError("summary request rejected: 401")

    keys = [
        _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id=leg, extension=ext, conference_id="m", **extra)
        for leg, ext, extra in (("a", "1", {"duration_sec": 300}), ("b", "2", {"duration_sec": 200}),
                                ("c", "3", {"duration_sec": 100}))
    ]
    longest, *others = keys
    transcriber = FakeTranscriber()
    base = datetime.now(UTC) + timedelta(days=1)
    for hour in range(3 * MAX_ATTEMPTS):
        settle(cfg, store, audit, base + timedelta(hours=hour))
        _run_at(base + timedelta(hours=hour), cfg, store, transcriber, RejectingSummarizer())

    assert (store.get(longest).status, store.get(longest).grouped_into) == (FAILED, None)
    assert {(store.get(k).status, store.get(k).grouped_into) for k in others} == {(GROUPED, longest)}
    assert len(transcriber.calls) == 1
    assert (cfg.paths.work_dir / longest / pipeline.SEGMENTS_FILE).is_file()
    assert all(e.action != SUPERSEDED for k in keys for e in audit.entries(k))

    assert requeue_failed(store, longest) == longest
    assert (store.get(longest).status, store.get(longest).stage) == (QUEUED, "stt")


def test_output_records_the_latency_for_status(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    job = store.get(key)
    assert job.latency_sec == _result(job)["timings"]["hangup_to_output_sec"]
    assert job.output_at is not None


class ChannelTranscriber(FakeTranscriber):
    """Answers each channel file with one line named after it, the far end one second later."""

    def transcribe(self, audio: Path) -> list[Segment]:
        self.calls.append(audio)
        start = 0.0 if audio.name == "stt-ch0.ogg" else 1.0
        return [Segment(start, start + 0.5, audio.stem)]


def test_dual_track_call_is_labelled_near_and_far(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar, tracks="dual")
    transcriber = ChannelTranscriber()
    summarizer = FakeSummarizer()

    run_once(cfg, store, transcriber, summarizer)

    job = store.get(key)
    assert [p.name for p in transcriber.calls] == ["stt-ch0.ogg", "stt-ch1.ogg"]
    transcript = _result(job)["transcript"]
    assert [(s["speaker"], s["text"]) for s in transcript] == [("מאיר", "stt-ch0"), ("דנה", "stt-ch1")]
    assert "[00:00:00] מאיר: stt-ch0" in (job.out_dir / TRANSCRIPT_FILE).read_text(encoding="utf-8")
    assert [s.speaker for s in summarizer.seen] == ["מאיר", "דנה"]
    assert list((cfg.paths.work_dir / key).glob("stt*.ogg*")) == []


def test_mixed_track_call_has_no_speakers(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    assert [s["speaker"] for s in _result(store.get(key))["transcript"]] == [None, None]
