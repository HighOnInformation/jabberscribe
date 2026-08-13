import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audit import PURGED_AUDIO, PURGED_PAGE, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import JobStore
from jabberscribe.retention import purge

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


class FakeConfluence:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_page(self, page_id: str) -> None:
        self.deleted.append(page_id)


def _cfg(tmp_path: Path, audio_days: int = 90, page_days: int = 365) -> Config:
    return Config(
        paths={
            "drop_root": tmp_path / "drop",
            "work_dir": tmp_path / "work",
            "audio_store": tmp_path / "audio",
            "db_path": tmp_path / "js.db",
        },
        retention={"audio_days": audio_days, "page_days": page_days},
    )


def _job(cfg, store, call_id: str, age_days: int, page_id: str | None = None) -> Path:
    started = (NOW - timedelta(days=age_days)).isoformat()
    audio = cfg.paths.audio_store / f"{call_id}.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFFfake")
    store.create(
        call_id=call_id,
        audio_path=audio,
        sidecar_json=json.dumps({"call_id": call_id}),
        kind="call",
        started_at=started,
        duration_sec=10,
    )
    if page_id:
        store.set_page_id(call_id, page_id)
    return audio


def _fixture(tmp_path: Path, **kw):
    cfg = _cfg(tmp_path, **kw)
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path, actor="svc")
    audit.init_schema()
    return cfg, store, audit


def test_audio_older_than_retention_is_deleted(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path)
    old = _job(cfg, store, "old", age_days=100)
    fresh = _job(cfg, store, "fresh", age_days=10)

    result = purge(cfg, store, audit, now=NOW)

    assert result.audio_deleted == ("old",)
    assert not old.exists()
    assert fresh.exists()
    assert [e.action for e in audit.entries("old")] == [PURGED_AUDIO]


def test_audio_exactly_at_the_boundary_is_kept(tmp_path: Path) -> None:
    """Retention is 'older than N days', so day N itself survives."""
    cfg, store, audit = _fixture(tmp_path, audio_days=90)
    boundary = _job(cfg, store, "boundary", age_days=90)

    result = purge(cfg, store, audit, now=NOW)

    assert result.audio_deleted == ()
    assert boundary.exists()


def test_pages_older_than_retention_are_deleted(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path, page_days=365)
    _job(cfg, store, "ancient", age_days=400, page_id="900")
    _job(cfg, store, "recent", age_days=10, page_id="901")
    client = FakeConfluence()

    result = purge(cfg, store, audit, now=NOW, confluence=client)

    assert result.pages_deleted == ("ancient",)
    assert client.deleted == ["900"]
    assert PURGED_PAGE in [e.action for e in audit.entries("ancient")]


def test_pages_are_left_alone_without_a_client(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path, page_days=365)
    _job(cfg, store, "ancient", age_days=400, page_id="900")

    result = purge(cfg, store, audit, now=NOW, confluence=None)

    assert result.pages_deleted == ()


def test_already_deleted_audio_is_not_an_error(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path)
    audio = _job(cfg, store, "gone", age_days=100)
    audio.unlink()

    result = purge(cfg, store, audit, now=NOW)

    assert result.errors == ()
    assert result.audio_deleted == ()


def test_a_failing_page_delete_is_reported_not_raised(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path, page_days=365)
    _job(cfg, store, "ancient", age_days=400, page_id="900")

    class Failing:
        def delete_page(self, page_id: str) -> None:
            raise RuntimeError("wiki says no")

    result = purge(cfg, store, audit, now=NOW, confluence=Failing())

    assert result.pages_deleted == ()
    assert any("ancient" in e for e in result.errors)


def test_unparseable_started_at_is_skipped_safely(tmp_path: Path) -> None:
    """Never delete on the basis of a date we could not read."""
    cfg, store, audit = _fixture(tmp_path)
    audio = cfg.paths.audio_store / "weird.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF")
    store.create(
        call_id="weird",
        audio_path=audio,
        sidecar_json="{}",
        kind="call",
        started_at="not-a-date",
        duration_sec=1,
    )

    result = purge(cfg, store, audit, now=NOW)

    assert result.audio_deleted == ()
    assert audio.exists()
    assert any("weird" in e for e in result.errors)
