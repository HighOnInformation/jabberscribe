import json
from pathlib import Path

import httpx
import pytest

from jabberscribe.audit import PUBLISHED, UPDATED, AuditLog
from jabberscribe.confluence import ConfluenceClient, ConfluenceError
from jabberscribe.jobs import JobStore
from jabberscribe.publish import publish_call
from jabberscribe.render import render_call
from jabberscribe.sidecar import parse_sidecar

SIDECAR = json.dumps(
    {
        "call_id": "pub1",
        "kind": "call",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 60,
        "participants": [
            {"display_name": "מאיר", "email": "meir@corp.local", "uri": "mhadad@corp.local", "role": "caller"}
        ],
        "audio": {"tracks": "mixed"},
    }
)


class FakeConfluence:
    """Records every call so tests can assert on the REST conversation."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.restrictions: list[dict] = []
        self.next_id = "998"

    def create_page(self, space_key, parent_id, title, body) -> str:
        self.created.append({"space": space_key, "parent": parent_id, "title": title, "body": body})
        return self.next_id

    def update_page(self, page_id, title, body) -> None:
        self.updated.append({"id": page_id, "title": title, "body": body})

    def set_read_restrictions(self, page_id, usernames, group) -> None:
        self.restrictions.append({"id": page_id, "users": usernames, "group": group})

    def page_url(self, page_id) -> str:
        return f"https://wiki.corp.local/pages/viewpage.action?pageId={page_id}"


def _cfg(tmp_path: Path):
    from jabberscribe.config import Config

    return Config(
        paths={
            "drop_root": tmp_path / "drop",
            "work_dir": tmp_path / "work",
            "audio_store": tmp_path / "audio",
            "db_path": tmp_path / "js.db",
        },
        pipeline={"stages": ("audio", "stt", "render", "publish")},
        confluence={
            "base_url": "https://wiki.corp.local",
            "space_key": "CALLS",
            "parent_page_id": "123",
            "compliance_group": "callrec-compliance",
        },
    )


def _fixture(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path, actor="svc")
    audit.init_schema()
    store.create(
        call_id="pub1",
        audio_path=cfg.paths.audio_store / "pub1.wav",
        sidecar_json=SIDECAR,
        kind="call",
        started_at="2026-08-12T14:03:11+03:00",
        duration_sec=60,
    )
    rendered = render_call(parse_sidecar(SIDECAR), [])
    return cfg, store, audit, store.get("pub1"), rendered


def test_publish_creates_page_and_stores_id(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    client = FakeConfluence()

    page_id = publish_call(job, cfg, store, audit, client, rendered)

    assert page_id == "998"
    assert len(client.created) == 1
    assert client.created[0]["space"] == "CALLS"
    assert client.created[0]["parent"] == "123"
    assert store.get("pub1").confluence_page_id == "998"
    assert [e.action for e in audit.entries("pub1")] == [PUBLISHED]


def test_publish_applies_read_restrictions(tmp_path: Path) -> None:
    """Default-closed: a page nobody was granted stays invisible."""
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    client = FakeConfluence()

    publish_call(job, cfg, store, audit, client, rendered)

    assert client.restrictions[0]["group"] == "callrec-compliance"
    assert "mhadad@corp.local" in client.restrictions[0]["users"]


def test_republish_updates_the_same_page(tmp_path: Path) -> None:
    """Idempotency: a retry must never create a second page."""
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    client = FakeConfluence()
    publish_call(job, cfg, store, audit, client, rendered)

    refreshed = store.get("pub1")
    page_id = publish_call(refreshed, cfg, store, audit, client, rendered)

    assert page_id == "998"
    assert len(client.created) == 1
    assert len(client.updated) == 1
    assert [e.action for e in audit.entries("pub1")] == [PUBLISHED, UPDATED]


def test_publish_records_page_id_even_if_restrictions_fail(tmp_path: Path) -> None:
    """A stored page id we cannot restrict is still a page we must not duplicate."""
    cfg, store, audit, job, rendered = _fixture(tmp_path)

    class Failing(FakeConfluence):
        def set_read_restrictions(self, page_id, usernames, group) -> None:
            raise ConfluenceError("restriction API refused")

    with pytest.raises(ConfluenceError):
        publish_call(job, cfg, store, audit, Failing(), rendered)

    assert store.get("pub1").confluence_page_id == "998"


def test_attach_audio_note_is_absent_by_default(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    client = FakeConfluence()

    publish_call(job, cfg, store, audit, client, rendered)

    assert "pub1.wav" not in client.created[0]["body"]


# --- ConfluenceClient wire-level tests, against a mock transport (no network) ---


def _client(handler) -> ConfluenceClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://wiki.corp.local")
    return ConfluenceClient("https://wiki.corp.local", "PAT123", http=http)


def test_client_create_page_posts_storage_format() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "555"})

    page_id = _client(handler).create_page("CALLS", "123", "T", "<p>hi</p>")

    assert page_id == "555"
    assert seen["url"].endswith("/rest/api/content")
    assert seen["auth"] == "Bearer PAT123"
    assert seen["body"]["body"]["storage"]["representation"] == "storage"
    assert seen["body"]["ancestors"] == [{"id": "123"}]


def test_client_update_page_increments_version() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"id": "555", "version": {"number": 4}})
        return httpx.Response(200, json={"id": "555"})

    _client(handler).update_page("555", "T", "<p>hi</p>")

    assert json.loads(calls[-1].content)["version"] == {"number": 5}


def test_client_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(ConfluenceError, match="403"):
        _client(handler).create_page("CALLS", "123", "T", "<p>x</p>")


def test_client_page_url_is_stable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={})

    assert _client(handler).page_url("998").endswith("pageId=998")
