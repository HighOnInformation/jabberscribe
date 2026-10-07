"""Typed configuration loaded from YAML.

Secrets never live here -- they come from environment variables in the modules
that need them. This file is safe to commit and safe to log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(Exception):
    """Configuration is missing, unreadable, or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Strict):
    drop_root: Path
    work_dir: Path
    #: Per-call output folders: out_root/<YYYY>/<MM>/<job_key>/.
    out_root: Path
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


class GroupConfig(_Strict):
    #: A conference is processed once no new copy has arrived for this long...
    settle_seconds: int = 60
    #: ...or once its first copy has waited this long, to hold the latency budget.
    max_wait_seconds: int = 300
    #: Copies whose time spans come within this many seconds of each other belong to one meeting.
    #: Kept small: a reused conference_id booked back to back must not merge two meetings.
    overlap_slack_seconds: int = 5


class LiteLLMConfig(_Strict):
    base_url: str
    timeout_seconds: float = 600.0


class SttConfig(_Strict):
    #: The model_name LiteLLM serves for ivrit.ai Whisper.
    model: str
    #: A relative path is relative to the config file, not the working directory.
    vocabulary_file: Path | None = None
    #: Dual-track calls: transcribe each channel on its own and label who spoke (two STT calls per call).
    split_channels: bool = True
    #: The channel of a dual-track recording that carries the recorded line (the near end).
    near_channel: Literal[0, 1] = 0


class SummaryConfig(_Strict):
    #: The model_name LiteLLM serves for Gemma.
    model: str
    #: Longer transcripts are summarized in chunks of about this many characters, then merged.
    max_chunk_chars: int = 12000


class CuesConfig(_Strict):
    #: Tag non-speech events ([צחוק], [מוזיקה], ...) into a separate layer. Needs `pip install .[cues]`.
    enabled: bool = True
    #: The PANNs Cnn14 checkpoint (Cnn14_mAP=0.431.pth). Relative paths are relative to the config file.
    model_path: Path | None = None
    #: A label is cued in a window when one of its AudioSet classes scores at least this.
    threshold: float = Field(default=0.3, gt=0, lt=1)


class RetentionConfig(_Strict):
    audio_days: int = 90
    text_days: int = 365


class Config(_Strict):
    paths: PathsConfig
    watcher: WatcherConfig = WatcherConfig()
    group: GroupConfig = GroupConfig()
    litellm: LiteLLMConfig
    stt: SttConfig
    summary: SummaryConfig
    retention: RetentionConfig = RetentionConfig()
    cues: CuesConfig = CuesConfig()


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
        cfg = Config(**raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}: {exc}") from exc
    # A Windows service runs in C:\Windows\System32; resolving against the config file keeps the glossary found.
    vocabulary = cfg.stt.vocabulary_file
    if vocabulary is not None and not vocabulary.is_absolute():
        cfg.stt.vocabulary_file = path.parent / vocabulary
    model = cfg.cues.model_path
    if model is not None and not model.is_absolute():
        cfg.cues.model_path = path.parent / model
    return cfg
