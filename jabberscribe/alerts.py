"""Operator alerts over a generic webhook.

The service is unattended, so a failed call, a growing backlog, an
unreachable LiteLLM or a purge that could not delete must reach a person.
Each alert is one JSON POST to one webhook -- an internal relay, a monitoring
system, or a Teams/Slack incoming webhook, which render the `text` field as is.

Each kind is sent at most once an hour. The send times persist in alerts.json
next to the database, so a restarting service does not spam. Payloads carry
kinds, counts and job keys -- never names or call content. Sending never
raises: an alert that cannot be delivered is logged and tried again at the
next opportunity.

The URL comes from JABBERSCRIBE_ALERT_WEBHOOK_URL when set (such URLs often
embed a secret), else from alerts.webhook_url; JABBERSCRIBE_ALERT_TOKEN, when
set, is sent as a bearer token.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from jabberscribe.config import Config
from jabberscribe.jobs import FAILED, JobStore, iso
from jabberscribe.output import write_atomic

log = logging.getLogger(__name__)

URL_ENV = "JABBERSCRIBE_ALERT_WEBHOOK_URL"
TOKEN_ENV = "JABBERSCRIBE_ALERT_TOKEN"
STATE_FILE = "alerts.json"
RATE_LIMIT = timedelta(hours=1)
#: After a failed delivery a kind is not retried for this long, so a dead webhook costs one timeout, not one per poll.
BACKOFF = timedelta(minutes=5)
TIMEOUT_SECONDS = 10.0
#: How many job keys one alert lists; the rest are counted.
MAX_KEYS = 5

JOB_FAILED = "job_failed"
BACKLOG = "backlog"
LITELLM_DOWN = "litellm_down"
PURGE_ERRORS = "purge_errors"


class _DropAll(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return False


@contextmanager
def _httpx_silenced() -> Iterator[None]:
    """httpx logs every request line with its full URL; the webhook URL may embed a secret, so mute it during a send."""
    httpx_log = logging.getLogger("httpx")
    mute = _DropAll()
    httpx_log.addFilter(mute)
    try:
        yield
    finally:
        httpx_log.removeFilter(mute)


def _parse_time(value: object) -> datetime | None:
    """A timezone-aware ISO timestamp, or None for anything else (corrupt state must not raise)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _times(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and _parse_time(v) is not None}


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    seen = data.get("failed_seen")
    return {
        "sent": _times(data.get("sent")),
        "failed_backoff": _times(data.get("failed_backoff")),
        "failed_seen": [k for k in seen if isinstance(k, str)] if isinstance(seen, list) else [],
    }


class Alerter:
    def __init__(
        self, url: str, state_path: Path, client: httpx.Client | None = None, token: str | None = None
    ) -> None:
        self._url = url
        self._state_path = state_path
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._state = _load_state(state_path)

    def send(self, kind: str, message: str, now: datetime) -> bool:
        """POST one alert unless `kind` went out within the last hour. True only when it was delivered. Never raises."""
        try:
            return self._send(kind, message, now)
        except Exception as exc:
            log.error("alert %s not delivered (%s)", kind, type(exc).__name__)
            return False

    def _send(self, kind: str, message: str, now: datetime) -> bool:
        last = _parse_time(self._state["sent"].get(kind))
        if last is not None and now - last < RATE_LIMIT:
            log.info("alert %s suppressed by the rate limit: %s", kind, message)
            return False
        failed = _parse_time(self._state["failed_backoff"].get(kind))
        if failed is not None and now - failed < BACKOFF:
            log.info("alert %s held back after a failed delivery: %s", kind, message)
            return False
        payload = {
            "text": f"JabberScribe {kind}: {message}",
            "source": "jabberscribe",
            "kind": kind,
            "message": message,
            "host": socket.gethostname(),
            "at": iso(now),
        }
        try:
            with _httpx_silenced():
                response = self._client.post(self._url, json=payload, headers=self._headers)
            response.raise_for_status()
        except Exception as exc:
            # Never log the exception text or URL: webhook URLs often embed a secret.
            reason = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            log.error("alert %s not delivered (%s): %s", kind, reason, message)
            self._state["failed_backoff"][kind] = iso(now)
            self._save()
            return False
        self._state["failed_backoff"].pop(kind, None)
        self._state["sent"][kind] = iso(now)
        self._save()
        log.warning("alert %s sent: %s", kind, message)
        return True

    @property
    def failed_seen(self) -> set[str]:
        """Failed jobs already alerted, so each failure is announced once."""
        return set(self._state["failed_seen"])

    def remember_failed(self, keys: set[str]) -> None:
        self._state["failed_seen"] = sorted(keys)
        self._save()

    def _save(self) -> None:
        try:
            write_atomic(self._state_path, json.dumps(self._state, indent=2))
        except OSError as exc:
            log.error("cannot save the alert state %s: %s", self._state_path, exc)


def make_alerter(cfg: Config, client: httpx.Client | None = None) -> Alerter | None:
    """The configured alerter, or None when no webhook URL is set (alerting off)."""
    url = os.environ.get(URL_ENV) or cfg.alerts.webhook_url
    if not url:
        return None
    return Alerter(url, cfg.paths.db_path.parent / STATE_FILE, client, os.environ.get(TOKEN_ENV))


def _listed(keys: list[str]) -> str:
    shown = ", ".join(keys[:MAX_KEYS])
    return shown if len(keys) <= MAX_KEYS else f"{shown} and {len(keys) - MAX_KEYS} more"


def check_jobs(store: JobStore, alerter: Alerter, now: datetime, backlog_minutes: int) -> None:
    """Alert on calls that newly failed and on a backlog older than `backlog_minutes`."""
    failed = {j.job_key for j in store.list_by_status(FAILED) if j.grouped_into is None}
    new = sorted(failed - alerter.failed_seen)
    if new:
        message = f"{len(new)} call(s) failed: {_listed(new)}; see `jabberscribe status`"
        if alerter.send(JOB_FAILED, message, now):
            alerter.remember_failed(failed)
    elif failed != alerter.failed_seen:
        # Retried or purged jobs leave the set, so a second failure of the same call is announced again.
        alerter.remember_failed(failed)

    oldest = store.oldest_pending_created_at()
    if oldest is not None:
        waited = now - datetime.fromisoformat(oldest)
        if waited > timedelta(minutes=backlog_minutes):
            alerter.send(BACKLOG, f"the oldest unfinished call has waited {waited.total_seconds() / 60:.0f} min", now)


def probe_litellm(client: httpx.Client) -> str | None:
    """None when LiteLLM answers its model listing, else what went wrong.

    A short timeout of its own: the shared client waits up to litellm.timeout_seconds (10 min) for a transcription,
    and a hung server must not stall every poll that long.
    """
    try:
        response = client.get("/v1/models", timeout=TIMEOUT_SECONDS)
    except httpx.TransportError as exc:
        return str(exc) or type(exc).__name__
    if response.status_code == 429 or response.status_code >= 500:
        return f"HTTP {response.status_code}"
    return None
