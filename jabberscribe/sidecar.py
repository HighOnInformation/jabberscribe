"""Sidecar metadata: the capture layer's half of the drop contract.

Validation is deliberately narrow. Only call_id, started_at, duration_sec, and
audio.tracks are required, because those are the fields the pipeline cannot
function without. Everything else degrades: a call with no participant emails
still gets transcribed and published.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

VALID_TRACKS = ("dual", "mixed")
VALID_KINDS = ("call", "conference")


class SidecarError(ValueError):
    """The sidecar is unusable. The caller should quarantine the pair."""


@dataclass(frozen=True)
class Participant:
    display_name: str | None = None
    uri: str | None = None
    extension: str | None = None
    email: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class Sidecar:
    call_id: str
    kind: str
    source: str
    started_at: str
    ended_at: str | None
    duration_sec: int
    subject: str | None
    participants: tuple[Participant, ...]
    tracks: str
    sample_rate: int | None
    channels: int | None
    raw: str

    @property
    def emails(self) -> tuple[str, ...]:
        return tuple(p.email for p in self.participants if p.email)


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _require_nonempty_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SidecarError(f"{key} is required and must be a non-empty string")
    return value


def _parse_participant(raw: object) -> Participant:
    if not isinstance(raw, dict):
        raise SidecarError("each entry in participants must be an object")
    return Participant(
        display_name=_opt_str(raw.get("display_name")),
        uri=_opt_str(raw.get("uri")),
        extension=_opt_str(raw.get("extension")),
        email=_opt_str(raw.get("email")) or None,
        role=_opt_str(raw.get("role")),
    )


def parse_sidecar(text: str) -> Sidecar:
    """Parse and validate a sidecar document.

    Raises SidecarError for anything the pipeline cannot work with.
    """
    # Strip a leading BOM. PowerShell, .NET, and Notepad all emit UTF-8 with a
    # BOM by default, so a recorder written in any of them would otherwise have
    # every one of its sidecars rejected.
    text = text.lstrip("﻿")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SidecarError(f"sidecar is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SidecarError("sidecar must be a JSON object")

    call_id = _require_nonempty_str(data, "call_id")
    started_at = _require_nonempty_str(data, "started_at")

    duration = data.get("duration_sec")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise SidecarError("duration_sec is required and must be a non-negative integer")

    audio = data.get("audio")
    if not isinstance(audio, dict):
        raise SidecarError("audio is required and must be an object")
    tracks = audio.get("tracks")
    if tracks not in VALID_TRACKS:
        raise SidecarError(f"audio.tracks must be one of {VALID_TRACKS}, got {tracks!r}")

    kind = data.get("kind") or "call"
    if kind not in VALID_KINDS:
        raise SidecarError(f"kind must be one of {VALID_KINDS}, got {kind!r}")

    raw_participants = data.get("participants") or []
    if not isinstance(raw_participants, list):
        raise SidecarError("participants must be a list")

    sample_rate = audio.get("sample_rate")
    channels = audio.get("channels")

    return Sidecar(
        call_id=call_id,
        kind=kind,
        source=_opt_str(data.get("source")) or "unknown",
        started_at=started_at,
        ended_at=_opt_str(data.get("ended_at")),
        duration_sec=duration,
        subject=_opt_str(data.get("subject")),
        participants=tuple(_parse_participant(p) for p in raw_participants),
        tracks=tracks,
        sample_rate=sample_rate if isinstance(sample_rate, int) else None,
        channels=channels if isinstance(channels, int) else None,
        raw=text,
    )
