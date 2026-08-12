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
    cfg = load_config(_write(tmp_path, "\npipeline:\n  stages: [audio, stt, render, publish]\n"))

    assert cfg.pipeline.stages == ("audio", "stt", "render", "publish")


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
