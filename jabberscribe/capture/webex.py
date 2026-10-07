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

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jabberscribe.sidecar import job_key

log = logging.getLogger(__name__)

TOKEN_ENV = "JABBERSCRIBE_WEBEX_TOKEN"
#: The list endpoint rejects a from/to range longer than 30 days.
MAX_WINDOW = timedelta(days=30)

# ASSUMPTION: admin/compliance list path, from the API reference snippet and wxc_sdk.
_LIST_PATH = "/admin/convergedRecordings"
# ASSUMPTION: details, metadata and delete paths are the wxc_sdk ones. The Webex
# blog says admin/compliance variants exist; whether they differ is unverified.
_DETAILS_PATH = "/convergedRecordings/{id}"
_METADATA_PATH = "/convergedRecordings/{id}/metadata"
_DELETE_PATH = "/convergedRecordings/{id}"

_FFMPEG_TIMEOUT_SECONDS = 1800
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class ExportError(RuntimeError):
    """One recording could not be exported. Counted against its attempts."""


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

    def details(self, recording_id: str) -> dict:
        return self._get(self._base_url + _DETAILS_PATH.format(id=recording_id)).json()

    def metadata(self, recording_id: str) -> dict:
        """Owner name and participants. Optional: a 403/404 degrades to {} rather than blocking export."""
        url = self._base_url + _METADATA_PATH.format(id=recording_id)
        try:
            return self._get(url, {"showAllTypes": "true"}).json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (403, 404):
                raise
            log.warning("no metadata for recording %s (HTTP %s)", recording_id, exc.response.status_code)
            return {}

    def download(self, url: str, dest: Path) -> None:
        """Stream the audio to `dest` via a .part file so a dead download never looks complete."""
        request = self._http.build_request("GET", url)
        if request.url.host != httpx.URL(self._base_url).host:
            # ASSUMPTION: the temporary link is self-authenticating; never leak the token to another host.
            del request.headers["Authorization"]
        part = dest.with_name(dest.name + ".part")
        response = self._http.send(request, stream=True)
        try:
            response.raise_for_status()
            with part.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        finally:
            response.close()
        if part.stat().st_size == 0:
            part.unlink()
            raise ExportError("downloaded audio is empty")
        part.replace(dest)

    def delete(self, recording_id: str) -> None:
        # ASSUMPTION: compliance hard delete takes an optional reason and comment in the body.
        response = self._http.request(
            "DELETE",
            self._base_url + _DELETE_PATH.format(id=recording_id),
            json={"reasonForDeletion": "exported to JabberScribe", "comment": "exported to JabberScribe"},
        )
        response.raise_for_status()


def _audio_link(details: dict) -> str:
    """ASSUMPTION: details carry temporaryDirectDownloadLinks.audioDownloadLink (wxc_sdk model), valid 3 h."""
    link = (details.get("temporaryDirectDownloadLinks") or {}).get("audioDownloadLink")
    if not link:
        raise ExportError("details have no audio download link (recording not ready?)")
    return link


# --- sidecar mapping --------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """What ffprobe says about the downloaded audio."""

    channels: int
    sample_rate: int
    duration_sec: float


def _service_data(rec: dict) -> dict:
    return rec.get("serviceData") or {}


def _session_id(rec: dict) -> str:
    """The id both ends (and every leg) of one call share.

    ASSUMPTION: serviceData.callSessionId is common to every recording of one
    call; callId is per leg. Falls back to the recording id so a sparse item
    still gets a stable, unique call_id.
    """
    sd = _service_data(rec)
    return str(sd.get("callSessionId") or sd.get("callId") or rec["id"])


def _owner_and_other(rec: dict) -> tuple[dict, dict]:
    """(owner's party, other party) from the recorded leg's direction.

    ASSUMPTION: personality TERMINATING means the owner was called; anything
    else (ORIGINATING, CLICK_TO_DIAL) means the owner placed the call.
    """
    sd = _service_data(rec)
    calling = sd.get("callingParty") or {}
    called = sd.get("calledParty") or {}
    if str(sd.get("personality", "")).upper() == "TERMINATING":
        return called, calling
    return calling, called


def _participants(meta: dict) -> list:
    """ASSUMPTION: metadata (showAllTypes=true) lists participants under serviceData.participants."""
    return (meta.get("serviceData") or {}).get("participants") or meta.get("participants") or []


def _parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _started_at(rec: dict) -> datetime:
    """Call start, rendered in the host's local offset so output folders date like the users do.

    timeRecorded is when recording began, which can be later than the call
    (On Demand mode); the session start is preferred when present.
    """
    session = _service_data(rec).get("session") or {}
    raw = session.get("startTime") or rec.get("timeRecorded") or rec["createTime"]
    return _parse_time(raw).astimezone()


def _email_user(rec: dict) -> str | None:
    email = rec.get("ownerEmail")
    return email.split("@", 1)[0] if email else None


