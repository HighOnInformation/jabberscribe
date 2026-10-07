"""Shared fixtures.

`make_wav` writes real RIFF files with the stdlib so audio tests need no
binary fixtures in git and no numpy.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from collections.abc import Callable
from pathlib import Path

import pytest

from jabberscribe.audit import AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import JobStore


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(
        paths={
            "drop_root": tmp_path / "drop",
            "work_dir": tmp_path / "work",
            "out_root": tmp_path / "out",
            "db_path": tmp_path / "js.db",
        },
        watcher={"min_age_seconds": 0},
        litellm={"base_url": "http://litellm.test"},
        stt={"model": "whisper-he"},
        summary={"model": "gemma-3"},
    )
    config.paths.inbox.mkdir(parents=True)
    config.paths.quarantine.mkdir(parents=True)
    config.paths.work_dir.mkdir(parents=True)
    return config


@pytest.fixture
def store(cfg: Config) -> JobStore:
    job_store = JobStore(cfg.paths.db_path)
    job_store.init_schema()
    return job_store


@pytest.fixture
def audit(cfg: Config) -> AuditLog:
    log = AuditLog(cfg.paths.db_path, actor="test")
    log.init_schema()
    return log


@pytest.fixture
def make_wav() -> Callable[..., Path]:
    def _make(
        path: Path,
        *,
        seconds: float = 1.0,
        rate: int = 8000,
        channels: int = 1,
        freq: float = 440.0,
    ) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        frames = int(seconds * rate)
        with wave.open(str(path), "wb") as out:
            out.setnchannels(channels)
            out.setsampwidth(2)
            out.setframerate(rate)
            samples = bytearray()
            for i in range(frames):
                for channel in range(channels):
                    # Each channel gets its own pitch (440, 660, ... Hz). An inverted
                    # copy would cancel to digital silence in a mono downmix.
                    pitch = freq * (1 + channel / 2)
                    samples += struct.pack("<h", int(12000 * math.sin(2 * math.pi * pitch * i / rate)))
            out.writeframes(bytes(samples))
        return path

    return _make


@pytest.fixture
def make_sidecar() -> Callable[..., Path]:
    def _make(
        path: Path,
        *,
        call_id: str = "c1",
        extension: str = "1042",
        conference_id: str | None = None,
        tracks: str = "mixed",
        **extra: object,
    ) -> Path:
        payload: dict[str, object] = {
            "schema_version": 2,
            "call_id": call_id,
            "conference_id": conference_id,
            "line_owner": {"extension": extension, "user": "meir", "display_name": "מאיר"},
            "parties": [{"extension": "2210", "display_name": "דנה"}],
            "kind": "conference" if conference_id else "call",
            "started_at": "2026-10-07T14:03:11+03:00",
            "duration_sec": 12,
            "audio": {"tracks": tracks, "sample_rate": 8000, "channels": 2 if tracks == "dual" else 1},
        }
        payload.update(extra)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    return _make
