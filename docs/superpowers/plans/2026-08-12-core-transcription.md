# JabberScribe Core Transcription — Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a recorded call dropped into a folder into a timestamped Hebrew transcript on disk, with a crash-resumable job pipeline.

**Architecture:** A flat Python package. A folder watcher validates `audio + JSON sidecar` pairs and enqueues them into a SQLite job table; a serial worker walks each job through idempotent stages (`audio` → `stt`), checkpointing after each so a crash resumes instead of re-transcribing. Speech-to-text is local faster-whisper with the Hebrew-specialised ivrit.ai model. No network calls anywhere in this plan.

**Tech Stack:** Python 3.12, pydantic 2 (config validation), PyYAML, stdlib `sqlite3`/`argparse`/`wave`, ffmpeg (subprocess), faster-whisper (CTranslate2), pytest, ruff.

## Global Constraints

- **On-premises only.** No code in this plan may make an outbound network call except the one-time STT model download. No cloud STT or LLM provider.
- **Python 3.12**, `requires-python = ">=3.12"`.
- **ruff** lints and formats, `line-length = 120`. `ruff check . && ruff format --check . && pytest` must pass before every commit.
- **Hebrew is the output language; English is the code language.** Comments, identifiers, log messages, and commit messages in English. Hebrew appears only in transcript content and test fixtures.
- **STT model:** `ivrit-ai/whisper-large-v3-turbo-ct2`, `compute_type="int8"` on CPU, `condition_on_previous_text=False`.
- **Speaker labels are exact or absent.** `tracks: dual` → labels from channel identity. `tracks: mixed` → `speaker=None`. Never infer a speaker from pause length.
- **No test may download the STT model** unless marked `@pytest.mark.slow`.
- **Git:** one branch per task, named in the task. Commit at every commit step. Merge to `main` with `--no-ff` when the task's tests pass.
- Required sidecar fields: `call_id`, `started_at`, `duration_sec`, `audio.tracks`. Anything else missing is tolerated.

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Deps, entry point, ruff + pytest config |
| `jabberscribe/config.py` | Typed config loaded from YAML; `load_config()` |
| `jabberscribe/cli.py` | `argparse` CLI: `doctor`, `process`, `run` |
| `jabberscribe/jobs.py` | `JobStore` (SQLite), `Job`, stage order helpers |
| `jabberscribe/sidecar.py` | `Sidecar`/`Participant` models, `parse_sidecar()` |
| `jabberscribe/watcher.py` | Ready-pair detection, dedup, quarantine, `scan_once()` |
| `jabberscribe/audio.py` | ffmpeg normalize + channel split, `prepare()` |
| `jabberscribe/stt.py` | `Segment`, `Transcriber` protocol, `WhisperLocal` |
| `jabberscribe/diarize.py` | `merge_tracks()` — channel label → speaker |
| `jabberscribe/pipeline.py` | Stage registry and `process_job()` |
| `config/jabberscribe.yaml` | Deployed config template |
| `config/custom_vocabulary.txt` | Hebrew glossary for the STT prompt |
| `tests/conftest.py` | Shared fixtures: temp config, synthetic WAV builder, fake transcriber |
| `tests/test_*.py` | One test module per source module |

Config sections for Confluence, mail, and retention are **not** created in this plan. They arrive in Plan 2 alongside the code that reads them.

---

### Task 1: Scaffolding, config, and `doctor`

**Branch:** `feat/config-and-cli`

**Files:**
- Create: `pyproject.toml`, `jabberscribe/__init__.py`, `jabberscribe/config.py`, `jabberscribe/cli.py`, `config/jabberscribe.yaml`, `config/custom_vocabulary.txt`
- Test: `tests/test_config.py`, `tests/test_cli_doctor.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `load_config(path: Path) -> Config`
  - `Config` with `.paths: PathsConfig`, `.watcher: WatcherConfig`, `.stt: SttConfig`
  - `PathsConfig` with `.drop_root: Path`, `.work_dir: Path`, `.audio_store: Path`, `.db_path: Path`, and read-only properties `.inbox -> Path` (`drop_root/"inbox"`) and `.quarantine -> Path` (`drop_root/"quarantine"`)
  - `WatcherConfig` with `.poll_seconds: int`, `.min_age_seconds: int`
  - `SttConfig` with `.model: str`, `.compute_type: str`, `.device: str`, `.vocabulary_file: Path | None`
  - `ConfigError(Exception)`
  - `jabberscribe.cli.doctor(cfg: Config) -> list[Check]` where `Check` is a frozen dataclass `(name: str, ok: bool, detail: str)`
  - `jabberscribe.cli.main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Create the branch and virtualenv**

```bash
cd /c/Users/meirh/Git/jabberscribe
git checkout -b feat/config-and-cli
python -m venv .venv
.venv/Scripts/python.exe -m pip install --upgrade pip
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "jabberscribe"
version = "0.1.0"
description = "On-prem Jabber call and conference transcription"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.7",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
stt = ["faster-whisper>=1.2"]
dev = ["pytest>=8.0", "ruff>=0.6"]

[project.scripts]
jabberscribe = "jabberscribe.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["jabberscribe*"]

[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "slow: requires the STT model to be downloaded",
]
```

- [ ] **Step 3: Install**

```bash
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```
Expected: `Successfully installed jabberscribe-0.1.0`

- [ ] **Step 4: Write the failing config test**

Create `tests/test_config.py`:

```python
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
```

- [ ] **Step 5: Run it and confirm it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.config'`

- [ ] **Step 6: Implement `jabberscribe/config.py`**

Create `jabberscribe/__init__.py` as an empty file, then `jabberscribe/config.py`:

```python
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
```

- [ ] **Step 7: Run the config tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: 5 passed

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml jabberscribe/__init__.py jabberscribe/config.py tests/test_config.py
git commit -m "feat(config): typed YAML config with strict validation

Every failure surfaces as ConfigError so callers never catch pydantic or
yaml exceptions. Secrets are deliberately absent -- they come from the
environment in the modules that use them, keeping this file loggable."
```

- [ ] **Step 9: Write the failing `doctor` test**

Create `tests/test_cli_doctor.py`:

```python
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
```

- [ ] **Step 10: Run it and confirm it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_doctor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.cli'`

- [ ] **Step 11: Implement `jabberscribe/cli.py`**

```python
"""Command-line entry point.

`doctor` exists so a bad deployment fails loudly at install time rather than
silently at 2 a.m. Subcommands are added by later tasks.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from jabberscribe.config import Config, ConfigError, load_config

DEFAULT_CONFIG = Path("config/jabberscribe.yaml")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _check_ffmpeg() -> Check:
    exe = shutil.which("ffmpeg")
    if exe is None:
        return Check("ffmpeg", False, "not found on PATH")
    try:
        proc = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("ffmpeg", False, f"{exe}: {exc}")
    if proc.returncode != 0:
        return Check("ffmpeg", False, f"{exe} exited {proc.returncode}")
    first_line = proc.stdout.splitlines()[0] if proc.stdout else exe
    return Check("ffmpeg", True, first_line)


def _check_dir(name: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check(name, False, f"cannot create {path}: {exc}")
    probe = path / ".jabberscribe-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(name, False, f"not writable: {path}: {exc}")
    return Check(name, True, str(path))


def doctor(cfg: Config) -> list[Check]:
    """Verify the environment. Creates missing directories as a side effect."""
    return [
        _check_ffmpeg(),
        _check_dir("paths.drop_root/inbox", cfg.paths.inbox),
        _check_dir("paths.drop_root/quarantine", cfg.paths.quarantine),
        _check_dir("paths.work_dir", cfg.paths.work_dir),
        _check_dir("paths.audio_store", cfg.paths.audio_store),
        _check_dir("paths.db_path parent", cfg.paths.db_path.parent),
    ]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jabberscribe")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to jabberscribe.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="verify environment and create missing directories")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "doctor":
        checks = doctor(cfg)
        for check in checks:
            print(f"[{'OK ' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        return 0 if all(c.ok for c in checks) else 1

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 12: Run the doctor tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cli_doctor.py -v`
Expected: 4 passed

- [ ] **Step 13: Write the deployed config template and vocabulary file**

Create `config/jabberscribe.yaml`:

