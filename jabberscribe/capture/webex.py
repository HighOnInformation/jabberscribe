"""Webex Calling exporter: Converged Recordings API -> drop folder.

Webex Calling records in the cloud, so for a cloud tenant there is no media
stream to fork on-prem. Instead this exporter polls the admin Converged
Recordings API, downloads each new recording, converts it to WAV, and writes a
v2 drop pair the watcher consumes unchanged. Polling, not webhooks: the service
sits behind NAT and webhook targets must be public HTTPS.

Several API details are documented only in SDK sources or not at all. Each one
lives in a single small function or constant marked ASSUMPTION, so a real
tenant test can correct it in one place. See
docs/superpowers/specs/2026-10-08-webex-capture-design.md.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

TOKEN_ENV = "JABBERSCRIBE_WEBEX_TOKEN"
#: The list endpoint rejects a from/to range longer than 30 days.
MAX_WINDOW = timedelta(days=30)

# ASSUMPTION: admin/compliance list path, from the API reference snippet and wxc_sdk.
_LIST_PATH = "/admin/convergedRecordings"


class WebexConfigError(Exception):
    """The exporter config is missing, unreadable, or invalid."""


class WebexConfig(BaseModel):
    """Exporter settings. The token is deliberately absent: it comes from TOKEN_ENV."""

    model_config = ConfigDict(extra="forbid")

    #: The watcher's inbox (drop_root/inbox in the main config).
    inbox: Path
    #: The exporter's own SQLite ledger. Never the jobs DB: the two must not share a schema.
    state_path: Path
    #: Scratch space for downloaded MP3s; must not be the inbox.
    work_dir: Path
    base_url: str = "https://webexapis.com/v1"
    poll_seconds: int = Field(60, ge=1)
    #: Each poll lists recordings created this far back; the ledger skips finished ones.
    lookback_hours: int = Field(48, ge=1, le=int(MAX_WINDOW.total_seconds() // 3600))
    page_size: int = Field(100, ge=1, le=1000)
    #: A recording that fails this many times is parked instead of retried forever.
    max_attempts: int = Field(5, ge=1)
    #: Remove the Webex copy after a successful export. A compliance decision, so off by default.
    delete_after_export: bool = False
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    timeout_seconds: float = 60.0


def load_webex_config(path: Path) -> WebexConfig:
    if not path.is_file():
        raise WebexConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise WebexConfigError(f"config file is not valid YAML: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise WebexConfigError(f"config file must contain a mapping at the top level: {path}")
    try:
        return WebexConfig(**raw)
    except ValidationError as exc:
        raise WebexConfigError(f"invalid config in {path}: {exc}") from exc


def _iso_z(moment: datetime) -> str:
    """Webex query timestamps: UTC with milliseconds and a Z suffix."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _windows(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    lo = start
    while lo < end:
        hi = min(lo + MAX_WINDOW, end)
        yield lo, hi
        lo = hi


class WebexClient:
    """Thin wrapper over the Converged Recordings endpoints the exporter needs."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        response = self._http.get(url, params=params)
        response.raise_for_status()
        return response

    def list_recordings(self, start: datetime, end: datetime, page_size: int = 100) -> Iterator[dict]:
        """Yield every available recording created in [start, end), all pages, all windows."""
        for lo, hi in _windows(start, end):
            url: str | None = self._base_url + _LIST_PATH
            params: dict | None = {"from": _iso_z(lo), "to": _iso_z(hi), "status": "available", "max": page_size}
            while url:
                response = self._get(url, params)
                yield from response.json().get("items") or []
                # The next link carries the full query, so later pages send no params.
                url = response.links.get("next", {}).get("url")
                params = None
