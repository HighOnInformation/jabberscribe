"""Meeting summary and action items via the LiteLLM chat route.

A summary failure must never cost the user their transcript. Failures split
four ways:

- Unusable model output (not JSON, wrong shape, empty content): retried once,
  then SummaryUnavailable: the call carries on with the summary marked
  unavailable and the reason recorded.
- HTTP 400, 413 or 422 (the input itself was rejected, e.g. the context window
  exceeded): SummaryUnavailable at once; retrying the same input cannot help.
- LiteLLM down or overloaded: TransientError, so the job retries later with
  the transcript already checkpointed. A two-minute Gemma restart must not
  cost a summary forever.
- Any other 4xx (401/403 bad key, 404 wrong model name): SummaryError, so the
  job fails loudly instead of shipping "unavailable" for every call.

The instructions travel in the user message: Gemma 1 and 2 chat templates
reject a system role. Transcripts longer than max_chunk_chars are summarized
per chunk, then the chunk summaries are merged, in batches that fit
max_chunk_chars and again until one is left; action items are the union of
the chunks' items, each keeping its own source_ts.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from jabberscribe.llm import post
from jabberscribe.stt import Segment, format_ts

log = logging.getLogger(__name__)

ATTEMPTS = 2
#: Explicit, so the server default cannot cut the JSON answer off mid-object.
MAX_TOKENS = 2048
#: How many hex digits of an unusable answer's SHA-256 go into the log. Never its text: that is call content.
LOG_HASH_CHARS = 12
#: The chat route rejected this input (too long, unprocessable): degrade, do not fail the job.
UNPROCESSABLE_STATUSES = (400, 413, 422)

INSTRUCTIONS = """You summarize Hebrew business phone calls and meetings from a timestamped transcript.
Write in Hebrew. Keep English technical terms exactly as spoken.
Return only a JSON object with this shape:
{"summary": "<concise Hebrew summary of the call>",
 "action_items": [{"task": "<what must be done>", "owner": "<name or null>",
                   "due": "<deadline or null>", "source_ts": "HH:MM:SS"}]}
