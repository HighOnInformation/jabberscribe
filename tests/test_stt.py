from pathlib import Path

import httpx
import pytest

from jabberscribe.llm import TransientError
from jabberscribe.stt import (
    FILLER_PROMPT,
    LiteLLMTranscriber,
    Segment,
    SttError,
    build_prompt,
    format_ts,
    load_vocabulary,
)


def _transcriber(handler, prompt: str = FILLER_PROMPT) -> LiteLLMTranscriber:
    client = httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(handler))
    return LiteLLMTranscriber(client, model="whisper-he", prompt=prompt)


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "stt.ogg"
    path.write_bytes(b"OggS fake")
    return path


def test_posts_verbatim_request_and_parses_segments(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={
                "text": "…",
                "segments": [
                    {"start": 0.0, "end": 1.2, "text": " אה, שלום "},
                    {"start": 1.2, "end": 1.5, "text": "   "},
                    {"start": 1.5, "end": 3.0, "text": "deploy מחר"},
                ],
            },
        )

    segments = _transcriber(handler).transcribe(_audio(tmp_path))

    assert seen["path"] == "/v1/audio/transcriptions"
    assert b"verbose_json" in seen["body"]
    assert b"whisper-he" in seen["body"]
    assert FILLER_PROMPT.encode() in seen["body"]
    assert segments == [Segment(0.0, 1.2, "אה, שלום"), Segment(1.5, 3.0, "deploy מחר")]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_overload_and_server_errors_are_transient(tmp_path: Path, status: int) -> None:
    with pytest.raises(TransientError, match=str(status)):
        _transcriber(lambda r: httpx.Response(status, text="overloaded")).transcribe(_audio(tmp_path))


def test_unreachable_server_is_transient(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(TransientError, match="refused"):
        _transcriber(handler).transcribe(_audio(tmp_path))


@pytest.mark.parametrize("status", [400, 401, 404, 413])
def test_client_errors_are_permanent(tmp_path: Path, status: int) -> None:
    with pytest.raises(SttError, match=str(status)):
        _transcriber(lambda r: httpx.Response(status, text="bad request")).transcribe(_audio(tmp_path))


def test_response_without_segments_raises(tmp_path: Path) -> None:
    with pytest.raises(SttError, match="segments"):
        _transcriber(lambda r: httpx.Response(200, json={"text": "שלום"})).transcribe(_audio(tmp_path))


def test_malformed_segment_raises(tmp_path: Path) -> None:
    body = {"segments": [{"text": "no timestamps"}]}

    with pytest.raises(SttError):
        _transcriber(lambda r: httpx.Response(200, json=body)).transcribe(_audio(tmp_path))


def test_format_ts() -> None:
    assert format_ts(0) == "00:00:00"
    assert format_ts(3725.9) == "01:02:05"


def test_build_prompt_puts_the_filler_cue_last() -> None:
    """Whisper keeps only the last 224 prompt tokens; the filler cue must survive truncation."""
    assert build_prompt(None) == FILLER_PROMPT
    assert build_prompt("ג'אבר, שלוחה") == f"ג'אבר, שלוחה {FILLER_PROMPT}"


def _segments_response(*segments: dict) -> httpx.Response:
    return httpx.Response(200, json={"segments": list(segments)})


@pytest.mark.parametrize(
    "quality",
    [
        {"compression_ratio": 2.6},
        {"no_speech_prob": 0.9, "avg_logprob": -1.5},
    ],
)
def test_likely_hallucinations_are_dropped(tmp_path: Path, quality: dict) -> None:
    bad = {"start": 0.0, "end": 5.0, "text": "תודה רבה תודה רבה תודה רבה", **quality}
    good = {"start": 5.0, "end": 6.0, "text": "שלום", "compression_ratio": 1.1, "no_speech_prob": 0.1}

    segments = _transcriber(lambda r: _segments_response(bad, good)).transcribe(_audio(tmp_path))

    assert segments == [Segment(5.0, 6.0, "שלום")]


def test_quiet_but_confident_speech_is_kept(tmp_path: Path) -> None:
    soft = {"start": 0.0, "end": 1.0, "text": "כן", "no_speech_prob": 0.9, "avg_logprob": -0.3}

    assert _transcriber(lambda r: _segments_response(soft)).transcribe(_audio(tmp_path)) == [Segment(0.0, 1.0, "כן")]


def test_prompt_echo_is_dropped_but_a_lone_filler_is_kept(tmp_path: Path) -> None:
    echo = {"start": 0.0, "end": 4.0, "text": FILLER_PROMPT}
    partial_echo = {"start": 4.0, "end": 6.0, "text": "אה, אממ, כאילו..."}
    filler = {"start": 6.0, "end": 7.0, "text": "אה,"}

    segments = _transcriber(lambda r: _segments_response(echo, partial_echo, filler)).transcribe(_audio(tmp_path))

    assert segments == [Segment(6.0, 7.0, "אה,")]


def test_load_vocabulary(tmp_path: Path) -> None:
    path = tmp_path / "vocab.txt"
    path.write_text("ג'אבר\n\nשלוחה\n", encoding="utf-8")

    assert load_vocabulary(path) == "ג'אבר, שלוחה"
    assert load_vocabulary(None) is None
    assert load_vocabulary(tmp_path / "missing.txt") is None
