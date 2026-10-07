"""Webex Calling exporter tests. No network: every HTTP call goes through httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jabberscribe.capture.webex import (
    MAX_WINDOW,
    Probe,
    WebexClient,
    WebexConfig,
    WebexConfigError,
    load_webex_config,
    to_sidecar,
)
from jabberscribe.sidecar import job_key, parse_sidecar

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
