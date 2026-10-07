import json
import shutil
from pathlib import Path

import httpx
import pytest

from jabberscribe.cli import doctor
from jabberscribe.cues import LAUGHTER, MUSIC, Cue
from jabberscribe.jobs import DONE
from jabberscribe.output import CUES_TRANSCRIPT_FILE, CUES_UNAVAILABLE, RESULT_FILE, TRANSCRIPT_FILE, write_outputs
from jabberscribe.pipeline import run_once
from jabberscribe.sidecar import parse_sidecar
from jabberscribe.stt import Segment
from jabberscribe.watcher import scan_once

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

SEGMENTS = [Segment(0.0, 1.0, "אה, שלום"), Segment(3.0, 4.0, "חחח, נכון")]
CUES = [Cue(2.0, 4.0, LAUGHTER), Cue(10.0, 14.0, MUSIC)]


def _write(tmp_path: Path, make_sidecar, cues: list[Cue] | None) -> Path:
    sidecar = parse_sidecar(make_sidecar(tmp_path / "s.json").read_text(encoding="utf-8"))
    out = tmp_path / "out"
    write_outputs(
        out,
        sidecar=sidecar,
        segments=SEGMENTS,
        summary=None,
        owners=[sidecar.line_owner],
        models={"stt": "whisper-he", "summary": "gemma-3"},
        recording=out / "recording.wav",
        cues=cues,
    )
    return out


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_cues_are_interleaved_in_their_own_file(tmp_path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, CUES)

    lines = [line for line in _text(out / CUES_TRANSCRIPT_FILE).splitlines() if line.startswith("[")]

    assert lines == ["[00:00:00] אה, שלום", "[00:00:02] [צחוק]", "[00:00:03] חחח, נכון", "[00:00:10] [מוזיקה]"]
    assert _text(out / CUES_TRANSCRIPT_FILE).startswith('<div dir="rtl">\n\n#')


def test_the_verbatim_transcript_never_carries_cues(tmp_path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, CUES)

    assert LAUGHTER not in _text(out / TRANSCRIPT_FILE)
    assert MUSIC not in _text(out / TRANSCRIPT_FILE)


def test_result_json_carries_the_cues(tmp_path, make_sidecar) -> None:
    result = json.loads(_text(_write(tmp_path, make_sidecar, CUES) / RESULT_FILE))

    assert result["cues_available"] is True
    assert result["cues"] == [
        {"start": 2.0, "end": 4.0, "label": LAUGHTER},
        {"start": 10.0, "end": 14.0, "label": MUSIC},
    ]


def test_unavailable_cues_are_said_so(tmp_path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, None)

    text = _text(out / CUES_TRANSCRIPT_FILE)
    assert CUES_UNAVAILABLE in text
    assert "[00:00:00] אה, שלום" in text
    result = json.loads(_text(out / RESULT_FILE))
    assert (result["cues_available"], result["cues"]) == (False, [])


def test_a_call_without_events_is_not_unavailable(tmp_path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, [])

    assert CUES_UNAVAILABLE not in _text(out / CUES_TRANSCRIPT_FILE)
    assert json.loads(_text(out / RESULT_FILE))["cues_available"] is True


class FakeTranscriber:
    def transcribe(self, audio: Path) -> list[Segment]:
        return [Segment(0.0, 1.5, "אה, שלום")]


class FakeSummarizer:
    def summarize(self, segments: list[Segment]) -> None:
        return None


class FakeTagger:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def tag(self, audio: Path) -> list[Cue]:
        self.seen.append(audio)
        return [Cue(0.5, 2.0, LAUGHTER)]


class ExplodingTagger:
    def tag(self, audio: Path) -> list[Cue]:
        raise RuntimeError("out of memory")


def _enqueue(cfg, store, audit, make_wav, make_sidecar) -> str:
    make_wav(cfg.paths.inbox / "c.wav", channels=2)
    make_sidecar(cfg.paths.inbox / "c.json", call_id="cue")
    scan_once(cfg, store, audit, min_age_seconds=0)
    return "cue_1042"


