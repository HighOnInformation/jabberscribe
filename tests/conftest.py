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

from jabberscribe.config import Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config = Config(
        paths={
            "drop_root": tmp_path / "drop",
            "work_dir": tmp_path / "work",
            "audio_store": tmp_path / "audio",
            "db_path": tmp_path / "js.db",
        },
        watcher={"min_age_seconds": 0},
    )
    config.paths.inbox.mkdir(parents=True)
    config.paths.quarantine.mkdir(parents=True)
    config.paths.work_dir.mkdir(parents=True)
    return config


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
                value = int(12000 * math.sin(2 * math.pi * freq * i / rate))
                for channel in range(channels):
                    # Right channel gets an inverted tone so channel-split tests
                    # can prove the channels did not get swapped or duplicated.
                    scale = 1 if channel == 0 else -1
                    samples += struct.pack("<h", value * scale)
            out.writeframes(bytes(samples))
        return path

    return _make


@pytest.fixture
def make_sidecar() -> Callable[..., Path]:
    def _make(path: Path, *, call_id: str = "c1", tracks: str = "mixed", **extra: object) -> Path:
        payload: dict[str, object] = {
            "call_id": call_id,
            "source": "endpoint-agent",
            "kind": "call",
            "started_at": "2026-08-12T14:03:11+03:00",
            "duration_sec": 12,
            "participants": [{"display_name": "מאיר", "email": "meir@corp.local", "role": "caller"}],
            "audio": {"tracks": tracks, "sample_rate": 8000, "channels": 2 if tracks == "dual" else 1},
        }
        payload.update(extra)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    return _make
