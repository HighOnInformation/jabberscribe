from pathlib import Path

from jabberscribe.cli import doctor, main
from jabberscribe.config import Config


def _config(tmp_path: Path) -> Config:
    return Config(
        paths={
            "drop_root": tmp_path / "drop",
            "work_dir": tmp_path / "work",
            "audio_store": tmp_path / "audio",
            "db_path": tmp_path / "js.db",
        }
    )


def test_doctor_reports_ffmpeg_and_paths(tmp_path: Path) -> None:
    checks = doctor(_config(tmp_path))

    names = [c.name for c in checks]
    assert "ffmpeg" in names
    assert "paths.work_dir" in names
    assert "paths.drop_root/inbox" in names


def test_doctor_creates_missing_directories(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert not cfg.paths.inbox.exists()

    checks = doctor(cfg)

    assert cfg.paths.inbox.is_dir()
    assert cfg.paths.work_dir.is_dir()
    assert all(c.ok for c in checks if c.name.startswith("paths."))


def test_main_doctor_returns_zero_when_healthy(tmp_path: Path, capsys) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        "paths:\n"
        f"  drop_root: {(tmp_path / 'drop').as_posix()}\n"
        f"  work_dir: {(tmp_path / 'work').as_posix()}\n"
        f"  audio_store: {(tmp_path / 'audio').as_posix()}\n"
        f"  db_path: {(tmp_path / 'js.db').as_posix()}\n",
        encoding="utf-8",
    )

    code = main(["--config", str(cfg_file), "doctor"])

    out = capsys.readouterr().out
    assert code == 0
    assert "ffmpeg" in out


def test_main_reports_bad_config_without_traceback(tmp_path: Path, capsys) -> None:
    code = main(["--config", str(tmp_path / "missing.yaml"), "doctor"])

    assert code == 2
    assert "not found" in capsys.readouterr().err
