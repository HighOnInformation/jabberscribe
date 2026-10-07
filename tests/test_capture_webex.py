"""Webex Calling exporter tests. No network: every HTTP call goes through httpx.MockTransport."""

from __future__ import annotations

import json
import logging
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
    ExportError,
    Ledger,
    Probe,
    WebexClient,
    WebexConfig,
    WebexConfigError,
    _poll,
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


def test_example_config_is_valid() -> None:
    example = Path(__file__).resolve().parents[1] / "config" / "webex.yaml.example"
    assert load_webex_config(example).delete_after_export is False


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
    assert first["serviceType"] == ["calling"]
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
    data = to_sidecar(_recording(), {"ownerName": "מאיר חדד"}, PROBE_STEREO, owners=1)
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
    data = to_sidecar(_recording(), {}, PROBE_MONO, owners=1)
    started = datetime.fromisoformat(data["started_at"])
    assert started.utcoffset() is not None
    assert started == datetime(2026, 10, 7, 11, 3, 11, tzinfo=UTC)
    assert datetime.fromisoformat(data["ended_at"]) - started == timedelta(seconds=42)
    assert data["audio"]["tracks"] == "mixed"


def test_terminating_leg_owner_is_the_called_party() -> None:
    data = to_sidecar(_recording(personality="TERMINATING"), {}, PROBE_MONO, owners=1)
    assert data["line_owner"]["extension"] == "2210"
    assert data["line_owner"]["display_name"] == "Dana"
    assert data["parties"] == [{"extension": "1042", "user": None, "display_name": "Meir Hadad"}]


def test_more_than_two_owners_in_a_session_is_a_conference() -> None:
    data = to_sidecar(_recording(), {}, PROBE_MONO, owners=3)
    sidecar = parse_sidecar(json.dumps(data))
    assert sidecar.kind == "conference"
    assert sidecar.conference_id == "wxc-sess-1"


def test_two_owners_in_a_session_is_still_a_call() -> None:
    data = to_sidecar(_recording(), {}, PROBE_MONO, owners=2)
    assert data["kind"] == "call"
    assert data["conference_id"] is None


def test_party_fields_come_from_metadata_first_then_details() -> None:
    meta = {
        "serviceData": {
            "personality": "TERMINATING",
            "callingParty": {"name": "Meta Caller", "number": "3001"},
            "calledParty": {"name": "Meta Callee", "number": "3002"},
            "session": {"startTime": "2026-10-07T10:00:00.000Z"},
        }
    }
    data = to_sidecar(_recording(), meta, PROBE_MONO, owners=1)
    assert data["line_owner"]["extension"] == "3002"
    assert data["parties"][0]["extension"] == "3001"
    assert datetime.fromisoformat(data["started_at"]) == datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    # Without metadata the details/list item is the fallback.
    fallback = to_sidecar(_recording(), {}, PROBE_MONO, owners=1)
    assert fallback["line_owner"]["extension"] == "1042"


def test_more_than_two_metadata_participants_is_a_conference() -> None:
    meta = {"serviceData": {"participants": [{"number": "1"}, {"number": "2"}, {"number": "3"}]}}
    data = to_sidecar(_recording(), meta, PROBE_MONO, owners=1)
    assert data["conference_id"] == "wxc-sess-1"


def test_missing_numbers_fall_back_to_email_and_ids() -> None:
    rec = _recording()
    rec["serviceData"] = {}
    data = to_sidecar(rec, {}, PROBE_MONO, owners=1)
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
        self.download_status_by_id: dict[str, int] = {}
        self.metadata_extra: dict = {}
        self.redirect_downloads = False
        self.link_by_id: dict[str, str] = {}
        self.details_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.scheme not in ("http", "https"):  # what a real transport does
            raise httpx.UnsupportedProtocol(f"Request URL has an unsupported protocol '{request.url.scheme}://'.")
        path = request.url.path
        if request.url.host == "media.webex.test" and self.redirect_downloads:
            return httpx.Response(302, headers={"Location": f"https://storage.webex.test{path}"})
        if request.url.host in ("media.webex.test", "storage.webex.test"):
            status = self.download_status_by_id.get(path.strip("/").removesuffix(".mp3"), self.download_status)
            return httpx.Response(status, content=self.audio if status == 200 else b"")
        if path.endswith("/admin/convergedRecordings"):
            return httpx.Response(200, json={"items": self.recordings})
        rec_id = path.split("/convergedRecordings/")[1].split("/")[0]
        if request.method == "DELETE":
            return httpx.Response(204)
        if path.endswith("/metadata"):
            return httpx.Response(200, json={"ownerName": "Owner " + rec_id, **self.metadata_extra})
        if self.details_status != 200:
            return httpx.Response(self.details_status, json={})
        item = next(r for r in self.recordings if r["id"] == rec_id)
        link = self.link_by_id.get(rec_id, f"{DOWNLOAD_HOST}/{rec_id}.mp3")
        links = {"audioDownloadLink": link, "expiration": "2026-10-07T15:00:00Z"}
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
def test_two_legs_of_an_internal_call_stay_separate_calls(wcfg: WebexConfig, mp3_bytes) -> None:
    legs = [_recording("rec-a", personality="ORIGINATING"), _recording("rec-b", personality="TERMINATING")]
    result = _exporter(wcfg, FakeWebex(legs, mp3_bytes(1))).run_once()

    assert len(result.exported) == 2
    sidecars = [parse_sidecar(p.read_text(encoding="utf-8")) for p in sorted(wcfg.inbox.glob("*.json"))]
    assert {s.conference_id for s in sidecars} == {None}
    assert {s.kind for s in sidecars} == {"call"}
    assert {s.line_owner.extension for s in sidecars} == {"1042", "2210"}
    assert len({s.job_key for s in sidecars}) == 2


