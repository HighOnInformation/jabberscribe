"""Typed configuration loaded from YAML.

Secrets never live here -- they come from environment variables in the modules
that need them. This file is safe to commit and safe to log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


class ConfigError(Exception):
    """Configuration is missing, unreadable, or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Strict):
    drop_root: Path
    work_dir: Path
    audio_store: Path
    db_path: Path

    @property
    def inbox(self) -> Path:
        return self.drop_root / "inbox"

    @property
    def quarantine(self) -> Path:
        return self.drop_root / "quarantine"


class WatcherConfig(_Strict):
    poll_seconds: int = 30
    min_age_seconds: int = 15


class SttConfig(_Strict):
    model: str = "ivrit-ai/whisper-large-v3-turbo-ct2"
    compute_type: str = "int8"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    vocabulary_file: Path | None = None


class Config(_Strict):
    paths: PathsConfig
    watcher: WatcherConfig = WatcherConfig()
    stt: SttConfig = SttConfig()


def load_config(path: Path) -> Config:
    """Read and validate the YAML config at `path`.

    Raises ConfigError for every failure mode so callers never have to catch
    pydantic or yaml exceptions.
    """
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file is not valid YAML: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config file must contain a mapping at the top level: {path}")
    try:
        return Config(**raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}: {exc}") from exc
