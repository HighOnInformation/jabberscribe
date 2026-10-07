import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jabberscribe import audio
from jabberscribe.audio import STT_FILENAME, AudioError, prepare_for_stt

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not on PATH"
)


def _stream(path: Path) -> tuple[str, int]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(proc.stdout)["streams"][0]
    return stream["codec_name"], stream["channels"]


def test_dual_channel_call_becomes_mono_opus(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "call.wav", channels=2, seconds=2.0)

    out = prepare_for_stt(src, tmp_path / "work")

    assert out == tmp_path / "work" / STT_FILENAME
    assert _stream(out) == ("opus", 1)


def test_existing_output_is_reused(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "call.wav")
    first = prepare_for_stt(src, tmp_path / "work")
    stamp = first.stat().st_mtime_ns

    second = prepare_for_stt(src, tmp_path / "work")

    assert second.stat().st_mtime_ns == stamp


def test_no_partial_file_is_left(tmp_path: Path, make_wav) -> None:
    prepare_for_stt(make_wav(tmp_path / "call.wav"), tmp_path / "work")

    assert list((tmp_path / "work").glob("*.part")) == []


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(AudioError, match="not found"):
        prepare_for_stt(tmp_path / "nope.wav", tmp_path / "work")


def test_undecodable_source_raises(tmp_path: Path) -> None:
    src = tmp_path / "junk.wav"
    src.write_bytes(b"this is not audio")

    with pytest.raises(AudioError, match="ffmpeg exited"):
        prepare_for_stt(src, tmp_path / "work")


def _mean_volume_db(path: Path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", proc.stderr).group(1))


def test_downmix_keeps_both_channels_audible(tmp_path: Path, make_wav) -> None:
    """A broken downmix (dropped or cancelled channel) would hand Whisper silence."""
    out = prepare_for_stt(make_wav(tmp_path / "call.wav", channels=2, seconds=2.0), tmp_path / "work")

    assert _mean_volume_db(out) > -40.0


def test_failed_encode_leaves_no_partial(tmp_path: Path) -> None:
    src = tmp_path / "junk.wav"
    src.write_bytes(b"this is not audio")

    with pytest.raises(AudioError):
        prepare_for_stt(src, tmp_path / "work")

    assert list((tmp_path / "work").iterdir()) == []


def test_ffmpeg_output_is_decoded_as_utf8(tmp_path: Path, make_wav, monkeypatch) -> None:
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        Path(args[-1]).write_bytes(b"OggS")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    prepare_for_stt(make_wav(tmp_path / "call.wav"), tmp_path / "work")

    assert (seen["encoding"], seen["errors"]) == ("utf-8", "replace")
