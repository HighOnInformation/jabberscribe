import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from jabberscribe.audio import (
    STT_FILENAME,
    STT_GLOB,
    channel_count,
    channel_filename,
    channel_peak_db,
    prepare_channel_for_stt,
)
from jabberscribe.config import SttConfig
from jabberscribe.sidecar import parse_sidecar
from jabberscribe.speakers import (
    CONFERENCE_FAR,
    FAR_FALLBACK,
    NEAR_FALLBACK,
    speaker_labels,
    stt_inputs,
    transcribe_inputs,
)
from jabberscribe.stt import Segment

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

STT = SttConfig(model="whisper-he")


def _sidecar(tmp_path: Path, make_sidecar, **fields):
    return parse_sidecar(make_sidecar(tmp_path / "s.json", **fields).read_text(encoding="utf-8"))


def _wav_with_silent_far_end(path: Path, seconds: float = 1.0, rate: int = 8000) -> Path:
    """Channel 0 carries a tone, channel 1 digital silence: a far end that never spoke."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for i in range(int(seconds * rate)):
        frames += struct.pack("<hh", int(12000 * math.sin(2 * math.pi * 440 * i / rate)), 0)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    return path


class ByFile:
    """Fake transcriber: answers each file name with canned segments."""

    def __init__(self, answers: dict[str, list[Segment]]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def transcribe(self, audio: Path) -> list[Segment]:
        self.calls.append(audio.name)
        return self.answers[audio.name]


def test_one_to_one_call_uses_both_display_names(tmp_path, make_sidecar) -> None:
    assert speaker_labels(_sidecar(tmp_path, make_sidecar)) == ("מאיר", "דנה")


def test_missing_names_fall_back_to_generic_labels(tmp_path, make_sidecar) -> None:
    sidecar = _sidecar(tmp_path, make_sidecar, line_owner={"extension": "1042"}, parties=[{"extension": "2210"}])

    assert speaker_labels(sidecar) == (NEAR_FALLBACK, FAR_FALLBACK)


def test_a_call_with_several_parties_gets_a_generic_far_label(tmp_path, make_sidecar) -> None:
    parties = [{"extension": "2210", "display_name": "דנה"}, {"extension": "3000", "display_name": "יוסי"}]

    assert speaker_labels(_sidecar(tmp_path, make_sidecar, parties=parties)) == ("מאיר", FAR_FALLBACK)


def test_a_conference_far_end_is_the_participants(tmp_path, make_sidecar) -> None:
    sidecar = _sidecar(tmp_path, make_sidecar, conference_id="conf-1")

    assert speaker_labels(sidecar) == ("מאיר", CONFERENCE_FAR)


def test_channels_are_labelled_and_merged_by_start_time() -> None:
    transcriber = ByFile(
        {
            "near.ogg": [Segment(0.0, 2.0, "שלום"), Segment(5.0, 6.0, "כן")],
            "far.ogg": [Segment(2.5, 4.0, "היי, מה נשמע")],
        }
    )

    merged = transcribe_inputs(transcriber, [(Path("near.ogg"), "מאיר"), (Path("far.ogg"), "דנה")])

    assert merged == [
        Segment(0.0, 2.0, "שלום", "מאיר"),
        Segment(2.5, 4.0, "היי, מה נשמע", "דנה"),
        Segment(5.0, 6.0, "כן", "מאיר"),
    ]


def test_downmix_segments_stay_unlabelled() -> None:
    transcriber = ByFile({STT_FILENAME: [Segment(0.0, 1.0, "שלום")]})

    assert transcribe_inputs(transcriber, [(Path(STT_FILENAME), None)]) == [Segment(0.0, 1.0, "שלום")]


@needs_ffmpeg
def test_mixed_track_call_is_one_downmix(tmp_path, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)

    inputs = stt_inputs(audio, tmp_path / "work", _sidecar(tmp_path, make_sidecar, tracks="mixed"), STT)

    assert inputs == [(tmp_path / "work" / STT_FILENAME, None)]


@needs_ffmpeg
def test_dual_track_call_is_one_file_per_channel(tmp_path, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)
    work = tmp_path / "work"

    inputs = stt_inputs(audio, work, _sidecar(tmp_path, make_sidecar, tracks="dual"), STT)

    assert inputs == [(work / channel_filename(0), "מאיר"), (work / channel_filename(1), "דנה")]
    assert all(path.stat().st_size > 0 for path, _ in inputs)
    assert sorted(p.name for p in work.glob(STT_GLOB)) == ["stt-ch0.ogg", "stt-ch1.ogg"]


@needs_ffmpeg
def test_near_channel_setting_swaps_the_labels(tmp_path, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)
    work = tmp_path / "work"
    cfg = SttConfig(model="whisper-he", near_channel=1)

    inputs = stt_inputs(audio, work, _sidecar(tmp_path, make_sidecar, tracks="dual"), cfg)

    assert inputs == [(work / channel_filename(1), "מאיר"), (work / channel_filename(0), "דנה")]


@needs_ffmpeg
def test_split_can_be_switched_off(tmp_path, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)
    cfg = SttConfig(model="whisper-he", split_channels=False)

    inputs = stt_inputs(audio, tmp_path / "work", _sidecar(tmp_path, make_sidecar, tracks="dual"), cfg)

    assert inputs == [(tmp_path / "work" / STT_FILENAME, None)]


@needs_ffmpeg
def test_a_silent_channel_is_not_transcribed(tmp_path, make_sidecar) -> None:
    audio = _wav_with_silent_far_end(tmp_path / "call.wav")
    work = tmp_path / "work"

    inputs = stt_inputs(audio, work, _sidecar(tmp_path, make_sidecar, tracks="dual"), STT)

    assert inputs == [(work / channel_filename(0), "מאיר")]
    assert not (work / channel_filename(1)).exists()


@needs_ffmpeg
def test_channel_peak_tells_a_tone_from_silence(tmp_path) -> None:
    audio = _wav_with_silent_far_end(tmp_path / "call.wav")

    assert channel_peak_db(audio, 0) > -20.0
    assert channel_peak_db(audio, 1) < -80.0


@needs_ffmpeg
def test_channel_count(tmp_path, make_wav) -> None:
    assert channel_count(make_wav(tmp_path / "stereo.wav", channels=2)) == 2
    assert channel_count(make_wav(tmp_path / "mono.wav", channels=1)) == 1


@needs_ffmpeg
def test_channel_encode_is_reused(tmp_path, make_wav) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)
    first = prepare_channel_for_stt(audio, tmp_path / "work", 1)
    stamp = first.stat().st_mtime_ns

    assert prepare_channel_for_stt(audio, tmp_path / "work", 1).stat().st_mtime_ns == stamp
    assert list((tmp_path / "work").glob("*.part")) == []


@needs_ffmpeg
def test_a_mono_file_labelled_dual_falls_back_to_the_downmix(tmp_path, make_wav, make_sidecar, caplog) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=1)

    inputs = stt_inputs(audio, tmp_path / "work", _sidecar(tmp_path, make_sidecar, tracks="dual"), STT)

    assert inputs == [(tmp_path / "work" / STT_FILENAME, None)]
    assert "downmix" in caplog.text
