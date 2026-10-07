import shutil
from pathlib import Path

import pytest

from jabberscribe import cues
from jabberscribe.config import CuesConfig
from jabberscribe.cues import (
    LAUGHTER,
    MUSIC,
    SAMPLE_RATE,
    SILENCE,
    Cue,
    CueError,
    cues_from_scores,
    load_tagger,
    read_windows,
    unavailable_reason,
)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

WINDOW_BYTES = 2 * SAMPLE_RATE * 4


def test_consecutive_windows_merge_into_one_cue() -> None:
    windows = [{"Laughter": 0.8}, {"Giggle": 0.5}, {"Speech": 0.9}]

    assert cues_from_scores(windows, 2.0, 0.3) == [Cue(0.0, 4.0, LAUGHTER)]


def test_weak_and_unmapped_classes_are_ignored() -> None:
    assert cues_from_scores([{"Laughter": 0.2, "Speech": 0.99, "Dog": 0.9}], 2.0, 0.3) == []


def test_overlapping_labels_are_separate_cues() -> None:
    windows = [{"Music": 0.6}, {"Music": 0.6, "Laughter": 0.4}, {}]

    assert cues_from_scores(windows, 2.0, 0.3) == [Cue(0.0, 4.0, MUSIC), Cue(2.0, 4.0, LAUGHTER)]


def test_a_cue_still_open_at_the_end_closes_there() -> None:
    assert cues_from_scores([{}, {"Music": 0.9}], 2.0, 0.3) == [Cue(2.0, 4.0, MUSIC)]


def test_only_long_silences_are_cued() -> None:
    short = [{"Silence": 0.9}, {"Silence": 0.9}, {}]
    long = [{"Silence": 0.9}, {"Silence": 0.9}, {"Silence": 0.9}]

    assert cues_from_scores(short, 2.0, 0.3) == []
    assert cues_from_scores(long, 2.0, 0.3) == [Cue(0.0, 6.0, SILENCE)]


@needs_ffmpeg
def test_read_windows_streams_32khz_float_windows(tmp_path: Path, make_wav) -> None:
    chunks = list(read_windows(make_wav(tmp_path / "a.wav", seconds=5.0)))

    assert len(chunks) == 3
    assert [len(c) for c in chunks[:2]] == [WINDOW_BYTES, WINDOW_BYTES]
    # Resampling 8 kHz to 32 kHz may shift the length by a few samples.
    assert abs(len(chunks[2]) - WINDOW_BYTES // 2) <= 4 * 256


@needs_ffmpeg
def test_read_windows_rejects_undecodable_audio(tmp_path: Path) -> None:
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"this is not audio")

    with pytest.raises(CueError, match="ffmpeg exited"):
        list(read_windows(junk))


def test_read_windows_rejects_missing_audio(tmp_path: Path) -> None:
    with pytest.raises(CueError, match="not found"):
        list(read_windows(tmp_path / "missing.wav"))


def test_disabled_cues_load_no_tagger() -> None:
    assert load_tagger(CuesConfig(enabled=False)) is None


def test_a_missing_checkpoint_skips_the_stage_with_a_reason(tmp_path: Path, caplog) -> None:
    assert load_tagger(CuesConfig(model_path=tmp_path / "cnn14.pth")) is None
    assert "cues stage skipped" in caplog.text
    assert "checkpoint not found" in caplog.text


def test_no_model_path_is_a_reason(tmp_path: Path) -> None:
    assert "model_path" in unavailable_reason(CuesConfig())


def test_a_small_checkpoint_is_refused_before_panns_would_download_another(tmp_path: Path) -> None:
    checkpoint = tmp_path / "cnn14.pth"
    checkpoint.write_bytes(b"x")

    assert "smaller" in unavailable_reason(CuesConfig(model_path=checkpoint))


def test_a_missing_extra_skips_the_stage(tmp_path: Path, monkeypatch) -> None:
    checkpoint = tmp_path / "cnn14.pth"
    checkpoint.write_bytes(b"x")
    monkeypatch.setattr(cues, "PANNS_MIN_CHECKPOINT_BYTES", 1)
    monkeypatch.setattr(cues, "_extra_installed", lambda: False)

    assert "pip install .[cues]" in unavailable_reason(CuesConfig(model_path=checkpoint))


def test_a_missing_label_file_skips_the_stage(tmp_path: Path, monkeypatch) -> None:
    checkpoint = tmp_path / "cnn14.pth"
    checkpoint.write_bytes(b"x")
    monkeypatch.setattr(cues, "PANNS_MIN_CHECKPOINT_BYTES", 1)
    monkeypatch.setattr(cues, "_extra_installed", lambda: True)
    monkeypatch.setattr(cues, "PANNS_LABELS", tmp_path / "panns_data" / "class_labels_indices.csv")

    assert "label file" in unavailable_reason(CuesConfig(model_path=checkpoint))


def test_a_tagger_that_fails_to_load_skips_the_stage(tmp_path: Path, monkeypatch, caplog) -> None:
    checkpoint = tmp_path / "cnn14.pth"
    checkpoint.write_bytes(b"x")
    monkeypatch.setattr(cues, "unavailable_reason", lambda cfg: None)

    def broken(model_path: Path, threshold: float):
        raise RuntimeError("torch not importable")

    monkeypatch.setattr(cues, "PannsTagger", broken)

    assert load_tagger(CuesConfig(model_path=checkpoint)) is None
    assert "failed to load" in caplog.text
