"""Local speech-to-text.

Configuration is ported from CallSight, where it won a Hebrew STT bake-off
against cloud providers. Two settings matter more than the rest:

- condition_on_previous_text=False stops the repetition-loop degeneration that
  Whisper falls into on Hebrew.
- initial_prompt seeded with a domain glossary measurably improves recognition
  of names and jargon.

faster-whisper is imported lazily so the rest of the service -- and its tests --
never pay for the model dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class SttError(RuntimeError):
    """Transcription failed."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None


class Transcriber(Protocol):
    def transcribe(self, wav: Path) -> list[Segment]: ...


def load_vocabulary(path: Path | None) -> str | None:
    """Read a one-term-per-line glossary into a Whisper initial_prompt."""
    if path is None or not path.is_file():
        return None
    terms = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return ", ".join(terms) or None


class WhisperLocal:
    """faster-whisper with the Hebrew-specialised ivrit.ai model."""

    def __init__(self, model: str, compute_type: str, device: str, vocabulary: str | None = None) -> None:
        self._model_name = model
        self._compute_type = compute_type
        self._device = device
        self._vocabulary = vocabulary
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - environment problem
                raise SttError("faster-whisper is not installed; pip install '.[stt]'") from exc
            log.info("loading STT model %s (%s, %s)", self._model_name, self._device, self._compute_type)
            self._model = WhisperModel(self._model_name, device=self._device, compute_type=self._compute_type)
        return self._model

    def transcribe(self, wav: Path) -> list[Segment]:
        model = self._load()
        try:
            segments, _info = model.transcribe(
                str(wav),
                language="he",
                initial_prompt=self._vocabulary,
                condition_on_previous_text=False,
                vad_filter=True,
                beam_size=5,
            )
            return [Segment(float(s.start), float(s.end), s.text.strip()) for s in segments if s.text.strip()]
        except SttError:
            raise
        except Exception as exc:  # faster-whisper raises assorted runtime errors
            raise SttError(f"transcription failed for {wav}: {exc}") from exc
