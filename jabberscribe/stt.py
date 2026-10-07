"""Speech-to-text through the LiteLLM server's OpenAI-compatible route.

Strict verbatim is the goal: fillers, false starts, and repetitions stay in.
Whisper tends to drop fillers, so the prompt *shows* them -- Whisper imitates
the style of its prompt. That is best effort, not a guarantee.

Whisper reads only the last 224 prompt tokens, so the filler cue goes last
where it survives truncation, and the vocabulary goes first. Segments Whisper
most likely invented (silence, hold music, repetition loops, prompt echo) are
dropped: a verbatim transcript must not contain words nobody said.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from jabberscribe.llm import post

log = logging.getLogger(__name__)

FILLER_PROMPT = "אה, אממ, כאילו... אה, רגע, רגע."

#: Whisper's own thresholds for "this segment is a repetition loop" and "this is silence".
MAX_COMPRESSION_RATIO = 2.4
NO_SPEECH_PROB = 0.6
MIN_AVG_LOGPROB = -1.0
#: Echo detection ignores short fragments: a lone "אה" is a real filler, not an echo.
MIN_ECHO_CHARS = len(FILLER_PROMPT) // 2


class SttError(RuntimeError):
    """Transcription failed for a reason retrying will not fix."""


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
    return f"{vocabulary} {FILLER_PROMPT}" if vocabulary else FILLER_PROMPT


def format_ts(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def is_hallucination(raw: dict, text: str, prompt: str) -> bool:
    """True for a segment Whisper most likely invented rather than heard."""
    compression = raw.get("compression_ratio")
    if isinstance(compression, int | float) and compression > MAX_COMPRESSION_RATIO:
        return True
    no_speech, logprob = raw.get("no_speech_prob"), raw.get("avg_logprob")
    if isinstance(no_speech, int | float) and isinstance(logprob, int | float):
        if no_speech > NO_SPEECH_PROB and logprob < MIN_AVG_LOGPROB:
            return True
    return text == prompt.strip() or (len(text) >= MIN_ECHO_CHARS and text in prompt)


class LiteLLMTranscriber:
    def __init__(self, client: httpx.Client, model: str, prompt: str) -> None:
        self._client = client
        self._model = model
        self._prompt = prompt

    def transcribe(self, audio: Path) -> list[Segment]:
        """Raises TransientError when LiteLLM is down or overloaded, SttError otherwise."""
        try:
            with audio.open("rb") as fh:
                response = post(
                    self._client,
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
            payload = response.json()
        except (httpx.HTTPStatusError, OSError, ValueError) as exc:
            raise SttError(f"transcription failed for {audio}: {exc}") from exc

        raw_segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(raw_segments, list):
            raise SttError("transcription response has no segments; the server must support verbose_json")
        segments: list[Segment] = []
        try:
            for raw in raw_segments:
                text = str(raw["text"]).strip()
                if not text:
                    continue
                if is_hallucination(raw, text, self._prompt):
                    log.info("dropping likely hallucinated segment at %.1fs: %r", float(raw["start"]), text[:80])
                    continue
                segments.append(Segment(float(raw["start"]), float(raw["end"]), text))
        except (KeyError, TypeError, ValueError) as exc:
            raise SttError(f"malformed segment in transcription response: {exc}") from exc
        return segments
