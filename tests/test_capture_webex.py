"""Webex Calling exporter tests. No network: every HTTP call goes through httpx.MockTransport."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jabberscribe.capture.webex import (
    MAX_WINDOW,
    Exporter,
    Ledger,
    Probe,
    WebexClient,
    WebexConfig,
    WebexConfigError,
    load_webex_config,
    to_sidecar,
)
from jabberscribe.config import Config
from jabberscribe.jobs import JobStore
from jabberscribe.sidecar import job_key, parse_sidecar
from jabberscribe.watcher import scan_once

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not installed"
)

BASE = "https://webexapis.test/v1"


def _client(handler) -> WebexClient:
    return WebexClient(BASE, "tok", transport=httpx.MockTransport(handler))


def _recording(rec_id: str = "rec-1", session: str = "sess-1", personality: str = "ORIGINATING", **extra) -> dict:
    """A list/details item shaped like the wxc_sdk ConvergedRecording model."""
    item = {
        "id": rec_id,
        "createTime": "2026-10-07T11:04:00.000Z",
        "timeRecorded": "2026-10-07T11:03:15.000Z",
        "format": "MP3",
        "serviceType": "calling",
        "durationSeconds": 42,
        "status": "available",
        "ownerId": "owner-1",
        "ownerEmail": "mhadad@example.co.il",
        "ownerType": "user",
        "serviceData": {
            "callSessionId": session,
            "callId": "callid-1",
            "personality": personality,
            "callingParty": {"name": "Meir Hadad", "number": "1042", "actor": {"id": "owner-1"}},
            "calledParty": {"name": "Dana", "number": "2210", "actor": {"id": "other"}},
            "session": {"startTime": "2026-10-07T11:03:11.000Z"},
        },
    }
    item.update(extra)
    return item


PROBE_STEREO = Probe(channels=2, sample_rate=8000, duration_sec=42.0)
PROBE_MONO = Probe(channels=1, sample_rate=8000, duration_sec=42.0)


# --- config -----------------------------------------------------------------


def test_config_loads_with_defaults(tmp_path: Path) -> None:
    path = tmp_path / "webex.yaml"
    path.write_text(f"inbox: {tmp_path / 'inbox'}\nstate_path: {tmp_path / 's.db'}\nwork_dir: {tmp_path / 'w'}\n")
    cfg = load_webex_config(path)
    assert cfg.base_url == "https://webexapis.com/v1"
    assert cfg.delete_after_export is False
    assert cfg.lookback_hours == 48


def test_config_rejects_unknown_keys_and_oversized_lookback(tmp_path: Path) -> None:
    base = {"inbox": tmp_path, "state_path": tmp_path / "s.db", "work_dir": tmp_path}
    with pytest.raises(ValueError):
        WebexConfig(**base, token="secret")
    with pytest.raises(ValueError):
        WebexConfig(**base, lookback_hours=24 * 31)


def test_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(WebexConfigError):
        load_webex_config(tmp_path / "nope.yaml")


# --- listing ----------------------------------------------------------------


def test_list_follows_link_next_and_sends_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "cursor" not in request.url.params:
            nxt = f"{BASE}/admin/convergedRecordings?cursor=2"
            return httpx.Response(200, json={"items": [{"id": "r1"}]}, headers={"Link": f'<{nxt}>; rel="next"'})
        return httpx.Response(200, json={"items": [{"id": "r2"}]})

    start = datetime(2026, 10, 1, tzinfo=UTC)
    items = list(_client(handler).list_recordings(start, start + timedelta(days=1)))
    assert [i["id"] for i in items] == ["r1", "r2"]
    assert len(seen) == 2
    assert all(r.headers["Authorization"] == "Bearer tok" for r in seen)
    first = parse_qs(urlparse(str(seen[0].url)).query)
    assert first["status"] == ["available"]
    assert first["from"] == ["2026-10-01T00:00:00.000Z"]
    assert first["to"] == ["2026-10-02T00:00:00.000Z"]


def test_list_splits_ranges_longer_than_the_window_cap() -> None:
    windows: list[tuple[datetime, datetime]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        lo = datetime.fromisoformat(params["from"])
        hi = datetime.fromisoformat(params["to"])
        windows.append((lo, hi))
        return httpx.Response(200, json={"items": []})

    start = datetime(2026, 8, 1, tzinfo=UTC)
    end = start + timedelta(days=45)
    list(_client(handler).list_recordings(start, end))
    assert len(windows) == 2
    assert all(hi - lo <= MAX_WINDOW for lo, hi in windows)
    assert windows[0][0] == start and windows[-1][1] == end
    assert windows[0][1] == windows[1][0]


def test_list_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "nope"})

    start = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(httpx.HTTPStatusError):
        list(_client(handler).list_recordings(start, start + timedelta(hours=1)))


# --- sidecar mapping --------------------------------------------------------


def test_call_maps_to_valid_v2_sidecar() -> None:
    data = to_sidecar(_recording(), {"ownerName": "מאיר חדד"}, PROBE_STEREO, shared_session=False)
    sidecar = parse_sidecar(json.dumps(data, ensure_ascii=False))
    assert data["schema_version"] == 2
    assert sidecar.call_id == "wxc-sess-1"
    assert sidecar.conference_id is None
    assert sidecar.kind == "call"
    assert sidecar.line_owner.extension == "1042"
    assert sidecar.line_owner.user == "mhadad"
    assert sidecar.line_owner.display_name == "מאיר חדד"
    assert [p.extension for p in sidecar.parties] == ["2210"]
    assert sidecar.tracks == "dual"
    assert data["audio"] == {"tracks": "dual", "sample_rate": 8000, "channels": 2}
    assert sidecar.duration_sec == 42
    assert sidecar.job_key == job_key("wxc-sess-1", "1042")


def test_started_at_carries_an_offset_and_is_the_session_start() -> None:
    data = to_sidecar(_recording(), {}, PROBE_MONO, shared_session=False)
    started = datetime.fromisoformat(data["started_at"])
    assert started.utcoffset() is not None
    assert started == datetime(2026, 10, 7, 11, 3, 11, tzinfo=UTC)
    assert datetime.fromisoformat(data["ended_at"]) - started == timedelta(seconds=42)
    assert data["audio"]["tracks"] == "mixed"


def test_terminating_leg_owner_is_the_called_party() -> None:
    data = to_sidecar(_recording(personality="TERMINATING"), {}, PROBE_MONO, shared_session=False)
    assert data["line_owner"]["extension"] == "2210"
    assert data["line_owner"]["display_name"] == "Dana"
    assert data["parties"] == [{"extension": "1042", "user": None, "display_name": "Meir Hadad"}]


def test_shared_session_becomes_a_conference() -> None:
    data = to_sidecar(_recording(), {}, PROBE_MONO, shared_session=True)
    sidecar = parse_sidecar(json.dumps(data))
    assert sidecar.kind == "conference"
    assert sidecar.conference_id == "wxc-sess-1"


def test_more_than_two_metadata_participants_is_a_conference() -> None:
    meta = {"serviceData": {"participants": [{"number": "1"}, {"number": "2"}, {"number": "3"}]}}
    data = to_sidecar(_recording(), meta, PROBE_MONO, shared_session=False)
    assert data["conference_id"] == "wxc-sess-1"


def test_missing_numbers_fall_back_to_email_and_ids() -> None:
    rec = _recording()
    rec["serviceData"] = {}
    data = to_sidecar(rec, {}, PROBE_MONO, shared_session=False)
    sidecar = parse_sidecar(json.dumps(data))
    assert sidecar.call_id == "wxc-rec-1"
    assert sidecar.line_owner.extension == "mhadad"
    # No session start: falls back to timeRecorded.
    assert datetime.fromisoformat(sidecar.started_at) == datetime(2026, 10, 7, 11, 3, 15, tzinfo=UTC)


# --- export -----------------------------------------------------------------

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
DOWNLOAD_HOST = "https://media.webex.test"


class FakeWebex:
    """Routes the Converged Recordings calls the exporter makes. Records every request."""

    def __init__(self, recordings: list[dict], audio: bytes) -> None:
        self.recordings = recordings
        self.audio = audio
        self.requests: list[httpx.Request] = []
        self.download_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "media.webex.test":
            return httpx.Response(self.download_status, content=self.audio if self.download_status == 200 else b"")
        if path.endswith("/admin/convergedRecordings"):
            return httpx.Response(200, json={"items": self.recordings})
        rec_id = path.split("/convergedRecordings/")[1].split("/")[0]
        if request.method == "DELETE":
            return httpx.Response(204)
        if path.endswith("/metadata"):
            return httpx.Response(200, json={"ownerName": "Owner " + rec_id})
        item = next(r for r in self.recordings if r["id"] == rec_id)
        links = {"audioDownloadLink": f"{DOWNLOAD_HOST}/{rec_id}.mp3", "expiration": "2026-10-07T15:00:00Z"}
        return httpx.Response(200, json={**item, "temporaryDirectDownloadLinks": links})

    def calls(self, method: str, fragment: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and fragment in str(r.url)]


@pytest.fixture
def mp3_bytes(tmp_path: Path, make_wav: Callable[..., Path]) -> Callable[[int], bytes]:
    def _make(channels: int) -> bytes:
        wav = make_wav(tmp_path / f"src{channels}.wav", channels=channels)
        mp3 = wav.with_suffix(".mp3")
        subprocess.run(["ffmpeg", "-y", "-nostdin", "-loglevel", "error", "-i", str(wav), str(mp3)], check=True)
        return mp3.read_bytes()

    return _make


@pytest.fixture
def wcfg(tmp_path: Path) -> WebexConfig:
    return WebexConfig(
        inbox=tmp_path / "drop" / "inbox",
        state_path=tmp_path / "webex-state.db",
        work_dir=tmp_path / "webex-work",
        base_url=BASE,
        max_attempts=2,
    )


def _exporter(cfg: WebexConfig, fake: FakeWebex) -> Exporter:
    client = WebexClient(cfg.base_url, "tok", transport=httpx.MockTransport(fake))
    return Exporter(cfg, client, Ledger(cfg.state_path), now=lambda: NOW)


def _inbox(cfg: WebexConfig) -> list[str]:
    return sorted(p.name for p in cfg.inbox.iterdir()) if cfg.inbox.exists() else []


@needs_ffmpeg
def test_export_writes_a_valid_drop_pair(wcfg: WebexConfig, mp3_bytes) -> None:
    fake = FakeWebex([_recording()], mp3_bytes(2))
    result = _exporter(wcfg, fake).run_once()

    key = job_key("wxc-sess-1", "1042")
    assert result.exported == (key,)
    assert _inbox(wcfg) == [f"{key}.json", f"{key}.wav"]
    sidecar = parse_sidecar((wcfg.inbox / f"{key}.json").read_text(encoding="utf-8"))
    assert sidecar.job_key == key
    assert sidecar.tracks == "dual"
    assert sidecar.line_owner.display_name == "Owner rec-1"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels", "-of", "json"]
        + [str(wcfg.inbox / f"{key}.wav")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(probe.stdout)["streams"][0] == {"codec_name": "pcm_s16le", "channels": 2}
    assert list(wcfg.work_dir.iterdir()) == []
    # The listing window is the configured lookback, ending now.
    listed = fake.calls("GET", "/admin/convergedRecordings")[0].url.params
    assert datetime.fromisoformat(listed["to"]) == NOW
    assert datetime.fromisoformat(listed["from"]) == NOW - timedelta(hours=wcfg.lookback_hours)


@needs_ffmpeg
def test_mono_recording_is_mixed(wcfg: WebexConfig, mp3_bytes) -> None:
    _exporter(wcfg, FakeWebex([_recording()], mp3_bytes(1))).run_once()
    key = job_key("wxc-sess-1", "1042")
    assert parse_sidecar((wcfg.inbox / f"{key}.json").read_text(encoding="utf-8")).tracks == "mixed"


@needs_ffmpeg
def test_second_poll_skips_exported_recordings(wcfg: WebexConfig, mp3_bytes) -> None:
    fake = FakeWebex([_recording()], mp3_bytes(1))
    _exporter(wcfg, fake).run_once()
    for path in wcfg.inbox.iterdir():
        path.unlink()  # the watcher consumed the pair

    second = _exporter(wcfg, fake).run_once()
    assert second.exported == ()
    assert second.skipped == ("rec-1",)
    assert _inbox(wcfg) == []
    assert len(fake.calls("GET", "/convergedRecordings/rec-1")) == 2  # details + metadata, first poll only


@needs_ffmpeg
def test_sidecar_is_renamed_into_place_last(wcfg: WebexConfig, mp3_bytes, monkeypatch) -> None:
    renamed: list[str] = []
    original = Path.replace

    def spy(self: Path, target):
        renamed.append(Path(target).name)
        return original(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    _exporter(wcfg, FakeWebex([_recording()], mp3_bytes(1))).run_once()

    key = job_key("wxc-sess-1", "1042")
    inbox_renames = [n for n in renamed if n.startswith(key)]
    assert inbox_renames[-1] == f"{key}.json"
    assert f"{key}.wav" in inbox_renames[:-1]
    assert not [n for n in _inbox(wcfg) if n.endswith(".part")]


@needs_ffmpeg
def test_two_legs_of_one_session_export_as_one_conference(wcfg: WebexConfig, mp3_bytes) -> None:
    legs = [_recording("rec-a", personality="ORIGINATING"), _recording("rec-b", personality="TERMINATING")]
    result = _exporter(wcfg, FakeWebex(legs, mp3_bytes(1))).run_once()

    assert len(result.exported) == 2
    sidecars = [parse_sidecar(p.read_text(encoding="utf-8")) for p in sorted(wcfg.inbox.glob("*.json"))]
    assert {s.conference_id for s in sidecars} == {"wxc-sess-1"}
    assert {s.kind for s in sidecars} == {"conference"}
    assert {s.line_owner.extension for s in sidecars} == {"1042", "2210"}


@needs_ffmpeg
def test_watcher_accepts_the_exported_pair(wcfg: WebexConfig, cfg: Config, mp3_bytes) -> None:
    wcfg = wcfg.model_copy(update={"inbox": cfg.paths.inbox})
    _exporter(wcfg, FakeWebex([_recording()], mp3_bytes(1))).run_once()
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    try:
        assert scan_once(cfg, store).enqueued == (job_key("wxc-sess-1", "1042"),)
    finally:
        store.close()


def test_rejected_download_leaves_no_partial_drop_and_parks_after_max_attempts(wcfg: WebexConfig) -> None:
    fake = FakeWebex([_recording()], b"")
    fake.download_status = 404  # e.g. the temporary link expired

    for _ in range(wcfg.max_attempts):
        assert _exporter(wcfg, fake).run_once().failed == ("rec-1",)
    assert _inbox(wcfg) == []

    parked = _exporter(wcfg, fake).run_once()
    assert parked.failed == () and parked.skipped == ("rec-1",)
    assert len(fake.calls("GET", "/convergedRecordings/rec-1")) == wcfg.max_attempts * 2


def test_server_error_aborts_the_poll_without_a_partial_drop(wcfg: WebexConfig) -> None:
    fake = FakeWebex([_recording()], b"")
    fake.download_status = 503
    with pytest.raises(httpx.HTTPStatusError):
        _exporter(wcfg, fake).run_once()
    assert _inbox(wcfg) == []
    # Transient: not counted against the recording, so it is retried in full next poll.
    assert Ledger(wcfg.state_path).attempts("rec-1") == 0


def test_download_does_not_send_the_token_to_another_host(wcfg: WebexConfig) -> None:
    fake = FakeWebex([_recording()], b"")
    fake.download_status = 404
    _exporter(wcfg, fake).run_once()
    download = next(r for r in fake.requests if r.url.host == "media.webex.test")
    assert "Authorization" not in download.headers


@needs_ffmpeg
def test_delete_is_off_by_default(wcfg: WebexConfig, mp3_bytes) -> None:
    fake = FakeWebex([_recording()], mp3_bytes(1))
    _exporter(wcfg, fake).run_once()
    assert fake.calls("DELETE", "") == []


@needs_ffmpeg
def test_delete_after_export_when_enabled(wcfg: WebexConfig, mp3_bytes) -> None:
    fake = FakeWebex([_recording()], mp3_bytes(1))
    _exporter(wcfg.model_copy(update={"delete_after_export": True}), fake).run_once()
    assert len(fake.calls("DELETE", "/convergedRecordings/rec-1")) == 1