def to_sidecar(rec: dict, meta: dict, probe: Probe, *, shared_session: bool) -> dict:
    """Map one recording (details item + metadata) to a v2 sidecar document.

    `shared_session` is True when another recording in the same listing has the
    same session id, i.e. several recorded legs of one call.
    """
    session = _session_id(rec)
    call_id = f"wxc-{session}"
    is_conference = shared_session or len(_participants(meta)) > 2
    owner, other = _owner_and_other(rec)
    user = _email_user(rec)
    # A pair without an extension is quarantined, so fall back to something that names the line.
    extension = owner.get("number") or user or str(rec.get("ownerId") or rec["id"])
    started = _started_at(rec)
    duration = int(rec.get("durationSeconds") or round(probe.duration_sec))
    parties = []
    if other.get("number") or other.get("name"):
        parties.append({"extension": other.get("number"), "user": None, "display_name": other.get("name")})
    return {
        "schema_version": 2,
        "call_id": call_id,
        "conference_id": call_id if is_conference else None,
        "line_owner": {
            "extension": str(extension),
            "user": user,
            "display_name": meta.get("ownerName") or owner.get("name"),
        },
        "parties": parties,
        "kind": "conference" if is_conference else "call",
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(seconds=duration)).isoformat(),
        "duration_sec": duration,
        "audio": {
            "tracks": "dual" if probe.channels == 2 else "mixed",
            "sample_rate": probe.sample_rate,
            "channels": probe.channels,
        },
    }


def drop_name(sidecar: dict) -> str:
    return job_key(sidecar["call_id"], sidecar["line_owner"]["extension"])


# --- audio and the drop pair ------------------------------------------------


def _run(args: list[str]) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_SECONDS, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ExportError(f"{args[0]} failed: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise ExportError(f"{args[0]} exited {proc.returncode}: {' | '.join(tail)}")
    return proc.stdout


def probe_audio(path: Path, ffprobe: str = "ffprobe") -> Probe:
    out = _run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels,sample_rate"]
        + ["-show_entries", "format=duration", "-of", "json", str(path)]
    )
    data = json.loads(out)
    streams = data.get("streams") or []
    if not streams:
        raise ExportError("no audio stream in downloaded file")
    return Probe(
        channels=int(streams[0]["channels"]),
        sample_rate=int(streams[0]["sample_rate"]),
        duration_sec=float((data.get("format") or {}).get("duration") or 0.0),
    )


def write_drop_pair(inbox: Path, name: str, src_audio: Path, sidecar: dict, ffmpeg: str = "ffmpeg") -> None:
    """Honour the drop contract: audio .part -> .wav, then the sidecar .part -> .json LAST.

    The watcher treats a sidecar as proof the audio is complete, so nothing may
    leave a .json in the inbox before the .wav is fully in place.
    """
    inbox.mkdir(parents=True, exist_ok=True)
    wav = inbox / f"{name}.wav"
    wav_part = inbox / f"{name}.wav.part"
    json_path = inbox / f"{name}.json"
    json_part = inbox / f"{name}.json.part"
    try:
        # Keep the channel layout and rate: the pipeline splits dual tracks itself.
        # -f wav is required: the .part suffix defeats ffmpeg's muxer detection.
        _run([ffmpeg, "-y", "-nostdin", "-i", str(src_audio), "-c:a", "pcm_s16le", "-f", "wav", str(wav_part)])
        wav_part.replace(wav)
        json_part.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
        json_part.replace(json_path)
    finally:
        wav_part.unlink(missing_ok=True)
        json_part.unlink(missing_ok=True)


