from jabberscribe.diarize import merge_tracks
from jabberscribe.stt import Segment


def test_dual_track_merge_is_ordered_and_labelled() -> None:
    merged = merge_tracks(
        {
            "near": [Segment(0.0, 1.0, "שלום"), Segment(4.0, 5.0, "תודה")],
            "far": [Segment(1.5, 3.0, "היי")],
        }
    )

    assert [(s.start, s.speaker, s.text) for s in merged] == [
        (0.0, "near", "שלום"),
        (1.5, "far", "היי"),
        (4.0, "near", "תודה"),
    ]


def test_mixed_track_gets_no_speaker_label() -> None:
    """A compliance transcript must never fabricate attribution."""
    merged = merge_tracks({"mixed": [Segment(0.0, 1.0, "שלום")]})

    assert merged[0].speaker is None


def test_overlapping_speech_is_kept_not_dropped() -> None:
    merged = merge_tracks({"near": [Segment(0.0, 2.0, "אני מדבר")], "far": [Segment(1.0, 3.0, "וגם אני")]})

    assert len(merged) == 2


def test_ties_break_deterministically_by_label() -> None:
    merged = merge_tracks({"near": [Segment(1.0, 2.0, "a")], "far": [Segment(1.0, 2.0, "b")]})

    assert [s.speaker for s in merged] == ["far", "near"]


def test_empty_input_yields_empty_output() -> None:
    assert merge_tracks({}) == []