```yaml
# JabberScribe configuration. Secrets come from environment variables, never here.
paths:
  drop_root: D:/jabberscribe/drop
  work_dir: D:/jabberscribe/work
  audio_store: D:/jabberscribe/audio
  db_path: D:/jabberscribe/jabberscribe.db

watcher:
  poll_seconds: 30
  min_age_seconds: 15

stt:
  model: ivrit-ai/whisper-large-v3-turbo-ct2
  compute_type: int8
  device: auto
  vocabulary_file: config/custom_vocabulary.txt
```

Create `config/custom_vocabulary.txt` — one term per line, seeding the Hebrew STT prompt. Start with the terms a Jabber call actually contains:

```
ג'אבר
שלוחה
ועידה
הקלטה
תמלול
קונפלואנס
שרת
תקלה
כרטיס
לקוח
פרויקט
דחוף
```

- [ ] **Step 14: Lint, format, run everything, and commit**

```bash
.venv/Scripts/python.exe -m ruff format .
.venv/Scripts/python.exe -m ruff check . --fix
.venv/Scripts/python.exe -m pytest -v
git add jabberscribe/cli.py tests/test_cli_doctor.py config/
git commit -m "feat(cli): doctor subcommand verifying ffmpeg and paths

doctor creates missing directories and probes each for writability, so a
misconfigured deployment fails at install time instead of at 2 a.m. Config
errors exit 2 with a plain message -- no traceback in an operator's face."
```

- [ ] **Step 15: Merge to main**

```bash
git checkout main
git merge --no-ff feat/config-and-cli -m "Merge branch 'feat/config-and-cli'"
```

---

### Task 2: Job store and crash-resumable state machine

**Branch:** `feat/jobs-store`

**Files:**
- Create: `jabberscribe/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: nothing from Task 1 (deliberately independent — `JobStore` takes a `Path`, not a `Config`, so it is testable alone).
- Produces:
  - `STAGE_ORDER: tuple[str, ...]` = `("audio", "stt", "enrich", "render", "publish", "notify")`
  - `next_stage(stage: str, pipeline: tuple[str, ...]) -> str | None`
  - `Job` frozen dataclass: `call_id: str`, `status: str`, `stage: str`, `audio_path: Path`, `sidecar_json: str`, `kind: str`, `started_at: str`, `duration_sec: int`, `transcript_path: Path | None`, `summary_path: Path | None`, `confluence_page_id: str | None`, `notified_at: str | None`, `attempts: int`, `last_error: str | None`
  - `JobStore(db_path: Path)` with `.init_schema()`, `.create(...) -> bool`, `.get(call_id) -> Job | None`, `.claim_next() -> Job | None`, `.complete_stage(call_id, stage)`, `.set_status(call_id, status, last_error=None)`, `.set_transcript_path(call_id, path)`, `.record_attempt(call_id, error) -> int`, `.list_by_status(status) -> list[Job]`, `.close()`
  - Status constants: `QUEUED = "queued"`, `RUNNING = "running"`, `DONE = "done"`, `FAILED = "failed"`, `NEEDS_REVIEW = "needs_review"`

**Note on schema scope:** the DDL creates the full table from the spec (§6), including `confluence_page_id` and `notified_at`, which Plan 2 uses. A single DDL is a data definition, not speculative code — and adding columns to a live SQLite table later is strictly worse than defining them now.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feat/jobs-store
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_jobs.py`:

```python
from pathlib import Path

from jabberscribe.jobs import (
    DONE,
    FAILED,
    QUEUED,
    RUNNING,
    JobStore,
    next_stage,
)

PIPELINE = ("audio", "stt")


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "js.db")
    store.init_schema()
    return store


def _create(store: JobStore, call_id: str = "c1") -> bool:
    return store.create(
        call_id=call_id,
        audio_path=Path(f"/inbox/{call_id}.wav"),
        sidecar_json='{"call_id": "%s"}' % call_id,
        kind="call",
        started_at="2026-08-12T14:03:11+03:00",
        duration_sec=812,
    )


def test_create_then_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert _create(store) is True

    job = store.get("c1")
    assert job is not None
    assert job.status == QUEUED
    assert job.stage == QUEUED
    assert job.duration_sec == 812
    assert job.attempts == 0
    assert job.transcript_path is None


def test_create_is_idempotent_on_call_id(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert _create(store) is True
    assert _create(store) is False

    assert len(store.list_by_status(QUEUED)) == 1


def test_claim_next_marks_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    job = store.claim_next()

    assert job is not None
    assert job.call_id == "c1"
    assert store.get("c1").status == RUNNING


def test_claim_next_returns_none_when_empty(tmp_path: Path) -> None:
    assert _store(tmp_path).claim_next() is None


def test_claim_next_ignores_done_and_failed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "done_one")
    _create(store, "failed_one")
    store.set_status("done_one", DONE)
    store.set_status("failed_one", FAILED, last_error="boom")

    assert store.claim_next() is None


def test_crash_mid_pipeline_resumes_at_next_stage(tmp_path: Path) -> None:
    """The core guarantee: a restart must not re-transcribe completed work."""
    db = tmp_path / "js.db"
    store = JobStore(db)
    store.init_schema()
    _create(store)
    store.claim_next()
    store.complete_stage("c1", "audio")
    store.close()  # simulate the process dying here

    reopened = JobStore(db)
    job = reopened.claim_next()

    assert job is not None
    assert job.stage == "audio"
    assert next_stage(job.stage, PIPELINE) == "stt"


def test_next_stage_returns_none_at_end_of_pipeline() -> None:
    assert next_stage(QUEUED, PIPELINE) == "audio"
    assert next_stage("audio", PIPELINE) == "stt"
    assert next_stage("stt", PIPELINE) is None


def test_next_stage_skips_stages_outside_the_pipeline() -> None:
    assert next_stage("audio", ("audio", "render")) == "render"


def test_record_attempt_increments_and_stores_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    assert store.record_attempt("c1", "ffmpeg exploded") == 1
    assert store.record_attempt("c1", "ffmpeg exploded again") == 2

    job = store.get("c1")
    assert job.attempts == 2
    assert job.last_error == "ffmpeg exploded again"


def test_set_transcript_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    store.set_transcript_path("c1", Path("/work/c1/transcript.json"))

    assert store.get("c1").transcript_path == Path("/work/c1/transcript.json")


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    JobStore(db).init_schema()
    store = JobStore(db)
    store.init_schema()

    assert _create(store) is True
```

- [ ] **Step 3: Run and confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.jobs'`

- [ ] **Step 4: Implement `jabberscribe/jobs.py`**