@needs_ffmpeg
def test_metadata_party_fields_win_over_details_in_an_export(wcfg: WebexConfig, mp3_bytes) -> None:
    fake = FakeWebex([_recording()], mp3_bytes(1))
    fake.metadata_extra = {"serviceData": {"callingParty": {"name": "M", "number": "3001"}}}
    _exporter(wcfg, fake).run_once()
    assert (wcfg.inbox / f"{job_key('wxc-sess-1', '3001')}.json").exists()


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


def test_repeated_server_error_counts_attempts_and_does_not_block_others(wcfg: WebexConfig) -> None:
    fake = FakeWebex([_recording("rec-bad", "s1"), _recording("rec-ok", "s2")], b"")
    fake.download_status = 404  # rec-ok fails too (cheaply), but only rec-bad is a 5xx
    fake.download_status_by_id["rec-bad"] = 503
    for _ in range(wcfg.max_attempts):
        assert _exporter(wcfg, fake).run_once().failed == ("rec-bad", "rec-ok")
    ledger = Ledger(wcfg.state_path)
    assert ledger.attempts("rec-bad") == wcfg.max_attempts
    # rec-ok was still attempted on every poll despite rec-bad failing first.
    assert ledger.attempts("rec-ok") == wcfg.max_attempts
    assert _exporter(wcfg, fake).run_once().skipped == ("rec-bad", "rec-ok")


@needs_ffmpeg
def test_malformed_recording_is_counted_not_fatal(wcfg: WebexConfig, mp3_bytes) -> None:
    bad = _recording("rec-bad", "s1", createTime="not-a-time", timeRecorded="also-bad")
    bad["serviceData"]["session"] = {}
    missing = _recording("rec-missing", "s2")
    del missing["createTime"], missing["timeRecorded"]
    missing["serviceData"]["session"] = {}
    fake = FakeWebex([bad, missing, _recording("rec-ok", "s3")], mp3_bytes(1))
    result = _exporter(wcfg, fake).run_once()
    assert set(result.failed) == {"rec-bad", "rec-missing"}
    assert Ledger(wcfg.state_path).attempts("rec-bad") == 1
    assert Ledger(wcfg.state_path).attempts("rec-missing") == 1
    assert len(result.exported) == 1


@needs_ffmpeg
@pytest.mark.parametrize("bad_link", ["http://x:abc/", "ftp://x"])
def test_malformed_download_link_is_counted_and_the_next_recording_still_exports(
    wcfg: WebexConfig, mp3_bytes, bad_link: str
) -> None:
    fake = FakeWebex([_recording("rec-bad", "s1"), _recording("rec-ok", "s2")], mp3_bytes(1))
    fake.link_by_id["rec-bad"] = bad_link
    result = _exporter(wcfg, fake).run_once()
    assert result.failed == ("rec-bad",)
    assert len(result.exported) == 1
    assert Ledger(wcfg.state_path).attempts("rec-bad") == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_on_details_stops_the_poll_without_counting(wcfg: WebexConfig, status: int, caplog) -> None:
    fake = FakeWebex([_recording("rec-1", "s1"), _recording("rec-2", "s2")], b"")
    fake.details_status = status
    with caplog.at_level(logging.ERROR), pytest.raises(httpx.HTTPStatusError):
        _exporter(wcfg, fake).run_once()
    assert Ledger(wcfg.state_path).attempts("rec-1") == 0
    assert Ledger(wcfg.state_path).attempts("rec-2") == 0
    assert str(status) in caplog.text and "http" not in caplog.text.lower().replace("http " + str(status), "")
    assert len(fake.calls("GET", "/convergedRecordings/rec-2")) == 0


