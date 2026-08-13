import json
from pathlib import Path

from jabberscribe.render import (
    format_timestamp,
    load_transcript,
    render_call,
    render_confluence_body,
    render_page_title,
    render_transcript_text,
)
from jabberscribe.sidecar import parse_sidecar
from jabberscribe.stt import Segment

SIDECAR_JSON = json.dumps(
    {
        "call_id": "8f2a1c",
        "kind": "call",
        "source": "cucm-bib",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 3672,
        "participants": [
            {"display_name": "מאיר חדד", "email": "meir@corp.local", "extension": "1042", "role": "caller"},
            {"display_name": "Support", "extension": "1099", "role": "callee"},
        ],
        "audio": {"tracks": "dual", "sample_rate": 8000, "channels": 2},
    }
)

SEGMENTS = [
    Segment(0.0, 2.5, "שלום, מה המצב?", "near"),
    Segment(2.9, 6.0, "הכל טוב, תודה", "far"),
]


def _sidecar():
    return parse_sidecar(SIDECAR_JSON)


def test_format_timestamp_short_and_long() -> None:
    assert format_timestamp(0) == "0:00"
    assert format_timestamp(65.4) == "1:05"
    assert format_timestamp(3672) == "1:01:12"


def test_page_title_carries_date_participants_and_duration() -> None:
    title = render_page_title(_sidecar())

    assert "2026-08-12" in title
    assert "14:03" in title
    assert "מאיר חדד" in title
    assert "1:01:12" in title


def test_transcript_text_is_timestamped_and_labelled() -> None:
    text = render_transcript_text(SEGMENTS)

    assert "[0:00] near: שלום, מה המצב?" in text
    assert "[0:02] far: הכל טוב, תודה" in text


def test_transcript_text_omits_speaker_when_absent() -> None:
    text = render_transcript_text([Segment(0.0, 1.0, "שלום")])

    assert text.strip() == "[0:00] שלום"


def test_confluence_body_is_rtl_and_contains_metadata() -> None:
    body = render_confluence_body(_sidecar(), SEGMENTS, summary=None, audio_note=None)

    assert 'dir="rtl"' in body
    assert "8f2a1c" in body
    assert "cucm-bib" in body
    assert "מאיר חדד" in body
    assert "שלום, מה המצב?" in body


def test_confluence_body_escapes_markup_in_content() -> None:
    """Transcribed speech and display names are untrusted text, not markup."""
    hostile = [Segment(0.0, 1.0, "<script>alert('x')</script> & more", "near")]

    body = render_confluence_body(_sidecar(), hostile, summary=None, audio_note=None)

    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&amp; more" in body


def test_confluence_body_notes_a_missing_summary() -> None:
    body = render_confluence_body(_sidecar(), SEGMENTS, summary=None, audio_note=None)

    assert "סיכום אינו זמין" in body


def test_confluence_body_includes_summary_when_present() -> None:
    body = render_confluence_body(_sidecar(), SEGMENTS, summary="הלקוח ביקש הצעת מחיר", audio_note=None)

    assert "הלקוח ביקש הצעת מחיר" in body
    assert "סיכום אינו זמין" not in body


def test_confluence_body_includes_audio_note_when_given() -> None:
    body = render_confluence_body(_sidecar(), SEGMENTS, summary=None, audio_note="D:/audio/8f2a1c.wav")

    assert "D:/audio/8f2a1c.wav" in body


def test_mail_subject_and_body() -> None:
    rendered = render_call(_sidecar(), SEGMENTS, summary="סיכום", page_url="https://wiki/x/998")

    assert rendered.mail_subject.startswith("[Call] 2026-08-12 14:03")
    assert "מאיר חדד" in rendered.mail_subject
    assert "סיכום" in rendered.mail_body
    assert "https://wiki/x/998" in rendered.mail_body


def test_mail_body_states_when_no_page_exists() -> None:
    rendered = render_call(_sidecar(), SEGMENTS, summary=None, page_url=None)

    assert "https://" not in rendered.mail_body


def test_load_transcript_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "transcript.json"
    path.write_text(
        json.dumps(
            {
                "call_id": "8f2a1c",
                "segments": [{"start": 0.0, "end": 1.0, "text": "שלום", "speaker": "near"}],
            }
        ),
        encoding="utf-8",
    )

    segments = load_transcript(path)

    assert segments == [Segment(0.0, 1.0, "שלום", "near")]


def test_render_call_produces_every_artifact() -> None:
    rendered = render_call(_sidecar(), SEGMENTS)

    assert rendered.title
    assert rendered.body_xhtml
    assert rendered.mail_subject
    assert rendered.mail_body
    assert "שלום, מה המצב?" in rendered.transcript_text