def _result(store, key: str) -> dict:
    return json.loads(_text(store.get(key).out_dir / RESULT_FILE))


@needs_ffmpeg
def test_the_cues_stage_tags_the_recording(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    tagger = FakeTagger()

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer(), tagger=tagger)

    job = store.get(key)
    assert job.status == DONE
    assert tagger.seen == [job.audio_path]
    result = _result(store, key)
    assert result["cues"] == [{"start": 0.5, "end": 2.0, "label": LAUGHTER}]
    assert "cues_sec" in result["timings"]


@needs_ffmpeg
def test_a_failing_tagger_never_fails_the_job(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer(), tagger=ExplodingTagger())

    job = store.get(key)
    assert (job.status, job.attempts) == (DONE, 0)
    assert _result(store, key)["cues_available"] is False


@needs_ffmpeg
def test_without_a_tagger_the_stage_is_skipped(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    job = store.get(key)
    assert job.status == DONE
    assert _result(store, key)["cues_available"] is False
    assert (job.out_dir / CUES_TRANSCRIPT_FILE).is_file()


def test_doctor_reports_the_tagger_without_failing(cfg) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(refuse))

    check = {c.name: c for c in doctor(cfg, client)}["cues"]

    assert check.ok
    assert check.detail == "skipped: no cues.model_path configured"


class MalformedTagger:
    def __init__(self, output) -> None:
        self.output = output

    def tag(self, audio: Path):
        return self.output


class RefusingTranscriber:
    def transcribe(self, audio: Path) -> list[Segment]:
        raise AssertionError("a checkpointed job must not be transcribed again")


@pytest.mark.parametrize(
    "output",
    [
        [{"start": 0.5, "end": 2.0, "label": LAUGHTER}],
        [Cue(0.5, 2.0, object())],
        None,
    ],
)
def test_malformed_tagger_output_degrades_to_cues_unavailable(
    cfg, store, audit, make_wav, make_sidecar, output
) -> None:
    key = _checkpointed(cfg, store, audit, make_wav, make_sidecar, "stt")

    run_once(cfg, store, RefusingTranscriber(), FakeSummarizer(), tagger=MalformedTagger(output))

    job = store.get(key)
    assert (job.status, job.attempts) == (DONE, 0)
    assert _result(store, key)["cues_available"] is False


def _checkpointed(cfg, store, audit, make_wav, make_sidecar, stage: str) -> str:
    """A job whose stages up to `stage` are done, with its segments checkpoint written."""
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    work = cfg.paths.work_dir / key
    work.mkdir(parents=True, exist_ok=True)
    (work / "segments.json").write_text(
        json.dumps([{"start": 0.0, "end": 1.5, "text": "אה, שלום"}], ensure_ascii=False), encoding="utf-8"
    )
    store.complete_stage(key, stage)
    return key


def test_a_job_checkpointed_at_stt_resumes_into_cues(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _checkpointed(cfg, store, audit, make_wav, make_sidecar, "stt")
    tagger = FakeTagger()

    run_once(cfg, store, RefusingTranscriber(), FakeSummarizer(), tagger=tagger)

    job = store.get(key)
    assert job.status == DONE
    assert tagger.seen == [job.audio_path]
    assert _result(store, key)["cues"] == [{"start": 0.5, "end": 2.0, "label": LAUGHTER}]


def test_a_job_past_cues_without_a_cues_checkpoint_goes_out_without_cues(
    cfg, store, audit, make_wav, make_sidecar
) -> None:
    key = _checkpointed(cfg, store, audit, make_wav, make_sidecar, "cues")
    tagger = FakeTagger()

    run_once(cfg, store, RefusingTranscriber(), FakeSummarizer(), tagger=tagger)

    job = store.get(key)
    assert job.status == DONE
    assert tagger.seen == []
    assert _result(store, key)["cues_available"] is False