def test_poll_loop_survives_unexpected_errors(caplog) -> None:
    class Boom:
        def run_once(self):
            raise RuntimeError("boom https://media.webex.test/secret.mp3")

    caplog.set_level(logging.INFO)
    assert _poll(Boom()) is False  # type: ignore[arg-type]
    assert "secret.mp3" not in caplog.text


@needs_ffmpeg
def test_download_follows_a_redirect_without_leaking_the_token(wcfg: WebexConfig, mp3_bytes) -> None:
    fake = FakeWebex([_recording()], mp3_bytes(1))
    fake.redirect_downloads = True
    assert len(_exporter(wcfg, fake).run_once().exported) == 1
    hops = [r for r in fake.requests if r.url.host in ("media.webex.test", "storage.webex.test")]
    assert [r.url.host for r in hops] == ["media.webex.test", "storage.webex.test"]
    assert all("Authorization" not in r.headers for r in hops)


def test_failed_download_removes_the_part_file(tmp_path: Path) -> None:
    dest = tmp_path / "a.mp3"
    (tmp_path / "a.mp3.part").write_bytes(b"x")  # a stale part from a dead earlier try
    with pytest.raises(httpx.HTTPStatusError):
        _client(lambda request: httpx.Response(404)).download("https://media.webex.test/a.mp3", dest)
    assert list(tmp_path.iterdir()) == []

    with pytest.raises(ExportError):
        _client(lambda request: httpx.Response(200, content=b"")).download("https://media.webex.test/a.mp3", dest)
    assert list(tmp_path.iterdir()) == []


def test_export_logs_never_contain_download_urls_or_the_token(
    wcfg: WebexConfig, tmp_path: Path, monkeypatch, caplog
) -> None:
    import jabberscribe.capture.webex as webex

    fake = FakeWebex([_recording()], b"")
    fake.download_status = 404
    cfg_path = tmp_path / "webex.yaml"
    cfg_path.write_text(
        f"inbox: {wcfg.inbox}\nstate_path: {wcfg.state_path}\nwork_dir: {wcfg.work_dir}\nbase_url: {BASE}\n"
    )
    monkeypatch.setenv(webex.TOKEN_ENV, "super-secret-token")
    monkeypatch.setattr(
        webex, "WebexClient", lambda base, token, **kw: WebexClient(base, token, transport=httpx.MockTransport(fake))
    )
    saved = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    try:
        with caplog.at_level(logging.INFO):
            webex.main(["--config", str(cfg_path), "--once"])
    finally:
        for n, level in saved.items():
            logging.getLogger(n).setLevel(level)
    assert fake.calls("GET", "media.webex.test")  # the download really happened
    assert "media.webex.test" not in caplog.text
    assert "rec-1.mp3" not in caplog.text
    assert "super-secret-token" not in caplog.text


@needs_ffmpeg
def test_crash_between_pair_write_and_ledger_mark_is_a_watcher_duplicate(
    wcfg: WebexConfig, cfg: Config, mp3_bytes, monkeypatch
) -> None:
    wcfg = wcfg.model_copy(update={"inbox": cfg.paths.inbox})
    fake = FakeWebex([_recording()], mp3_bytes(1))

    def crash(self, recording_id: str, key: str) -> None:
        raise RuntimeError("crash before ledger mark")

    with monkeypatch.context() as patch:
        patch.setattr(Ledger, "mark_done", crash)
        with pytest.raises(RuntimeError):
            _exporter(wcfg, fake).run_once()

    key = job_key("wxc-sess-1", "1042")
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    try:
        assert scan_once(cfg, store).enqueued == (key,)  # the watcher took the first pair
        again = _exporter(wcfg, fake).run_once()
        assert again.exported == (key,)  # same key after the re-export
        assert scan_once(cfg, store).skipped == (key,)  # discarded as a duplicate
        assert _inbox(wcfg) == []
    finally:
        store.close()


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


# --- CLI --------------------------------------------------------------------


def test_cli_refuses_to_start_without_a_token(tmp_path: Path, monkeypatch, caplog) -> None:
    from jabberscribe.capture.webex import TOKEN_ENV, main

    path = tmp_path / "webex.yaml"
    path.write_text(f"inbox: {tmp_path / 'i'}\nstate_path: {tmp_path / 's.db'}\nwork_dir: {tmp_path / 'w'}\n")
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    assert main(["--config", str(path), "--once"]) == 2
    assert TOKEN_ENV in caplog.text


def test_cli_rejects_a_bad_config(tmp_path: Path, monkeypatch) -> None:
    from jabberscribe.capture.webex import TOKEN_ENV, main

    monkeypatch.setenv(TOKEN_ENV, "tok")
    assert main(["--config", str(tmp_path / "missing.yaml"), "--once"]) == 2
