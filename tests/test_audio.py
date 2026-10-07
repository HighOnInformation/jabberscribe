import json
import shutil
import subprocess
from pathlib import Path

import pytest

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