Rules:
- owner and due: fill them only when explicitly stated in the call; otherwise null. Never guess.
- source_ts: the timestamp of the transcript line the item comes from, copied exactly.
- If there are no action items, return an empty list."""

MERGE_INSTRUCTIONS = """You merge the partial summaries of one Hebrew call, given in order, into one summary.
Write in Hebrew. Keep English technical terms exactly as written.
Return only a JSON object with this shape:
{"summary": "<concise Hebrew summary of the whole call>"}"""


class SummaryError(RuntimeError):
    """LiteLLM rejected the summary request as misconfigured (401, 403, 404, ...). Retrying will not help."""


class SummaryUnavailable(Exception):
    """No summary for this call; it carries on with its transcript. str() is the reason, without call content."""


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
    def summarize(self, segments: list[Segment]) -> Summary | None:
        """None, or SummaryUnavailable raised: the call carries on without a summary."""
        ...


class _ItemModel(BaseModel):
    task: str = Field(min_length=1)
    owner: str | None = None
    due: str | None = None
    source_ts: str = Field(pattern=r"^\d{2}:\d{2}:\d{2}$")


class _SummaryModel(BaseModel):
    summary: str = Field(min_length=1)
    action_items: list[_ItemModel]


class _MergeModel(BaseModel):
    summary: str = Field(min_length=1)


def transcript_text(segments: list[Segment]) -> str:
    return "\n".join(f"[{format_ts(s.start)}] {s.text}" for s in segments)


def chunk_segments(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    """Split into consecutive chunks whose transcript text stays within max_chars.

    A single segment longer than max_chars still gets a chunk of its own.
    """
    if len(transcript_text(segments)) <= max_chars:
        return [segments]
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        line = len(transcript_text([segment])) + 1
        if current and size + line > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(segment)
        size += line
    if current:
        chunks.append(current)
    return chunks


def strip_fences(content: str) -> str:
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
    model = _SummaryModel.model_validate_json(strip_fences(content))
    return Summary(
        text=model.summary.strip(),
        action_items=tuple(
            ActionItem(i.task.strip(), _blank_to_none(i.owner), _blank_to_none(i.due), i.source_ts)
            for i in model.action_items
        ),
    )


def _parse_merge(content: str) -> str:
    return _MergeModel.model_validate_json(strip_fences(content)).summary.strip()


def _numbered(texts: list[str]) -> str:
    return "\n\n".join(f"Part {i}:\n{t}" for i, t in enumerate(texts, start=1))


def _merge_batches(texts: list[str], max_chars: int) -> list[list[str]]:
    """Consecutive batches whose numbered text fits max_chars. A batch takes at least two, so merging progresses."""
    batches: list[list[str]] = [[]]
    for text in texts:
        if len(batches[-1]) >= 2 and len(_numbered([*batches[-1], text])) > max_chars:
            batches.append([])
        batches[-1].append(text)
    return batches


def _union(parts: list[Summary]) -> tuple[ActionItem, ...]:
    items: list[ActionItem] = []
    for part in parts:
        items.extend(i for i in part.action_items if i not in items)
    return tuple(items)


class LiteLLMSummarizer:
    def __init__(self, client: httpx.Client, model: str, max_chunk_chars: int = 12000) -> None:
        self._client = client
        self._model = model
        self._max_chunk_chars = max_chunk_chars

    def summarize(self, segments: list[Segment]) -> Summary | None:
        """None for an empty transcript. Raises SummaryUnavailable, TransientError or SummaryError."""
        if not segments:
            return None
        chunks = chunk_segments(segments, self._max_chunk_chars)
        try:
            parts = [
                self._ask(INSTRUCTIONS, "Transcript:\n" + transcript_text(chunk), parse_summary) for chunk in chunks
            ]
            texts = [p.text for p in parts]
            while len(texts) > 1:
                texts = [
                    self._ask(MERGE_INSTRUCTIONS, "Partial summaries:\n" + _numbered(batch), _parse_merge)
                    if len(batch) > 1
                    else batch[0]
                    for batch in _merge_batches(texts, self._max_chunk_chars)
                ]
        except SummaryUnavailable as exc:
            log.warning("summary unavailable (%d chunk(s)): %s", len(chunks), exc)
            raise
        return parts[0] if len(parts) == 1 else Summary(texts[0], _union(parts))

    def _ask[T](self, instructions: str, body: str, parse: Callable[[str], T]) -> T:
        for attempt in range(1, ATTEMPTS + 1):
            content: str | None = None
            try:
                content = self._complete(f"{instructions}\n\n{body}")
                return parse(content)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                # Error type, length and hash only: messages such as pydantic's quote the input.
                raw = (content or "").encode("utf-8")
                log.warning(
                    "summary attempt %d/%d returned unusable output (%s): %d chars, sha256 %s",
                    attempt,
                    ATTEMPTS,
                    type(exc).__name__,
                    len(content or ""),
                    hashlib.sha256(raw).hexdigest()[:LOG_HASH_CHARS],
                )
        raise SummaryUnavailable(f"model output stayed unusable after {ATTEMPTS} attempts")

    def _complete(self, prompt: str) -> str:
        """One chat completion. Raises ValueError when the answer has no text content."""
        try:
            response = post(
                self._client,
                "/v1/chat/completions",
                json={
                    "model": self._model,
                    "temperature": 0.2,
                    "max_tokens": MAX_TOKENS,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in UNPROCESSABLE_STATUSES:
                raise SummaryUnavailable(f"chat route rejected the request: HTTP {status}") from exc
            raise SummaryError(f"summary request rejected: {exc}") from exc
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            # A refusal or an empty completion: unusable output, not a crash.
            raise ValueError(f"model returned no text content ({type(content).__name__})")
        return content
