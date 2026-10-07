"""Speech-to-text through the LiteLLM server's OpenAI-compatible route.

Strict verbatim is the goal: fillers, false starts, and repetitions stay in.
Whisper tends to drop fillers, so the prompt *shows* them -- Whisper imitates
the style of its prompt. That is best effort, not a guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

FILLER_PROMPT = "אה, אממ, כאילו... אה, רגע, רגע."


class SttError(RuntimeError):
    """Transcription failed."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


class Transcriber(Protocol):
    def transcribe(self, audio: Path) -> list[Segment]: ...


def load_vocabulary(path: Path | None) -> str | None:
    """Read a one-term-per-line glossary into a comma-separated prompt fragment."""
    if path is None or not path.is_file():
        return None
    terms = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return ", ".join(terms) or None


def build_prompt(vocabulary: str | None) -> str:
    return f"{FILLER_PROMPT} {vocabulary}" if vocabulary else FILLER_PROMPT


def format_ts(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


class LiteLLMTranscriber:
    def __init__(self, client: httpx.Client, model: str, prompt: str) -> None:
        self._client = client
        self._model = model
        self._prompt = prompt

    def transcribe(self, audio: Path) -> list[Segment]:
        try:
            with audio.open("rb") as fh:
                response = self._client.post(
                    "/v1/audio/transcriptions",
                    files={"file": (audio.name, fh, "audio/ogg")},
                    data={
                        "model": self._model,
                        "language": "he",
                        "response_format": "verbose_json",
                        "timestamp_granularities[]": "segment",
                        "prompt": self._prompt,
                        "temperature": "0",
                    },
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise SttError(f"transcription failed for {audio}: {exc}") from exc

        raw_segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(raw_segments, list):
            raise SttError("transcription response has no segments; the server must support verbose_json")
        try:
            segments = [Segment(float(s["start"]), float(s["end"]), str(s["text"]).strip()) for s in raw_segments]
        except (KeyError, TypeError, ValueError) as exc:
            raise SttError(f"malformed segment in transcription response: {exc}") from exc
        return [s for s in segments if s.text]