# --- ledger -----------------------------------------------------------------

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS exported (
    recording_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,            -- done | failed
    job_key TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
"""


class Ledger:
    """Which Webex recordings are already exported. The exporter's own file, never the jobs DB."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.executescript(_LEDGER_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _row(self, recording_id: str) -> tuple[str, int] | None:
        return self._conn.execute(
            "SELECT state, attempts FROM exported WHERE recording_id = ?", (recording_id,)
        ).fetchone()

    def attempts(self, recording_id: str) -> int:
        row = self._row(recording_id)
        return row[1] if row else 0

    def is_settled(self, recording_id: str, max_attempts: int) -> bool:
        """Done, or failed often enough to be parked."""
        row = self._row(recording_id)
        return row is not None and (row[0] == "done" or row[1] >= max_attempts)

    def mark_done(self, recording_id: str, key: str) -> None:
        self._conn.execute(
            "INSERT INTO exported (recording_id, state, job_key, updated_at) VALUES (?, 'done', ?, ?) "
            "ON CONFLICT(recording_id) DO UPDATE SET state='done', job_key=excluded.job_key, "
            "last_error=NULL, updated_at=excluded.updated_at",
            (recording_id, key, _utcnow()),
        )

    def mark_failed(self, recording_id: str, error: str) -> int:
        self._conn.execute(
            "INSERT INTO exported (recording_id, state, attempts, last_error, updated_at) "
            "VALUES (?, 'failed', 1, ?, ?) "
            "ON CONFLICT(recording_id) DO UPDATE SET attempts=attempts+1, last_error=excluded.last_error, "
            "updated_at=excluded.updated_at",
            (recording_id, error, _utcnow()),
        )
        return self.attempts(recording_id)


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


# --- exporter ---------------------------------------------------------------


@dataclass(frozen=True)
class PollResult:
    exported: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def _is_transient(exc: httpx.HTTPError) -> bool:
    """Rate limits, server errors and network failures say nothing about the recording itself."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or status >= 500
    return True


class Exporter:
    def __init__(
        self,
        cfg: WebexConfig,
        client: WebexClient,
        ledger: Ledger,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._ledger = ledger
        self._now = now

    def run_once(self) -> PollResult:
        """One poll. Transient HTTP errors propagate: the poll stops and the next one retries."""
        end = self._now()
        start = end - timedelta(hours=self._cfg.lookback_hours)
        items = list(self._client.list_recordings(start, end, self._cfg.page_size))
        sessions = Counter(_session_id(item) for item in items)
        exported: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []

        for item in items:
            rec_id = item["id"]
            if self._ledger.is_settled(rec_id, self._cfg.max_attempts):
                skipped.append(rec_id)
                continue
            try:
                key = self._export(item, shared_session=sessions[_session_id(item)] > 1)
            except httpx.HTTPError as exc:
                if _is_transient(exc) or not isinstance(exc, httpx.HTTPStatusError):
                    raise
                # Status only: the exception text carries the URL, and download URLs are credentials.
                self._fail(rec_id, f"HTTP {exc.response.status_code}")
                failed.append(rec_id)
                continue
            except ExportError as exc:
                self._fail(rec_id, str(exc))
                failed.append(rec_id)
                continue
            self._ledger.mark_done(rec_id, key)
            exported.append(key)
            log.info("exported recording %s as %s", rec_id, key)
            if self._cfg.delete_after_export:
                self._delete(rec_id)

        return PollResult(tuple(exported), tuple(skipped), tuple(failed))

    def _export(self, item: dict, *, shared_session: bool) -> str:
        rec_id = item["id"]
        details = self._client.details(rec_id)
        link = _audio_link(details)
        meta = self._client.metadata(rec_id)
        self._cfg.work_dir.mkdir(parents=True, exist_ok=True)
        mp3 = self._cfg.work_dir / f"{_UNSAFE.sub('-', rec_id)}.mp3"
        try:
            self._client.download(link, mp3)
            probe = probe_audio(mp3, self._cfg.ffprobe)
            sidecar = to_sidecar({**item, **details}, meta, probe, shared_session=shared_session)
            name = drop_name(sidecar)
            write_drop_pair(self._cfg.inbox, name, mp3, sidecar, self._cfg.ffmpeg)
        finally:
            mp3.unlink(missing_ok=True)
        return name

    def _fail(self, rec_id: str, error: str) -> None:
        attempts = self._ledger.mark_failed(rec_id, error)
        if attempts >= self._cfg.max_attempts:
            log.error("parking recording %s after %d attempts: %s", rec_id, attempts, error)
        else:
            log.warning("recording %s failed (attempt %d): %s", rec_id, attempts, error)

    def _delete(self, rec_id: str) -> None:
        try:
            self._client.delete(rec_id)
            log.info("deleted Webex copy of recording %s", rec_id)
        except httpx.HTTPError as exc:
            # The export already stands; a stale cloud copy is not worth undoing it for.
            log.error("could not delete Webex copy of recording %s: %s", rec_id, exc.__class__.__name__)



# --- entry point ------------------------------------------------------------


def _poll(exporter: Exporter) -> bool:
    try:
        result = exporter.run_once()
    except httpx.HTTPError as exc:
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.__class__.__name__
        log.error("poll aborted (%s); retrying next poll", status)
        return False
    log.info("poll: %d exported, %d skipped, %d failed", len(result.exported), len(result.skipped), len(result.failed))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jabberscribe.capture.webex", description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=Path("config/webex.yaml"))
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get(TOKEN_ENV)
    if not token:
        log.error("%s is not set", TOKEN_ENV)
        return 2
    try:
        cfg = load_webex_config(args.config)
    except WebexConfigError as exc:
        log.error("%s", exc)
        return 2

    client = WebexClient(cfg.base_url, token, timeout=cfg.timeout_seconds)
    ledger = Ledger(cfg.state_path)
    exporter = Exporter(cfg, client, ledger)
    try:
        if args.once:
            return 0 if _poll(exporter) else 1
        while True:
            _poll(exporter)
            time.sleep(cfg.poll_seconds)
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
