"""Webex Calling exporter tests. No network: every HTTP call goes through httpx.MockTransport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from jabberscribe.capture.webex import (
    MAX_WINDOW,
    WebexClient,
    WebexConfig,
    WebexConfigError,
    load_webex_config,
)

BASE = "https://webexapis.test/v1"


def _client(handler) -> WebexClient:
    return WebexClient(BASE, "tok", transport=httpx.MockTransport(handler))


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