```python
"""SQLite-backed job store.

This is the queue seam. At pilot volume a serial worker over a SQLite table is
the right amount of machinery; scaling out means replacing this module with a
broker and changing nothing else.

Every stage checkpoints here, so a crashed worker resumes at the next
incomplete stage instead of re-transcribing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
NEEDS_REVIEW = "needs_review"

STAGE_ORDER: tuple[str, ...] = ("audio", "stt", "enrich", "render", "publish", "notify")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  call_id            TEXT PRIMARY KEY,
  status             TEXT NOT NULL,
  stage              TEXT NOT NULL,
  audio_path         TEXT NOT NULL,
  sidecar_json       TEXT NOT NULL,
  kind               TEXT NOT NULL,
  started_at         TEXT NOT NULL,
  duration_sec       INTEGER NOT NULL,
  transcript_path    TEXT,
  summary_path       TEXT,
  confluence_page_id TEXT,
  notified_at        TEXT,
  attempts           INTEGER NOT NULL DEFAULT 0,
  last_error         TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def next_stage(stage: str, pipeline: tuple[str, ...]) -> str | None:
    """Return the stage to run after `stage`, or None if the pipeline is done.

    `pipeline` is the ordered subset of STAGE_ORDER this deployment runs, so a
    deployment can omit stages without the resume logic changing.
    """
    if stage == QUEUED:
        return pipeline[0] if pipeline else None
    if stage not in pipeline:
        raise ValueError(f"stage {stage!r} is not in pipeline {pipeline!r}")
    index = pipeline.index(stage)
    return pipeline[index + 1] if index + 1 < len(pipeline) else None


@dataclass(frozen=True)
class Job:
    call_id: str
    status: str
    stage: str
    audio_path: Path
    sidecar_json: str
    kind: str
    started_at: str
    duration_sec: int
    transcript_path: Path | None
    summary_path: Path | None
    confluence_page_id: str | None
    notified_at: str | None
    attempts: int
    last_error: str | None


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        call_id=row["call_id"],
        status=row["status"],
        stage=row["stage"],
        audio_path=Path(row["audio_path"]),
        sidecar_json=row["sidecar_json"],
        kind=row["kind"],
        started_at=row["started_at"],
        duration_sec=row["duration_sec"],
        transcript_path=Path(row["transcript_path"]) if row["transcript_path"] else None,
        summary_path=Path(row["summary_path"]) if row["summary_path"] else None,
        confluence_page_id=row["confluence_page_id"],
        notified_at=row["notified_at"],
        attempts=row["attempts"],
        last_error=row["last_error"],
    )


class JobStore:
    """Data access only. No business logic lives here."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def create(
        self,
        *,
        call_id: str,
        audio_path: Path,
        sidecar_json: str,
        kind: str,
        started_at: str,
        duration_sec: int,
    ) -> bool:
        """Insert a new job. Returns False if `call_id` is already known.

        This is the deduplication point: a recorder that drops the same call
        twice produces one job, one page, and one email.
        """
        now = utcnow()
        try:
            self._conn.execute(
                "INSERT INTO jobs (call_id, status, stage, audio_path, sidecar_json, kind, started_at,"
                " duration_sec, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    call_id,
                    QUEUED,
                    QUEUED,
                    str(audio_path),
                    sidecar_json,
                    kind,
                    started_at,
                    duration_sec,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get(self, call_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE call_id = ?", (call_id,)).fetchone()
        return _row_to_job(row) if row else None

    def claim_next(self) -> Job | None:
        """Claim the oldest runnable job and mark it running.

        `running` rows are claimable because a row left running belongs to a
        crashed worker; its checkpointed stage tells us where to resume. This is
        safe under the single-worker deployment this design specifies.
        """
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY created_at LIMIT 1",
            (QUEUED, RUNNING),
        ).fetchone()
        if row is None:
            return None
        self.set_status(row["call_id"], RUNNING)
        return _row_to_job(self._conn.execute("SELECT * FROM jobs WHERE call_id = ?", (row["call_id"],)).fetchone())

    def complete_stage(self, call_id: str, stage: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET stage = ?, updated_at = ? WHERE call_id = ?",
            (stage, utcnow(), call_id),
        )

    def set_status(self, call_id: str, status: str, last_error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, last_error = COALESCE(?, last_error), updated_at = ? WHERE call_id = ?",
            (status, last_error, utcnow(), call_id),
        )

    def set_transcript_path(self, call_id: str, path: Path) -> None:
        self._conn.execute(
            "UPDATE jobs SET transcript_path = ?, updated_at = ? WHERE call_id = ?",
            (str(path), utcnow(), call_id),
        )

    def record_attempt(self, call_id: str, error: str) -> int:
        cur = self._conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, last_error = ?, updated_at = ?"
            " WHERE call_id = ? RETURNING attempts",
            (error, utcnow(), call_id),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown call_id: {call_id}")
        return int(row["attempts"])

    def list_by_status(self, status: str) -> list[Job]:
        rows = self._conn.execute("SELECT * FROM jobs WHERE status = ? ORDER BY created_at", (status,)).fetchall()
        return [_row_to_job(r) for r in rows]
```

- [ ] **Step 5: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_jobs.py -v`
Expected: 11 passed

- [ ] **Step 6: Lint and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . --fix
.venv/Scripts/python.exe -m pytest -q
git add jabberscribe/jobs.py tests/test_jobs.py
git commit -m "feat(jobs): SQLite job store with crash-resumable stages

call_id is the primary key, so a recorder that drops the same call twice
yields one job -- dedup costs nothing and needs no extra code path.
claim_next also claims 'running' rows: such a row belongs to a crashed
worker, and its checkpointed stage says where to resume. Safe under the
single-worker deployment this design specifies."
```

- [ ] **Step 7: Merge to main**

```bash
git checkout main
git merge --no-ff feat/jobs-store -m "Merge branch 'feat/jobs-store'"
```

---

### Task 3: Sidecar parsing and validation

**Branch:** `feat/sidecar`

**Files:**
- Create: `jabberscribe/sidecar.py`
- Test: `tests/test_sidecar.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Participant` frozen dataclass: `display_name: str | None`, `uri: str | None`, `extension: str | None`, `email: str | None`, `role: str | None`
  - `Sidecar` frozen dataclass: `call_id: str`, `kind: str`, `source: str`, `started_at: str`, `ended_at: str | None`, `duration_sec: int`, `subject: str | None`, `participants: tuple[Participant, ...]`, `tracks: str`, `sample_rate: int | None`, `channels: int | None`, `raw: str`
  - `Sidecar.emails` property → `tuple[str, ...]` of non-empty participant emails
  - `SidecarError(ValueError)`
  - `parse_sidecar(text: str) -> Sidecar`

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feat/sidecar
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_sidecar.py`:

```python
import json

import pytest

from jabberscribe.sidecar import SidecarError, parse_sidecar

VALID = {
    "schema_version": 1,
    "call_id": "8f2a1c4e",
    "source": "cucm-bib",
    "kind": "call",
    "started_at": "2026-08-12T14:03:11+03:00",
    "ended_at": "2026-08-12T14:16:43+03:00",
    "duration_sec": 812,
    "subject": None,
    "participants": [
        {
            "display_name": "מאיר חדד",
            "uri": "mhadad@corp.local",
            "extension": "1042",
            "email": "mhadad@corp.local",
            "role": "caller",
        },
        {"display_name": "Support", "extension": "1099", "role": "callee"},
    ],
    "audio": {"tracks": "dual", "codec": "pcm_s16le", "sample_rate": 8000, "channels": 2},
}


def test_parse_valid_sidecar() -> None:
    sc = parse_sidecar(json.dumps(VALID))

    assert sc.call_id == "8f2a1c4e"
    assert sc.kind == "call"
    assert sc.tracks == "dual"
    assert sc.duration_sec == 812
    assert sc.sample_rate == 8000
    assert len(sc.participants) == 2
    assert sc.participants[0].display_name == "מאיר חדד"
    assert sc.participants[1].email is None


def test_raw_is_preserved_verbatim() -> None:
    text = json.dumps(VALID)

    assert parse_sidecar(text).raw == text


def test_emails_property_skips_missing_and_blank() -> None:
    payload = dict(VALID)
    payload["participants"] = [
        {"email": "a@corp.local"},
        {"email": ""},
        {"extension": "1099"},
        {"email": "b@corp.local"},
    ]

    assert parse_sidecar(json.dumps(payload)).emails == ("a@corp.local", "b@corp.local")


def test_defaults_applied_for_optional_fields() -> None:
    minimal = {
        "call_id": "c9",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 10,
        "audio": {"tracks": "mixed"},
    }

    sc = parse_sidecar(json.dumps(minimal))

    assert sc.kind == "call"
    assert sc.source == "unknown"
    assert sc.participants == ()
    assert sc.ended_at is None
    assert sc.sample_rate is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"call_id": None}, "call_id"),
        ({"call_id": ""}, "call_id"),
        ({"started_at": None}, "started_at"),
        ({"duration_sec": None}, "duration_sec"),
        ({"duration_sec": -5}, "duration_sec"),
        ({"duration_sec": "long"}, "duration_sec"),
        ({"audio": {}}, "tracks"),
        ({"audio": {"tracks": "quad"}}, "tracks"),
        ({"audio": None}, "audio"),
        ({"kind": "webinar"}, "kind"),
    ],
)
def test_invalid_sidecars_are_rejected(mutation: dict, message: str) -> None:
    payload = {**VALID, **mutation}

    with pytest.raises(SidecarError, match=message):
        parse_sidecar(json.dumps(payload))


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(SidecarError, match="JSON"):
        parse_sidecar("{not json")


def test_non_object_json_is_rejected() -> None:
    with pytest.raises(SidecarError, match="object"):
        parse_sidecar("[1, 2, 3]")


def test_participants_must_be_a_list() -> None:
    payload = {**VALID, "participants": "מאיר"}

    with pytest.raises(SidecarError, match="participants"):
        parse_sidecar(json.dumps(payload))
```

- [ ] **Step 3: Run and confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sidecar.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.sidecar'`

- [ ] **Step 4: Implement `jabberscribe/sidecar.py`**

