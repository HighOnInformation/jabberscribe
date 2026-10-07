import copy
from pathlib import Path

import pytest
import yaml

from jabberscribe.config import ConfigError, load_config

BASE: dict = {
    "paths": {
        "drop_root": "D:/js/drop",
        "work_dir": "D:/js/work",
        "out_root": "D:/js/out",
        "db_path": "D:/js/js.db",
    },
    "litellm": {"base_url": "http://litellm.corp.local:4000"},
    "stt": {"model": "whisper-he"},
    "summary": {"model": "gemma-3"},
}


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_minimal_config_loads_with_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, BASE))

    assert cfg.paths.inbox == Path("D:/js/drop/inbox")
    assert cfg.paths.quarantine == Path("D:/js/drop/quarantine")
    assert cfg.paths.out_root == Path("D:/js/out")
    assert cfg.watcher.min_age_seconds == 15
    assert (cfg.group.settle_seconds, cfg.group.max_wait_seconds, cfg.group.overlap_slack_seconds) == (60, 300, 5)
    assert cfg.litellm.timeout_seconds == 600
    assert cfg.stt.model == "whisper-he"
    assert cfg.stt.vocabulary_file is None
    assert cfg.summary.model == "gemma-3"
    assert cfg.summary.max_chunk_chars == 12000
    assert (cfg.retention.audio_days, cfg.retention.text_days) == (90, 365)


@pytest.mark.parametrize("section", ["litellm", "stt", "summary"])
def test_required_sections(tmp_path: Path, section: str) -> None:
    data = copy.deepcopy(BASE)
    del data[section]

    with pytest.raises(ConfigError, match=section):
        load_config(_write(tmp_path, data))


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["paths"]["audio_store"] = "D:/js/audio"

    with pytest.raises(ConfigError, match="audio_store"):
        load_config(_write(tmp_path, data))


def test_v1_delivery_sections_are_rejected(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["confluence"] = {"base_url": "https://wiki"}

    with pytest.raises(ConfigError, match="confluence"):
        load_config(_write(tmp_path, data))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("paths: [unclosed", encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mapping"):
        load_config(_write(tmp_path, ["a", "b"]))


def test_relative_vocabulary_file_is_relative_to_the_config_file(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["stt"]["vocabulary_file"] = "custom_vocabulary.txt"

    cfg = load_config(_write(tmp_path, data))

    assert cfg.stt.vocabulary_file == tmp_path / "custom_vocabulary.txt"


def test_absolute_vocabulary_file_is_kept(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["stt"]["vocabulary_file"] = str(tmp_path / "elsewhere" / "vocab.txt")

    cfg = load_config(_write(tmp_path, data))

    assert cfg.stt.vocabulary_file == tmp_path / "elsewhere" / "vocab.txt"


def test_shipped_config_is_valid() -> None:
    shipped = Path(__file__).resolve().parent.parent / "config" / "jabberscribe.yaml"

    cfg = load_config(shipped)

    assert cfg.retention.audio_days == 90
    assert cfg.retention.text_days == 365
    assert cfg.stt.vocabulary_file == shipped.parent / "custom_vocabulary.txt"
    assert cfg.stt.vocabulary_file.is_file()


def test_speaker_split_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, BASE))

    assert (cfg.stt.split_channels, cfg.stt.near_channel) == (True, 0)


def test_near_channel_must_be_zero_or_one(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["stt"]["near_channel"] = 2

    with pytest.raises(ConfigError, match="near_channel"):
        load_config(_write(tmp_path, data))


def test_cues_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, BASE))

    assert (cfg.cues.enabled, cfg.cues.model_path, cfg.cues.threshold) == (True, None, 0.3)


def test_relative_cues_model_path_is_relative_to_the_config_file(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["cues"] = {"model_path": "models/cnn14.pth"}

    assert load_config(_write(tmp_path, data)).cues.model_path == tmp_path / "models" / "cnn14.pth"


def test_cues_threshold_must_be_a_probability(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["cues"] = {"threshold": 1.5}

    with pytest.raises(ConfigError, match="threshold"):
        load_config(_write(tmp_path, data))


def test_alerts_are_off_by_default(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, BASE))

    assert (cfg.alerts.webhook_url, cfg.alerts.backlog_minutes) == (None, 15)


def test_shipped_config_spells_out_the_extras() -> None:
    shipped = Path(__file__).resolve().parent.parent / "config" / "jabberscribe.yaml"

    cfg = load_config(shipped)

    assert (cfg.stt.split_channels, cfg.stt.near_channel) == (True, 0)
    assert cfg.cues.enabled is True
    assert cfg.cues.model_path == shipped.parent / "models" / "Cnn14_mAP=0.431.pth"
    assert cfg.cues.threshold == 0.3
    assert (cfg.alerts.webhook_url, cfg.alerts.backlog_minutes) == (None, 15)
