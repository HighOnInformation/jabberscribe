from pathlib import Path

import pytest

from jabberscribe.config import ConfigError, load_config

MINIMAL_YAML = """
paths:
  drop_root: D:/js/drop
  work_dir: D:/js/work
  audio_store: D:/js/audio
  db_path: D:/js/jabberscribe.db
"""


def test_load_config_applies_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(MINIMAL_YAML, encoding="utf-8")

    cfg = load_config(cfg_file)

    assert cfg.paths.drop_root == Path("D:/js/drop")
    assert cfg.paths.inbox == Path("D:/js/drop/inbox")
    assert cfg.paths.quarantine == Path("D:/js/drop/quarantine")
    assert cfg.watcher.poll_seconds == 30
    assert cfg.watcher.min_age_seconds == 15
    assert cfg.stt.model == "ivrit-ai/whisper-large-v3-turbo-ct2"
    assert cfg.stt.compute_type == "int8"
    assert cfg.stt.device == "auto"


def test_load_config_overrides_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        MINIMAL_YAML + "\nwatcher:\n  min_age_seconds: 5\nstt:\n  device: cuda\n",
        encoding="utf-8",
    )

    cfg = load_config(cfg_file)

    assert cfg.watcher.min_age_seconds == 5
    assert cfg.stt.device == "cuda"


def test_load_config_rejects_missing_paths_section(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("watcher:\n  poll_seconds: 10\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="paths"):
        load_config(cfg_file)


def test_load_config_rejects_unknown_device(tmp_path: Path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(MINIMAL_YAML + "\nstt:\n  device: tpu\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="device"):
        load_config(cfg_file)


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def _write(tmp_path: Path, extra: str) -> Path:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(MINIMAL_YAML + extra, encoding="utf-8")
    return cfg_file


def test_pipeline_defaults_to_core_stages(tmp_path: Path) -> None:
    assert load_config(_write(tmp_path, "")).pipeline.stages == ("audio", "stt")


def test_pipeline_stages_can_be_extended(tmp_path: Path) -> None:
    """`render` needs no config section, so it extends the list on its own."""
    cfg = load_config(_write(tmp_path, "\npipeline:\n  stages: [audio, stt, render]\n"))

    assert cfg.pipeline.stages == ("audio", "stt", "render")


def test_pipeline_rejects_unknown_stage(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown stage"):
        load_config(_write(tmp_path, "\npipeline:\n  stages: [audio, telepathy]\n"))


def test_pipeline_rejects_out_of_order_stages(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="order"):
        load_config(_write(tmp_path, "\npipeline:\n  stages: [publish, audio]\n"))


def test_pipeline_rejects_duplicate_stages(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="duplicates"):
        load_config(_write(tmp_path, "\npipeline:\n  stages: [audio, audio, stt]\n"))


def test_pipeline_rejects_empty_stage_list(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="at least one"):
        load_config(_write(tmp_path, "\npipeline:\n  stages: []\n"))


CONFLUENCE_YAML = """
confluence:
  base_url: https://wiki.corp.local
  space_key: CALLS
  parent_page_id: "123456"
  compliance_group: callrec-compliance
"""

MAIL_YAML = """
mail:
  smtp_host: smtp.corp.local
  from_address: jabberscribe@corp.local
  fallback_to: it-ops@corp.local
"""


def test_delivery_config_is_absent_by_default(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))

    assert cfg.confluence is None
    assert cfg.mail is None
    assert cfg.retention.audio_days == 90
    assert cfg.retention.page_days == 365


def test_confluence_config_parses(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, CONFLUENCE_YAML))

    assert cfg.confluence.base_url == "https://wiki.corp.local"
    assert cfg.confluence.space_key == "CALLS"
    assert cfg.confluence.parent_page_id == "123456"
    assert cfg.confluence.attach_audio is False


def test_publish_stage_without_confluence_config_is_rejected(tmp_path: Path) -> None:
    extra = "\npipeline:\n  stages: [audio, stt, render, publish]\n"

    with pytest.raises(ConfigError, match="confluence"):
        load_config(_write(tmp_path, extra))


def test_notify_stage_without_mail_config_is_rejected(tmp_path: Path) -> None:
    extra = "\npipeline:\n  stages: [audio, stt, render, publish, notify]\n" + CONFLUENCE_YAML

    with pytest.raises(ConfigError, match="mail"):
        load_config(_write(tmp_path, extra))


def test_full_delivery_pipeline_config_is_accepted(tmp_path: Path) -> None:
    extra = "\npipeline:\n  stages: [audio, stt, render, publish, notify]\n" + CONFLUENCE_YAML + MAIL_YAML

    cfg = load_config(_write(tmp_path, extra))

    assert cfg.pipeline.stages == ("audio", "stt", "render", "publish", "notify")
    assert cfg.mail.smtp_port == 25