```python
"""Sidecar metadata: the capture layer's half of the drop contract.

Validation is deliberately narrow. Only call_id, started_at, duration_sec, and
audio.tracks are required, because those are the fields the pipeline cannot
function without. Everything else degrades: a call with no participant emails
still gets transcribed and published.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

VALID_TRACKS = ("dual", "mixed")
VALID_KINDS = ("call", "conference")


class SidecarError(ValueError):
    """The sidecar is unusable. The caller should quarantine the pair."""


@dataclass(frozen=True)
class Participant:
    display_name: str | None = None
    uri: str | None = None
    extension: str | None = None
    email: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class Sidecar:
    call_id: str
    kind: str
    source: str
    started_at: str
    ended_at: str | None
    duration_sec: int
    subject: str | None
    participants: tuple[Participant, ...]
    tracks: str
    sample_rate: int | None
    channels: int | None
    raw: str

    @property
    def emails(self) -> tuple[str, ...]:
        return tuple(p.email for p in self.participants if p.email)


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _require_nonempty_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SidecarError(f"{key} is required and must be a non-empty string")
    return value


def _parse_participant(raw: object) -> Participant:
    if not isinstance(raw, dict):
        raise SidecarError("each entry in participants must be an object")
    return Participant(
        display_name=_opt_str(raw.get("display_name")),
        uri=_opt_str(raw.get("uri")),
        extension=_opt_str(raw.get("extension")),
        email=_opt_str(raw.get("email")) or None,
        role=_opt_str(raw.get("role")),
    )


def parse_sidecar(text: str) -> Sidecar:
    """Parse and validate a sidecar document.

    Raises SidecarError for anything the pipeline cannot work with.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SidecarError(f"sidecar is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SidecarError("sidecar must be a JSON object")

    call_id = _require_nonempty_str(data, "call_id")
    started_at = _require_nonempty_str(data, "started_at")

    duration = data.get("duration_sec")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise SidecarError("duration_sec is required and must be a non-negative integer")

    audio = data.get("audio")
    if not isinstance(audio, dict):
        raise SidecarError("audio is required and must be an object")
    tracks = audio.get("tracks")
    if tracks not in VALID_TRACKS:
        raise SidecarError(f"audio.tracks must be one of {VALID_TRACKS}, got {tracks!r}")

    kind = data.get("kind") or "call"
    if kind not in VALID_KINDS:
        raise SidecarError(f"kind must be one of {VALID_KINDS}, got {kind!r}")

    raw_participants = data.get("participants") or []
    if not isinstance(raw_participants, list):
        raise SidecarError("participants must be a list")

    sample_rate = audio.get("sample_rate")
    channels = audio.get("channels")

    return Sidecar(
        call_id=call_id,
        kind=kind,
        source=_opt_str(data.get("source")) or "unknown",
        started_at=started_at,
        ended_at=_opt_str(data.get("ended_at")),
        duration_sec=duration,
        subject=_opt_str(data.get("subject")),
        participants=tuple(_parse_participant(p) for p in raw_participants),
        tracks=tracks,
        sample_rate=sample_rate if isinstance(sample_rate, int) else None,
        channels=channels if isinstance(channels, int) else None,
        raw=text,
    )
```

- [ ] **Step 5: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sidecar.py -v`
Expected: 17 passed (10 parametrized cases plus 7 others)

- [ ] **Step 6: Lint and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . --fix
.venv/Scripts/python.exe -m pytest -q
git add jabberscribe/sidecar.py tests/test_sidecar.py
git commit -m "feat(sidecar): parse and validate the capture contract

Validation is narrow on purpose: only call_id, started_at, duration_sec and
audio.tracks are required, because they are what the pipeline cannot run
without. Missing participant emails degrade rather than reject -- a call
with no roster still deserves a transcript. raw is kept verbatim so replay
survives retention deleting the original file."
```

- [ ] **Step 7: Merge to main**

```bash
git checkout main
git merge --no-ff feat/sidecar -m "Merge branch 'feat/sidecar'"
```

---

### Task 4: Watcher — ready detection, dedup, quarantine

**Branch:** `feat/watcher`

**Files:**
- Create: `jabberscribe/watcher.py`, `tests/conftest.py`
- Test: `tests/test_watcher.py`

**Interfaces:**
- Consumes: `jabberscribe.config.Config`, `jabberscribe.jobs.JobStore`, `jabberscribe.sidecar.parse_sidecar`, `SidecarError`
- Produces:
  - `ScanResult` frozen dataclass: `enqueued: tuple[str, ...]`, `quarantined: tuple[str, ...]`, `skipped: tuple[str, ...]`
  - `find_ready_pairs(inbox: Path, min_age_seconds: int, now: float | None = None) -> list[tuple[Path, Path]]` → `(audio_path, sidecar_path)` pairs
  - `quarantine_pair(paths: list[Path], quarantine_dir: Path, reason: str) -> None`
  - `scan_once(cfg: Config, store: JobStore, min_age_seconds: int | None = None) -> ScanResult` — the override bypasses the settling delay for one-shot runs

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feat/watcher
```

- [ ] **Step 2: Write shared fixtures**

Create `tests/conftest.py`:

```python
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
                    # Right channel gets a different tone so channel-split tests
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
```

- [ ] **Step 3: Write the failing watcher tests**

Create `tests/test_watcher.py`:

```python
from pathlib import Path

from jabberscribe.jobs import QUEUED, JobStore
from jabberscribe.watcher import find_ready_pairs, scan_once


def _store(cfg) -> JobStore:
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    return store


def test_pair_without_sidecar_is_not_ready(cfg, make_wav) -> None:
    make_wav(cfg.paths.inbox / "a.wav")

    assert find_ready_pairs(cfg.paths.inbox, 0) == []


def test_partial_audio_is_ignored(cfg, make_sidecar) -> None:
    (cfg.paths.inbox / "a.wav.part").write_bytes(b"RIFF")
    make_sidecar(cfg.paths.inbox / "a.json")

    assert find_ready_pairs(cfg.paths.inbox, 0) == []


def test_complete_pair_is_ready(cfg, make_wav, make_sidecar) -> None:
    audio = make_wav(cfg.paths.inbox / "a.wav")
    sidecar = make_sidecar(cfg.paths.inbox / "a.json")

    assert find_ready_pairs(cfg.paths.inbox, 0) == [(audio, sidecar)]


def test_min_age_guard_defers_fresh_files(cfg, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json")

    assert find_ready_pairs(cfg.paths.inbox, min_age_seconds=3600) == []


def test_scan_enqueues_and_moves_audio_out_of_inbox(cfg, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="abc")
    store = _store(cfg)

    result = scan_once(cfg, store)

    assert result.enqueued == ("abc",)
    job = store.get("abc")
    assert job is not None
    assert job.status == QUEUED
    assert job.audio_path == cfg.paths.audio_store / "abc.wav"
    assert job.audio_path.is_file()
    assert not (cfg.paths.inbox / "a.wav").exists()
    assert not (cfg.paths.inbox / "a.json").exists()


def test_scan_dedups_repeated_call_id(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="dup")
    scan_once(cfg, store)

    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="dup")
    result = scan_once(cfg, store)

    assert result.enqueued == ()
    assert result.skipped == ("dup",)
    assert len(store.list_by_status(QUEUED)) == 1


