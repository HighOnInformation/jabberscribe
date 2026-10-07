from pathlib import Path

import httpx
import pytest

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


def test_http_error_raises_stt_error(tmp_path: Path) -> None:
    with pytest.raises(SttError, match="503"):
        _transcriber(lambda r: httpx.Response(503, text="overloaded")).transcribe(_audio(tmp_path))


def test_unreachable_server_raises_stt_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(SttError, match="refused"):
        _transcriber(handler).transcribe(_audio(tmp_path))


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


def test_build_prompt_appends_vocabulary() -> None:
    assert build_prompt(None) == FILLER_PROMPT
    assert build_prompt("ג'אבר, שלוחה") == f"{FILLER_PROMPT} ג'אבר, שלוחה"


def test_load_vocabulary(tmp_path: Path) -> None:
    path = tmp_path / "vocab.txt"
    path.write_text("ג'אבר\n\nשלוחה\n", encoding="utf-8")

    assert load_vocabulary(path) == "ג'אבר, שלוחה"
    assert load_vocabulary(None) is None
    assert load_vocabulary(tmp_path / "missing.txt") is None
