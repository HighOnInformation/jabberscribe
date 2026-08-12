from pathlib import Path

import pytest

from jabberscribe.stt import Segment, load_vocabulary


def test_segment_defaults_to_no_speaker() -> None:
    assert Segment(0.0, 1.0, "שלום").speaker is None


def test_load_vocabulary_joins_lines_and_skips_blanks(tmp_path: Path) -> None:
    vocab = tmp_path / "vocab.txt"
    vocab.write_text("ג'אבר\n\nשלוחה\n  ועידה  \n", encoding="utf-8")

    assert load_vocabulary(vocab) == "ג'אבר, שלוחה, ועידה"


def test_load_vocabulary_handles_none_and_missing(tmp_path: Path) -> None:
    assert load_vocabulary(None) is None
    assert load_vocabulary(tmp_path / "absent.txt") is None


@pytest.mark.slow
def test_whisper_local_transcribes_hebrew_sample() -> None:
    """Downloads the model. Run explicitly: pytest -m slow"""
    from jabberscribe.stt import WhisperLocal

    sample = Path("tests/fixtures/hebrew_sample.wav")
    if not sample.is_file():
        pytest.skip("tests/fixtures/hebrew_sample.wav not provided")

    segments = WhisperLocal("ivrit-ai/whisper-large-v3-turbo-ct2", "int8", "cpu").transcribe(sample)

    assert segments
    assert any(seg.text.strip() for seg in segments)