def test_scan_quarantines_invalid_sidecar(cfg, make_wav) -> None:
    make_wav(cfg.paths.inbox / "bad.wav")
    (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")
    store = _store(cfg)

    result = scan_once(cfg, store)

    assert result.quarantined == ("bad",)
    assert (cfg.paths.quarantine / "bad.wav").is_file()
    assert (cfg.paths.quarantine / "bad.json").is_file()
    reason = (cfg.paths.quarantine / "bad.reason.txt").read_text(encoding="utf-8")
    assert "JSON" in reason
    assert store.list_by_status(QUEUED) == []


def test_quarantine_does_not_collide_on_repeat(cfg, make_wav) -> None:
    store = _store(cfg)
    for _ in range(2):
        make_wav(cfg.paths.inbox / "bad.wav")
        (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")
        scan_once(cfg, store)

    quarantined = sorted(p.name for p in cfg.paths.quarantine.glob("bad*.wav"))
    assert len(quarantined) == 2


def test_scan_of_empty_inbox_is_harmless(cfg) -> None:
    result = scan_once(cfg, _store(cfg))

    assert (result.enqueued, result.quarantined, result.skipped) == ((), (), ())


def test_scan_honours_min_age_override(cfg, make_wav, make_sidecar) -> None:
    """`process` needs to bypass the settling delay for a one-shot run."""
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="ovr")
    store = _store(cfg)

    deferred = scan_once(cfg, store, min_age_seconds=3600)
    assert deferred.enqueued == ()

    immediate = scan_once(cfg, store, min_age_seconds=0)
    assert immediate.enqueued == ("ovr",)
```

- [ ] **Step 4: Run and confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_watcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.watcher'`

- [ ] **Step 5: Implement `jabberscribe/watcher.py`**

```python
"""Inbox watcher: the service's only entry point for new work.

The readiness rule comes from the drop contract -- the recorder writes audio
first (as .part, then renames) and the sidecar last, so a sidecar's presence
proves the audio is complete. A min-age guard catches a recorder that died
mid-write, where the sidecar exists but nothing is finished.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from jabberscribe.config import Config
from jabberscribe.jobs import JobStore
from jabberscribe.sidecar import SidecarError, parse_sidecar

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".ogg")


@dataclass(frozen=True)
class ScanResult:
    enqueued: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def find_ready_pairs(inbox: Path, min_age_seconds: int, now: float | None = None) -> list[tuple[Path, Path]]:
    """Return (audio, sidecar) pairs that are complete and settled."""
    current = time.time() if now is None else now
    pairs: list[tuple[Path, Path]] = []
    for sidecar in sorted(inbox.glob("*.json")):
        audio = next((c for suffix in AUDIO_SUFFIXES if (c := sidecar.with_suffix(suffix)).is_file()), None)
        if audio is None:
            log.debug("sidecar without audio, skipping: %s", sidecar)
            continue
        youngest = max(audio.stat().st_mtime, sidecar.stat().st_mtime)
        if current - youngest < min_age_seconds:
            log.debug("pair still settling, deferring: %s", sidecar.stem)
            continue
        pairs.append((audio, sidecar))
    return pairs


def _unique_target(directory: Path, name: str) -> Path:
    """Never overwrite. A repeated bad file must not erase the earlier evidence."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free name for {name} in {directory}")


def quarantine_pair(paths: list[Path], quarantine_dir: Path, reason: str) -> None:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    stem = paths[0].stem
    for path in paths:
        if path.exists():
            shutil.move(str(path), str(_unique_target(quarantine_dir, path.name)))
    _unique_target(quarantine_dir, f"{stem}.reason.txt").write_text(reason, encoding="utf-8")
    log.warning("quarantined %s: %s", stem, reason)


def scan_once(cfg: Config, store: JobStore, min_age_seconds: int | None = None) -> ScanResult:
    """Process every ready pair in the inbox exactly once.

    `min_age_seconds` overrides the configured settling delay. The `process`
    command passes 0: a human handing us one file is not a race with a recorder.
    """
    cfg.paths.audio_store.mkdir(parents=True, exist_ok=True)
    min_age = cfg.watcher.min_age_seconds if min_age_seconds is None else min_age_seconds
    enqueued: list[str] = []
    quarantined: list[str] = []
    skipped: list[str] = []

    for audio, sidecar_path in find_ready_pairs(cfg.paths.inbox, min_age):
        try:
            sidecar = parse_sidecar(sidecar_path.read_text(encoding="utf-8"))
        except (SidecarError, OSError, UnicodeDecodeError) as exc:
            quarantine_pair([audio, sidecar_path], cfg.paths.quarantine, str(exc))
            quarantined.append(sidecar_path.stem)
            continue

        stored_audio = cfg.paths.audio_store / f"{sidecar.call_id}{audio.suffix}"
        created = store.create(
            call_id=sidecar.call_id,
            audio_path=stored_audio,
            sidecar_json=sidecar.raw,
            kind=sidecar.kind,
            started_at=sidecar.started_at,
            duration_sec=sidecar.duration_sec,
        )
        if not created:
            # Already known. Drop the duplicate rather than reprocess it.
            log.info("duplicate call_id %s, discarding inbox copy", sidecar.call_id)
            audio.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            skipped.append(sidecar.call_id)
            continue

        shutil.move(str(audio), str(stored_audio))
        sidecar_path.unlink(missing_ok=True)
        enqueued.append(sidecar.call_id)
        log.info("enqueued %s", sidecar.call_id)

    return ScanResult(tuple(enqueued), tuple(quarantined), tuple(skipped))
```

- [ ] **Step 6: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_watcher.py -v`
Expected: 10 passed

- [ ] **Step 7: Lint and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . --fix
.venv/Scripts/python.exe -m pytest -q
git add jabberscribe/watcher.py tests/conftest.py tests/test_watcher.py
git commit -m "feat(watcher): ready-pair detection, dedup, and quarantine

Readiness comes from the drop contract: the sidecar lands last, so its
presence proves the audio is complete. The min-age guard catches the other
case -- a recorder that died leaving both files behind unfinished.

Quarantine never overwrites: a repeated bad file gets a numbered name so
earlier evidence survives for whoever investigates."
```

- [ ] **Step 8: Merge to main**

```bash
git checkout main
git merge --no-ff feat/watcher -m "Merge branch 'feat/watcher'"
```

---

### Task 5: Audio preprocessing

**Branch:** `feat/audio`

**Files:**
- Create: `jabberscribe/audio.py`
- Test: `tests/test_audio.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (takes plain paths).
- Produces:
  - `AudioError(RuntimeError)`
  - `Track` frozen dataclass: `label: str`, `path: Path`
  - `prepare(src: Path, work_dir: Path, tracks: str, ffmpeg: str = "ffmpeg") -> tuple[Track, ...]`
  - `TARGET_RATE: int = 16000`

Behaviour: `tracks="mixed"` yields one `Track(label="mixed", ...)`; `tracks="dual"` yields `Track(label="near")` and `Track(label="far")` from channels 0 and 1. Every output is 16 kHz, mono, `pcm_s16le`, loudness-normalized. `prepare()` is idempotent — an existing output file of non-zero size is reused rather than regenerated, which is what makes the `audio` stage safe to retry.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feat/audio
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_audio.py`:

```python
import shutil
import wave
from pathlib import Path

import pytest

from jabberscribe.audio import TARGET_RATE, AudioError, prepare

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _probe(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as handle:
        return handle.getnchannels(), handle.getframerate()


def test_mixed_track_is_resampled_to_mono_16k(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "in.wav", seconds=1.0, rate=8000, channels=1)

    tracks = prepare(src, tmp_path / "work", "mixed")

    assert [t.label for t in tracks] == ["mixed"]
    assert _probe(tracks[0].path) == (1, TARGET_RATE)


def test_dual_track_splits_into_near_and_far(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "in.wav", seconds=1.0, rate=8000, channels=2)

    tracks = prepare(src, tmp_path / "work", "dual")

    assert [t.label for t in tracks] == ["near", "far"]
    for track in tracks:
        assert _probe(track.path) == (1, TARGET_RATE)
    assert tracks[0].path != tracks[1].path


def test_dual_split_channels_differ(tmp_path: Path, make_wav) -> None:
    """Proves the split reads two distinct channels rather than duplicating one."""
    src = make_wav(tmp_path / "in.wav", seconds=1.0, rate=8000, channels=2)

    near, far = prepare(src, tmp_path / "work", "dual")

    assert near.path.read_bytes() != far.path.read_bytes()


def test_prepare_is_idempotent(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "in.wav", channels=1)
    work = tmp_path / "work"

    first = prepare(src, work, "mixed")
    stamp = first[0].path.stat().st_mtime_ns
    second = prepare(src, work, "mixed")

    assert second[0].path == first[0].path
    assert second[0].path.stat().st_mtime_ns == stamp


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(AudioError, match="not found"):
        prepare(tmp_path / "nope.wav", tmp_path / "work", "mixed")


def test_undecodable_source_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"this is not audio")

    with pytest.raises(AudioError, match="ffmpeg"):
        prepare(bad, tmp_path / "work", "mixed")


def test_unknown_tracks_value_raises(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "in.wav")

    with pytest.raises(AudioError, match="tracks"):
        prepare(src, tmp_path / "work", "quad")
```

- [ ] **Step 3: Run and confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_audio.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.audio'`

- [ ] **Step 4: Implement `jabberscribe/audio.py`**

```python
"""Audio preprocessing via ffmpeg.

Whisper wants 16 kHz mono. Telephony gives 8 kHz, often stereo with the near
and far end on separate channels -- which is a gift, because splitting those
channels yields exact speaker attribution with no model involved.

Approach ported from CallSight's pipeline/audio/preprocessor.py.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

TARGET_RATE = 16000
_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
_TIMEOUT_SECONDS = 1800


class AudioError(RuntimeError):
    """Audio could not be prepared. Treated as a permanent failure."""


@dataclass(frozen=True)
class Track:
    label: str
    path: Path


def _run_ffmpeg(args: list[str]) -> None:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError as exc:
        raise AudioError(f"ffmpeg not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg timed out after {_TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise AudioError(f"ffmpeg exited {proc.returncode}: {' | '.join(tail)}")


def _convert(src: Path, dest: Path, pan: str | None, ffmpeg: str) -> None:
    """Convert one (optionally panned) channel to 16 kHz mono PCM."""
    if dest.is_file() and dest.stat().st_size > 0:
        log.debug("reusing existing %s", dest)
        return
    filters = [f for f in (pan, _LOUDNORM) if f]
    partial = dest.with_suffix(dest.suffix + ".part")
    args = [ffmpeg, "-y", "-nostdin", "-i", str(src)]
    if filters:
        args += ["-af", ",".join(filters)]
    args += ["-ac", "1", "-ar", str(TARGET_RATE), "-c:a", "pcm_s16le", str(partial)]
    _run_ffmpeg(args)
    partial.replace(dest)


def prepare(src: Path, work_dir: Path, tracks: str, ffmpeg: str = "ffmpeg") -> tuple[Track, ...]:
    """Normalize `src` into 16 kHz mono track(s) inside `work_dir`.

    Idempotent: an existing non-empty output is reused, which is what makes the
    audio stage safe to retry after a crash.
    """
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    work_dir.mkdir(parents=True, exist_ok=True)

    if tracks == "mixed":
        dest = work_dir / f"{src.stem}.mixed.wav"
        _convert(src, dest, None, ffmpeg)
        return (Track("mixed", dest),)

    if tracks == "dual":
        result: list[Track] = []
        for label, channel in (("near", 0), ("far", 1)):
            dest = work_dir / f"{src.stem}.{label}.wav"
            _convert(src, dest, f"pan=mono|c0=c{channel}", ffmpeg)
            result.append(Track(label, dest))
        return tuple(result)

    raise AudioError(f"unsupported tracks value: {tracks!r}")
```

- [ ] **Step 5: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_audio.py -v`
Expected: 7 passed

If `test_undecodable_source_raises` fails because ffmpeg still emits a zero-length file, that is the bug the `.part`-then-`replace` pattern prevents — confirm `_run_ffmpeg` raises before `partial.replace(dest)` runs.

- [ ] **Step 6: Lint and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . --fix
.venv/Scripts/python.exe -m pytest -q
git add jabberscribe/audio.py tests/test_audio.py
git commit -m "feat(audio): ffmpeg normalization and channel split

Telephony stereo puts near and far end on separate channels, so splitting
them buys exact speaker attribution with no diarization model at all.
Outputs land via .part-then-replace and are reused when present, making the
audio stage safe to retry after a crash."
```

- [ ] **Step 7: Merge to main**

```bash
git checkout main
git merge --no-ff feat/audio -m "Merge branch 'feat/audio'"
```

---

### Task 6: STT, diarization, and the end-to-end pipeline

**Branch:** `feat/stt-pipeline`

**Files:**
- Create: `jabberscribe/stt.py`, `jabberscribe/diarize.py`, `jabberscribe/pipeline.py`
- Modify: `jabberscribe/cli.py` (add `process` and `run` subcommands)
- Test: `tests/test_stt.py`, `tests/test_diarize.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `Config`, `JobStore`, `Job`, `next_stage`, `parse_sidecar`, `audio.prepare`, `audio.Track`
- Produces:
  - `stt.Segment` frozen dataclass: `start: float`, `end: float`, `text: str`, `speaker: str | None = None`
  - `stt.Transcriber` Protocol with `transcribe(self, wav: Path) -> list[Segment]`
  - `stt.WhisperLocal(model: str, compute_type: str, device: str, vocabulary: str | None = None)` implementing `Transcriber`
  - `stt.load_vocabulary(path: Path | None) -> str | None`
  - `stt.SttError(RuntimeError)`
  - `diarize.merge_tracks(per_track: dict[str, list[Segment]]) -> list[Segment]`
  - `pipeline.PIPELINE: tuple[str, ...]` = `("audio", "stt")`
  - `pipeline.process_job(job: Job, cfg: Config, store: JobStore, transcriber: Transcriber) -> Path` → transcript JSON path
  - `pipeline.run_once(cfg: Config, store: JobStore, transcriber: Transcriber) -> int` → jobs processed
  - `pipeline.write_transcript(path: Path, segments: list[Segment], call_id: str) -> None`

Transcript JSON shape (Plan 2's `render` consumes this):

```json
{
  "call_id": "abc",
  "segments": [{"start": 0.0, "end": 2.4, "text": "שלום", "speaker": "near"}]
}
```

- [ ] **Step 1: Create the branch**

```bash
git checkout -b feat/stt-pipeline
```

- [ ] **Step 2: Write the failing diarize tests**

Create `tests/test_diarize.py`:

```python
from jabberscribe.diarize import merge_tracks
from jabberscribe.stt import Segment


def test_dual_track_merge_is_ordered_and_labelled() -> None:
    merged = merge_tracks(
        {
            "near": [Segment(0.0, 1.0, "שלום"), Segment(4.0, 5.0, "תודה")],
            "far": [Segment(1.5, 3.0, "היי")],
        }
    )

    assert [(s.start, s.speaker, s.text) for s in merged] == [
        (0.0, "near", "שלום"),
        (1.5, "far", "היי"),
        (4.0, "near", "תודה"),
    ]


def test_mixed_track_gets_no_speaker_label() -> None:
    """A compliance transcript must never fabricate attribution."""
    merged = merge_tracks({"mixed": [Segment(0.0, 1.0, "שלום")]})

    assert merged[0].speaker is None


def test_overlapping_speech_is_kept_not_dropped() -> None:
    merged = merge_tracks({"near": [Segment(0.0, 2.0, "אני מדבר")], "far": [Segment(1.0, 3.0, "וגם אני")]})

    assert len(merged) == 2


def test_ties_break_deterministically_by_label() -> None:
    merged = merge_tracks({"near": [Segment(1.0, 2.0, "a")], "far": [Segment(1.0, 2.0, "b")]})

    assert [s.speaker for s in merged] == ["far", "near"]


def test_empty_input_yields_empty_output() -> None:
    assert merge_tracks({}) == []
```

- [ ] **Step 3: Write the failing STT tests**

Create `tests/test_stt.py`:

```python
from pathlib import Path

import pytest

from jabberscribe.stt import Segment, load_vocabulary


def test_segment_defaults_to_no_speaker() -> None:
    assert Segment(0.0, 1.0, "שלום").speaker is None


def test_load_vocabulary_joins_lines_and_skips_blanks(tmp_path: Path) -> None:
    vocab = tmp_path / "vocab.txt"
    vocab.write_text("ג'אבר\n\nשלוחה\n  ועידה  \n", encoding="utf-8")

    assert load_vocabulary(vocab) == "ג'אבר, שלוחה, ועידה"


def test_load_vocabulary_handles_none_and_missing(tmp_path: Path) -> None:
    assert load_vocabulary(None) is None
    assert load_vocabulary(tmp_path / "absent.txt") is None


@pytest.mark.slow
def test_whisper_local_transcribes_hebrew_sample() -> None:
    """Downloads the model. Run explicitly: pytest -m slow"""
    from jabberscribe.stt import WhisperLocal

    sample = Path("tests/fixtures/hebrew_sample.wav")
    if not sample.is_file():
        pytest.skip("tests/fixtures/hebrew_sample.wav not provided")

    segments = WhisperLocal("ivrit-ai/whisper-large-v3-turbo-ct2", "int8", "cpu").transcribe(sample)

    assert segments
    assert any(seg.text.strip() for seg in segments)
```

- [ ] **Step 4: Run both and confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_stt.py tests/test_diarize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.stt'`

- [ ] **Step 5: Implement `jabberscribe/stt.py`**

```python
"""Local speech-to-text.

Configuration is ported from CallSight, where it won a Hebrew STT bake-off
against cloud providers. Two settings matter more than the rest:

- condition_on_previous_text=False stops the repetition-loop degeneration that
  Whisper falls into on Hebrew.
- initial_prompt seeded with a domain glossary measurably improves recognition
  of names and jargon.

faster-whisper is imported lazily so the rest of the service -- and its tests --
never pay for the model dependency.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class SttError(RuntimeError):
    """Transcription failed."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None


class Transcriber(Protocol):
    def transcribe(self, wav: Path) -> list[Segment]: ...


def load_vocabulary(path: Path | None) -> str | None:
    """Read a one-term-per-line glossary into a Whisper initial_prompt."""
    if path is None or not path.is_file():
        return None
    terms = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return ", ".join(terms) or None


class WhisperLocal:
    """faster-whisper with the Hebrew-specialised ivrit.ai model."""

    def __init__(self, model: str, compute_type: str, device: str, vocabulary: str | None = None) -> None:
        self._model_name = model
        self._compute_type = compute_type
        self._device = device
        self._vocabulary = vocabulary
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - environment problem
                raise SttError("faster-whisper is not installed; pip install '.[stt]'") from exc
            log.info("loading STT model %s (%s, %s)", self._model_name, self._device, self._compute_type)
            self._model = WhisperModel(self._model_name, device=self._device, compute_type=self._compute_type)
        return self._model

    def transcribe(self, wav: Path) -> list[Segment]:
        model = self._load()
        try:
            segments, _info = model.transcribe(
                str(wav),
                language="he",
                initial_prompt=self._vocabulary,
                condition_on_previous_text=False,
                vad_filter=True,
                beam_size=5,
            )
            return [Segment(float(s.start), float(s.end), s.text.strip()) for s in segments if s.text.strip()]
        except SttError:
            raise
        except Exception as exc:  # faster-whisper raises assorted runtime errors
            raise SttError(f"transcription failed for {wav}: {exc}") from exc
```

- [ ] **Step 6: Implement `jabberscribe/diarize.py`**

```python
"""Speaker labels: exact or absent.

With dual-track audio, the channel a segment came from *is* the speaker -- a
fact, not an inference. With a mixed track we emit no label at all. Guessing
turns from pause length produces confidently wrong attributions, and a
compliance transcript must never fabricate who said what.
"""

from __future__ import annotations

from jabberscribe.stt import Segment

MIXED_LABEL = "mixed"


def merge_tracks(per_track: dict[str, list[Segment]]) -> list[Segment]:
    """Merge per-channel segments into one timeline.

    Overlapping speech is preserved as separate segments: on a real call people
    talk over each other, and dropping either side loses content.
    """
    labelled: list[Segment] = []
    for label, segments in per_track.items():
        speaker = None if label == MIXED_LABEL else label
        labelled.extend(Segment(start=s.start, end=s.end, text=s.text, speaker=speaker) for s in segments)
    labelled.sort(key=lambda s: (s.start, s.speaker or "", s.end))
    return labelled
```

- [ ] **Step 7: Run the STT and diarize tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_stt.py tests/test_diarize.py -v`
Expected: 8 passed, 1 skipped (the `slow` test)

- [ ] **Step 8: Commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . --fix
git add jabberscribe/stt.py jabberscribe/diarize.py tests/test_stt.py tests/test_diarize.py
git commit -m "feat(stt): local ivrit.ai Whisper and channel-based speaker labels

condition_on_previous_text=False is not a tuning preference -- it stops the
repetition-loop degeneration Whisper falls into on Hebrew. faster-whisper is
imported lazily so tests never pay for the model.

Dual-track labels are derived from channel identity, so they are facts. Mixed
tracks get no label at all: a compliance transcript must not invent
attribution, and pause-based guessing is confidently wrong."
```

- [ ] **Step 9: Write the failing pipeline tests**

Create `tests/test_pipeline.py`:

```python
import json
from pathlib import Path

import pytest

from jabberscribe.jobs import DONE, FAILED, JobStore
from jabberscribe.pipeline import process_job, run_once
from jabberscribe.stt import Segment, SttError
from jabberscribe.watcher import scan_once


class FakeTranscriber:
    """Returns one segment per call and counts invocations."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe(self, wav: Path) -> list[Segment]:
        self.calls.append(wav)
        return [Segment(0.0, 1.5, f"טקסט מ{wav.stem}")]


class ExplodingTranscriber:
    def transcribe(self, wav: Path) -> list[Segment]:
        raise SttError("model on fire")


def _store(cfg) -> JobStore:
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    return store


def _enqueue(cfg, store, make_wav, make_sidecar, *, call_id="abc", tracks="mixed") -> None:
    channels = 2 if tracks == "dual" else 1
    make_wav(cfg.paths.inbox / "in.wav", channels=channels)
    make_sidecar(cfg.paths.inbox / "in.json", call_id=call_id, tracks=tracks)
    scan_once(cfg, store, min_age_seconds=0)


def test_process_job_writes_transcript_and_marks_done(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)
    job = store.claim_next()

    path = process_job(job, cfg, store, FakeTranscriber())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["call_id"] == "abc"
    assert payload["segments"][0]["text"].startswith("טקסט")
    assert store.get("abc").status == DONE
    assert store.get("abc").transcript_path == path


def test_dual_track_produces_two_speakers(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar, tracks="dual")
    job = store.claim_next()

    path = process_job(job, cfg, store, FakeTranscriber())

    speakers = {seg["speaker"] for seg in json.loads(path.read_text(encoding="utf-8"))["segments"]}
    assert speakers == {"near", "far"}


def test_mixed_track_has_null_speaker(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)
    job = store.claim_next()

    path = process_job(job, cfg, store, FakeTranscriber())

    assert json.loads(path.read_text(encoding="utf-8"))["segments"][0]["speaker"] is None


def test_completed_stages_are_not_redone_after_resume(cfg, make_wav, make_sidecar) -> None:
    """The crash-resume guarantee, end to end."""
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)
    transcriber = FakeTranscriber()
    process_job(store.claim_next(), cfg, store, transcriber)
    first_count = len(transcriber.calls)

    store.set_status("abc", "running")
    store.complete_stage("abc", "stt")
    run_once(cfg, store, transcriber)

    assert len(transcriber.calls) == first_count


def test_stt_failure_records_attempt_and_does_not_mark_done(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)
    job = store.claim_next()

    with pytest.raises(SttError):
        process_job(job, cfg, store, ExplodingTranscriber())

    refreshed = store.get("abc")
    assert refreshed.status != DONE
    assert refreshed.attempts == 1
    assert "on fire" in refreshed.last_error


def test_run_once_gives_up_after_max_attempts(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    _enqueue(cfg, store, make_wav, make_sidecar)

    for _ in range(3):
        run_once(cfg, store, ExplodingTranscriber())

    assert store.get("abc").status == FAILED


def test_run_once_on_empty_queue_returns_zero(cfg) -> None:
    assert run_once(cfg, _store(cfg), FakeTranscriber()) == 0
```

- [ ] **Step 10: Run and confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.pipeline'`

- [ ] **Step 11: Implement `jabberscribe/pipeline.py`**

```python
"""Stage orchestration.

Stages are idempotent and checkpointed, so a restart resumes at the first
incomplete stage. PIPELINE lists the stages this deployment runs; Plan 2 extends
it with render/publish/notify and the resume logic does not change.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from jabberscribe.audio import prepare
from jabberscribe.config import Config
from jabberscribe.diarize import merge_tracks
from jabberscribe.jobs import DONE, FAILED, QUEUED, Job, JobStore, next_stage
from jabberscribe.sidecar import parse_sidecar
from jabberscribe.stt import Segment, Transcriber

log = logging.getLogger(__name__)

PIPELINE: tuple[str, ...] = ("audio", "stt")
MAX_ATTEMPTS = 3


def write_transcript(path: Path, segments: list[Segment], call_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "call_id": call_id,
        "segments": [{"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker} for s in segments],
    }
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _work_dir(cfg: Config, call_id: str) -> Path:
    return cfg.paths.work_dir / call_id


def process_job(job: Job, cfg: Config, store: JobStore, transcriber: Transcriber) -> Path:
    """Run `job` from its checkpoint to the end of the pipeline.

    Returns the transcript path. Raises on failure after recording the attempt,
    leaving the retry decision to the caller.
    """
    sidecar = parse_sidecar(job.sidecar_json)
    work = _work_dir(cfg, job.call_id)
    transcript_path = work / "transcript.json"
    stage = job.stage

    try:
        while (stage := next_stage(stage, PIPELINE)) is not None:
            log.info("%s: stage %s", job.call_id, stage)
            if stage == "audio":
                prepare(job.audio_path, work, sidecar.tracks)
            elif stage == "stt":
                tracks = prepare(job.audio_path, work, sidecar.tracks)
                per_track = {track.label: transcriber.transcribe(track.path) for track in tracks}
                write_transcript(transcript_path, merge_tracks(per_track), job.call_id)
                store.set_transcript_path(job.call_id, transcript_path)
            else:  # pragma: no cover - PIPELINE and this branch list are in sync
                raise AssertionError(f"no handler for stage {stage!r}")
            store.complete_stage(job.call_id, stage)
    except Exception as exc:
        store.record_attempt(job.call_id, str(exc))
        log.exception("%s: stage %s failed", job.call_id, stage)
        raise

    store.set_status(job.call_id, DONE)
    log.info("%s: done", job.call_id)
    return transcript_path


def run_once(cfg: Config, store: JobStore, transcriber: Transcriber) -> int:
    """Process every runnable job once. Returns how many were attempted."""
    processed = 0
    while (job := store.claim_next()) is not None:
        processed += 1
        try:
            process_job(job, cfg, store, transcriber)
        except Exception:
            refreshed = store.get(job.call_id)
            if refreshed is not None and refreshed.attempts >= MAX_ATTEMPTS:
                store.set_status(job.call_id, FAILED)
                log.error("%s: giving up after %d attempts", job.call_id, refreshed.attempts)
            else:
                # Leave it queued so the next pass retries it.
                store.set_status(job.call_id, QUEUED)
                break
    return processed
```

Note the `audio` stage calling `prepare` twice across stages is deliberate and free: `prepare` reuses existing outputs, so the `stt` stage after a crash gets its tracks back without re-encoding.

- [ ] **Step 12: Run the pipeline tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -v`
Expected: 7 passed

- [ ] **Step 13: Wire `process` and `run` into the CLI**

In `jabberscribe/cli.py`, add these imports at the top:

```python
import json
import logging
import time

from jabberscribe.jobs import JobStore
from jabberscribe.pipeline import run_once
from jabberscribe.stt import WhisperLocal, load_vocabulary
from jabberscribe.watcher import scan_once
```

Add this helper above `_build_parser`:

```python
def _transcriber(cfg: Config) -> WhisperLocal:
    device = cfg.stt.device
    if device == "auto":
        device = "cpu"  # CUDA selection is a deployment decision, made explicit in config
    return WhisperLocal(
        model=cfg.stt.model,
        compute_type=cfg.stt.compute_type,
        device=device,
        vocabulary=load_vocabulary(cfg.stt.vocabulary_file),
    )
```

Register the subcommands inside `_build_parser`, after the `doctor` line:

```python
    process_cmd = sub.add_parser("process", help="process one recording from a path pair")
    process_cmd.add_argument("audio", type=Path)
    process_cmd.add_argument("sidecar", type=Path)

    run_cmd = sub.add_parser("run", help="watch the inbox and process jobs")
    run_cmd.add_argument("--once", action="store_true", help="single pass, then exit")
```

Add the command handling in `main`, before the final `raise AssertionError`:

```python
    if args.command == "process":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        doctor(cfg)  # ensure directories exist
        store = JobStore(cfg.paths.db_path)
        store.init_schema()
        shutil.copy2(args.audio, cfg.paths.inbox / args.audio.name)
        shutil.copy2(args.sidecar, cfg.paths.inbox / args.sidecar.name)
        # min_age 0: a human handing us one file is not racing a recorder.
        result = scan_once(cfg, store, min_age_seconds=0)
        if result.quarantined:
            print(f"quarantined: {', '.join(result.quarantined)}", file=sys.stderr)
            return 1
        run_once(cfg, store, _transcriber(cfg))
        for call_id in result.enqueued:
            job = store.get(call_id)
            print(f"{call_id}: {job.status} -> {job.transcript_path}")
            if job.transcript_path and job.transcript_path.is_file():
                payload = json.loads(job.transcript_path.read_text(encoding="utf-8"))
                print(f"  {len(payload['segments'])} segments")
        return 0

    if args.command == "run":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        doctor(cfg)
        store = JobStore(cfg.paths.db_path)
        store.init_schema()
        transcriber = _transcriber(cfg)
        while True:
            scan_once(cfg, store)
            run_once(cfg, store, transcriber)
            if args.once:
                return 0
            time.sleep(cfg.watcher.poll_seconds)
```

- [ ] **Step 14: Verify the CLI wiring end to end**

Add to `tests/test_pipeline.py`:

```python
def test_cli_process_end_to_end(cfg, tmp_path, make_wav, make_sidecar, monkeypatch, capsys) -> None:
    from jabberscribe import cli

    monkeypatch.setattr(cli, "_transcriber", lambda _cfg: FakeTranscriber())
    audio = make_wav(tmp_path / "src" / "call.wav")
    sidecar = make_sidecar(tmp_path / "src" / "call.json", call_id="cli1")
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        "paths:\n"
        f"  drop_root: {cfg.paths.drop_root.as_posix()}\n"
        f"  work_dir: {cfg.paths.work_dir.as_posix()}\n"
        f"  audio_store: {cfg.paths.audio_store.as_posix()}\n"
        f"  db_path: {cfg.paths.db_path.as_posix()}\n"
        "watcher:\n  min_age_seconds: 0\n",
        encoding="utf-8",
    )

    code = cli.main(["--config", str(cfg_file), "process", str(audio), str(sidecar)])

    assert code == 0
    assert "cli1: done" in capsys.readouterr().out
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline.py -v`
Expected: 8 passed

- [ ] **Step 15: Run the full suite, lint, and commit**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . --fix
.venv/Scripts/python.exe -m pytest -q
git add jabberscribe/pipeline.py jabberscribe/cli.py tests/test_pipeline.py
git commit -m "feat(pipeline): checkpointed stages plus process and run commands

process copies a path pair through the real inbox rather than bypassing it,
so the development entry point exercises the same code the service does.

The audio stage re-runs prepare() during stt on purpose: prepare reuses
existing outputs, so a crash-resumed job gets its tracks back without
re-encoding, and the stt stage never depends on in-memory state."
```

