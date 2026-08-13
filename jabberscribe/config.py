"""Typed configuration loaded from YAML.

Secrets never live here -- they come from environment variables in the modules
that need them. This file is safe to commit and safe to log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from jabberscribe.jobs import STAGE_ORDER


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


class PipelineConfig(_Strict):
    """Which stages this deployment runs, in order.

    This is the activation switch for the whole system. The core stages ship
    first; each outer stage becomes available by adding its name here once it
    exists. `doctor` reports any stage that is enabled but not yet implemented,
    so turning one on early tells you so instead of failing mid-call.
    """

    stages: tuple[str, ...] = ("audio", "stt")

    @field_validator("stages")
    @classmethod
    def _validate_stages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("pipeline.stages must list at least one stage")
        unknown = [s for s in value if s not in STAGE_ORDER]
        if unknown:
            raise ValueError(f"pipeline.stages contains unknown stage(s) {unknown}; valid stages are {STAGE_ORDER}")
        if len(set(value)) != len(value):
            raise ValueError(f"pipeline.stages contains duplicates: {value}")
        positions = [STAGE_ORDER.index(s) for s in value]
        if positions != sorted(positions):
            raise ValueError(f"pipeline.stages must follow the order {STAGE_ORDER}, got {value}")
        return value


class ConfluenceConfig(_Strict):
    base_url: str
    space_key: str
    parent_page_id: str
    compliance_group: str | None = None
    # Attaching voice recordings to a wiki multiplies the exposure surface for no
    # reading benefit, so the page links the audio instead by default.
    attach_audio: bool = False


class MailConfig(_Strict):
    smtp_host: str
    smtp_port: int = 25
    use_tls: bool = False
    from_address: str
    compliance_bcc: str | None = None
    ops_alert_to: str | None = None
    #: Where a transcript goes when no participant email could be resolved.
    fallback_to: str


class RetentionConfig(_Strict):
    audio_days: int = 90
    page_days: int = 365


class Config(_Strict):
    paths: PathsConfig
    watcher: WatcherConfig = WatcherConfig()
    stt: SttConfig = SttConfig()
    pipeline: PipelineConfig = PipelineConfig()
    confluence: ConfluenceConfig | None = None
    mail: MailConfig | None = None
    retention: RetentionConfig = RetentionConfig()

    @model_validator(mode="after")
    def _require_config_for_enabled_stages(self) -> Config:
        """A stage cannot be enabled without the config it needs.

        Catching this at load beats discovering it when the first real call
        reaches the publish stage.
        """
        if "publish" in self.pipeline.stages and self.confluence is None:
            raise ValueError("pipeline.stages enables 'publish' but no confluence: section is configured")
        if "notify" in self.pipeline.stages and self.mail is None:
            raise ValueError("pipeline.stages enables 'notify' but no mail: section is configured")
        return self


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
