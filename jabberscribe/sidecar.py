"""Sidecar metadata: the recorder's half of the drop contract.

Validation is deliberately narrow. Only call_id, line_owner.extension,
started_at, duration_sec, and audio.tracks are required -- the fields the
pipeline cannot work without. Everything else degrades.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
VALID_TRACKS = ("dual", "mixed")
VALID_KINDS = ("call", "conference")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class SidecarError(ValueError):
    """The sidecar is unusable. The caller should quarantine the pair."""


def job_key(call_id: str, extension: str) -> str:
    """Identity of one recorded line's copy of a call.

    It names both the job row and the output folder, so it must be a valid
    Windows path segment. CUCM shares one call id across both ends of an
    internal call, which is why the line is part of the key.
    """
    return f"{_UNSAFE.sub('-', call_id)}_{_UNSAFE.sub('-', extension)}"


@dataclass(frozen=True)
class Party:
    extension: str | None = None
    user: str | None = None
    display_name: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"extension": self.extension, "user": self.user, "display_name": self.display_name}


@dataclass(frozen=True)
class Sidecar:
    call_id: str
    conference_id: str | None
    line_owner: Party
    parties: tuple[Party, ...]
    kind: str
    started_at: str
    ended_at: str | None
    duration_sec: int
    tracks: str
    raw: str

    @property
    def job_key(self) -> str:
        # parse_sidecar guarantees a non-empty line_owner.extension.
        return job_key(self.call_id, self.line_owner.extension or "")


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _require_nonempty_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SidecarError(f"{key} is required and must be a non-empty string")
    return value


def _parse_party(raw: object, where: str) -> Party:
    if not isinstance(raw, dict):
        raise SidecarError(f"{where} must be an object")
    return Party(
        extension=_opt_str(raw.get("extension")),
        user=_opt_str(raw.get("user")),
        display_name=_opt_str(raw.get("display_name")),
    )


def parse_sidecar(text: str) -> Sidecar:
    """Parse and validate a sidecar document.

    Raises SidecarError for anything the pipeline cannot work with.
    """
    # Strip a leading BOM. PowerShell, .NET, and Notepad all emit UTF-8 with a
    # BOM by default, so a recorder written in any of them would otherwise have
    # every one of its sidecars rejected.
    text = text.lstrip("\N{BYTE ORDER MARK}")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        # json.loads raises RecursionError, not JSONDecodeError, on deeply nested input.
        raise SidecarError(f"sidecar is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SidecarError("sidecar must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SidecarError(f"schema_version must be {SCHEMA_VERSION}, got {data.get('schema_version')!r}")

    call_id = _require_nonempty_str(data, "call_id")
    started_at = _require_nonempty_str(data, "started_at")
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError as exc:
        raise SidecarError(f"started_at must be an ISO 8601 timestamp, got {started_at!r}") from exc
    if started.tzinfo is None:
        # Grouping and retention compare timestamps; naive and aware values cannot be compared.
        raise SidecarError(f"started_at must carry a UTC offset, got {started_at!r}")

    line_owner = _parse_party(data.get("line_owner"), "line_owner")
    extension = (line_owner.extension or "").strip()
    if not extension:
        raise SidecarError("line_owner.extension is required and must be a non-empty string")
    # " 1042" and "1042" are one line: they must give one job key and one owner.
    line_owner = Party(extension=extension, user=line_owner.user, display_name=line_owner.display_name)
    if not line_owner.user:
        log.warning("sidecar for call %s has no line_owner.user; the web app cannot attribute it", call_id)

    conference_id = data.get("conference_id")
    if conference_id is not None and (not isinstance(conference_id, str) or not conference_id.strip()):
        raise SidecarError("conference_id must be a non-empty string or null")

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

    raw_parties = data.get("parties") or []
    if not isinstance(raw_parties, list):
        raise SidecarError("parties must be a list")

    return Sidecar(
        call_id=call_id,
        conference_id=conference_id,
        line_owner=line_owner,
        parties=tuple(_parse_party(p, "each entry in parties") for p in raw_parties),
        kind=kind,
        started_at=started_at,
        ended_at=_opt_str(data.get("ended_at")),
        duration_sec=duration,
        tracks=tracks,
        raw=text,
    )