- [ ] **Step 16: Merge to main**

```bash
git checkout main
git merge --no-ff feat/stt-pipeline -m "Merge branch 'feat/stt-pipeline'"
```

- [ ] **Step 17: Update the README status section**

Replace the `## Status` section of `README.md` with:

```markdown
## Status

Plan 1 (core transcription) implemented: a recording dropped into `inbox/` is
validated, normalized, transcribed locally, and written out as a transcript.
Delivery to Confluence and email is Plan 2.

```bash
jabberscribe doctor                          # verify ffmpeg and paths
jabberscribe process call.wav call.json      # one recording, end to end
jabberscribe run                             # watch the inbox
```

See [the tech spec](docs/superpowers/specs/2026-08-12-jabberscribe-design.md) and
[Plan 1](docs/superpowers/plans/2026-08-12-core-transcription.md).
```

```bash
git add README.md
git commit -m "docs(readme): record Plan 1 completion and CLI usage"
```

---

## Definition of Done for Plan 1

1. `jabberscribe doctor` reports OK for ffmpeg and every path.
2. `jabberscribe process sample.wav sample.json` writes `transcript.json` with non-empty segments.
3. Dual-track input yields `near`/`far` speakers; mixed input yields `null`.
4. A malformed sidecar lands in `quarantine/` with a reason file and creates no job row.
5. The same `call_id` dropped twice creates exactly one job.
6. Completing `stt` and re-running never re-invokes the transcriber.
7. `ruff check . && ruff format --check . && pytest` passes clean.

## Plan 2 preview (not implemented here)

`render` (transcript JSON → Confluence XHTML + mail bodies), `publish` (Confluence DC, restrictions, page-id idempotency), `notify` (SMTP, at-most-once), `audit`, `retention`, and `enrich` (local Ollama summary, default off). Plan 2 consumes the transcript JSON shape defined in Task 6 and extends `pipeline.PIPELINE`.
