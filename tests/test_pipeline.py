import json
from pathlib import Path

import pytest

from jabberscribe.jobs import DONE, FAILED, JobStore
from jabberscribe.pipeline import StageNotImplementedError, process_job, run_once
from jabberscribe.stt import Segment, SttError
from jabberscribe.watcher import scan_once


class FakeTranscriber:
    """Returns one segment per call and counts invocations."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe(self, wav: Path) -> list[Segment]:
        self.calls.append(wav)
        return [Segment(0.0, 1.5, f"טקסט מ{wav.stem}")]


class ExplodingTranscriber:
    def transcribe(self, wav: Path) -> list[Segment]:
        raise SttError("model on fire")


def _store(cfg) -> JobStore:
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    return store


def _enqueue(cfg, store, make_wav, make_sidecar, *, call_id="abc", tracks="mixed") -> None:
    channels = 2 if tracks == "dual" else 1
    make_wav(cfg.paths.inbox / "in.wav", channels=channels)
    make_sidecar(cfg.paths.inbox / "in.json", call_id=call_id, tracks=tracks)
    scan_once(cfg, store, min_age_seconds=0)


def test_process_job_writes_transcript_and_marks_done(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)
    job = store.claim_next()

    path = process_job(job, cfg, store, FakeTranscriber())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["call_id"] == "abc"
    assert payload["segments"][0]["text"].startswith("טקסט")
    assert store.get("abc").status == DONE
    assert store.get("abc").transcript_path == path


def test_dual_track_produces_two_speakers(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar, tracks="dual")
    job = store.claim_next()

    path = process_job(job, cfg, store, FakeTranscriber())

    speakers = {seg["speaker"] for seg in json.loads(path.read_text(encoding="utf-8"))["segments"]}
    assert speakers == {"near", "far"}


def test_mixed_track_has_null_speaker(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)
    job = store.claim_next()

    path = process_job(job, cfg, store, FakeTranscriber())

    assert json.loads(path.read_text(encoding="utf-8"))["segments"][0]["speaker"] is None


def test_completed_stages_are_not_redone_after_resume(cfg, make_wav, make_sidecar) -> None:
    """The crash-resume guarantee, end to end."""
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)
    transcriber = FakeTranscriber()
    process_job(store.claim_next(), cfg, store, transcriber)
    first_count = len(transcriber.calls)

    store.set_status("abc", "running")
    store.complete_stage("abc", "stt")
    run_once(cfg, store, transcriber)

    assert len(transcriber.calls) == first_count


def test_stt_failure_records_attempt_and_does_not_mark_done(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)
    job = store.claim_next()

    with pytest.raises(SttError):
        process_job(job, cfg, store, ExplodingTranscriber())

    refreshed = store.get("abc")
    assert refreshed.status != DONE
    assert refreshed.attempts == 1
    assert "on fire" in refreshed.last_error


def test_run_once_gives_up_after_max_attempts(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)

    for _ in range(3):
        run_once(cfg, store, ExplodingTranscriber())

    assert store.get("abc").status == FAILED


def test_run_once_on_empty_queue_returns_zero(cfg) -> None:
    assert run_once(cfg, _store(cfg), FakeTranscriber()) == 0


def test_enabling_an_unimplemented_stage_fails_clearly(cfg, make_wav, make_sidecar) -> None:
    """Turning on an outer stage before it exists must say so, not crash oddly.

    `enrich` is the remaining unimplemented stage; render/publish/notify landed
    with Plan 2.
    """
    staged = cfg.model_copy(update={"pipeline": cfg.pipeline.model_copy(update={"stages": ("audio", "stt", "enrich")})})
    store = _store(staged)
    _enqueue(staged, store, make_wav, make_sidecar)
    job = store.claim_next()

    with pytest.raises(StageNotImplementedError, match="enrich"):
        process_job(job, staged, store, FakeTranscriber())


def test_core_only_pipeline_stops_after_audio(cfg, make_wav, make_sidecar) -> None:
    """Stage activation is config: an audio-only deployment never transcribes."""
    staged = cfg.model_copy(update={"pipeline": cfg.pipeline.model_copy(update={"stages": ("audio",)})})
    store = _store(staged)
    _enqueue(staged, store, make_wav, make_sidecar)
    transcriber = FakeTranscriber()

    process_job(store.claim_next(), staged, store, transcriber)

    assert transcriber.calls == []
    assert store.get("abc").status == DONE


def test_cli_process_end_to_end(cfg, tmp_path, make_wav, make_sidecar, capsys, monkeypatch) -> None:
    from jabberscribe import cli

    monkeypatch.setattr(cli, "_transcriber", lambda _cfg: FakeTranscriber())
    audio = make_wav(tmp_path / "src" / "call.wav")
    sidecar = make_sidecar(tmp_path / "src" / "call.json", call_id="cli1")
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        "paths:\n"
        f"  drop_root: {cfg.paths.drop_root.as_posix()}\n"
        f"  work_dir: {cfg.paths.work_dir.as_posix()}\n"
        f"  audio_store: {cfg.paths.audio_store.as_posix()}\n"
        f"  db_path: {cfg.paths.db_path.as_posix()}\n"
        "watcher:\n  min_age_seconds: 0\n",
        encoding="utf-8",
    )

    code = cli.main(["--config", str(cfg_file), "process", str(audio), str(sidecar)])

    assert code == 0
    assert "cli1: done" in capsys.readouterr().out
