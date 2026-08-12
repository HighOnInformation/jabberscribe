import shutil
import wave
from pathlib import Path

import pytest

from jabberscribe.audio import TARGET_RATE, AudioError, prepare

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _probe(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as handle:
        return handle.getnchannels(), handle.getframerate()


def test_mixed_track_is_resampled_to_mono_16k(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "in.wav", seconds=1.0, rate=8000, channels=1)

    tracks = prepare(src, tmp_path / "work", "mixed")

    assert [t.label for t in tracks] == ["mixed"]
    assert _probe(tracks[0].path) == (1, TARGET_RATE)


def test_dual_track_splits_into_near_and_far(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "in.wav", seconds=1.0, rate=8000, channels=2)

    tracks = prepare(src, tmp_path / "work", "dual")

    assert [t.label for t in tracks] == ["near", "far"]
    for track in tracks:
        assert _probe(track.path) == (1, TARGET_RATE)
    assert tracks[0].path != tracks[1].path


def test_dual_split_channels_differ(tmp_path: Path, make_wav) -> None:
    """Proves the split reads two distinct channels rather than duplicating one."""
    src = make_wav(tmp_path / "in.wav", seconds=1.0, rate=8000, channels=2)

    near, far = prepare(src, tmp_path / "work", "dual")

    assert near.path.read_bytes() != far.path.read_bytes()


def test_prepare_is_idempotent(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "in.wav", channels=1)
    work = tmp_path / "work"

    first = prepare(src, work, "mixed")
    stamp = first[0].path.stat().st_mtime_ns
    second = prepare(src, work, "mixed")

    assert second[0].path == first[0].path
    assert second[0].path.stat().st_mtime_ns == stamp


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(AudioError, match="not found"):
        prepare(tmp_path / "nope.wav", tmp_path / "work", "mixed")


def test_undecodable_source_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"this is not audio")

    with pytest.raises(AudioError, match="ffmpeg"):
        prepare(bad, tmp_path / "work", "mixed")


def test_unknown_tracks_value_raises(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "in.wav")

    with pytest.raises(AudioError, match="tracks"):
        prepare(src, tmp_path / "work", "quad")
