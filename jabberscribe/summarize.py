"""Meeting summary and action items via the LiteLLM chat route.

A summary failure must never cost the user their transcript: on a transport
error or unusable output the request is retried once, then the call carries on
with the summary marked unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from jabberscribe.stt import Segment, format_ts

log = logging.getLogger(__name__)

ATTEMPTS = 2

SYSTEM_PROMPT = """You summarize Hebrew business phone calls and meetings from a timestamped transcript.
Write in Hebrew. Keep English technical terms exactly as spoken.
Return only a JSON object with this shape:
{"summary": "<concise Hebrew summary of the call>",
 "action_items": [{"task": "<what must be done>", "owner": "<name or null>",
                   "due": "<deadline or null>", "source_ts": "HH:MM:SS"}]}
Rules:
- owner and due: fill them only when explicitly stated in the call; otherwise null. Never guess.
- source_ts: the timestamp of the transcript line the item comes from, copied exactly.
- If there are no action items, return an empty list."""


@dataclass(frozen=True)
class ActionItem:
    task: str
    owner: str | None
    due: str | None
    source_ts: str


@dataclass(frozen=True)
class Summary:
    text: str
    action_items: tuple[ActionItem, ...]


class Summarizer(Protocol):
    def summarize(self, segments: list[Segment]) -> Summary | None: ...


class _ItemModel(BaseModel):
    task: str = Field(min_length=1)
    owner: str | None = None
    due: str | None = None
    source_ts: str = Field(pattern=r"^\d{2}:\d{2}:\d{2}$")


class _SummaryModel(BaseModel):
    summary: str = Field(min_length=1)
    action_items: list[_ItemModel]


def transcript_text(segments: list[Segment]) -> str:
    return "\n".join(f"[{format_ts(s.start)}] {s.text}" for s in segments)


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rstrip()
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def parse_summary(content: str) -> Summary:
    """Validate the model's answer. Raises ValueError when it is unusable."""
    model = _SummaryModel.model_validate_json(_strip_fences(content))
    return Summary(
        text=model.summary.strip(),
        action_items=tuple(
            ActionItem(i.task.strip(), _blank_to_none(i.owner), _blank_to_none(i.due), i.source_ts)
            for i in model.action_items
        ),
    )


class LiteLLMSummarizer:
    def __init__(self, client: httpx.Client, model: str) -> None:
        self._client = client
        self._model = model

    def summarize(self, segments: list[Segment]) -> Summary | None:
        if not segments:
            return None
        transcript = transcript_text(segments)
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return parse_summary(self._complete(transcript))
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                log.warning("summary attempt %d/%d failed: %s", attempt, ATTEMPTS, exc)
        return None

    def _complete(self, transcript: str) -> str:
        response = self._client.post(
            "/v1/chat/completions",
            json={
                "model": self._model,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": transcript},
                ],
            },
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
