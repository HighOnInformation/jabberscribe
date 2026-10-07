# JabberScribe v2 Hardened Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the v2 MVP pipeline (grouping, pipeline, retention, CLI, shipping docs) with the expert audit's Critical, Important and selected Minor findings fixed, so an outage delays calls instead of losing them, a conference yields one complete transcript, and nothing outlives its retention.

**Architecture:** Same service as the original plan: watcher → SQLite job store → serial checkpointed stages (`audio`, `stt`, `summarize`, `output`) → `out/<YYYY>/<MM>/<job_key>/`. This plan adds error classification (`TransientError` vs permanent) with per-job `next_attempt_at` backoff, overlap-scoped conference groups with re-election, a single-instance OS lock, chunked summarization, hallucination filtering, a complete retention sweep, and `retry`/`status` commands.

**Tech Stack:** Python ≥3.12, pydantic 2, PyYAML, httpx (with `httpx.MockTransport` in tests), SQLite (stdlib), ffmpeg/ffprobe on PATH, pytest, ruff.

**Supersedes:** Tasks 8–12 of `docs/superpowers/plans/2026-10-07-v2-pipeline.md`. That plan's Tasks 1–7 are done and committed (last: `c8d645e feat(output)`); this plan starts at Task 8 and also edits code those tasks produced. Task 19 marks the original plan accordingly and fixes its Task 3 Step 8 text (audit M13).

**Baseline and verification:** every full-file rewrite below was produced against commit `c8d645e`. If a file you are about to replace differs from that commit (for example a review fix landed after it), stop and reconcile before replacing it. The whole plan was replayed on a copy of `c8d645e`: after each task's test step the named tests fail as stated, and after its code step the full suite passes with the stated count and `ruff check .` is clean.

**Spec:** `docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md`. **Audit:** `.superpowers/audit/expert-audit.md` (finding IDs C1–C3, I1–I11, M1–M16 are cited per task).

**Out of scope:** Webex capture, the SIPREC recorder, the web app, email, bracket cues, speaker labels.

## Global Constraints

- Everything stays on-prem: STT and summary go only to the configured LiteLLM `base_url`.
- Secrets come from environment variables, never config: `JABBERSCRIBE_LITELLM_KEY`.
- User-facing output (Markdown files, summary, action items) is Hebrew, with English technical terms kept as spoken. Code, comments, commits, logs and CLI operator output are English.
- Transcript is strict verbatim: fillers, false starts, repetitions kept; no cleanup. Segments Whisper most likely invented are dropped (Task 11).
- Action items: `owner` and `due` only when stated in the call, else null — never guessed. Every item carries `source_ts` (`HH:MM:SS`).
- A summary failure never costs the transcript: invalid model output is retried once, then the call completes with "summary unavailable". LiteLLM outages are retried with backoff; a 4xx fails the job loudly.
- Retry policy: every failure counts an attempt and schedules `next_attempt_at` with backoff 30 s doubling, capped at 30 min. `TransientError` (transport error, HTTP 429, HTTP 5xx) never fails a job; anything else is `failed` after `MAX_ATTEMPTS = 3`.
- Dedup key is `(call_id, line_owner.extension)`, encoded as `job_key`.
- Retention: audio 90 days, text 365 days. "Older than N days" — the boundary day survives. Age counts from the later of `started_at` and `created_at`. Every deletion is audited.
- Every file the service writes goes through `.part` + rename.
- v2 refuses databases written by another schema version (`PRAGMA user_version` = 2); deployments point `paths.db_path` at a fresh file.
- `run`, `process` and `purge` hold the single-instance lock `<db_path parent>/jabberscribe.lock`.
- Windows host. Never put a literal U+FEFF in source: write it as `"\N{BYTE ORDER MARK}"`.
- Python 3.12. Line length 120; ruff rules `E, F, I, UP, B` must pass (`python -m ruff check .`).
- Every commit message ends with the trailer line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (pass it as a second `-m`).

## File Map

| File | Task | Responsibility |
|---|---|---|
| `jabberscribe/sidecar.py` | 8 (rewrite) | v2 sidecar: schema_version 2, UTC offset required, extension trimmed, BOM escape |
| `jabberscribe/audit.py` | 9 (rewrite) | Audit trail keyed by `job_key`, v2 action names |
| `jabberscribe/watcher.py` | 9 (rewrite) | Per-pair fault isolation, future-date quarantine, audited quarantine and duplicate discard |
| `jabberscribe/jobs.py` | 10 (rewrite) | `next_attempt_at`, due-aware `claim_next`, `claim`, `schedule_retry`, `requeue`, `reset_job`, `hand_over`, `scrub_sidecar`, schema version guard |
| `jabberscribe/llm.py` | 11 (rewrite) | `TransientError`, `post()` error classification, `/v1` suffix strip |
| `jabberscribe/stt.py` | 11 (rewrite) | Prompt order, hallucination filter, transient vs permanent errors |
| `jabberscribe/audio.py` | 11 (rewrite) | UTF-8 ffmpeg stderr, no `.part` left on failure |
| `jabberscribe/summarize.py` | 12 (rewrite) | User-role prompt, `max_tokens`, error classes, null content, map-reduce chunks |
| `jabberscribe/config.py` | 12, 17 (edit) | `summary.max_chunk_chars`; vocabulary path relative to the config file |
| `jabberscribe/output.py` | 13 (rewrite) | `write_atomic` retry on PermissionError, RTL Markdown, `timings` |
| `jabberscribe/group.py` | 14 (create) | Overlap-scoped groups, re-election (longer copy, failed primary), retried owner updates |
| `jabberscribe/pipeline.py` | 15 (create) | Stage orchestration, retry policy, `run_job`, timings, STT copy cleanup on FAILED |
| `jabberscribe/retention.py` | 16 (create) | Purge audio/text, scrub metadata, sweep STT leftovers, quarantine and inbox orphans |
| `jabberscribe/lock.py` | 17 (create) | Single-instance OS lock |
| `jabberscribe/cli.py` | 18 (create) | `doctor` (deep probes), `process`, `run`, `purge`, `retry`, `status` |
| `tests/conftest.py` | 9, 11 (edit) | `store`/`audit` fixtures; two-pitch `make_wav` |
| `config/jabberscribe.yaml`, `README.md`, `tests/test_live.py`, spec, original plan | 19 | Shipping config and docs |

---

### Task 8: Sidecar strictness

Fixes audit M1 (naive `started_at` crashes grouping), M3 (untrimmed extension), M4 (literal BOM in source), and §5 gaps (`schema_version` unchecked, missing `line_owner.user` silent).

**Files:**
- Modify (full rewrite): `jabberscribe/sidecar.py`
- Test (full rewrite): `tests/test_sidecar.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (unchanged names, stricter behavior): `SCHEMA_VERSION = 2`; `parse_sidecar(text: str) -> Sidecar` now raises `SidecarError` when `schema_version != 2` or `started_at` has no UTC offset, strips `line_owner.extension`, and logs a warning (logger `jabberscribe.sidecar`) containing `line_owner.user` when it is missing. `job_key`, `Party`, `Sidecar` are unchanged.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_sidecar.py` with:

```python
import json
from pathlib import Path

import pytest

import jabberscribe.sidecar as sidecar_module
from jabberscribe.sidecar import SidecarError, job_key, parse_sidecar


def _doc(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_version": 2,
        "call_id": "gcid-1",
        "conference_id": None,
        "line_owner": {"extension": "1042", "user": "meir", "display_name": "מאיר"},
        "parties": [{"extension": "2210", "display_name": "דנה"}],
        "kind": "call",
        "started_at": "2026-10-07T14:03:11+03:00",
        "ended_at": "2026-10-07T14:16:43+03:00",
        "duration_sec": 812,
        "audio": {"tracks": "dual", "sample_rate": 8000, "channels": 2},
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_parses_valid_sidecar() -> None:
    sidecar = parse_sidecar(_doc())

    assert sidecar.call_id == "gcid-1"
    assert sidecar.conference_id is None
    assert sidecar.line_owner.extension == "1042"
    assert sidecar.line_owner.user == "meir"
    assert sidecar.parties[0].display_name == "דנה"
    assert sidecar.kind == "call"
    assert sidecar.started_at == "2026-10-07T14:03:11+03:00"
    assert sidecar.ended_at == "2026-10-07T14:16:43+03:00"
    assert sidecar.duration_sec == 812
    assert sidecar.tracks == "dual"


def test_job_key_combines_call_and_line() -> None:
    assert parse_sidecar(_doc()).job_key == "gcid-1_1042"


def test_job_key_is_filesystem_safe() -> None:
    assert job_key("a:b/c\\d*e", "10 42") == "a-b-c-d-e_10-42"


def test_party_as_dict() -> None:
    owner = parse_sidecar(_doc()).line_owner

    assert owner.as_dict() == {"extension": "1042", "user": "meir", "display_name": "מאיר"}


def test_conference_id_is_parsed() -> None:
    sidecar = parse_sidecar(_doc(conference_id="conf-9", kind="conference"))

    assert sidecar.conference_id == "conf-9"
    assert sidecar.kind == "conference"


def test_leading_bom_is_tolerated() -> None:
    assert parse_sidecar("\N{BYTE ORDER MARK}" + _doc()).call_id == "gcid-1"


def test_unknown_fields_are_ignored() -> None:
    assert parse_sidecar(_doc(recorder_build="7.1")).call_id == "gcid-1"


def test_missing_parties_defaults_to_empty() -> None:
    payload = json.loads(_doc())
    del payload["parties"]

    assert parse_sidecar(json.dumps(payload)).parties == ()


def test_rejects_non_json() -> None:
    with pytest.raises(SidecarError, match="not valid JSON"):
        parse_sidecar("{nope")


def test_rejects_non_object() -> None:
    with pytest.raises(SidecarError, match="JSON object"):
        parse_sidecar("[]")


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"call_id": ""}, "call_id"),
        ({"call_id": None}, "call_id"),
        ({"line_owner": None}, "line_owner"),
        ({"line_owner": "1042"}, "line_owner"),
        ({"line_owner": {"user": "meir"}}, "line_owner.extension"),
        ({"started_at": "yesterday"}, "started_at"),
        ({"duration_sec": -1}, "duration_sec"),
        ({"duration_sec": True}, "duration_sec"),
        ({"duration_sec": "812"}, "duration_sec"),
        ({"audio": None}, "audio"),
        ({"audio": {"tracks": "quad"}}, "audio.tracks"),
        ({"kind": "webinar"}, "kind"),
        ({"parties": "everyone"}, "parties"),
        ({"parties": ["dana"]}, "parties"),
        ({"conference_id": 7}, "conference_id"),
    ],
)
def test_rejects_malformed(overrides: dict, fragment: str) -> None:
    with pytest.raises(SidecarError, match=fragment):
        parse_sidecar(_doc(**overrides))


@pytest.mark.parametrize("version", [None, 1, 3, "2"])
def test_rejects_other_schema_versions(version: object) -> None:
    with pytest.raises(SidecarError, match="schema_version"):
        parse_sidecar(_doc(schema_version=version))


def test_rejects_started_at_without_offset() -> None:
    with pytest.raises(SidecarError, match="UTC offset"):
        parse_sidecar(_doc(started_at="2026-10-07T14:03:11"))


def test_extension_is_stripped() -> None:
    sidecar = parse_sidecar(_doc(line_owner={"extension": " 1042 ", "user": "meir"}))

    assert sidecar.line_owner.extension == "1042"
    assert sidecar.job_key == "gcid-1_1042"


def test_missing_user_is_accepted_with_a_warning(caplog) -> None:
    sidecar = parse_sidecar(_doc(line_owner={"extension": "1042"}))

    assert sidecar.line_owner.user is None
    assert "line_owner.user" in caplog.text


def test_source_has_no_literal_bom() -> None:
    assert "\N{BYTE ORDER MARK}" not in Path(sidecar_module.__file__).read_text(encoding="utf-8")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_sidecar.py -q`
Expected: FAIL — `DID NOT RAISE <class 'jabberscribe.sidecar.SidecarError'>` for the schema_version and offset cases, `assert ' 1042 ' == '1042'`, `assert 'line_owner.user' in ''`, and `test_source_has_no_literal_bom`.

- [ ] **Step 3: Rewrite `jabberscribe/sidecar.py`**

```python
"""Sidecar metadata: the recorder's half of the drop contract.

Validation is deliberately narrow. Only call_id, line_owner.extension,
started_at, duration_sec, and audio.tracks are required -- the fields the
pipeline cannot work without. Everything else degrades.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2
VALID_TRACKS = ("dual", "mixed")
VALID_KINDS = ("call", "conference")

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


class SidecarError(ValueError):
    """The sidecar is unusable. The caller should quarantine the pair."""


def job_key(call_id: str, extension: str) -> str:
    """Identity of one recorded line's copy of a call.

    It names both the job row and the output folder, so it must be a valid
    Windows path segment. CUCM shares one call id across both ends of an
    internal call, which is why the line is part of the key.
    """
    return f"{_UNSAFE.sub('-', call_id)}_{_UNSAFE.sub('-', extension)}"


@dataclass(frozen=True)
class Party:
    extension: str | None = None
    user: str | None = None
    display_name: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"extension": self.extension, "user": self.user, "display_name": self.display_name}


@dataclass(frozen=True)
class Sidecar:
    call_id: str
    conference_id: str | None
    line_owner: Party
    parties: tuple[Party, ...]
    kind: str
    started_at: str
    ended_at: str | None
    duration_sec: int
    tracks: str
    raw: str

    @property
    def job_key(self) -> str:
        # parse_sidecar guarantees a non-empty line_owner.extension.
        return job_key(self.call_id, self.line_owner.extension or "")


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _require_nonempty_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SidecarError(f"{key} is required and must be a non-empty string")
    return value


def _parse_party(raw: object, where: str) -> Party:
    if not isinstance(raw, dict):
        raise SidecarError(f"{where} must be an object")
    return Party(
        extension=_opt_str(raw.get("extension")),
        user=_opt_str(raw.get("user")),
        display_name=_opt_str(raw.get("display_name")),
    )


def parse_sidecar(text: str) -> Sidecar:
    """Parse and validate a sidecar document.

    Raises SidecarError for anything the pipeline cannot work with.
    """
    # Strip a leading BOM. PowerShell, .NET, and Notepad all emit UTF-8 with a
    # BOM by default, so a recorder written in any of them would otherwise have
    # every one of its sidecars rejected.
    text = text.lstrip("\N{BYTE ORDER MARK}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SidecarError(f"sidecar is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SidecarError("sidecar must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SidecarError(f"schema_version must be {SCHEMA_VERSION}, got {data.get('schema_version')!r}")

    call_id = _require_nonempty_str(data, "call_id")
    started_at = _require_nonempty_str(data, "started_at")
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError as exc:
        raise SidecarError(f"started_at must be an ISO 8601 timestamp, got {started_at!r}") from exc
    if started.tzinfo is None:
        # Grouping and retention compare timestamps; naive and aware values cannot be compared.
        raise SidecarError(f"started_at must carry a UTC offset, got {started_at!r}")

    line_owner = _parse_party(data.get("line_owner"), "line_owner")
    extension = (line_owner.extension or "").strip()
    if not extension:
        raise SidecarError("line_owner.extension is required and must be a non-empty string")
    # " 1042" and "1042" are one line: they must give one job key and one owner.
    line_owner = Party(extension=extension, user=line_owner.user, display_name=line_owner.display_name)
    if not line_owner.user:
        log.warning("sidecar for call %s has no line_owner.user; the web app cannot attribute it", call_id)

    conference_id = data.get("conference_id")
    if conference_id is not None and (not isinstance(conference_id, str) or not conference_id.strip()):
        raise SidecarError("conference_id must be a non-empty string or null")

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

    raw_parties = data.get("parties") or []
    if not isinstance(raw_parties, list):
        raise SidecarError("parties must be a list")

    return Sidecar(
        call_id=call_id,
        conference_id=conference_id,
        line_owner=line_owner,
        parties=tuple(_parse_party(p, "each entry in parties") for p in raw_parties),
        kind=kind,
        started_at=started_at,
        ended_at=_opt_str(data.get("ended_at")),
        duration_sec=duration,
        tracks=tracks,
        raw=text,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_sidecar.py -q`
Expected: 33 passed.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (114 passed). The `make_sidecar` fixture already writes `schema_version: 2` and an offset `started_at`.

- [ ] **Step 6: Commit**

```bash
git add jabberscribe/sidecar.py tests/test_sidecar.py
git commit -m "fix(sidecar): require schema_version 2 and a UTC offset, trim extension" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Audited, fault-isolated ingest

Fixes audit I1 (one bad pair blocks all ingest and kills the service), I6 (future `started_at` from a recorder clock fault), M8 (duplicate discard and quarantine not audited), I4(b) (`recording.wav.part` left after a failed copy). Rewrites `audit.py` for v2 (keyed by `job_key`, the original Task 10 audit part).

**Files:**
- Modify (full rewrite): `jabberscribe/audit.py`, `jabberscribe/watcher.py`
- Modify: `tests/conftest.py` (add `store` and `audit` fixtures)
- Test (full rewrite): `tests/test_audit.py`, `tests/test_watcher.py`

**Interfaces:**
- Consumes: `parse_sidecar`, `Sidecar`, `SidecarError` (Task 8); `JobStore` (committed).
- Produces:
  - `audit`: constants `QUARANTINED`, `DISCARDED_DUPLICATE`, `SUPERSEDED`, `PURGED_AUDIO`, `PURGED_TEXT`, `PURGED_STT_AUDIO`, `PURGED_QUARANTINE`, `PURGED_ORPHAN`, `SCRUBBED_METADATA`; `AuditEntry(job_key, action, detail, actor, at)`; `AuditLog(db_path: Path, actor: str | None = None)` with `init_schema()`, `close()`, `record(job_key: str, action: str, detail: str = "") -> None`, `entries(job_key: str | None = None) -> list[AuditEntry]`.
  - `watcher`: `MAX_FUTURE = timedelta(days=1)`; `ScanResult(enqueued, quarantined, skipped, deferred)` (tuples; `deferred` holds stems of pairs left in the inbox after an `OSError`); `scan_once(cfg: Config, store: JobStore, audit: AuditLog, min_age_seconds: int | None = None) -> ScanResult` — **`audit` is a new required positional parameter**; `quarantine_pair(paths: list[Path], quarantine_dir: Path, reason: str, audit: AuditLog) -> None`; `find_ready_pairs` and `out_dir_for` unchanged.
  - fixtures `store(cfg) -> JobStore` and `audit(cfg) -> AuditLog` (actor `"test"`), both on `cfg.paths.db_path` with `init_schema()` done.

- [ ] **Step 1: Add the shared fixtures**

In `tests/conftest.py`, replace:

```python
from jabberscribe.config import Config
```

with:

```python
from jabberscribe.audit import AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import JobStore
```

and replace:

```python
@pytest.fixture
def make_wav() -> Callable[..., Path]:
```

with:

```python
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
```

- [ ] **Step 2: Write the failing tests**

Replace `tests/test_audit.py` with:

```python
from pathlib import Path

from jabberscribe.audit import PURGED_AUDIO, PURGED_TEXT, AuditLog
from jabberscribe.jobs import JobStore


def _log(tmp_path: Path, actor: str | None = "svc") -> AuditLog:
    log = AuditLog(tmp_path / "js.db", actor=actor)
    log.init_schema()
    return log


def test_records_in_order(tmp_path: Path) -> None:
    log = _log(tmp_path)

    log.record("k1", PURGED_AUDIO, "a")
    log.record("k1", PURGED_TEXT, "b")

    entries = log.entries("k1")
    assert [(e.job_key, e.action, e.detail, e.actor) for e in entries] == [
        ("k1", PURGED_AUDIO, "a", "svc"),
        ("k1", PURGED_TEXT, "b", "svc"),
    ]
    assert all(e.at for e in entries)


def test_filters_by_job_key(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.record("k1", PURGED_AUDIO)
    log.record("k2", PURGED_AUDIO)

    assert [e.job_key for e in log.entries("k2")] == ["k2"]
    assert len(log.entries()) == 2


def test_actor_defaults_to_os_account(tmp_path: Path) -> None:
    log = _log(tmp_path, actor=None)

    log.record("k1", PURGED_AUDIO)

    assert log.entries("k1")[0].actor


def test_shares_a_database_with_the_job_store(tmp_path: Path) -> None:
    """Audit rows and job rows live in one file so they cannot diverge."""
    store = JobStore(tmp_path / "js.db")
    store.init_schema()
    log = _log(tmp_path)

    store.create(
        job_key="k1",
        call_id="c1",
        conference_id=None,
        audio_path=Path("/a.wav"),
        out_dir=Path("/out/k1"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )
    log.record("k1", PURGED_AUDIO)

    assert store.get("k1") is not None
    assert len(log.entries("k1")) == 1


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    _log(tmp_path)
    log = _log(tmp_path)

    log.record("k1", PURGED_AUDIO)

    assert len(log.entries("k1")) == 1
```

Replace `tests/test_watcher.py` with:

```python
import shutil

from jabberscribe.audit import DISCARDED_DUPLICATE, QUARANTINED
from jabberscribe.jobs import QUEUED, WAITING
from jabberscribe.watcher import find_ready_pairs, scan_once


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


def test_future_mtime_does_not_defer_a_zero_min_age_pair(cfg, make_wav, make_sidecar) -> None:
    """A just-written file can report an mtime ahead of the clock on Windows."""
    audio = make_wav(cfg.paths.inbox / "a.wav")
    sidecar = make_sidecar(cfg.paths.inbox / "a.json")

    pairs = find_ready_pairs(cfg.paths.inbox, 0, now=audio.stat().st_mtime - 5.0)

    assert pairs == [(audio, sidecar)]


def test_scan_enqueues_into_dated_out_dir(cfg, store, audit, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="abc")

    result = scan_once(cfg, store, audit)

    assert result.enqueued == ("abc_1042",)
    job = store.get("abc_1042")
    assert job.status == QUEUED
    assert job.out_dir == cfg.paths.out_root / "2026" / "10" / "abc_1042"
    assert job.audio_path == job.out_dir / "recording.wav"
    assert job.audio_path.is_file()
    assert not (cfg.paths.inbox / "a.wav").exists()
    assert not (cfg.paths.inbox / "a.json").exists()


def test_conference_copy_is_enqueued_waiting(cfg, store, audit, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="leg", conference_id="conf-1")

    scan_once(cfg, store, audit)

    assert store.get("leg_1042").status == WAITING


def test_bom_encoded_sidecar_is_accepted(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A recorder written in PowerShell or .NET emits BOM'd JSON by default."""
    make_wav(cfg.paths.inbox / "a.wav")
    sidecar = make_sidecar(cfg.paths.inbox / "a.json", call_id="bom")
    sidecar.write_bytes(b"\xef\xbb\xbf" + sidecar.read_bytes())

    result = scan_once(cfg, store, audit)

    assert result.enqueued == ("bom_1042",)
    assert result.quarantined == ()


def test_scan_dedups_repeated_line_copy_and_audits_the_discard(cfg, store, audit, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="dup")
    scan_once(cfg, store, audit)

    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="dup")
    result = scan_once(cfg, store, audit)

    assert result.enqueued == ()
    assert result.skipped == ("dup_1042",)
    assert not (cfg.paths.inbox / "b.wav").exists()
    assert len(store.list_all()) == 1
    assert [e.action for e in audit.entries("dup_1042")] == [DISCARDED_DUPLICATE]
    assert "b.wav" in audit.entries("dup_1042")[0].detail


def test_same_call_on_two_lines_is_two_jobs(cfg, store, audit, make_wav, make_sidecar) -> None:
    """Both ends of an internal call are recorded; each line owner gets a copy."""
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="gc", extension="1042")
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="gc", extension="2210")

    result = scan_once(cfg, store, audit)

    assert sorted(result.enqueued) == ["gc_1042", "gc_2210"]


def test_scan_quarantines_invalid_sidecar_and_audits_it(cfg, store, audit, make_wav) -> None:
    make_wav(cfg.paths.inbox / "bad.wav")
    (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")

    result = scan_once(cfg, store, audit)

    assert result.quarantined == ("bad",)
    assert (cfg.paths.quarantine / "bad.wav").is_file()
    assert (cfg.paths.quarantine / "bad.json").is_file()
    assert "JSON" in (cfg.paths.quarantine / "bad.reason.txt").read_text(encoding="utf-8")
    assert store.list_all() == []
    entry = audit.entries("bad")[0]
    assert entry.action == QUARANTINED
    assert "bad.wav" in entry.detail


def test_non_utf8_sidecar_is_quarantined(cfg, store, audit, make_wav) -> None:
    make_wav(cfg.paths.inbox / "enc.wav")
    (cfg.paths.inbox / "enc.json").write_bytes(b'{"call_id": "\xff"}')

    assert scan_once(cfg, store, audit).quarantined == ("enc",)


def test_future_started_at_is_quarantined(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A recorder without NTP would otherwise create a call retention never purges."""
    make_wav(cfg.paths.inbox / "f.wav")
    make_sidecar(cfg.paths.inbox / "f.json", call_id="future", started_at="2099-01-01T00:00:00+00:00")

    result = scan_once(cfg, store, audit)

    assert result.quarantined == ("f",)
    assert "future" in (cfg.paths.quarantine / "f.reason.txt").read_text(encoding="utf-8")


def test_quarantine_does_not_collide_on_repeat(cfg, store, audit, make_wav) -> None:
    for _ in range(2):
        make_wav(cfg.paths.inbox / "bad.wav")
        (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")
        scan_once(cfg, store, audit)

    assert len(list(cfg.paths.quarantine.glob("bad*.wav"))) == 2


def test_scan_of_empty_inbox_is_harmless(cfg, store, audit) -> None:
    result = scan_once(cfg, store, audit)

    assert (result.enqueued, result.quarantined, result.skipped, result.deferred) == ((), (), (), ())


def test_scan_honours_min_age_override(cfg, store, audit, make_wav, make_sidecar) -> None:
    """`process` needs to bypass the settling delay for a one-shot run."""
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="ovr")

    assert scan_once(cfg, store, audit, min_age_seconds=3600).enqueued == ()
    assert scan_once(cfg, store, audit, min_age_seconds=0).enqueued == ("ovr_1042",)


def test_failed_copy_keeps_the_recording_for_retry(cfg, store, audit, make_wav, make_sidecar, monkeypatch) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="abc")
    monkeypatch.setattr("jabberscribe.watcher.shutil.copyfile", _raise_disk_full)

    result = scan_once(cfg, store, audit)

    assert result.deferred == ("a",)
    assert store.list_all() == []
    assert (cfg.paths.inbox / "a.wav").exists()
    assert (cfg.paths.inbox / "a.json").exists()

    monkeypatch.undo()
    result = scan_once(cfg, store, audit)

    assert result.enqueued == ("abc_1042",)
    assert store.get("abc_1042").audio_path.is_file()
    assert not (cfg.paths.inbox / "a.wav").exists()
    assert not (cfg.paths.inbox / "a.json").exists()


def test_one_unreadable_pair_does_not_block_the_rest(cfg, store, audit, make_wav, make_sidecar, monkeypatch) -> None:
    """A locked file sorts first; everything after it must still be ingested."""
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="locked")
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="fine")
    real_copy = shutil.copyfile

    def copy_unless_locked(src, dst, *args, **kwargs):
        if str(src).endswith("a.wav"):
            raise PermissionError("held by antivirus")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr("jabberscribe.watcher.shutil.copyfile", copy_unless_locked)

    result = scan_once(cfg, store, audit)

    assert result.deferred == ("a",)
    assert result.enqueued == ("fine_1042",)


def test_no_partial_recording_is_left(cfg, store, audit, make_wav, make_sidecar, monkeypatch) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json")
    scan_once(cfg, store, audit)
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="c2")
    monkeypatch.setattr("jabberscribe.watcher.shutil.copyfile", _write_half_then_fail)

    scan_once(cfg, store, audit)

    assert list(cfg.paths.out_root.rglob("*.part")) == []


def _raise_disk_full(*args, **kwargs):
    raise OSError("disk full")


def _write_half_then_fail(src, dst, *args, **kwargs):
    with open(dst, "wb") as fh:
        fh.write(b"RIFF")
    raise OSError("disk full")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_audit.py tests/test_watcher.py -q`
Expected: FAIL — `ImportError: cannot import name 'PURGED_TEXT' from 'jabberscribe.audit'` and `cannot import name 'DISCARDED_DUPLICATE'`.

- [ ] **Step 4: Rewrite `jabberscribe/audit.py`**

```python
"""Append-only audit trail.

A record-everything recording policy guarantees somebody will eventually ask
what happened to a given call. That answer has to exist, so every deletion and
every quarantine writes a row here. Rows are never updated or deleted.

`job_key` names the call when there is one. For files that never became a job
(quarantined pairs, inbox orphans) it is the file name.
"""

from __future__ import annotations

import getpass
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

QUARANTINED = "quarantined"
DISCARDED_DUPLICATE = "discarded_duplicate"
SUPERSEDED = "superseded"
PURGED_AUDIO = "purged_audio"
PURGED_TEXT = "purged_text"
PURGED_STT_AUDIO = "purged_stt_audio"
PURGED_QUARANTINE = "purged_quarantine"
PURGED_ORPHAN = "purged_orphan"
SCRUBBED_METADATA = "scrubbed_metadata"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  job_key TEXT NOT NULL,
  action  TEXT NOT NULL,
  detail  TEXT,
  actor   TEXT NOT NULL,
  at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_log (job_key, id);
"""


@dataclass(frozen=True)
class AuditEntry:
    job_key: str
    action: str
    detail: str
    actor: str
    at: str


def _default_actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - unusual service accounts
        return "unknown"


class AuditLog:
    def __init__(self, db_path: Path, actor: str | None = None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._actor = actor or _default_actor()

    def close(self) -> None:
        self._conn.close()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)

    def record(self, job_key: str, action: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO audit_log (job_key, action, detail, actor, at) VALUES (?, ?, ?, ?, ?)",
            (job_key, action, detail, self._actor, datetime.now(UTC).isoformat(timespec="seconds")),
        )

    def entries(self, job_key: str | None = None) -> list[AuditEntry]:
        if job_key is None:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM audit_log WHERE job_key = ? ORDER BY id", (job_key,)).fetchall()
        return [AuditEntry(r["job_key"], r["action"], r["detail"] or "", r["actor"], r["at"]) for r in rows]
```

- [ ] **Step 5: Rewrite `jabberscribe/watcher.py`**

```python
"""Inbox watcher: the service's only entry point for new work.

The readiness rule comes from the drop contract -- the recorder writes audio
first (as .part, then renames) and the sidecar last, so a sidecar's presence
proves the audio is complete. A min-age guard catches a recorder that died
mid-write, where the sidecar exists but nothing is finished.

One bad pair must never block the rest of the inbox: a filesystem error on a
pair is logged and the pair is left for the next scan.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audit import DISCARDED_DUPLICATE, QUARANTINED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import JobStore
from jabberscribe.sidecar import Sidecar, SidecarError, parse_sidecar

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".ogg")

#: A recorder clock this far ahead is wrong, and retention would never purge the call.
MAX_FUTURE = timedelta(days=1)


@dataclass(frozen=True)
class ScanResult:
    enqueued: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    #: Stems of pairs that hit a filesystem error and stay in the inbox for the next scan.
    deferred: tuple[str, ...] = ()


def find_ready_pairs(inbox: Path, min_age_seconds: int, now: float | None = None) -> list[tuple[Path, Path]]:
    """Return (audio, sidecar) pairs that are complete and settled."""
    current = time.time() if now is None else now
    pairs: list[tuple[Path, Path]] = []
    for sidecar in sorted(inbox.glob("*.json")):
        audio = next((c for suffix in AUDIO_SUFFIXES if (c := sidecar.with_suffix(suffix)).is_file()), None)
        if audio is None:
            log.debug("sidecar without audio, skipping: %s", sidecar)
            continue
        try:
            youngest = max(audio.stat().st_mtime, sidecar.stat().st_mtime)
        except OSError as exc:
            log.warning("cannot stat %s, skipping this scan: %s", sidecar.stem, exc)
            continue
        # Clamp at zero: a just-written file can report an mtime slightly ahead
        # of time.time() (filesystem and clock resolution differ on Windows),
        # which would make a negative age look younger than any positive
        # threshold and defer a pair that min_age_seconds=0 means to take now.
        age = max(0.0, current - youngest)
        if age < min_age_seconds:
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


def quarantine_pair(paths: list[Path], quarantine_dir: Path, reason: str, audit: AuditLog) -> None:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    stem = paths[0].stem
    moved: list[str] = []
    for path in paths:
        if path.exists():
            target = _unique_target(quarantine_dir, path.name)
            shutil.move(str(path), str(target))
            moved.append(target.name)
    _unique_target(quarantine_dir, f"{stem}.reason.txt").write_text(reason, encoding="utf-8")
    audit.record(stem, QUARANTINED, f"{', '.join(moved)}: {reason}")
    log.warning("quarantined %s: %s", stem, reason)


def out_dir_for(out_root: Path, sidecar: Sidecar) -> Path:
    """out_root/<YYYY>/<MM>/<job_key>, dated by when the call started."""
    started = datetime.fromisoformat(sidecar.started_at)
    return out_root / f"{started:%Y}" / f"{started:%m}" / sidecar.job_key


def _read_sidecar(path: Path, now: datetime) -> Sidecar:
    # utf-8-sig tolerates a BOM and is identical to utf-8 without one.
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SidecarError(f"sidecar is not UTF-8: {exc}") from exc
    sidecar = parse_sidecar(text)
    if datetime.fromisoformat(sidecar.started_at) > now + MAX_FUTURE:
        raise SidecarError(f"started_at {sidecar.started_at} is in the future; check the recorder clock")
    return sidecar


def _ingest(cfg: Config, store: JobStore, audit: AuditLog, audio: Path, sidecar: Sidecar, sidecar_path: Path) -> bool:
    """Move one valid pair into the store. Returns False when it was a duplicate."""
    if store.get(sidecar.job_key) is not None:
        # Already known. Drop the duplicate rather than reprocess it.
        log.info("duplicate %s, discarding inbox copy", sidecar.job_key)
        audio.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        audit.record(sidecar.job_key, DISCARDED_DUPLICATE, f"{audio.name}, {sidecar_path.name}")
        return False

    out_dir = out_dir_for(cfg.paths.out_root, sidecar)
    stored_audio = out_dir / f"recording{audio.suffix}"
    # Every step is safe to repeat: a crash before create() leaves the inbox
    # pair in place and the next scan redoes the copy. The recording is never
    # deleted from the inbox before a job row references a complete copy.
    out_dir.mkdir(parents=True, exist_ok=True)
    part = stored_audio.with_suffix(stored_audio.suffix + ".part")
    try:
        shutil.copyfile(audio, part)
        part.replace(stored_audio)
    except OSError:
        part.unlink(missing_ok=True)
        raise
    store.create(
        job_key=sidecar.job_key,
        call_id=sidecar.call_id,
        conference_id=sidecar.conference_id,
        audio_path=stored_audio,
        out_dir=out_dir,
        sidecar_json=sidecar.raw,
        started_at=sidecar.started_at,
        duration_sec=sidecar.duration_sec,
    )
    audio.unlink(missing_ok=True)
    sidecar_path.unlink(missing_ok=True)
    return True


def scan_once(cfg: Config, store: JobStore, audit: AuditLog, min_age_seconds: int | None = None) -> ScanResult:
    """Process every ready pair in the inbox exactly once.

    `min_age_seconds` overrides the configured settling delay. The `process`
    command passes 0: a human handing us one file is not a race with a recorder.
    """
    min_age = cfg.watcher.min_age_seconds if min_age_seconds is None else min_age_seconds
    now = datetime.now(UTC)
    enqueued: list[str] = []
    quarantined: list[str] = []
    skipped: list[str] = []
    deferred: list[str] = []

    for audio, sidecar_path in find_ready_pairs(cfg.paths.inbox, min_age):
        try:
            try:
                sidecar = _read_sidecar(sidecar_path, now)
            except SidecarError as exc:
                quarantine_pair([audio, sidecar_path], cfg.paths.quarantine, str(exc), audit)
                quarantined.append(sidecar_path.stem)
                continue
            if _ingest(cfg, store, audit, audio, sidecar, sidecar_path):
                enqueued.append(sidecar.job_key)
                log.info("enqueued %s", sidecar.job_key)
            else:
                skipped.append(sidecar.job_key)
        except OSError as exc:
            # A locked file (AV scanner, indexer) or a bad ACL: leave the pair and move on.
            log.error("cannot ingest %s, will retry next scan: %s", sidecar_path.stem, exc)
            deferred.append(sidecar_path.stem)

    return ScanResult(tuple(enqueued), tuple(quarantined), tuple(skipped), tuple(deferred))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_audit.py tests/test_watcher.py -q`
Expected: 24 passed.

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (116 passed).

- [ ] **Step 8: Commit**

```bash
git add jabberscribe/audit.py jabberscribe/watcher.py tests/conftest.py tests/test_audit.py tests/test_watcher.py
git commit -m "fix(ingest): isolate bad pairs, quarantine future dates, audit quarantine and discards" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Job store retry scheduling and schema guard

Fixes the store half of audit C3 (no backoff, head-of-line blocking) and M16 (a reused older database is accepted silently), and adds the primitives grouping (Task 14), the pipeline (Task 15), retention (Task 16) and the CLI (Task 18) need.

**Files:**
- Modify (full rewrite): `jabberscribe/jobs.py`
- Test (full rewrite): `tests/test_jobs.py`

**Interfaces:**
- Consumes: nothing new.
- Produces (everything from the committed store, plus):
  - constants `SCHEMA_VERSION = 2`, `SCRUBBED_SIDECAR = "{}"`; `SchemaError(RuntimeError)`; `iso(moment: datetime) -> str` (UTC, seconds); `utcnow() -> str` (unchanged format).
  - `Job` gains a last field `next_attempt_at: str | None`.
  - `JobStore.init_schema()` raises `SchemaError` when a `jobs` table exists with `PRAGMA user_version != 2`, then sets `user_version = 2`.
  - `claim_next(now: datetime | None = None) -> Job | None` — skips rows whose `next_attempt_at > now`.
  - `claim(job_key: str) -> Job | None` — marks one QUEUED/RUNNING job RUNNING regardless of `next_attempt_at`; None otherwise.
  - `schedule_retry(job_key: str, at: datetime) -> None` — status QUEUED, `next_attempt_at = iso(at)`.
  - `requeue(job_key: str) -> bool` — FAILED with `grouped_into IS NULL` → QUEUED, attempts 0, `next_attempt_at` NULL.
  - `reset_job(job_key: str) -> None` — QUEUED, stage QUEUED, `grouped_into`/`last_error`/`next_attempt_at` NULL, attempts 0.
  - `hand_over(old_primary: str, new_primary: str) -> None` — re-points every member of `old_primary` (except `new_primary`) to `new_primary`; `old_primary` gets `grouped_into = new_primary` and becomes GROUPED, or stays FAILED if it was FAILED.
  - `scrub_sidecar(job_key: str) -> bool` — sets `sidecar_json = SCRUBBED_SIDECAR`; False if already scrubbed.
  - `conference_ids_to_settle() -> list[str]` — conferences with a WAITING copy, or with a FAILED primary (`grouped_into IS NULL`) that has GROUPED members.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_jobs.py` with:

```python
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jabberscribe.jobs import (
    DONE,
    FAILED,
    GROUPED,
    QUEUED,
    RUNNING,
    SCRUBBED_SIDECAR,
    STAGE_ORDER,
    WAITING,
    JobStore,
    SchemaError,
    next_stage,
)


def _store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "js.db")
    store.init_schema()
    return store


def _create(store: JobStore, key: str = "c1_1042", *, conference_id: str | None = None) -> bool:
    return store.create(
        job_key=key,
        call_id=key.split("_")[0],
        conference_id=conference_id,
        audio_path=Path(f"/out/{key}/recording.wav"),
        out_dir=Path(f"/out/{key}"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=812,
    )


def test_create_then_get_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert _create(store) is True

    job = store.get("c1_1042")
    assert job is not None
    assert job.call_id == "c1"
    assert job.status == QUEUED
    assert job.stage == QUEUED
    assert job.audio_path == Path("/out/c1_1042/recording.wav")
    assert job.out_dir == Path("/out/c1_1042")
    assert job.conference_id is None
    assert job.grouped_into is None
    assert job.attempts == 0
    assert job.created_at


def test_duplicate_job_key_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    assert _create(store) is False
    assert len(store.list_all()) == 1


def test_conference_copy_starts_waiting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, conference_id="conf-1")

    assert store.get("c1_1042").status == WAITING


def test_claim_next_marks_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1")
    _create(store, "b_2")

    job = store.claim_next()

    assert job.job_key == "a_1"
    assert job.status == RUNNING


def test_claim_next_skips_waiting_grouped_and_done(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "w_1", conference_id="conf-1")
    _create(store, "p_2", conference_id="conf-1")
    store.set_status("p_2", QUEUED)
    store.group_into("w_1", "p_2")
    store.set_status("p_2", DONE)

    assert store.claim_next() is None


def test_running_job_is_reclaimed_after_crash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.claim_next()
    store.close()

    reopened = JobStore(tmp_path / "js.db")

    assert reopened.claim_next().job_key == "c1_1042"


def test_complete_stage_records_checkpoint(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    store.complete_stage("c1_1042", "stt")

    assert store.get("c1_1042").stage == "stt"


def test_record_attempt_counts_and_keeps_last_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    assert store.record_attempt("c1_1042", "first") == 1
    assert store.record_attempt("c1_1042", "second") == 2
    assert store.get("c1_1042").last_error == "second"


def test_record_attempt_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        _store(tmp_path).record_attempt("nope", "x")


def test_next_stage_walks_fixed_order() -> None:
    walked = []
    stage = QUEUED
    while (stage := next_stage(stage)) is not None:
        walked.append(stage)

    assert tuple(walked) == STAGE_ORDER == ("audio", "stt", "summarize", "output")


def test_next_stage_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="publish"):
        next_stage("publish")


def test_group_into_and_members(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")
    store.set_status("a_1", QUEUED)

    store.group_into("b_2", "a_1")

    member = store.get("b_2")
    assert member.status == GROUPED
    assert member.grouped_into == "a_1"
    assert [m.job_key for m in store.members("a_1")] == ["b_2"]


def test_waiting_conference_ids_are_distinct(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")
    _create(store, "c_3", conference_id="conf-2")
    _create(store, "d_4")

    assert store.waiting_conference_ids() == ["conf-1", "conf-2"]


def test_conference_jobs_in_arrival_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")

    assert [j.job_key for j in store.conference_jobs("conf-1")] == ["a_1", "b_2"]


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def test_failed_job_is_not_claimed_before_it_is_due(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1")
    _create(store, "b_2")
    store.schedule_retry("a_1", NOW + timedelta(seconds=30))

    claimed = store.claim_next(NOW)

    assert claimed.job_key == "b_2"
    assert store.get("a_1").status == QUEUED
    assert store.get("a_1").next_attempt_at == "2026-10-07T12:00:30+00:00"


def test_failed_job_is_claimed_once_due(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1")
    store.schedule_retry("a_1", NOW + timedelta(seconds=30))

    assert store.claim_next(NOW + timedelta(seconds=30)).job_key == "a_1"


def test_claim_takes_a_specific_job_even_if_not_due(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1")
    _create(store, "b_2")
    store.schedule_retry("b_2", NOW + timedelta(hours=1))

    assert store.claim("b_2").status == RUNNING
    assert store.get("a_1").status == QUEUED


def test_claim_refuses_a_finished_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.set_status("c1_1042", DONE)

    assert store.claim("c1_1042") is None


def test_requeue_resets_attempts_of_a_failed_primary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.record_attempt("c1_1042", "boom")
    store.schedule_retry("c1_1042", NOW + timedelta(hours=1))
    store.set_status("c1_1042", FAILED)

    assert store.requeue("c1_1042") is True

    job = store.get("c1_1042")
    assert (job.status, job.attempts, job.next_attempt_at) == (QUEUED, 0, None)


def test_requeue_refuses_jobs_that_did_not_fail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    assert store.requeue("c1_1042") is False
    assert store.requeue("missing") is False


def test_reset_job_starts_a_copy_from_scratch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")
    store.group_into("b_2", "a_1")
    store.complete_stage("b_2", "output")
    store.record_attempt("b_2", "x")

    store.reset_job("b_2")

    job = store.get("b_2")
    assert (job.status, job.stage, job.grouped_into, job.attempts, job.last_error) == (QUEUED, QUEUED, None, 0, None)


def test_hand_over_moves_members_and_demotes_the_old_primary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for key in ("a_1", "b_2", "c_3"):
        _create(store, key, conference_id="conf-1")
    store.set_status("a_1", DONE)
    store.group_into("b_2", "a_1")
    store.group_into("c_3", "a_1")

    store.hand_over("a_1", "c_3")
    store.reset_job("c_3")

    assert store.get("a_1").status == GROUPED
    assert {m.job_key for m in store.members("c_3")} == {"a_1", "b_2"}
    assert store.get("c_3").grouped_into is None


def test_hand_over_keeps_a_failed_primary_failed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "a_1", conference_id="conf-1")
    _create(store, "b_2", conference_id="conf-1")
    store.set_status("a_1", FAILED)

    store.hand_over("a_1", "b_2")

    assert (store.get("a_1").status, store.get("a_1").grouped_into) == (FAILED, "b_2")
    assert store.requeue("a_1") is False


def test_conference_ids_to_settle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "w_1", conference_id="waiting")
    _create(store, "f_1", conference_id="failed")
    _create(store, "m_2", conference_id="failed")
    store.set_status("f_1", FAILED)
    store.group_into("m_2", "f_1")
    _create(store, "x_1", conference_id="failed-alone")
    store.set_status("x_1", FAILED)
    _create(store, "d_1", conference_id="done")
    store.set_status("d_1", DONE)

    assert store.conference_ids_to_settle() == ["failed", "waiting"]


def test_scrub_sidecar_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(
        job_key="c1_1042",
        call_id="c1",
        conference_id=None,
        audio_path=Path("/out/c1_1042/recording.wav"),
        out_dir=Path("/out/c1_1042"),
        sidecar_json='{"call_id": "c1", "parties": [{"display_name": "דנה"}]}',
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=812,
    )

    assert store.scrub_sidecar("c1_1042") is True
    assert store.scrub_sidecar("c1_1042") is False
    assert store.get("c1_1042").sidecar_json == SCRUBBED_SIDECAR


def test_init_schema_refuses_an_older_database(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "js.db")
    conn.execute("CREATE TABLE jobs (call_id TEXT PRIMARY KEY)")
    conn.close()

    with pytest.raises(SchemaError, match="fresh file"):
        JobStore(tmp_path / "js.db").init_schema()


def test_init_schema_accepts_its_own_database_again(tmp_path: Path) -> None:
    _store(tmp_path).close()

    _store(tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_jobs.py -q`
Expected: FAIL — `ImportError: cannot import name 'SCRUBBED_SIDECAR' from 'jabberscribe.jobs'`.

- [ ] **Step 3: Rewrite `jabberscribe/jobs.py`**

```python
"""SQLite-backed job store.

This is the queue seam. At this volume a serial worker over a SQLite table is
the right amount of machinery; scaling out means replacing this module with a
broker and changing nothing else.

A job is one recorded line's copy of a call, keyed by job_key. Every stage
checkpoints here, so a crashed worker resumes at the next incomplete stage
instead of re-transcribing. A job that failed waits in QUEUED until its
next_attempt_at; claim_next skips it until then.
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
#: A conference copy held until its group settles (see group.py).
WAITING = "waiting"
#: A conference copy whose meeting is processed by another job, its primary.
GROUPED = "grouped"

STAGE_ORDER: tuple[str, ...] = ("audio", "stt", "summarize", "output")

#: Stored in PRAGMA user_version. A database written by any other version is refused.
SCHEMA_VERSION = 2

#: What a scrubbed row keeps of its sidecar once the text retention has passed.
SCRUBBED_SIDECAR = "{}"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_key         TEXT PRIMARY KEY,
  call_id         TEXT NOT NULL,
  conference_id   TEXT,
  status          TEXT NOT NULL,
  stage           TEXT NOT NULL,
  audio_path      TEXT NOT NULL,
  out_dir         TEXT NOT NULL,
  sidecar_json    TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  duration_sec    INTEGER NOT NULL,
  grouped_into    TEXT REFERENCES jobs (job_key),
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  next_attempt_at TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_conference ON jobs (conference_id);
CREATE INDEX IF NOT EXISTS idx_jobs_grouped_into ON jobs (grouped_into);
"""


class SchemaError(RuntimeError):
    """The database was written by another JabberScribe version."""


def iso(moment: datetime) -> str:
    """The one timestamp format the store writes, so stored values compare as strings."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def utcnow() -> str:
    return iso(datetime.now(UTC))


def next_stage(stage: str) -> str | None:
    """Return the stage to run after `stage`, or None when the job is finished."""
    if stage == QUEUED:
        return STAGE_ORDER[0]
    if stage not in STAGE_ORDER:
        raise ValueError(f"unknown stage {stage!r}; stages are {STAGE_ORDER}")
    index = STAGE_ORDER.index(stage)
    return STAGE_ORDER[index + 1] if index + 1 < len(STAGE_ORDER) else None


@dataclass(frozen=True)
class Job:
    job_key: str
    call_id: str
    conference_id: str | None
    status: str
    stage: str
    audio_path: Path
    out_dir: Path
    sidecar_json: str
    started_at: str
    duration_sec: int
    grouped_into: str | None
    attempts: int
    last_error: str | None
    created_at: str
    next_attempt_at: str | None


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_key=row["job_key"],
        call_id=row["call_id"],
        conference_id=row["conference_id"],
        status=row["status"],
        stage=row["stage"],
        audio_path=Path(row["audio_path"]),
        out_dir=Path(row["out_dir"]),
        sidecar_json=row["sidecar_json"],
        started_at=row["started_at"],
        duration_sec=row["duration_sec"],
        grouped_into=row["grouped_into"],
        attempts=row["attempts"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        next_attempt_at=row["next_attempt_at"],
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
        """Create the tables, or refuse a database written by another version.

        CREATE TABLE IF NOT EXISTS would silently keep an older jobs table and
        fail later with a cryptic column error, so the version is checked first.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        has_jobs = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()
        if has_jobs and version != SCHEMA_VERSION:
            raise SchemaError(
                f"database schema version {version} is not {SCHEMA_VERSION}; point paths.db_path at a fresh file"
            )
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def create(
        self,
        *,
        job_key: str,
        call_id: str,
        conference_id: str | None,
        audio_path: Path,
        out_dir: Path,
        sidecar_json: str,
        started_at: str,
        duration_sec: int,
    ) -> bool:
        """Insert a new job. Returns False if `job_key` is already known.

        This is the deduplication point: a recorder that drops the same line's
        copy twice produces one job and one output. Conference copies start
        WAITING so grouping can pick one of them; everything else is QUEUED.
        """
        now = utcnow()
        status = WAITING if conference_id else QUEUED
        try:
            self._conn.execute(
                "INSERT INTO jobs (job_key, call_id, conference_id, status, stage, audio_path, out_dir,"
                " sidecar_json, started_at, duration_sec, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    job_key,
                    call_id,
                    conference_id,
                    status,
                    QUEUED,
                    str(audio_path),
                    str(out_dir),
                    sidecar_json,
                    started_at,
                    duration_sec,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def get(self, job_key: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE job_key = ?", (job_key,)).fetchone()
        return _row_to_job(row) if row else None

    def claim_next(self, now: datetime | None = None) -> Job | None:
        """Claim the oldest runnable job that is due and mark it running.

        A job whose next_attempt_at is still in the future is skipped, not
        waited for, so one failing job never blocks the jobs behind it.
        `running` rows are claimable because a row left running belongs to a
        crashed worker; its checkpointed stage tells us where to resume. The
        single-instance lock (lock.py) makes that safe.
        """
        due = iso(now or datetime.now(UTC))
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE status IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
            " ORDER BY created_at, rowid LIMIT 1",
            (QUEUED, RUNNING, due),
        ).fetchone()
        if row is None:
            return None
        self.set_status(row["job_key"], RUNNING)
        return self.get(row["job_key"])

    def claim(self, job_key: str) -> Job | None:
        """Claim one specific job now, due or not. Returns None unless it is QUEUED or RUNNING."""
        cur = self._conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE job_key = ? AND status IN (?, ?)",
            (RUNNING, utcnow(), job_key, QUEUED, RUNNING),
        )
        return self.get(job_key) if cur.rowcount else None

    def complete_stage(self, job_key: str, stage: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET stage = ?, updated_at = ? WHERE job_key = ?",
            (stage, utcnow(), job_key),
        )

    def set_status(self, job_key: str, status: str, last_error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, last_error = COALESCE(?, last_error), updated_at = ? WHERE job_key = ?",
            (status, last_error, utcnow(), job_key),
        )

    def record_attempt(self, job_key: str, error: str) -> int:
        cur = self._conn.execute(
            "UPDATE jobs SET attempts = attempts + 1, last_error = ?, updated_at = ?"
            " WHERE job_key = ? RETURNING attempts",
            (error, utcnow(), job_key),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"unknown job_key: {job_key}")
        return int(row["attempts"])

    def schedule_retry(self, job_key: str, at: datetime) -> None:
        """Put a failed job back in the queue, not to be claimed before `at`."""
        self._conn.execute(
            "UPDATE jobs SET status = ?, next_attempt_at = ?, updated_at = ? WHERE job_key = ?",
            (QUEUED, iso(at), utcnow(), job_key),
        )

    def requeue(self, job_key: str) -> bool:
        """Give a FAILED job a fresh set of attempts. Returns False if it is not a FAILED primary.

        A FAILED job that was handed over to another copy (grouped_into set)
        is not requeued: its conference already has a primary.
        """
        cur = self._conn.execute(
            "UPDATE jobs SET status = ?, attempts = 0, next_attempt_at = NULL, updated_at = ?"
            " WHERE job_key = ? AND status = ? AND grouped_into IS NULL",
            (QUEUED, utcnow(), job_key, FAILED),
        )
        return cur.rowcount == 1

    def reset_job(self, job_key: str) -> None:
        """Make a conference copy the primary from scratch: QUEUED, no stage done, no attempts."""
        self._conn.execute(
            "UPDATE jobs SET status = ?, stage = ?, grouped_into = NULL, attempts = 0, last_error = NULL,"
            " next_attempt_at = NULL, updated_at = ? WHERE job_key = ?",
            (QUEUED, QUEUED, utcnow(), job_key),
        )

    def hand_over(self, old_primary: str, new_primary: str) -> None:
        """Move a conference from one primary to another.

        Every member follows, and the old primary becomes a member too: GROUPED,
        or still FAILED if it failed, so it is never elected again.
        """
        now = utcnow()
        self._conn.execute(
            "UPDATE jobs SET grouped_into = ?, updated_at = ? WHERE grouped_into = ? AND job_key != ?",
            (new_primary, now, old_primary, new_primary),
        )
        self._conn.execute(
            "UPDATE jobs SET grouped_into = ?, status = CASE WHEN status = ? THEN ? ELSE ? END, updated_at = ?"
            " WHERE job_key = ?",
            (new_primary, FAILED, FAILED, GROUPED, now, old_primary),
        )

    def scrub_sidecar(self, job_key: str) -> bool:
        """Drop the call metadata of a row past text retention. Returns False if already scrubbed."""
        cur = self._conn.execute(
            "UPDATE jobs SET sidecar_json = ?, updated_at = ? WHERE job_key = ? AND sidecar_json != ?",
            (SCRUBBED_SIDECAR, utcnow(), job_key, SCRUBBED_SIDECAR),
        )
        return cur.rowcount == 1

    def list_all(self) -> list[Job]:
        rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at, rowid").fetchall()
        return [_row_to_job(r) for r in rows]

    def list_by_status(self, status: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at, rowid", (status,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def waiting_conference_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT conference_id FROM jobs WHERE status = ? ORDER BY conference_id", (WAITING,)
        ).fetchall()
        return [r["conference_id"] for r in rows]

    def conference_ids_to_settle(self) -> list[str]:
        """Conferences with a waiting copy, or with a FAILED primary that still has members to elect."""
        rows = self._conn.execute(
            "SELECT DISTINCT conference_id FROM jobs j WHERE conference_id IS NOT NULL AND (status = ?"
            " OR (status = ? AND grouped_into IS NULL"
            " AND EXISTS (SELECT 1 FROM jobs m WHERE m.grouped_into = j.job_key AND m.status = ?)))"
            " ORDER BY conference_id",
            (WAITING, FAILED, GROUPED),
        ).fetchall()
        return [r["conference_id"] for r in rows]

    def conference_jobs(self, conference_id: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE conference_id = ? ORDER BY created_at, rowid", (conference_id,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def group_into(self, job_key: str, primary_key: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET status = ?, grouped_into = ?, updated_at = ? WHERE job_key = ?",
            (GROUPED, primary_key, utcnow(), job_key),
        )

    def members(self, primary_key: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE grouped_into = ? ORDER BY created_at, rowid", (primary_key,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_jobs.py -q`
Expected: 27 passed.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (129 passed).

- [ ] **Step 6: Commit**

```bash
git add jabberscribe/jobs.py tests/test_jobs.py
git commit -m "feat(jobs): due-aware claiming, retry scheduling, hand-over, schema version guard" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Error classification and transcription hygiene

Fixes the STT half of audit C3 (an outage must not FAIL calls), I10 (prompt order; Whisper hallucinations and prompt echo kept), M15 (`base_url` ending in `/v1`), M5 (ffmpeg stderr decoded with the locale code page), I4(b) (`stt.ogg.part` left after a failed encode), M11 (audio tests transcode digital silence).

**Files:**
- Modify (full rewrite): `jabberscribe/llm.py`, `jabberscribe/stt.py`, `jabberscribe/audio.py`
- Modify: `tests/conftest.py` (the `make_wav` sample loop)
- Test (full rewrite): `tests/test_llm.py`, `tests/test_stt.py`, `tests/test_audio.py`

**Interfaces:**
- Consumes: `LiteLLMConfig` (committed).
- Produces:
  - `llm.TransientError(RuntimeError)`; `llm.post(client: httpx.Client, path: str, **kwargs: Any) -> httpx.Response` — raises `TransientError` on `httpx.TransportError`, HTTP 429 and HTTP ≥500; raises `httpx.HTTPStatusError` on other 4xx; returns the response otherwise. `make_client` unchanged in signature, strips a trailing `/` and `/v1` from `base_url`.
  - `stt`: `SttError` now means permanent; `LiteLLMTranscriber.transcribe(audio: Path) -> list[Segment]` raises `TransientError` (from `llm`) for outages and `SttError` for everything else. `build_prompt(vocabulary)` returns `f"{vocabulary} {FILLER_PROMPT}"` (filler last). New: `is_hallucination(raw: dict, text: str, prompt: str) -> bool`; constants `MAX_COMPRESSION_RATIO = 2.4`, `NO_SPEECH_PROB = 0.6`, `MIN_AVG_LOGPROB = -1.0`, `MIN_ECHO_CHARS`. `Segment`, `Transcriber`, `FILLER_PROMPT`, `load_vocabulary`, `format_ts` unchanged.
  - `audio`: unchanged interface (`STT_FILENAME`, `prepare_for_stt`, `AudioError`).
  - fixture `make_wav`: channel *n* carries a sine at `freq * (1 + n / 2)` Hz (440, 660 by default).

- [ ] **Step 1: Make the dual-channel fixture audible after a downmix**

In `tests/conftest.py`, replace:

```python
            for i in range(frames):
                value = int(12000 * math.sin(2 * math.pi * freq * i / rate))
                for channel in range(channels):
                    # Right channel gets an inverted tone so channel-split tests
                    # can prove the channels did not get swapped or duplicated.
                    scale = 1 if channel == 0 else -1
                    samples += struct.pack("<h", value * scale)
```

with:

```python
            for i in range(frames):
                for channel in range(channels):
                    # Each channel gets its own pitch (440, 660, ... Hz). An inverted
                    # copy would cancel to digital silence in a mono downmix.
                    pitch = freq * (1 + channel / 2)
                    samples += struct.pack("<h", int(12000 * math.sin(2 * math.pi * pitch * i / rate)))
```

- [ ] **Step 2: Write the failing tests**

Replace `tests/test_llm.py` with:

```python
import httpx
import pytest

from jabberscribe.config import LiteLLMConfig
from jabberscribe.llm import KEY_ENV, TransientError, make_client, post


def _capture() -> tuple[dict, httpx.MockTransport]:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    return seen, httpx.MockTransport(handler)


def test_sends_bearer_key_from_environment(monkeypatch) -> None:
    monkeypatch.setenv(KEY_ENV, "sk-test")
    seen, transport = _capture()

    make_client(LiteLLMConfig(base_url="http://litellm.test"), transport=transport).get("/v1/models")

    assert seen["auth"] == "Bearer sk-test"
    assert seen["url"] == "http://litellm.test/v1/models"


def test_no_auth_header_without_key(monkeypatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    seen, transport = _capture()

    make_client(LiteLLMConfig(base_url="http://litellm.test"), transport=transport).get("/v1/models")

    assert seen["auth"] is None


def test_trailing_v1_in_base_url_is_not_doubled(monkeypatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    seen, transport = _capture()

    make_client(LiteLLMConfig(base_url="http://litellm.test/v1/"), transport=transport).get("/v1/models")

    assert seen["url"] == "http://litellm.test/v1/models"


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_post_turns_overload_into_transient_error(status: int) -> None:
    with pytest.raises(TransientError, match=str(status)):
        post(_client(lambda r: httpx.Response(status)), "/v1/chat/completions", json={})


def test_post_turns_transport_failure_into_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(TransientError, match="timed out"):
        post(_client(handler), "/v1/chat/completions", json={})


def test_post_leaves_client_errors_permanent() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        post(_client(lambda r: httpx.Response(404)), "/v1/chat/completions", json={})


def test_post_returns_successful_responses() -> None:
    assert post(_client(lambda r: httpx.Response(200, json={"ok": True})), "/x", json={}).json() == {"ok": True}
```

Replace `tests/test_stt.py` with:

```python
from pathlib import Path

import httpx
import pytest

from jabberscribe.llm import TransientError
from jabberscribe.stt import (
    FILLER_PROMPT,
    LiteLLMTranscriber,
    Segment,
    SttError,
    build_prompt,
    format_ts,
    load_vocabulary,
)


def _transcriber(handler, prompt: str = FILLER_PROMPT) -> LiteLLMTranscriber:
    client = httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(handler))
    return LiteLLMTranscriber(client, model="whisper-he", prompt=prompt)


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "stt.ogg"
    path.write_bytes(b"OggS fake")
    return path


def test_posts_verbatim_request_and_parses_segments(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={
                "text": "…",
                "segments": [
                    {"start": 0.0, "end": 1.2, "text": " אה, שלום "},
                    {"start": 1.2, "end": 1.5, "text": "   "},
                    {"start": 1.5, "end": 3.0, "text": "deploy מחר"},
                ],
            },
        )

    segments = _transcriber(handler).transcribe(_audio(tmp_path))

    assert seen["path"] == "/v1/audio/transcriptions"
    assert b"verbose_json" in seen["body"]
    assert b"whisper-he" in seen["body"]
    assert FILLER_PROMPT.encode() in seen["body"]
    assert segments == [Segment(0.0, 1.2, "אה, שלום"), Segment(1.5, 3.0, "deploy מחר")]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_overload_and_server_errors_are_transient(tmp_path: Path, status: int) -> None:
    with pytest.raises(TransientError, match=str(status)):
        _transcriber(lambda r: httpx.Response(status, text="overloaded")).transcribe(_audio(tmp_path))


def test_unreachable_server_is_transient(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(TransientError, match="refused"):
        _transcriber(handler).transcribe(_audio(tmp_path))


@pytest.mark.parametrize("status", [400, 401, 404, 413])
def test_client_errors_are_permanent(tmp_path: Path, status: int) -> None:
    with pytest.raises(SttError, match=str(status)):
        _transcriber(lambda r: httpx.Response(status, text="bad request")).transcribe(_audio(tmp_path))


def test_response_without_segments_raises(tmp_path: Path) -> None:
    with pytest.raises(SttError, match="segments"):
        _transcriber(lambda r: httpx.Response(200, json={"text": "שלום"})).transcribe(_audio(tmp_path))


def test_malformed_segment_raises(tmp_path: Path) -> None:
    body = {"segments": [{"text": "no timestamps"}]}

    with pytest.raises(SttError):
        _transcriber(lambda r: httpx.Response(200, json=body)).transcribe(_audio(tmp_path))


def test_format_ts() -> None:
    assert format_ts(0) == "00:00:00"
    assert format_ts(3725.9) == "01:02:05"


def test_build_prompt_puts_the_filler_cue_last() -> None:
    """Whisper keeps only the last 224 prompt tokens; the filler cue must survive truncation."""
    assert build_prompt(None) == FILLER_PROMPT
    assert build_prompt("ג'אבר, שלוחה") == f"ג'אבר, שלוחה {FILLER_PROMPT}"


def _segments_response(*segments: dict) -> httpx.Response:
    return httpx.Response(200, json={"segments": list(segments)})


@pytest.mark.parametrize(
    "quality",
    [
        {"compression_ratio": 2.6},
        {"no_speech_prob": 0.9, "avg_logprob": -1.5},
    ],
)
def test_likely_hallucinations_are_dropped(tmp_path: Path, quality: dict) -> None:
    bad = {"start": 0.0, "end": 5.0, "text": "תודה רבה תודה רבה תודה רבה", **quality}
    good = {"start": 5.0, "end": 6.0, "text": "שלום", "compression_ratio": 1.1, "no_speech_prob": 0.1}

    segments = _transcriber(lambda r: _segments_response(bad, good)).transcribe(_audio(tmp_path))

    assert segments == [Segment(5.0, 6.0, "שלום")]


def test_quiet_but_confident_speech_is_kept(tmp_path: Path) -> None:
    soft = {"start": 0.0, "end": 1.0, "text": "כן", "no_speech_prob": 0.9, "avg_logprob": -0.3}

    assert _transcriber(lambda r: _segments_response(soft)).transcribe(_audio(tmp_path)) == [Segment(0.0, 1.0, "כן")]


def test_prompt_echo_is_dropped_but_a_lone_filler_is_kept(tmp_path: Path) -> None:
    echo = {"start": 0.0, "end": 4.0, "text": FILLER_PROMPT}
    partial_echo = {"start": 4.0, "end": 6.0, "text": "אה, אממ, כאילו..."}
    filler = {"start": 6.0, "end": 7.0, "text": "אה,"}

    segments = _transcriber(lambda r: _segments_response(echo, partial_echo, filler)).transcribe(_audio(tmp_path))

    assert segments == [Segment(6.0, 7.0, "אה,")]


def test_load_vocabulary(tmp_path: Path) -> None:
    path = tmp_path / "vocab.txt"
    path.write_text("ג'אבר\n\nשלוחה\n", encoding="utf-8")

    assert load_vocabulary(path) == "ג'אבר, שלוחה"
    assert load_vocabulary(None) is None
    assert load_vocabulary(tmp_path / "missing.txt") is None
```

Replace `tests/test_audio.py` with:

```python
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jabberscribe import audio
from jabberscribe.audio import STT_FILENAME, AudioError, prepare_for_stt

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not on PATH"
)


def _stream(path: Path) -> tuple[str, int]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    stream = json.loads(proc.stdout)["streams"][0]
    return stream["codec_name"], stream["channels"]


def test_dual_channel_call_becomes_mono_opus(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "call.wav", channels=2, seconds=2.0)

    out = prepare_for_stt(src, tmp_path / "work")

    assert out == tmp_path / "work" / STT_FILENAME
    assert _stream(out) == ("opus", 1)


def test_existing_output_is_reused(tmp_path: Path, make_wav) -> None:
    src = make_wav(tmp_path / "call.wav")
    first = prepare_for_stt(src, tmp_path / "work")
    stamp = first.stat().st_mtime_ns

    second = prepare_for_stt(src, tmp_path / "work")

    assert second.stat().st_mtime_ns == stamp


def test_no_partial_file_is_left(tmp_path: Path, make_wav) -> None:
    prepare_for_stt(make_wav(tmp_path / "call.wav"), tmp_path / "work")

    assert list((tmp_path / "work").glob("*.part")) == []


def test_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(AudioError, match="not found"):
        prepare_for_stt(tmp_path / "nope.wav", tmp_path / "work")


def test_undecodable_source_raises(tmp_path: Path) -> None:
    src = tmp_path / "junk.wav"
    src.write_bytes(b"this is not audio")

    with pytest.raises(AudioError, match="ffmpeg exited"):
        prepare_for_stt(src, tmp_path / "work")


def _mean_volume_db(path: Path) -> float:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return float(re.search(r"mean_volume: (-?[\d.]+) dB", proc.stderr).group(1))


def test_downmix_keeps_both_channels_audible(tmp_path: Path, make_wav) -> None:
    """A broken downmix (dropped or cancelled channel) would hand Whisper silence."""
    out = prepare_for_stt(make_wav(tmp_path / "call.wav", channels=2, seconds=2.0), tmp_path / "work")

    assert _mean_volume_db(out) > -40.0


def test_failed_encode_leaves_no_partial(tmp_path: Path) -> None:
    src = tmp_path / "junk.wav"
    src.write_bytes(b"this is not audio")

    with pytest.raises(AudioError):
        prepare_for_stt(src, tmp_path / "work")

    assert list((tmp_path / "work").iterdir()) == []


def test_ffmpeg_output_is_decoded_as_utf8(tmp_path: Path, make_wav, monkeypatch) -> None:
    seen: dict = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        Path(args[-1]).write_bytes(b"OggS")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)

    prepare_for_stt(make_wav(tmp_path / "call.wav"), tmp_path / "work")

    assert (seen["encoding"], seen["errors"]) == ("utf-8", "replace")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_llm.py tests/test_stt.py tests/test_audio.py -q`
Expected: FAIL — `ImportError: cannot import name 'TransientError' from 'jabberscribe.llm'` (test_llm, test_stt); in test_audio, `test_ffmpeg_output_is_decoded_as_utf8` fails with `KeyError: 'encoding'`. (`test_downmix_keeps_both_channels_audible` passes because of Step 1; with the old inverted fixture it measures -91 dB. `test_failed_encode_leaves_no_partial` is a regression guard: ffmpeg rejects this input before it opens the output.)

- [ ] **Step 4: Rewrite `jabberscribe/llm.py`**

```python
"""HTTP client for the on-prem LiteLLM server.

LiteLLM exposes OpenAI-compatible routes, so STT and summary both speak plain
OpenAI JSON over httpx. The API key comes from the environment; a server
without auth simply leaves it unset.

Errors split in two. TransientError means LiteLLM is down or overloaded: the
job waits and retries for as long as that lasts. Everything else is a real
problem with the request or the data and fails the job after a few attempts.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from jabberscribe.config import LiteLLMConfig

KEY_ENV = "JABBERSCRIBE_LITELLM_KEY"


class TransientError(RuntimeError):
    """LiteLLM is unreachable, overloaded (429) or failing (5xx). Retry later; never give up."""


def make_client(cfg: LiteLLMConfig, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    key = os.environ.get(KEY_ENV)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    # The routes below already start with /v1; an OpenAI-style base_url ending in /v1 would double it.
    base_url = cfg.base_url.rstrip("/").removesuffix("/v1")
    return httpx.Client(base_url=base_url, headers=headers, timeout=cfg.timeout_seconds, transport=transport)


def post(client: httpx.Client, path: str, **kwargs: Any) -> httpx.Response:
    """POST and sort failures: TransientError for outages, HTTPStatusError for other 4xx.

    The caller wraps HTTPStatusError in its own permanent error type.
    """
    try:
        response = client.post(path, **kwargs)
    except httpx.TransportError as exc:
        raise TransientError(f"{path}: {exc}") from exc
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientError(f"{path}: HTTP {response.status_code} {response.text[:200]}")
    response.raise_for_status()
    return response
```

- [ ] **Step 5: Rewrite `jabberscribe/stt.py`**

```python
"""Speech-to-text through the LiteLLM server's OpenAI-compatible route.

Strict verbatim is the goal: fillers, false starts, and repetitions stay in.
Whisper tends to drop fillers, so the prompt *shows* them -- Whisper imitates
the style of its prompt. That is best effort, not a guarantee.

Whisper reads only the last 224 prompt tokens, so the filler cue goes last
where it survives truncation, and the vocabulary goes first. Segments Whisper
most likely invented (silence, hold music, repetition loops, prompt echo) are
dropped: a verbatim transcript must not contain words nobody said.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from jabberscribe.llm import post

log = logging.getLogger(__name__)

FILLER_PROMPT = "אה, אממ, כאילו... אה, רגע, רגע."

#: Whisper's own thresholds for "this segment is a repetition loop" and "this is silence".
MAX_COMPRESSION_RATIO = 2.4
NO_SPEECH_PROB = 0.6
MIN_AVG_LOGPROB = -1.0
#: Echo detection ignores short fragments: a lone "אה" is a real filler, not an echo.
MIN_ECHO_CHARS = len(FILLER_PROMPT) // 2


class SttError(RuntimeError):
    """Transcription failed for a reason retrying will not fix."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


class Transcriber(Protocol):
    def transcribe(self, audio: Path) -> list[Segment]: ...


def load_vocabulary(path: Path | None) -> str | None:
    """Read a one-term-per-line glossary into a comma-separated prompt fragment."""
    if path is None or not path.is_file():
        return None
    terms = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return ", ".join(terms) or None


def build_prompt(vocabulary: str | None) -> str:
    return f"{vocabulary} {FILLER_PROMPT}" if vocabulary else FILLER_PROMPT


def format_ts(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def is_hallucination(raw: dict, text: str, prompt: str) -> bool:
    """True for a segment Whisper most likely invented rather than heard."""
    compression = raw.get("compression_ratio")
    if isinstance(compression, int | float) and compression > MAX_COMPRESSION_RATIO:
        return True
    no_speech, logprob = raw.get("no_speech_prob"), raw.get("avg_logprob")
    if isinstance(no_speech, int | float) and isinstance(logprob, int | float):
        if no_speech > NO_SPEECH_PROB and logprob < MIN_AVG_LOGPROB:
            return True
    return text == prompt.strip() or (len(text) >= MIN_ECHO_CHARS and text in prompt)


class LiteLLMTranscriber:
    def __init__(self, client: httpx.Client, model: str, prompt: str) -> None:
        self._client = client
        self._model = model
        self._prompt = prompt

    def transcribe(self, audio: Path) -> list[Segment]:
        """Raises TransientError when LiteLLM is down or overloaded, SttError otherwise."""
        try:
            with audio.open("rb") as fh:
                response = post(
                    self._client,
                    "/v1/audio/transcriptions",
                    files={"file": (audio.name, fh, "audio/ogg")},
                    data={
                        "model": self._model,
                        "language": "he",
                        "response_format": "verbose_json",
                        "timestamp_granularities[]": "segment",
                        "prompt": self._prompt,
                        "temperature": "0",
                    },
                )
            payload = response.json()
        except (httpx.HTTPStatusError, OSError, ValueError) as exc:
            raise SttError(f"transcription failed for {audio}: {exc}") from exc

        raw_segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(raw_segments, list):
            raise SttError("transcription response has no segments; the server must support verbose_json")
        segments: list[Segment] = []
        try:
            for raw in raw_segments:
                text = str(raw["text"]).strip()
                if not text:
                    continue
                if is_hallucination(raw, text, self._prompt):
                    log.info("dropping likely hallucinated segment at %.1fs: %r", float(raw["start"]), text[:80])
                    continue
                segments.append(Segment(float(raw["start"]), float(raw["end"]), text))
        except (KeyError, TypeError, ValueError) as exc:
            raise SttError(f"malformed segment in transcription response: {exc}") from exc
        return segments
```

- [ ] **Step 6: Rewrite `jabberscribe/audio.py`**

```python
"""Audio preprocessing via ffmpeg.

The STT endpoint gets one compact mono file: both telephony channels
downmixed (speaker labels are a later extra), resampled to 16 kHz,
loudness-normalized, and encoded as Opus so an hour-long call stays around
15 MB -- well under typical transcription upload limits.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

STT_FILENAME = "stt.ogg"
TARGET_RATE = 16000
_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
_TIMEOUT_SECONDS = 1800


class AudioError(RuntimeError):
    """Audio could not be prepared."""


def _run_ffmpeg(args: list[str]) -> None:
    try:
        # ffmpeg writes UTF-8; the Windows locale code page (cp1255, cp437) cannot decode every message.
        proc = subprocess.run(
            args,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AudioError(f"ffmpeg not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg timed out after {_TIMEOUT_SECONDS}s") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise AudioError(f"ffmpeg exited {proc.returncode}: {' | '.join(tail)}")


def prepare_for_stt(src: Path, work_dir: Path, ffmpeg: str = "ffmpeg") -> Path:
    """Encode `src` as work_dir/stt.ogg for transcription.

    Idempotent: an existing non-empty output is reused, which is what makes the
    stage safe to retry after a crash.
    """
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    work_dir.mkdir(parents=True, exist_ok=True)
    dest = work_dir / STT_FILENAME
    if dest.is_file() and dest.stat().st_size > 0:
        log.debug("reusing existing %s", dest)
        return dest
    partial = dest.with_suffix(dest.suffix + ".part")
    # -f ogg is required, not cosmetic: the .part suffix defeats ffmpeg's
    # extension-based format detection and it refuses to choose a muxer.
    try:
        _run_ffmpeg(
            [
                ffmpeg, "-y", "-nostdin", "-i", str(src),
                "-af", _LOUDNORM, "-ac", "1", "-ar", str(TARGET_RATE),
                "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(partial),
            ]
        )
    except AudioError:
        # A half-written Opus file is still the caller's voice; it must not linger.
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)
    return dest
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_llm.py tests/test_stt.py tests/test_audio.py -q`
Expected: 36 passed.

- [ ] **Step 8: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (150 passed).

- [ ] **Step 9: Commit**

```bash
git add jabberscribe/llm.py jabberscribe/stt.py jabberscribe/audio.py tests/conftest.py tests/test_llm.py tests/test_stt.py tests/test_audio.py
git commit -m "fix(stt): transient vs permanent errors, filler cue last, drop hallucinated segments" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Summary hardening and long transcripts

Fixes audit I7 (system role rejected by Gemma 1/2 templates; config errors silently become "summary unavailable"), I8 (long meetings exceed the context window; no `max_tokens`), I9 (a transient outage permanently loses the summary), and the Task 6 review finding: a `"content": null` completion raised `AttributeError` out of `summarize()`. Each failed attempt now logs a truncated copy of the raw answer.

**Files:**
- Modify (full rewrite): `jabberscribe/summarize.py`
- Modify: `jabberscribe/config.py` (`SummaryConfig`), `tests/test_config.py` (one assertion)
- Test (full rewrite): `tests/test_summarize.py`

**Interfaces:**
- Consumes: `llm.post`, `llm.TransientError` (Task 11); `Segment`, `format_ts` (committed).
- Produces:
  - unchanged: `ActionItem`, `Summary`, `Summarizer` Protocol (`summarize(segments) -> Summary | None`), `ATTEMPTS = 2`, `parse_summary(content: str) -> Summary`, `transcript_text(segments) -> str`.
  - changed: `LiteLLMSummarizer(client: httpx.Client, model: str, max_chunk_chars: int = 12000)`. `summarize` returns None only for unusable model output (after one retry); raises `TransientError` for outages and `SummaryError` for other 4xx.
  - new: `SummaryError(RuntimeError)`; `MAX_TOKENS = 2048`; `LOG_CHARS = 300`; `INSTRUCTIONS`, `MERGE_INSTRUCTIONS` (str; replace `SYSTEM_PROMPT`); `chunk_segments(segments: list[Segment], max_chars: int) -> list[list[Segment]]`; `strip_fences(content: str) -> str` (was `_strip_fences`; Task 18's `doctor` uses it).
  - `SummaryConfig.max_chunk_chars: int = 12000`.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_summarize.py` with:

```python
import json

import httpx
import pytest

from jabberscribe.llm import TransientError
from jabberscribe.stt import Segment
from jabberscribe.summarize import (
    MAX_TOKENS,
    ActionItem,
    LiteLLMSummarizer,
    Summary,
    SummaryError,
    chunk_segments,
    parse_summary,
    transcript_text,
)

SEGMENTS = [Segment(0.0, 2.0, "שלום, מה שלומך"), Segment(65.0, 70.0, "דנה תשלח את הדוח עד יום חמישי")]

GOOD = {
    "summary": "שיחה קצרה על הדוח.",
    "action_items": [
        {"task": "לשלוח את הדוח", "owner": "דנה", "due": "יום חמישי", "source_ts": "00:01:05"},
        {"task": "לבדוק את ה-API", "owner": None, "due": None, "source_ts": "00:00:00"},
    ],
}
GOOD_JSON = json.dumps(GOOD, ensure_ascii=False)


class Server:
    """Fake LiteLLM chat route: answers each request with the next response."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": response}}]})


def _summarizer(server: Server, max_chunk_chars: int = 12000) -> LiteLLMSummarizer:
    client = httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(server))
    return LiteLLMSummarizer(client, model="gemma-3", max_chunk_chars=max_chunk_chars)


def test_transcript_text_is_timestamped_lines() -> None:
    assert transcript_text(SEGMENTS) == "[00:00:00] שלום, מה שלומך\n[00:01:05] דנה תשלח את הדוח עד יום חמישי"


def test_returns_summary_and_action_items() -> None:
    summary = _summarizer(Server(GOOD_JSON)).summarize(SEGMENTS)

    assert summary == Summary(
        "שיחה קצרה על הדוח.",
        (
            ActionItem("לשלוח את הדוח", "דנה", "יום חמישי", "00:01:05"),
            ActionItem("לבדוק את ה-API", None, None, "00:00:00"),
        ),
    )


def test_request_has_no_system_role_and_caps_tokens() -> None:
    """Gemma 1 and 2 chat templates reject a system message."""
    server = Server(GOOD_JSON)

    _summarizer(server).summarize(SEGMENTS)

    request = server.requests[0]
    assert request["model"] == "gemma-3"
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == MAX_TOKENS
    assert [m["role"] for m in request["messages"]] == ["user"]
    assert "Return only a JSON object" in request["messages"][0]["content"]
    assert "[00:01:05] דנה תשלח" in request["messages"][0]["content"]


def test_retries_once_after_unusable_output() -> None:
    server = Server("not json at all", GOOD_JSON)

    assert _summarizer(server).summarize(SEGMENTS) is not None
    assert len(server.requests) == 2


def test_gives_up_after_two_unusable_answers_and_logs_them(caplog) -> None:
    server = Server("nope", "still nope")

    assert _summarizer(server).summarize(SEGMENTS) is None
    assert len(server.requests) == 2
    assert "still nope" in caplog.text


def test_null_content_is_unusable_output_not_a_crash() -> None:
    """A refusal or empty completion comes back as content: null."""
    server = Server(None, "")

    assert _summarizer(server).summarize(SEGMENTS) is None
    assert len(server.requests) == 2


@pytest.mark.parametrize("status", [429, 500, 503])
def test_server_overload_is_transient(status: int) -> None:
    with pytest.raises(TransientError):
        _summarizer(Server(httpx.Response(status))).summarize(SEGMENTS)


def test_unreachable_server_is_transient() -> None:
    with pytest.raises(TransientError):
        _summarizer(Server(httpx.ConnectError("refused"))).summarize(SEGMENTS)


@pytest.mark.parametrize("status", [400, 401, 404])
def test_rejected_request_fails_loudly(status: int) -> None:
    """A wrong model name must not quietly ship "summary unavailable" for every call."""
    with pytest.raises(SummaryError, match=str(status)):
        _summarizer(Server(httpx.Response(status))).summarize(SEGMENTS)


def test_empty_transcript_skips_the_request() -> None:
    server = Server()

    assert _summarizer(server).summarize([]) is None
    assert server.requests == []


def test_short_transcript_is_one_chunk() -> None:
    assert chunk_segments(SEGMENTS, 12000) == [SEGMENTS]


def test_long_transcript_is_split_on_segment_boundaries() -> None:
    segments = [Segment(float(i), float(i + 1), "מילה " * 10) for i in range(10)]

    chunks = chunk_segments(segments, 200)

    assert [s for chunk in chunks for s in chunk] == segments
    assert len(chunks) > 1
    assert all(len(transcript_text(chunk)) <= 200 for chunk in chunks)


def test_long_transcript_is_summarized_per_chunk_then_merged() -> None:
    first = {"summary": "חלק ראשון.", "action_items": [GOOD["action_items"][1]]}
    second = {"summary": "חלק שני.", "action_items": [GOOD["action_items"][0], GOOD["action_items"][1]]}
    merged = {"summary": "סיכום מאוחד."}
    server = Server(*(json.dumps(d, ensure_ascii=False) for d in (first, second, merged)))

    summary = _summarizer(server, max_chunk_chars=40).summarize(SEGMENTS)

    assert summary == Summary(
        "סיכום מאוחד.",
        (
            ActionItem("לבדוק את ה-API", None, None, "00:00:00"),
            ActionItem("לשלוח את הדוח", "דנה", "יום חמישי", "00:01:05"),
        ),
    )
    assert "שלום, מה שלומך" in server.requests[0]["messages"][0]["content"]
    assert "דנה תשלח" in server.requests[1]["messages"][0]["content"]
    assert "חלק ראשון." in server.requests[2]["messages"][0]["content"]


def test_an_unusable_chunk_makes_the_summary_unavailable() -> None:
    server = Server("nope", "still nope")

    assert _summarizer(server, max_chunk_chars=40).summarize(SEGMENTS) is None


def test_parse_strips_code_fences() -> None:
    assert parse_summary(f"```json\n{GOOD_JSON}\n```").text == "שיחה קצרה על הדוח."


def test_parse_turns_blank_owner_and_due_into_none() -> None:
    doc = {"summary": "ס", "action_items": [{"task": "t", "owner": " ", "due": "", "source_ts": "00:00:01"}]}

    item = parse_summary(json.dumps(doc)).action_items[0]

    assert (item.owner, item.due) == (None, None)


@pytest.mark.parametrize(
    "doc",
    [
        {"summary": "", "action_items": []},
        {"summary": "ס"},
        {"summary": "ס", "action_items": [{"task": "t", "source_ts": "1:05"}]},
        {"summary": "ס", "action_items": [{"task": "", "source_ts": "00:00:01"}]},
    ],
)
def test_parse_rejects_invalid_documents(doc: dict) -> None:
    with pytest.raises(ValueError):
        parse_summary(json.dumps(doc))
```

In `tests/test_config.py`, inside `test_minimal_config_loads_with_defaults`, replace:

```python
    assert cfg.summary.model == "gemma-3"
```

with:

```python
    assert cfg.summary.model == "gemma-3"
    assert cfg.summary.max_chunk_chars == 12000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_summarize.py tests/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'MAX_TOKENS' from 'jabberscribe.summarize'`, and `'SummaryConfig' object has no attribute 'max_chunk_chars'`.

- [ ] **Step 3: Add the chunk size to the config**

In `jabberscribe/config.py`, replace:

```python
class SummaryConfig(_Strict):
    #: The model_name LiteLLM serves for Gemma.
    model: str
```

with:

```python
class SummaryConfig(_Strict):
    #: The model_name LiteLLM serves for Gemma.
    model: str
    #: Longer transcripts are summarized in chunks of about this many characters, then merged.
    max_chunk_chars: int = 12000
```

- [ ] **Step 4: Rewrite `jabberscribe/summarize.py`**

```python
"""Meeting summary and action items via the LiteLLM chat route.

A summary failure must never cost the user their transcript. Failures split
three ways:

- Unusable model output (not JSON, wrong shape, empty content): retried once,
  then the call carries on with the summary marked unavailable.
- LiteLLM down or overloaded: TransientError, so the job retries later with
  the transcript already checkpointed. A two-minute Gemma restart must not
  cost a summary forever.
- Any other 4xx (wrong model name, bad key, rejected parameter): SummaryError,
  so the job fails loudly instead of shipping "unavailable" for every call.

The instructions travel in the user message: Gemma 1 and 2 chat templates
reject a system role. Transcripts longer than max_chunk_chars are summarized
per chunk, then the chunk summaries are merged; action items are the union of
the chunks' items, each keeping its own source_ts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import BaseModel, Field

from jabberscribe.llm import post
from jabberscribe.stt import Segment, format_ts

log = logging.getLogger(__name__)

ATTEMPTS = 2
#: Explicit, so the server default cannot cut the JSON answer off mid-object.
MAX_TOKENS = 2048
#: How much of an unusable answer goes into the log.
LOG_CHARS = 300

INSTRUCTIONS = """You summarize Hebrew business phone calls and meetings from a timestamped transcript.
Write in Hebrew. Keep English technical terms exactly as spoken.
Return only a JSON object with this shape:
{"summary": "<concise Hebrew summary of the call>",
 "action_items": [{"task": "<what must be done>", "owner": "<name or null>",
                   "due": "<deadline or null>", "source_ts": "HH:MM:SS"}]}
Rules:
- owner and due: fill them only when explicitly stated in the call; otherwise null. Never guess.
- source_ts: the timestamp of the transcript line the item comes from, copied exactly.
- If there are no action items, return an empty list."""

MERGE_INSTRUCTIONS = """You merge the partial summaries of one Hebrew call, given in order, into one summary.
Write in Hebrew. Keep English technical terms exactly as written.
Return only a JSON object with this shape:
{"summary": "<concise Hebrew summary of the whole call>"}"""


class SummaryError(RuntimeError):
    """LiteLLM rejected the summary request (4xx other than 429). Retrying will not help."""


@dataclass(frozen=True)
class ActionItem:
    task: str
    owner: str | None
    due: str | None
    source_ts: str


@dataclass(frozen=True)
class Summary:
    text: str
    action_items: tuple[ActionItem, ...]


class Summarizer(Protocol):
    def summarize(self, segments: list[Segment]) -> Summary | None: ...


class _ItemModel(BaseModel):
    task: str = Field(min_length=1)
    owner: str | None = None
    due: str | None = None
    source_ts: str = Field(pattern=r"^\d{2}:\d{2}:\d{2}$")


class _SummaryModel(BaseModel):
    summary: str = Field(min_length=1)
    action_items: list[_ItemModel]


class _MergeModel(BaseModel):
    summary: str = Field(min_length=1)


def transcript_text(segments: list[Segment]) -> str:
    return "\n".join(f"[{format_ts(s.start)}] {s.text}" for s in segments)


def chunk_segments(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    """Split into consecutive chunks whose transcript text stays within max_chars.

    A single segment longer than max_chars still gets a chunk of its own.
    """
    if len(transcript_text(segments)) <= max_chars:
        return [segments]
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        line = len(transcript_text([segment])) + 1
        if current and size + line > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(segment)
        size += line
    if current:
        chunks.append(current)
    return chunks


def strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rstrip()
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def parse_summary(content: str) -> Summary:
    """Validate the model's answer. Raises ValueError when it is unusable."""
    model = _SummaryModel.model_validate_json(strip_fences(content))
    return Summary(
        text=model.summary.strip(),
        action_items=tuple(
            ActionItem(i.task.strip(), _blank_to_none(i.owner), _blank_to_none(i.due), i.source_ts)
            for i in model.action_items
        ),
    )


def _parse_merge(content: str) -> str:
    return _MergeModel.model_validate_json(strip_fences(content)).summary.strip()


def _union(parts: list[Summary]) -> tuple[ActionItem, ...]:
    items: list[ActionItem] = []
    for part in parts:
        items.extend(i for i in part.action_items if i not in items)
    return tuple(items)


class LiteLLMSummarizer:
    def __init__(self, client: httpx.Client, model: str, max_chunk_chars: int = 12000) -> None:
        self._client = client
        self._model = model
        self._max_chunk_chars = max_chunk_chars

    def summarize(self, segments: list[Segment]) -> Summary | None:
        """None means the model's output stayed unusable; transport and 4xx errors raise."""
        if not segments:
            return None
        chunks = chunk_segments(segments, self._max_chunk_chars)
        parts: list[Summary] = []
        for index, chunk in enumerate(chunks, start=1):
            part = self._ask(INSTRUCTIONS, "Transcript:\n" + transcript_text(chunk), parse_summary)
            if part is None:
                log.warning("summary chunk %d/%d stayed unusable; summary unavailable", index, len(chunks))
                return None
            parts.append(part)
        if len(parts) == 1:
            return parts[0]
        numbered = "\n\n".join(f"Part {i}:\n{p.text}" for i, p in enumerate(parts, start=1))
        merged = self._ask(MERGE_INSTRUCTIONS, "Partial summaries:\n" + numbered, _parse_merge)
        if merged is None:
            return None
        return Summary(merged, _union(parts))

    def _ask[T](self, instructions: str, body: str, parse: Callable[[str], T]) -> T | None:
        for attempt in range(1, ATTEMPTS + 1):
            content: str | None = None
            try:
                content = self._complete(f"{instructions}\n\n{body}")
                return parse(content)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                log.warning(
                    "summary attempt %d/%d returned unusable output: %s; raw: %r",
                    attempt,
                    ATTEMPTS,
                    exc,
                    (content or "")[:LOG_CHARS],
                )
        return None

    def _complete(self, prompt: str) -> str:
        """One chat completion. Raises ValueError when the answer has no text content."""
        try:
            response = post(
                self._client,
                "/v1/chat/completions",
                json={
                    "model": self._model,
                    "temperature": 0.2,
                    "max_tokens": MAX_TOKENS,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
        except httpx.HTTPStatusError as exc:
            raise SummaryError(f"summary request rejected: {exc}") from exc
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            # A refusal or an empty completion: unusable output, not a crash.
            raise ValueError(f"model returned no text content: {content!r}")
        return content
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_summarize.py tests/test_config.py -q`
Expected: all pass (24 in test_summarize).

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (161 passed).

- [ ] **Step 7: Commit**

```bash
git add jabberscribe/summarize.py jabberscribe/config.py tests/test_summarize.py tests/test_config.py
git commit -m "fix(summarize): user-role prompt, loud 4xx, transient retries, chunked long calls" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Output hardening

Fixes audit I11 (a reader holding `result.json` open makes `os.replace` fail on Windows), adds spec §7 `timings` to `result.json`, and improvement 9 (RTL Markdown). Edits the Task 7 code.

**Files:**
- Modify (full rewrite): `jabberscribe/output.py`
- Test (full rewrite): `tests/test_output.py`

**Interfaces:**
- Consumes: `Sidecar`, `Party` (Task 8); `utcnow` (Task 10); `Segment`, `format_ts` (Task 11); `Summary` (Task 12).
- Produces:
  - unchanged: `TRANSCRIPT_FILE`, `SUMMARY_FILE`, `ACTIONS_FILE`, `RESULT_FILE`, `TEXT_FILES`, `SUMMARY_UNAVAILABLE`.
  - `write_atomic(path: Path, text: str) -> None` — retries the rename `REPLACE_ATTEMPTS = 5` times, `REPLACE_DELAY_SECONDS = 0.2` apart, on `PermissionError`; then removes the `.part` and re-raises.
  - `write_outputs(out_dir, *, sidecar, segments, summary, owners, models, recording, timings: dict[str, float] | None = None) -> Path` — `result.json` gains `"timings"` (`{}` when None).
  - `update_owners(out_dir: Path, owners: list[Party]) -> None` — unchanged; documented to raise `OSError` for the caller to retry.
  - Markdown files start with `<div dir="rtl">` + blank line and end with `</div>`.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_output.py` with:

```python
import json
from pathlib import Path

import pytest

from jabberscribe.output import (
    ACTIONS_FILE,
    REPLACE_ATTEMPTS,
    RESULT_FILE,
    SUMMARY_FILE,
    SUMMARY_UNAVAILABLE,
    TEXT_FILES,
    TRANSCRIPT_FILE,
    update_owners,
    write_atomic,
    write_outputs,
)
from jabberscribe.sidecar import Party, parse_sidecar
from jabberscribe.stt import Segment
from jabberscribe.summarize import ActionItem, Summary

SEGMENTS = [Segment(0.0, 1.0, "אה, שלום"), Segment(61.0, 62.5, "נדבר מחר")]
SUMMARY = Summary("סיכום קצר.", (ActionItem("לשלוח | לבדוק", None, "מחר", "00:01:01"),))
MODELS = {"stt": "whisper-he", "summary": "gemma-3"}


def _write(tmp_path: Path, make_sidecar, summary: Summary | None = SUMMARY) -> Path:
    sidecar = parse_sidecar(make_sidecar(tmp_path / "s.json", call_id="gc1").read_text(encoding="utf-8"))
    out = tmp_path / "out"
    write_outputs(
        out,
        sidecar=sidecar,
        segments=SEGMENTS,
        summary=summary,
        owners=[sidecar.line_owner],
        models=MODELS,
        recording=out / "recording.wav",
    )
    return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_writes_all_files_without_partials(tmp_path: Path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar)

    assert sorted(p.name for p in out.iterdir()) == sorted(TEXT_FILES)


def test_transcript_has_timestamped_lines(tmp_path: Path, make_sidecar) -> None:
    text = _read(_write(tmp_path, make_sidecar) / TRANSCRIPT_FILE)

    assert "[00:00:00] אה, שלום" in text
    assert "[00:01:01] נדבר מחר" in text


def test_summary_markdown(tmp_path: Path, make_sidecar) -> None:
    assert "סיכום קצר." in _read(_write(tmp_path, make_sidecar) / SUMMARY_FILE)


def test_actions_table_escapes_pipes_and_marks_missing_owner(tmp_path: Path, make_sidecar) -> None:
    text = _read(_write(tmp_path, make_sidecar) / ACTIONS_FILE)

    assert "| לשלוח \\| לבדוק | — | מחר | 00:01:01 |" in text


def test_no_action_items_says_so(tmp_path: Path, make_sidecar) -> None:
    text = _read(_write(tmp_path, make_sidecar, Summary("ס", ())) / ACTIONS_FILE)

    assert "לא עלו משימות" in text


def test_unavailable_summary_is_visible(tmp_path: Path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, summary=None)

    assert SUMMARY_UNAVAILABLE in _read(out / SUMMARY_FILE)
    assert SUMMARY_UNAVAILABLE in _read(out / ACTIONS_FILE)
    result = json.loads(_read(out / RESULT_FILE))
    assert result["summary_available"] is False
    assert result["summary"] is None
    assert result["action_items"] == []


def test_result_json_contents(tmp_path: Path, make_sidecar) -> None:
    result = json.loads(_read(_write(tmp_path, make_sidecar) / RESULT_FILE))

    assert result["schema_version"] == 1
    assert result["job_key"] == "gc1_1042"
    assert result["call_id"] == "gc1"
    assert result["kind"] == "call"
    assert result["owners"] == [{"extension": "1042", "user": "meir", "display_name": "מאיר"}]
    assert result["parties"] == [{"extension": "2210", "user": None, "display_name": "דנה"}]
    assert result["recording"] == "recording.wav"
    assert result["transcript"][1] == {"start": 61.0, "end": 62.5, "text": "נדבר מחר"}
    assert result["summary_available"] is True
    assert result["action_items"] == [{"task": "לשלוח | לבדוק", "owner": None, "due": "מחר", "source_ts": "00:01:01"}]
    assert result["models"] == MODELS
    assert result["generated_at"]


def test_update_owners_rewrites_only_owners(tmp_path: Path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar)
    before = json.loads(_read(out / RESULT_FILE))

    update_owners(out, [Party("1042", "meir", "מאיר"), Party("3000", "yossi", "יוסי")])

    after = json.loads(_read(out / RESULT_FILE))
    assert [o["extension"] for o in after["owners"]] == ["1042", "3000"]
    assert {k: v for k, v in after.items() if k != "owners"} == {k: v for k, v in before.items() if k != "owners"}


def test_markdown_bodies_are_right_to_left(tmp_path: Path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar)

    for name in (TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE):
        text = _read(out / name)
        assert text.startswith('<div dir="rtl">\n\n#')
        assert text.endswith("</div>\n")


def test_timings_are_recorded(tmp_path: Path, make_sidecar) -> None:
    sidecar = parse_sidecar(make_sidecar(tmp_path / "s.json", call_id="gc1").read_text(encoding="utf-8"))
    timings = {"audio_sec": 1.5, "stt_sec": 20.0, "summarize_sec": 4.0, "hangup_to_output_sec": 95.0}

    path = write_outputs(
        tmp_path / "out",
        sidecar=sidecar,
        segments=SEGMENTS,
        summary=SUMMARY,
        owners=[sidecar.line_owner],
        models=MODELS,
        recording=tmp_path / "out" / "recording.wav",
        timings=timings,
    )

    assert json.loads(_read(path))["timings"] == timings


def test_write_atomic_retries_while_a_reader_holds_the_file(tmp_path: Path, monkeypatch) -> None:
    real_replace = Path.replace
    failures = iter([PermissionError("in use"), PermissionError("in use")])

    def flaky_replace(self: Path, target: Path) -> Path:
        error = next(failures, None)
        if error is not None:
            raise error
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr("jabberscribe.output.time.sleep", lambda seconds: None)

    write_atomic(tmp_path / "result.json", "{}")

    assert _read(tmp_path / "result.json") == "{}"


def test_write_atomic_gives_up_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []

    def locked(self: Path, target: Path) -> Path:
        calls.append(target)
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr("jabberscribe.output.time.sleep", lambda seconds: None)

    with pytest.raises(PermissionError):
        write_atomic(tmp_path / "result.json", "{}")

    assert len(calls) == REPLACE_ATTEMPTS
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_output.py -q`
Expected: FAIL — `ImportError: cannot import name 'REPLACE_ATTEMPTS' from 'jabberscribe.output'`.

- [ ] **Step 3: Rewrite `jabberscribe/output.py`**

```python
"""Per-call output folder: Markdown for people, JSON for machines.

result.json is the contract with the future web app; the Markdown files are
renderings of the same data. Every file is written via .part + rename so a
reader never sees half a file.

The Markdown bodies are wrapped in <div dir="rtl">: mixed Hebrew and English
lines and tables render scrambled in most viewers without it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from jabberscribe.jobs import utcnow
from jabberscribe.sidecar import Party, Sidecar
from jabberscribe.stt import Segment, format_ts
from jabberscribe.summarize import Summary

TRANSCRIPT_FILE = "transcript.md"
SUMMARY_FILE = "summary.md"
ACTIONS_FILE = "actions.md"
RESULT_FILE = "result.json"
TEXT_FILES = (TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE, RESULT_FILE)

SUMMARY_UNAVAILABLE = "הסיכום אינו זמין עבור שיחה זו."

#: A reader holding the destination open (Explorer preview, an editor, AV) blocks os.replace on Windows.
REPLACE_ATTEMPTS = 5
REPLACE_DELAY_SECONDS = 0.2


def write_atomic(path: Path, text: str) -> None:
    """Write via .part + rename, retrying the rename while a reader holds the file.

    Raises PermissionError when the file stays locked; the .part is removed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(REPLACE_DELAY_SECONDS)


def _rtl(markdown: str) -> str:
    # The blank lines matter: without them CommonMark treats the body as raw HTML, not Markdown.
    return '<div dir="rtl">\n\n' + markdown + "\n</div>\n"


def _cell(value: str | None) -> str:
    return (value or "—").replace("|", "\\|").replace("\n", " ")


def _render_transcript(segments: list[Segment]) -> str:
    # Blank lines between segments: consecutive Markdown lines would merge into one paragraph.
    lines = [f"[{format_ts(s.start)}] {s.text}" for s in segments]
    return _rtl("# תמליל\n\n" + "\n\n".join(lines) + "\n")


def _render_summary(summary: Summary | None) -> str:
    return _rtl("# סיכום\n\n" + (summary.text if summary else SUMMARY_UNAVAILABLE) + "\n")


def _render_actions(summary: Summary | None) -> str:
    if summary is None:
        return _rtl("# משימות\n\n" + SUMMARY_UNAVAILABLE + "\n")
    if not summary.action_items:
        return _rtl("# משימות\n\nלא עלו משימות בשיחה.\n")
    rows = ["| משימה | אחראי | מועד | זמן בהקלטה |", "|---|---|---|---|"]
    rows += [f"| {_cell(i.task)} | {_cell(i.owner)} | {_cell(i.due)} | {i.source_ts} |" for i in summary.action_items]
    return _rtl("# משימות\n\n" + "\n".join(rows) + "\n")


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def write_outputs(
    out_dir: Path,
    *,
    sidecar: Sidecar,
    segments: list[Segment],
    summary: Summary | None,
    owners: list[Party],
    models: dict[str, str],
    recording: Path,
    timings: dict[str, float] | None = None,
) -> Path:
    """Write every output file for one call. Returns the result.json path.

    `timings` holds per-stage seconds and the hang-up-to-output latency (see pipeline.py).
    """
    write_atomic(out_dir / TRANSCRIPT_FILE, _render_transcript(segments))
    write_atomic(out_dir / SUMMARY_FILE, _render_summary(summary))
    write_atomic(out_dir / ACTIONS_FILE, _render_actions(summary))
    result = {
        "schema_version": 1,
        "job_key": sidecar.job_key,
        "call_id": sidecar.call_id,
        "conference_id": sidecar.conference_id,
        "kind": sidecar.kind,
        "started_at": sidecar.started_at,
        "ended_at": sidecar.ended_at,
        "duration_sec": sidecar.duration_sec,
        "owners": [o.as_dict() for o in owners],
        "parties": [p.as_dict() for p in sidecar.parties],
        "recording": recording.name,
        "transcript": [asdict(s) for s in segments],
        "summary_available": summary is not None,
        "summary": summary.text if summary else None,
        "action_items": [asdict(i) for i in summary.action_items] if summary else [],
        "models": models,
        "timings": timings or {},
        "generated_at": utcnow(),
    }
    path = out_dir / RESULT_FILE
    write_atomic(path, _dump(result))
    return path


def update_owners(out_dir: Path, owners: list[Party]) -> None:
    """Replace the owner list of an already-written call (a late conference copy joined).

    Raises OSError (e.g. PermissionError while a reader holds the file); the caller retries later.
    """
    path = out_dir / RESULT_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["owners"] = [o.as_dict() for o in owners]
    write_atomic(path, _dump(data))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_output.py -q`
Expected: 12 passed.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (165 passed).

- [ ] **Step 6: Commit**

```bash
git add jabberscribe/output.py tests/test_output.py
git commit -m "fix(output): retry locked renames, right-to-left Markdown, stage timings" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: Conference grouping

Replaces original Task 8. Fixes audit C1 (a conference is released on the first leaver's copy and stays truncated), C2 (a reused `conference_id` attaches a new meeting to an old one), I3 (a FAILED primary takes its whole conference down), M9 (owner dropped after a crash between output and DONE), M12 (a test that asserts nothing), and the `settle` half of I11/I1 (a failed owner update must not crash the service).

**Controller decisions implemented here:**
- A group is the copies of one `conference_id` whose spans `[started_at, started_at + duration_sec]` overlap, allowing `group.settle_seconds` of slack. A copy that overlaps no existing group starts a new one (WAITING).
- Release stays "quiet or overdue". When a later copy is **longer** and **ends more than `settle_seconds` after** the current primary, it becomes the primary: `hand_over` + `reset_job` (processed from scratch), the old primary becomes GROUPED, and the old primary's text outputs and work folder are deleted and audited as `superseded`. Requiring "longer" guarantees each replacement strictly increases duration, so replacements terminate. **This deviates from spec §6 "processed once" and needs owner sign-off (extra GPU cost); Task 19 records it in the spec.**
- A WAITING copy never attaches to a FAILED primary. A FAILED primary with GROUPED members hands its conference to the next-longest member; the failed job keeps status FAILED with `grouped_into` set, so it stays an owner and is never elected (or `retry`-ed) again.
- When `result.json` exists (any status), a late copy is first added to its owners; if `update_owners` raises `OSError`/`ValueError`, the copy stays WAITING and the next poll retries.

**Files:**
- Create: `jabberscribe/group.py`
- Test: `tests/test_group.py`

**Interfaces:**
- Consumes: `Config` (committed); `AuditLog`, `SUPERSEDED` (Task 9); `scan_once` (Task 9, tests only); `JobStore.conference_ids_to_settle`, `conference_jobs`, `members`, `group_into`, `set_status`, `hand_over`, `reset_job`, `get`, constants `FAILED`, `GROUPED`, `QUEUED`, `WAITING`, `Job` (Task 10); `parse_sidecar`, `Party` (Task 8); `RESULT_FILE`, `TEXT_FILES`, `update_owners` (Task 13).
- Produces:
  - `SettleResult(released: tuple[str, ...] = (), attached: tuple[str, ...] = (), superseded: tuple[str, ...] = ())`
  - `settle(cfg: Config, store: JobStore, audit: AuditLog, now: datetime, *, conference_id: str | None = None) -> SettleResult` (`now` timezone-aware; `conference_id` limits the pass to one conference)
  - `owners_for(job: Job, store: JobStore) -> list[Party]` — the job's own line owner first, then each member's (any status, including a handed-over FAILED job), deduplicated by extension.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_group.py`:

```python
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from jabberscribe.audit import SUPERSEDED
from jabberscribe.group import SettleResult, owners_for, settle
from jabberscribe.jobs import DONE, FAILED, GROUPED, QUEUED, RUNNING, WAITING
from jabberscribe.output import RESULT_FILE, TRANSCRIPT_FILE
from jabberscribe.watcher import scan_once


def _drop(cfg, store, audit, make_wav, make_sidecar, leg: str, ext: str, *, conference_id="conf-1", **extra) -> str:
    make_wav(cfg.paths.inbox / f"{leg}.wav")
    make_sidecar(cfg.paths.inbox / f"{leg}.json", call_id=leg, extension=ext, conference_id=conference_id, **extra)
    scan_once(cfg, store, audit, min_age_seconds=0)
    return f"{leg}_{ext}"


def _age(cfg, key: str, seconds: int) -> None:
    """Backdate a job's arrival; the store stamps created_at itself."""
    created = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    conn = sqlite3.connect(cfg.paths.db_path)
    conn.execute("UPDATE jobs SET created_at = ? WHERE job_key = ?", (created, key))
    conn.commit()
    conn.close()


def _now() -> datetime:
    return datetime.now(UTC)


def _settle(cfg, store, audit, **kwargs) -> SettleResult:
    return settle(cfg, store, audit, _now(), **kwargs)


def _release(cfg, store, audit, *keys: str) -> SettleResult:
    for key in keys:
        _age(cfg, key, 61)
    return _settle(cfg, store, audit)


def _finish(store, key: str, owners: list | None = None) -> None:
    """Pretend the pipeline wrote outputs for `key` and marked it DONE."""
    out_dir = store.get(key).out_dir
    (out_dir / RESULT_FILE).write_text(json.dumps({"owners": owners or []}), encoding="utf-8")
    (out_dir / TRANSCRIPT_FILE).write_text("x", encoding="utf-8")
    store.set_status(key, DONE)


def _owners(store, key: str) -> list[str]:
    data = json.loads((store.get(key).out_dir / RESULT_FILE).read_text(encoding="utf-8"))
    return [o["extension"] for o in data["owners"]]


def test_fresh_copy_keeps_waiting(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")

    assert _settle(cfg, store, audit) == SettleResult()
    assert store.get(key).status == WAITING


def test_quiet_group_releases_longest_copy(cfg, store, audit, make_wav, make_sidecar) -> None:
    short = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=100)
    longest = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=300)
    middle = _drop(cfg, store, audit, make_wav, make_sidecar, "c", "3000", duration_sec=200)

    result = _release(cfg, store, audit, short, longest, middle)

    assert result.released == (longest,)
    assert sorted(result.attached) == sorted([short, middle])
    assert store.get(longest).status == QUEUED
    assert {m.job_key for m in store.members(longest)} == {short, middle}
    assert store.get(short).status == GROUPED


def test_duration_tie_goes_to_earliest_start(cfg, store, audit, make_wav, make_sidecar) -> None:
    late = _drop(
        cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at="2026-10-07T14:05:00+03:00", duration_sec=600
    )
    early = _drop(
        cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at="2026-10-07T14:03:00+03:00", duration_sec=600
    )

    assert _release(cfg, store, audit, late, early).released == (early,)


def test_overdue_group_is_released_despite_new_arrivals(cfg, store, audit, make_wav, make_sidecar) -> None:
    first = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")
    _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210")
    _age(cfg, first, 301)

    assert _settle(cfg, store, audit).released == (first,)


def test_late_shorter_copy_joins_released_primary(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=600)
    _release(cfg, store, audit, primary)

    late = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=300)
    result = _settle(cfg, store, audit)

    assert result.attached == (late,)
    assert store.get(late).grouped_into == primary
    assert store.get(primary).status == QUEUED


def test_late_copy_after_done_updates_result_owners(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")
    _release(cfg, store, audit, primary)
    _finish(store, primary)

    _drop(cfg, store, audit, make_wav, make_sidecar, "b", "3000")
    _settle(cfg, store, audit)

    assert _owners(store, primary) == ["1042", "3000"]


def test_late_copy_updates_owners_even_if_done_was_never_recorded(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A crash between the output stage and DONE leaves the primary RUNNING with a result.json."""
    primary = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")
    _release(cfg, store, audit, primary)
    _finish(store, primary)
    store.set_status(primary, RUNNING)

    _drop(cfg, store, audit, make_wav, make_sidecar, "b", "3000")
    _settle(cfg, store, audit)

    assert _owners(store, primary) == ["1042", "3000"]


def test_locked_result_keeps_the_copy_waiting_until_next_poll(cfg, store, audit, make_wav, make_sidecar, monkeypatch):
    primary = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")
    _release(cfg, store, audit, primary)
    _finish(store, primary)
    late = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "3000")

    def locked(out_dir, owners):
        raise PermissionError("result.json is open in Explorer")

    monkeypatch.setattr("jabberscribe.group.update_owners", locked)
    assert _settle(cfg, store, audit).attached == ()
    assert store.get(late).status == WAITING

    monkeypatch.undo()
    assert _settle(cfg, store, audit).attached == (late,)
    assert _owners(store, primary) == ["1042", "3000"]


def test_plain_calls_are_not_touched(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", conference_id=None)

    assert _settle(cfg, store, audit) == SettleResult()
    assert store.get(key).status == QUEUED


def test_owners_dedupe_by_extension(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A participant who dropped and rejoined yields two copies from one line."""
    first = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=300)
    _drop(cfg, store, audit, make_wav, make_sidecar, "b", "1042")
    _drop(cfg, store, audit, make_wav, make_sidecar, "c", "2210")
    _release(cfg, store, audit, "a_1042", "b_1042", "c_2210")

    assert [o.extension for o in owners_for(store.get(first), store)] == ["1042", "2210"]


def test_longer_later_copy_replaces_an_early_leavers_primary(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A leaves at minute 8 and is processed; the host's 60-minute copy must not be thrown away."""
    start = "2026-10-07T14:00:00+03:00"
    leaver = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at=start, duration_sec=480)
    _release(cfg, store, audit, leaver)
    _finish(store, leaver)

    host = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at=start, duration_sec=3600)
    other = _drop(cfg, store, audit, make_wav, make_sidecar, "c", "3000", started_at=start, duration_sec=3500)
    result = _settle(cfg, store, audit)

    assert result.released == (host,)
    assert result.superseded == (leaver,)
    assert result.attached == (other,)
    assert (store.get(host).status, store.get(host).stage) == (QUEUED, QUEUED)
    assert (store.get(leaver).status, store.get(leaver).grouped_into) == (GROUPED, host)
    assert {m.job_key for m in store.members(host)} == {leaver, other}
    assert not (store.get(leaver).out_dir / RESULT_FILE).exists()
    assert not (store.get(leaver).out_dir / TRANSCRIPT_FILE).exists()
    assert store.get(leaver).audio_path.is_file()
    entry = audit.entries(leaver)[-1]
    assert entry.action == SUPERSEDED
    assert host in entry.detail
    assert RESULT_FILE in entry.detail


def test_later_copy_that_is_not_longer_just_joins(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _drop(
        cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at="2026-10-07T14:00:00+03:00", duration_sec=600
    )
    _release(cfg, store, audit, primary)
    late = _drop(
        cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at="2026-10-07T14:09:00+03:00", duration_sec=300
    )

    result = _settle(cfg, store, audit)

    assert (result.attached, result.superseded) == ((late,), ())


def test_reused_conference_id_starts_a_new_group(cfg, store, audit, make_wav, make_sidecar) -> None:
    """The weekly Meet-Me call reuses its id; week 2 must get its own transcript and owners."""
    week1 = _drop(cfg, store, audit, make_wav, make_sidecar, "w1", "1042", started_at="2026-09-30T10:00:00+03:00")
    _release(cfg, store, audit, week1)
    _finish(store, week1, owners=[{"extension": "1042"}])

    week2 = _drop(cfg, store, audit, make_wav, make_sidecar, "w2", "3000", started_at="2026-10-07T10:00:00+03:00")
    assert _settle(cfg, store, audit) == SettleResult()
    assert store.get(week2).status == WAITING

    assert _release(cfg, store, audit, week2).released == (week2,)
    assert store.get(week2).grouped_into is None
    assert _owners(store, week1) == ["1042"]


def test_failed_primary_hands_its_conference_to_the_next_longest_copy(
    cfg, store, audit, make_wav, make_sidecar
) -> None:
    longest = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=300)
    second = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=200)
    third = _drop(cfg, store, audit, make_wav, make_sidecar, "c", "3000", duration_sec=100)
    _release(cfg, store, audit, longest, second, third)
    store.set_status(longest, FAILED)

    result = _settle(cfg, store, audit)

    assert (result.released, result.superseded) == ((second,), (longest,))
    assert store.get(second).status == QUEUED
    assert (store.get(longest).status, store.get(longest).grouped_into) == (FAILED, second)
    assert {m.job_key for m in store.members(second)} == {longest, third}
    assert [o.extension for o in owners_for(store.get(second), store)] == ["2210", "1042", "3000"]


def test_late_copy_takes_over_a_failed_primary_without_members(cfg, store, audit, make_wav, make_sidecar) -> None:
    failed = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=300)
    _release(cfg, store, audit, failed)
    store.set_status(failed, FAILED)

    late = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=100)
    result = _settle(cfg, store, audit)

    assert result.released == (late,)
    assert store.get(late).status == QUEUED
    assert store.get(failed).grouped_into == late


def test_settle_can_be_limited_to_one_conference(cfg, store, audit, make_wav, make_sidecar) -> None:
    mine = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", conference_id="conf-1")
    other = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", conference_id="conf-2")
    _age(cfg, mine, 61)
    _age(cfg, other, 61)

    assert _settle(cfg, store, audit, conference_id="conf-1").released == (mine,)
    assert store.get(other).status == WAITING
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_group.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.group'`.

- [ ] **Step 3: Create `jabberscribe/group.py`**

```python
"""Conference grouping: one meeting, one transcript.

CUCM forks every participating line separately, so a conference arrives as
several recordings sharing a conference_id. A group is the copies of one
conference_id whose time spans overlap (within settle_seconds): a reused
conference_id -- the weekly Meet-Me number -- therefore starts a new group
instead of joining last week's meeting.

New copies wait (status WAITING) until their group goes quiet or has waited
max_wait_seconds. Then the longest copy is processed and the rest are attached
to it, so every participating line owner gets the same single result.

A group can be released before the meeting is over: the first participant to
hang up produces the first copy. When a later copy is longer and ends more
than settle_seconds after the primary, it replaces the primary: it is
processed from scratch and the old primary's text outputs are deleted
(audited as superseded). This trades extra GPU time for a complete transcript
and is a deviation from "processed once" that needs owner sign-off.

A FAILED primary never keeps its conference: the next-longest copy is elected.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jabberscribe.audit import SUPERSEDED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import FAILED, GROUPED, QUEUED, WAITING, Job, JobStore
from jabberscribe.output import RESULT_FILE, TEXT_FILES, update_owners
from jabberscribe.sidecar import Party, parse_sidecar

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettleResult:
    #: Job keys that became the processed copy of their conference (new, replacing, or re-elected).
    released: tuple[str, ...] = ()
    #: Job keys attached to a primary instead of being processed.
    attached: tuple[str, ...] = ()
    #: Former primaries replaced by a longer copy or by a re-election after failure.
    superseded: tuple[str, ...] = ()


@dataclass
class _Changes:
    released: list[str] = field(default_factory=list)
    attached: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)


def owners_for(job: Job, store: JobStore) -> list[Party]:
    """The job's own line owner, then each attached copy's, one per extension."""
    owners: list[Party] = []
    for member in [job, *store.members(job.job_key)]:
        _add_owner(owners, parse_sidecar(member.sidecar_json).line_owner)
    return owners


def _add_owner(owners: list[Party], owner: Party) -> None:
    if all(o.extension != owner.extension for o in owners):
        owners.append(owner)


def _start(job: Job) -> datetime:
    return datetime.fromisoformat(job.started_at)


def _end(job: Job) -> datetime:
    return _start(job) + timedelta(seconds=job.duration_sec)


def _rank(job: Job) -> tuple[int, datetime]:
    # Longest recording first; a tie goes to whoever joined first.
    return (-job.duration_sec, _start(job))


def _span(primary: Job, store: JobStore) -> tuple[datetime, datetime]:
    jobs = [primary, *store.members(primary.job_key)]
    return min(_start(j) for j in jobs), max(_end(j) for j in jobs)


def _overlaps(job: Job, span: tuple[datetime, datetime], slack: timedelta) -> bool:
    start, end = span
    return _start(job) <= end + slack and _end(job) >= start - slack


def _clusters(jobs: list[Job], slack: timedelta) -> list[list[Job]]:
    """Group copies whose time spans overlap, allowing `slack` between them."""
    clusters: list[list[Job]] = []
    cluster_end: datetime | None = None
    for job in sorted(jobs, key=_start):
        if cluster_end is not None and _start(job) <= cluster_end + slack:
            clusters[-1].append(job)
            cluster_end = max(cluster_end, _end(job))
        else:
            clusters.append([job])
            cluster_end = _end(job)
    return clusters


def _discard_outputs(cfg: Config, audit: AuditLog, old: Job, new_key: str) -> None:
    removed = [name for name in TEXT_FILES if (old.out_dir / name).is_file()]
    for name in removed:
        (old.out_dir / name).unlink()
    work = cfg.paths.work_dir / old.job_key
    if work.is_dir():
        shutil.rmtree(work)
        removed.append("work")
    if removed:
        audit.record(old.job_key, SUPERSEDED, f"replaced by {new_key}: {', '.join(removed)}")


def _hand_over(cfg: Config, store: JobStore, audit: AuditLog, old: Job, new: Job, changes: _Changes) -> Job:
    """Make `new` the conference's primary, processed from scratch; `old` becomes a member."""
    store.hand_over(old.job_key, new.job_key)
    store.reset_job(new.job_key)
    _discard_outputs(cfg, audit, old, new.job_key)
    changes.released.append(new.job_key)
    changes.superseded.append(old.job_key)
    log.info("conference %s: %s replaces %s as primary", new.conference_id, new.job_key, old.job_key)
    refreshed = store.get(new.job_key)
    assert refreshed is not None
    return refreshed


def _attach(store: JobStore, copy: Job, primary: Job, changes: _Changes) -> None:
    """Attach a late copy. If the primary already wrote result.json, add the owner there first.

    When result.json cannot be updated (a reader holds it open), the copy stays
    WAITING and the next poll tries again; nothing is lost.
    """
    if (primary.out_dir / RESULT_FILE).is_file():
        owners = owners_for(primary, store)
        _add_owner(owners, parse_sidecar(copy.sidecar_json).line_owner)
        try:
            update_owners(primary.out_dir, owners)
        except (OSError, ValueError) as exc:
            log.warning("cannot add owner %s to %s yet, retrying next poll: %s", copy.job_key, primary.job_key, exc)
            return
    store.group_into(copy.job_key, primary.job_key)
    changes.attached.append(copy.job_key)


def _replaces(copy: Job, primary: Job, slack: timedelta) -> bool:
    return copy.duration_sec > primary.duration_sec and _end(copy) > _end(primary) + slack


def _settle_conference(
    cfg: Config, store: JobStore, audit: AuditLog, now: datetime, conference_id: str, changes: _Changes
) -> None:
    slack = timedelta(seconds=cfg.group.settle_seconds)
    jobs = store.conference_jobs(conference_id)
    primaries = [j for j in jobs if j.status not in (WAITING, GROUPED) and j.grouped_into is None]
    unmatched: list[Job] = []

    for copy in sorted((j for j in jobs if j.status == WAITING), key=_rank):
        index = next((i for i, p in enumerate(primaries) if _overlaps(copy, _span(p, store), slack)), None)
        if index is None:
            unmatched.append(copy)
            continue
        primary = primaries[index]
        if primary.status == FAILED:
            # Never attach to a failed primary: join as a candidate for the re-election below.
            store.group_into(copy.job_key, primary.job_key)
        elif _replaces(copy, primary, slack):
            primaries[index] = _hand_over(cfg, store, audit, primary, copy, changes)
        else:
            _attach(store, copy, primary, changes)

    for primary in primaries:
        if primary.status != FAILED:
            continue
        candidates = [m for m in store.members(primary.job_key) if m.status == GROUPED]
        if candidates:
            _hand_over(cfg, store, audit, primary, min(candidates, key=_rank), changes)

    for cluster in _clusters(unmatched, slack):
        arrivals = [datetime.fromisoformat(j.created_at) for j in cluster]
        quiet = (now - max(arrivals)).total_seconds() >= cfg.group.settle_seconds
        overdue = (now - min(arrivals)).total_seconds() >= cfg.group.max_wait_seconds
        if not (quiet or overdue):
            continue
        chosen = min(cluster, key=_rank)
        store.set_status(chosen.job_key, QUEUED)
        changes.released.append(chosen.job_key)
        for job in cluster:
            if job.job_key != chosen.job_key:
                store.group_into(job.job_key, chosen.job_key)
                changes.attached.append(job.job_key)
        log.info("conference %s: processing %s for %d copies", conference_id, chosen.job_key, len(cluster))


def settle(
    cfg: Config, store: JobStore, audit: AuditLog, now: datetime, *, conference_id: str | None = None
) -> SettleResult:
    """Release, attach, replace and re-elect conference copies. `now` must be timezone-aware.

    `conference_id` limits the pass to one conference (the `process` command).
    """
    changes = _Changes()
    for cid in [conference_id] if conference_id else store.conference_ids_to_settle():
        _settle_conference(cfg, store, audit, now, cid, changes)
    return SettleResult(tuple(changes.released), tuple(changes.attached), tuple(changes.superseded))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_group.py -q`
Expected: 16 passed.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (181 passed).

- [ ] **Step 6: Commit**

```bash
git add jabberscribe/group.py tests/test_group.py
git commit -m "feat(group): overlap-scoped conference groups with re-election" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Pipeline with retry policy

Replaces original Task 9. Fixes audit C3 (an outage FAILs calls; one poisoned job blocks the queue), I9 (a transient summary outage loses the summary forever — the summarizer now raises and the job retries from the `stt` checkpoint), I4(a) (`stt.ogg` survives a FAILED job), M10 (`parse_sidecar` outside the `try`), and spec §7 `timings`.

**Files:**
- Create: `jabberscribe/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `prepare_for_stt`, `STT_FILENAME` (Task 11); `Config` (committed); `owners_for` (Task 14); `JobStore.claim_next(now)`, `claim`, `schedule_retry`, `record_attempt`, `complete_stage`, `set_status`, `get`, `next_stage`, `DONE`, `FAILED`, `Job` (Task 10); `TransientError` (Task 11); `RESULT_FILE`, `write_atomic`, `write_outputs(..., timings=)` (Task 13); `parse_sidecar`, `Sidecar` (Task 8); `Segment`, `Transcriber` (Task 11); `ActionItem`, `Summary`, `Summarizer` (Task 12).
- Produces:
  - constants `MAX_ATTEMPTS = 3`, `BACKOFF_BASE_SECONDS = 30`, `BACKOFF_CAP_SECONDS = 1800`, `SEGMENTS_FILE = "segments.json"`, `SUMMARY_JSON = "summary.json"`, `TIMINGS_FILE = "timings.json"` (all in `work/<job_key>/`)
  - `backoff(attempts: int) -> timedelta`
  - `process_job(job: Job, cfg: Config, store: JobStore, transcriber: Transcriber, summarizer: Summarizer) -> Path` — records the attempt as `"<stage>: <error>"` and re-raises on failure.
  - `run_once(cfg: Config, store: JobStore, transcriber: Transcriber, summarizer: Summarizer, now: datetime | None = None) -> int` — processes every job due at `now`; returns how many were attempted.
  - `run_job(job_key: str, cfg: Config, store: JobStore, transcriber: Transcriber, summarizer: Summarizer) -> bool` — runs that one job now (due or not); False if it is not QUEUED/RUNNING.
  - `result.json` `timings` keys: `audio_sec`, `stt_sec`, `summarize_sec`, `hangup_to_output_sec` (seconds from `started_at + duration_sec` to the output stage).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline.py`:

```python
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jabberscribe.audio import STT_FILENAME
from jabberscribe.jobs import DONE, FAILED, QUEUED
from jabberscribe.llm import TransientError
from jabberscribe.output import ACTIONS_FILE, RESULT_FILE, SUMMARY_FILE, TRANSCRIPT_FILE
from jabberscribe.pipeline import MAX_ATTEMPTS, backoff, run_job, run_once
from jabberscribe.stt import Segment, SttError
from jabberscribe.summarize import ActionItem, Summary
from jabberscribe.watcher import scan_once

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

SUMMARY = Summary("סיכום.", (ActionItem("לשלוח את הדוח", "דנה", None, "00:00:01"),))
T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def transcribe(self, audio: Path) -> list[Segment]:
        self.calls.append(audio)
        return [Segment(0.0, 1.5, "אה, שלום"), Segment(1.5, 3.0, "נדבר מחר")]


class FailingTranscriber:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def transcribe(self, audio: Path) -> list[Segment]:
        raise self.error


class FakeSummarizer:
    def __init__(self, result: Summary | None = SUMMARY) -> None:
        self.result = result
        self.seen: list[Segment] | None = None

    def summarize(self, segments: list[Segment]) -> Summary | None:
        self.seen = segments
        return self.result


class ExplodingSummarizer:
    def summarize(self, segments: list[Segment]) -> Summary | None:
        raise RuntimeError("summarizer bug")


def _enqueue(cfg, store, audit, make_wav, make_sidecar, *, call_id="abc", extension="1042", **extra) -> str:
    make_wav(cfg.paths.inbox / f"{call_id}{extension}.wav", channels=2)
    make_sidecar(
        cfg.paths.inbox / f"{call_id}{extension}.json", call_id=call_id, extension=extension, tracks="dual", **extra
    )
    scan_once(cfg, store, audit, min_age_seconds=0)
    return f"{call_id}_{extension}"


def _result(job) -> dict:
    return json.loads((job.out_dir / RESULT_FILE).read_text(encoding="utf-8"))


def test_end_to_end_writes_outputs_and_marks_done(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    assert run_once(cfg, store, FakeTranscriber(), FakeSummarizer()) == 1

    job = store.get(key)
    assert job.status == DONE
    result = _result(job)
    assert result["transcript"][0]["text"] == "אה, שלום"
    assert result["summary"] == "סיכום."
    assert result["models"] == {"stt": "whisper-he", "summary": "gemma-3"}
    for name in ("recording.wav", TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE):
        assert (job.out_dir / name).is_file()


def test_result_records_stage_timings_and_latency(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    timings = _result(store.get(key))["timings"]
    assert set(timings) == {"audio_sec", "stt_sec", "summarize_sec", "hangup_to_output_sec"}
    assert timings["hangup_to_output_sec"] > 0


def test_summarizer_gets_the_transcript(cfg, store, audit, make_wav, make_sidecar) -> None:
    _enqueue(cfg, store, audit, make_wav, make_sidecar)
    summarizer = FakeSummarizer()

    run_once(cfg, store, FakeTranscriber(), summarizer)

    assert [s.text for s in summarizer.seen] == ["אה, שלום", "נדבר מחר"]


def test_stt_audio_is_removed_after_transcription(cfg, store, audit, make_wav, make_sidecar) -> None:
    """The Opus copy is the voice too; keeping it would dodge audio retention."""
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    assert not (cfg.paths.work_dir / key / STT_FILENAME).exists()


def test_unavailable_summary_still_completes(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer(None))

    job = store.get(key)
    assert job.status == DONE
    assert _result(job)["summary_available"] is False


def test_resume_after_crash_does_not_retranscribe(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), ExplodingSummarizer(), now=T0)
    crashed = store.get(key)
    assert (crashed.status, crashed.stage, crashed.attempts) == (QUEUED, "stt", 1)
    assert "summarize: summarizer bug" in crashed.last_error

    transcriber = FakeTranscriber()
    run_once(cfg, store, transcriber, FakeSummarizer(), now=T0 + timedelta(minutes=1))

    assert store.get(key).status == DONE
    assert transcriber.calls == []


def test_failed_job_waits_for_its_backoff(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    run_once(cfg, store, FailingTranscriber(SttError("bad audio")), FakeSummarizer(), now=T0)

    assert store.get(key).next_attempt_at == "2026-10-07T12:00:30+00:00"
    assert run_once(cfg, store, FakeTranscriber(), FakeSummarizer(), now=T0 + timedelta(seconds=29)) == 0
    assert run_once(cfg, store, FakeTranscriber(), FakeSummarizer(), now=T0 + timedelta(seconds=30)) == 1


def test_permanent_failure_gives_up_and_deletes_the_stt_copy(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    transcriber = FailingTranscriber(SttError("HTTP 413 too large"))

    for hour in range(MAX_ATTEMPTS):
        assert store.get(key).status == QUEUED
        run_once(cfg, store, transcriber, FakeSummarizer(), now=T0 + timedelta(hours=hour))
        if hour == 0:
            assert (cfg.paths.work_dir / key / STT_FILENAME).is_file()

    job = store.get(key)
    assert job.status == FAILED
    assert "413" in job.last_error
    assert not (cfg.paths.work_dir / key / STT_FILENAME).exists()


def test_transient_failure_never_fails_the_job(cfg, store, audit, make_wav, make_sidecar) -> None:
    """An hour-long LiteLLM outage delays calls; it must not lose them."""
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    transcriber = FailingTranscriber(TransientError("HTTP 503"))

    for hour in range(10):
        run_once(cfg, store, transcriber, FakeSummarizer(), now=T0 + timedelta(hours=hour))

    job = store.get(key)
    assert (job.status, job.attempts) == (QUEUED, 10)
    run_once(cfg, store, FakeTranscriber(), FakeSummarizer(), now=T0 + timedelta(hours=10))
    assert store.get(key).status == DONE


def test_a_failing_job_does_not_block_the_queue(cfg, store, audit, make_wav, make_sidecar) -> None:
    first = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="aaa")
    second = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="bbb")

    class FailFirst(FakeTranscriber):
        def transcribe(self, audio: Path) -> list[Segment]:
            if first in str(audio):
                raise SttError("poisoned")
            return super().transcribe(audio)

    assert run_once(cfg, store, FailFirst(), FakeSummarizer(), now=T0) == 2
    assert store.get(first).status == QUEUED
    assert store.get(second).status == DONE


def test_backoff_doubles_up_to_thirty_minutes() -> None:
    assert [backoff(n).total_seconds() for n in (1, 2, 3, 6, 7, 20)] == [30, 60, 120, 960, 1800, 1800]


def test_unreadable_sidecar_counts_as_an_attempt(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    store.scrub_sidecar(key)

    for hour in range(MAX_ATTEMPTS):
        run_once(cfg, store, FakeTranscriber(), FakeSummarizer(), now=T0 + timedelta(hours=hour))

    assert store.get(key).status == FAILED


def test_grouped_copies_are_listed_as_owners(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _enqueue(
        cfg, store, audit, make_wav, make_sidecar, call_id="leg1", extension="1042", conference_id="conf-1"
    )
    member = _enqueue(
        cfg, store, audit, make_wav, make_sidecar, call_id="leg2", extension="3000", conference_id="conf-1"
    )
    store.set_status(primary, QUEUED)
    store.group_into(member, primary)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    assert [o["extension"] for o in _result(store.get(primary))["owners"]] == ["1042", "3000"]


def test_run_job_processes_only_that_job_even_if_not_due(cfg, store, audit, make_wav, make_sidecar) -> None:
    mine = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="mine")
    other = _enqueue(cfg, store, audit, make_wav, make_sidecar, call_id="other")
    store.schedule_retry(mine, datetime.now(UTC) + timedelta(hours=1))

    assert run_job(mine, cfg, store, FakeTranscriber(), FakeSummarizer()) is True

    assert store.get(mine).status == DONE
    assert store.get(other).status == QUEUED


def test_run_job_refuses_a_finished_job(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    store.set_status(key, DONE)

    assert run_job(key, cfg, store, FakeTranscriber(), FakeSummarizer()) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_pipeline.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.pipeline'`.

- [ ] **Step 3: Create `jabberscribe/pipeline.py`**

```python
"""Stage orchestration.

Stages run in the fixed order audio -> stt -> summarize -> output, and each one
checkpoints in the job store. A restart resumes at the first incomplete stage,
so a crash after transcription never transcribes again.

Retry policy. Every failure counts an attempt and schedules the next one with
exponential backoff (30 s, doubling, capped at 30 min); claim_next skips the
job until then, so it never blocks the jobs behind it.

- TransientError (LiteLLM down, overloaded, 5xx): retried forever. An outage
  delays calls; it must never lose them.
- Anything else (corrupt audio, a 4xx, a bug): FAILED after MAX_ATTEMPTS, for a
  human to inspect and `jabberscribe retry`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audio import STT_FILENAME, prepare_for_stt
from jabberscribe.config import Config
from jabberscribe.group import owners_for
from jabberscribe.jobs import DONE, FAILED, Job, JobStore, next_stage
from jabberscribe.llm import TransientError
from jabberscribe.output import RESULT_FILE, write_atomic, write_outputs
from jabberscribe.sidecar import Sidecar, parse_sidecar
from jabberscribe.stt import Segment, Transcriber
from jabberscribe.summarize import ActionItem, Summarizer, Summary

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 1800

SEGMENTS_FILE = "segments.json"
SUMMARY_JSON = "summary.json"
TIMINGS_FILE = "timings.json"


def backoff(attempts: int) -> timedelta:
    """Delay before the next try after `attempts` failures: 30 s, 60 s, 120 s, ... at most 30 min."""
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS))


def _write_segments(path: Path, segments: list[Segment]) -> None:
    write_atomic(path, json.dumps([asdict(s) for s in segments], ensure_ascii=False, indent=2))


def _read_segments(path: Path) -> list[Segment]:
    return [Segment(**s) for s in json.loads(path.read_text(encoding="utf-8"))]


def _write_summary(path: Path, summary: Summary | None) -> None:
    payload = None
    if summary is not None:
        payload = {"text": summary.text, "action_items": [asdict(i) for i in summary.action_items]}
    write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_summary(path: Path) -> Summary | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data is None:
        return None
    return Summary(data["text"], tuple(ActionItem(**i) for i in data["action_items"]))


def _read_timings(path: Path) -> dict[str, float]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _hangup(sidecar: Sidecar) -> datetime:
    return datetime.fromisoformat(sidecar.started_at) + timedelta(seconds=sidecar.duration_sec)


def _delete_stt_audio(work: Path) -> None:
    # The Opus copy is the voice too; it must not outlive the recording's retention.
    (work / STT_FILENAME).unlink(missing_ok=True)
    (work / f"{STT_FILENAME}.part").unlink(missing_ok=True)


def process_job(
    job: Job,
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    summarizer: Summarizer,
) -> Path:
    """Run `job` from its checkpoint to the end. Returns the result.json path.

    Raises on failure after recording the attempt, leaving the retry decision
    to the caller.
    """
    work = cfg.paths.work_dir / job.job_key
    segments_path = work / SEGMENTS_FILE
    summary_path = work / SUMMARY_JSON
    timings_path = work / TIMINGS_FILE
    stage = job.stage

    try:
        sidecar = parse_sidecar(job.sidecar_json)
        timings = _read_timings(timings_path)
        while (stage := next_stage(stage)) is not None:
            log.info("%s: stage %s", job.job_key, stage)
            began = time.monotonic()
            if stage == "audio":
                prepare_for_stt(job.audio_path, work)
            elif stage == "stt":
                _write_segments(segments_path, transcriber.transcribe(prepare_for_stt(job.audio_path, work)))
                _delete_stt_audio(work)
            elif stage == "summarize":
                _write_summary(summary_path, summarizer.summarize(_read_segments(segments_path)))
            elif stage == "output":
                timings["hangup_to_output_sec"] = round((datetime.now(UTC) - _hangup(sidecar)).total_seconds(), 1)
                write_outputs(
                    job.out_dir,
                    sidecar=sidecar,
                    segments=_read_segments(segments_path),
                    summary=_read_summary(summary_path),
                    owners=owners_for(job, store),
                    models={"stt": cfg.stt.model, "summary": cfg.summary.model},
                    recording=job.audio_path,
                    timings=timings,
                )
            if stage != "output":
                timings[f"{stage}_sec"] = round(time.monotonic() - began, 1)
                write_atomic(timings_path, json.dumps(timings))
            store.complete_stage(job.job_key, stage)
    except Exception as exc:
        store.record_attempt(job.job_key, f"{stage}: {exc}")
        log.exception("%s: stage %s failed", job.job_key, stage)
        raise

    store.set_status(job.job_key, DONE)
    log.info("%s: done", job.job_key)
    return job.out_dir / RESULT_FILE


def _run(
    job: Job, cfg: Config, store: JobStore, transcriber: Transcriber, summarizer: Summarizer, now: datetime
) -> None:
    try:
        process_job(job, cfg, store, transcriber, summarizer)
    except Exception as exc:
        attempts = store.get(job.job_key).attempts
        if isinstance(exc, TransientError) or attempts < MAX_ATTEMPTS:
            retry_at = now + backoff(attempts)
            store.schedule_retry(job.job_key, retry_at)
            log.warning("%s: attempt %d failed, retrying at %s", job.job_key, attempts, retry_at.isoformat())
        else:
            store.set_status(job.job_key, FAILED)
            _delete_stt_audio(cfg.paths.work_dir / job.job_key)
            log.error("%s: FAILED after %d attempts", job.job_key, attempts)


def run_once(
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    summarizer: Summarizer,
    now: datetime | None = None,
) -> int:
    """Process every job that is due once. Returns how many were attempted."""
    moment = now or datetime.now(UTC)
    processed = 0
    while (job := store.claim_next(moment)) is not None:
        processed += 1
        _run(job, cfg, store, transcriber, summarizer, moment)
    return processed


def run_job(job_key: str, cfg: Config, store: JobStore, transcriber: Transcriber, summarizer: Summarizer) -> bool:
    """Process one job now, due or not (the `process` command). Returns False if it was not runnable."""
    job = store.claim(job_key)
    if job is None:
        return False
    _run(job, cfg, store, transcriber, summarizer, datetime.now(UTC))
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_pipeline.py -q`
Expected: 15 passed.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (196 passed).

- [ ] **Step 6: Commit**

```bash
git add jabberscribe/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): checkpointed stages with transient-aware backoff" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: Complete retention

Replaces the retention half of original Task 10. Fixes audit I4 (STT copies, quarantine and inbox orphans escape audio retention), I5(a) (job metadata outlives text retention), I6 (a recorder clock in the past gets a fresh call deleted — age now counts from the later of `started_at` and `created_at`).

**Files:**
- Create: `jabberscribe/retention.py`
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: `Config` (committed); `AuditLog` and `PURGED_*`, `SCRUBBED_METADATA` (Task 9); `JobStore.list_all`, `scrub_sidecar`, `SCRUBBED_SIDECAR`, `Job` (Task 10); `STT_FILENAME` (Task 11); `TEXT_FILES` (Task 13).
- Produces:
  - `STT_LEFTOVER_DAYS = 1`
  - `PurgeResult(audio_deleted: tuple[str, ...], text_deleted: tuple[str, ...], swept: tuple[str, ...], errors: tuple[str, ...])` — `swept` holds file paths.
  - `purge(cfg: Config, store: JobStore, audit: AuditLog, now: datetime) -> PurgeResult` — per job: audio after `audio_days`, text files + `work/<key>/` + `sidecar_json` scrub after `text_days`; sweeps `work/*/stt.ogg*` older than 1 day (audit key: folder name), files in `quarantine/` and `inbox/` older than `audio_days` by mtime (audit key: file name up to the first dot).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_retention.py`:

```python
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jabberscribe.audit import (
    PURGED_AUDIO,
    PURGED_ORPHAN,
    PURGED_QUARANTINE,
    PURGED_STT_AUDIO,
    PURGED_TEXT,
    SCRUBBED_METADATA,
)
from jabberscribe.jobs import SCRUBBED_SIDECAR
from jabberscribe.output import RESULT_FILE, TEXT_FILES
from jabberscribe.retention import purge
from jabberscribe.watcher import scan_once

# Matches the make_sidecar default started_at.
STARTED = datetime(2026, 10, 7, 14, 3, 11, tzinfo=timezone(timedelta(hours=3)))


def _set_created(cfg, key: str, created: datetime) -> None:
    conn = sqlite3.connect(cfg.paths.db_path)
    conn.execute("UPDATE jobs SET created_at = ? WHERE job_key = ?", (created.isoformat(timespec="seconds"), key))
    conn.commit()
    conn.close()


def _touch(path: Path, when: datetime, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (when.timestamp(), when.timestamp()))
    return path


def _setup(cfg, store, audit, make_wav, make_sidecar, **sidecar_fields):
    make_wav(cfg.paths.inbox / "r.wav")
    make_sidecar(cfg.paths.inbox / "r.json", call_id="r1", **sidecar_fields)
    scan_once(cfg, store, audit, min_age_seconds=0)
    _set_created(cfg, "r1_1042", STARTED)
    job = store.get("r1_1042")
    for name in TEXT_FILES:
        (job.out_dir / name).write_text("x", encoding="utf-8")
    work = cfg.paths.work_dir / job.job_key
    work.mkdir(parents=True)
    (work / "segments.json").write_text("[]", encoding="utf-8")
    return job, work


def test_young_call_is_untouched(cfg, store, audit, make_wav, make_sidecar) -> None:
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar)

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=10))

    assert (result.audio_deleted, result.text_deleted, result.swept, result.errors) == ((), (), (), ())
    assert job.audio_path.is_file()
    assert (job.out_dir / RESULT_FILE).is_file()


def test_audio_goes_after_90_days_and_text_stays(cfg, store, audit, make_wav, make_sidecar) -> None:
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar)

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=91))

    assert result.audio_deleted == ("r1_1042",)
    assert not job.audio_path.exists()
    assert (job.out_dir / RESULT_FILE).is_file()
    assert [e.action for e in audit.entries("r1_1042")] == [PURGED_AUDIO]


def test_boundary_day_survives(cfg, store, audit, make_wav, make_sidecar) -> None:
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar)

    purge(cfg, store, audit, now=STARTED + timedelta(days=90))

    assert job.audio_path.is_file()


def test_text_goes_after_365_days_and_metadata_is_scrubbed(cfg, store, audit, make_wav, make_sidecar) -> None:
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar)

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=366))

    assert result.text_deleted == ("r1_1042",)
    assert not job.out_dir.exists()
    assert not work.exists()
    assert store.get("r1_1042").sidecar_json == SCRUBBED_SIDECAR
    actions = [e.action for e in audit.entries("r1_1042")]
    assert actions == [PURGED_AUDIO, PURGED_TEXT, SCRUBBED_METADATA]
    text_entry = audit.entries("r1_1042")[1]
    assert RESULT_FILE in text_entry.detail
    assert "work" in text_entry.detail


def test_purge_is_idempotent(cfg, store, audit, make_wav, make_sidecar) -> None:
    _setup(cfg, store, audit, make_wav, make_sidecar)
    later = STARTED + timedelta(days=366)
    purge(cfg, store, audit, now=later)
    entries = len(audit.entries())

    result = purge(cfg, store, audit, now=later)

    assert (result.audio_deleted, result.text_deleted) == ((), ())
    assert len(audit.entries()) == entries


def test_a_recorder_clock_in_the_past_does_not_delete_a_fresh_call(cfg, store, audit, make_wav, make_sidecar) -> None:
    """Age counts from the later of started_at and arrival."""
    job, work = _setup(cfg, store, audit, make_wav, make_sidecar, started_at="2024-01-01T00:00:00+00:00")

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=1))

    assert result.audio_deleted == ()
    assert job.audio_path.is_file()


def test_unparseable_start_is_never_deleted(cfg, store, audit, make_wav, make_sidecar) -> None:
    _setup(cfg, store, audit, make_wav, make_sidecar)
    store.create(
        job_key="bad_1",
        call_id="bad",
        conference_id=None,
        audio_path=Path(cfg.paths.out_root / "bad" / "recording.wav"),
        out_dir=Path(cfg.paths.out_root / "bad"),
        sidecar_json="{}",
        started_at="garbage",
        duration_sec=1,
    )

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=1000))

    assert any("bad_1" in e and "cannot parse" in e for e in result.errors)


def test_leftover_stt_copies_are_swept_after_a_day(cfg, store, audit) -> None:
    now = STARTED + timedelta(days=10)
    stale = _touch(cfg.paths.work_dir / "k1_1042" / "stt.ogg", now - timedelta(days=2))
    stale_part = _touch(cfg.paths.work_dir / "k1_1042" / "stt.ogg.part", now - timedelta(days=2))
    fresh = _touch(cfg.paths.work_dir / "k2_1042" / "stt.ogg", now - timedelta(hours=2))
    segments = _touch(cfg.paths.work_dir / "k1_1042" / "segments.json", now - timedelta(days=2))

    result = purge(cfg, store, audit, now=now)

    assert not stale.exists() and not stale_part.exists()
    assert fresh.exists() and segments.exists()
    assert sorted(result.swept) == sorted([str(stale), str(stale_part)])
    assert [e.action for e in audit.entries("k1_1042")] == [PURGED_STT_AUDIO, PURGED_STT_AUDIO]


def test_quarantine_and_inbox_orphans_follow_audio_retention(cfg, store, audit) -> None:
    now = STARTED + timedelta(days=200)
    old_quarantine = _touch(cfg.paths.quarantine / "bad.wav", now - timedelta(days=91))
    old_reason = _touch(cfg.paths.quarantine / "bad.reason.txt", now - timedelta(days=91))
    young_quarantine = _touch(cfg.paths.quarantine / "new.wav", now - timedelta(days=5))
    orphan = _touch(cfg.paths.inbox / "lost.wav.part", now - timedelta(days=91))
    waiting = _touch(cfg.paths.inbox / "pending.wav", now - timedelta(days=5))

    purge(cfg, store, audit, now=now)

    assert not old_quarantine.exists() and not old_reason.exists() and not orphan.exists()
    assert young_quarantine.exists() and waiting.exists()
    assert [e.action for e in audit.entries("bad")] == [PURGED_QUARANTINE, PURGED_QUARANTINE]
    assert [e.action for e in audit.entries("lost")] == [PURGED_ORPHAN]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_retention.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.retention'`.

- [ ] **Step 3: Create `jabberscribe/retention.py`**

```python
"""Retention purge.

A record-everything policy without a delete policy is a liability. Two
independent clocks: audio is bulky and sensitive so it goes early; the text
outputs live longer because they are the business record.

The rule is "older than N days", so the boundary day itself survives -- and a
row whose start date cannot be parsed is never deleted. Refusing to act on a
date we could not read is the only safe default when the action is deletion.
A call's age counts from the later of its start and its arrival, so a recorder
clock stuck in the past cannot get a fresh call deleted.

Besides the job rows, the purge sweeps every other place a voice can linger:
leftover STT copies in work/, quarantined pairs, and inbox orphans. Past the
text retention a row keeps no call metadata. Every deletion is audited.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audio import STT_FILENAME
from jabberscribe.audit import (
    PURGED_AUDIO,
    PURGED_ORPHAN,
    PURGED_QUARANTINE,
    PURGED_STT_AUDIO,
    PURGED_TEXT,
    SCRUBBED_METADATA,
    AuditLog,
)
from jabberscribe.config import Config
from jabberscribe.jobs import Job, JobStore
from jabberscribe.output import TEXT_FILES

log = logging.getLogger(__name__)

#: A finished or failed job deletes its STT copy itself; anything older than this was left by a crash.
STT_LEFTOVER_DAYS = 1


@dataclass(frozen=True)
class PurgeResult:
    audio_deleted: tuple[str, ...] = ()
    text_deleted: tuple[str, ...] = ()
    #: Paths of swept files: STT leftovers, quarantined pairs, inbox orphans.
    swept: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _age_days(job: Job, now: datetime) -> float | None:
    try:
        started = datetime.fromisoformat(job.started_at)
        created = datetime.fromisoformat(job.created_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return (now - max(started, created)) / timedelta(days=1)


def _file_age_days(path: Path, now: datetime) -> float:
    return (now - datetime.fromtimestamp(path.stat().st_mtime, UTC)) / timedelta(days=1)


def _delete_text(job: Job, work: Path) -> list[str]:
    removed: list[str] = []
    for name in TEXT_FILES:
        path = job.out_dir / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    if work.is_dir():
        shutil.rmtree(work)
        removed.append("work")
    return removed


def _sweep(
    paths: list[Path], max_days: float, now: datetime, action: str, audit: AuditLog, key_of: Callable[[Path], str]
) -> tuple[list[str], list[str]]:
    """Delete the files in `paths` older than `max_days`. Returns (deleted paths, errors)."""
    swept: list[str] = []
    errors: list[str] = []
    for path in paths:
        try:
            if not path.is_file() or _file_age_days(path, now) <= max_days:
                continue
            path.unlink()
        except OSError as exc:
            errors.append(f"{path}: cannot delete: {exc}")
            continue
        swept.append(str(path))
        audit.record(key_of(path), action, str(path))
    return swept, errors


def purge(cfg: Config, store: JobStore, audit: AuditLog, now: datetime) -> PurgeResult:
    """Delete aged audio and text, sweep leftovers, scrub old metadata. Every deletion is audited."""
    audio_deleted: list[str] = []
    text_deleted: list[str] = []
    errors: list[str] = []

    for job in store.list_all():
        age = _age_days(job, now)
        if age is None:
            errors.append(f"{job.job_key}: cannot parse started_at {job.started_at!r}, skipping")
            continue

        if age > cfg.retention.audio_days and job.audio_path.is_file():
            try:
                job.audio_path.unlink()
            except OSError as exc:
                errors.append(f"{job.job_key}: cannot delete audio: {exc}")
            else:
                audio_deleted.append(job.job_key)
                audit.record(job.job_key, PURGED_AUDIO, str(job.audio_path))

        if age > cfg.retention.text_days:
            try:
                removed = _delete_text(job, cfg.paths.work_dir / job.job_key)
            except OSError as exc:
                errors.append(f"{job.job_key}: cannot delete text: {exc}")
                continue
            if removed:
                text_deleted.append(job.job_key)
                audit.record(job.job_key, PURGED_TEXT, ", ".join(removed))
            if store.scrub_sidecar(job.job_key):
                audit.record(job.job_key, SCRUBBED_METADATA, "sidecar_json")
            try:
                job.out_dir.rmdir()
            except OSError:
                pass  # not empty (audio kept longer than text) or already gone

    stt_copies = [p for d in cfg.paths.work_dir.glob("*") for p in d.glob(f"{STT_FILENAME}*")]
    swept, sweep_errors = _sweep(stt_copies, STT_LEFTOVER_DAYS, now, PURGED_STT_AUDIO, audit, lambda p: p.parent.name)
    errors += sweep_errors
    for folder, action in ((cfg.paths.quarantine, PURGED_QUARANTINE), (cfg.paths.inbox, PURGED_ORPHAN)):
        more, sweep_errors = _sweep(list(folder.glob("*")), cfg.retention.audio_days, now, action, audit, _stem)
        swept += more
        errors += sweep_errors

    log.info(
        "purge deleted %d audio file(s), text for %d call(s), %d leftover file(s)",
        len(audio_deleted),
        len(text_deleted),
        len(swept),
    )
    return PurgeResult(tuple(audio_deleted), tuple(text_deleted), tuple(swept), tuple(errors))


def _stem(path: Path) -> str:
    return path.name.split(".", 1)[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_retention.py -q`
Expected: 9 passed.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (205 passed).

- [ ] **Step 6: Commit**

```bash
git add jabberscribe/retention.py tests/test_retention.py
git commit -m "feat(retention): audited purge of jobs, metadata, STT leftovers and orphans" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 17: Single-instance lock and config-relative vocabulary

Fixes audit I2 (two workers corrupt each other; the CLI wiring is Task 18) and M7 (the vocabulary path resolves against the service's working directory, `C:\Windows\System32`).

**Files:**
- Create: `jabberscribe/lock.py`
- Modify: `jabberscribe/config.py` (`SttConfig` comment, `load_config`)
- Test: `tests/test_lock.py` (create), `tests/test_config.py` (append two tests)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `lock.LOCK_NAME = "jabberscribe.lock"`; `lock.LockError(RuntimeError)`; `lock.lock_path(db_path: Path) -> Path` (`db_path.parent / LOCK_NAME`); `lock.instance_lock(db_path: Path)` — a context manager holding an exclusive non-blocking lock (`msvcrt.locking(..., LK_NBLCK, 1)` on Windows, `fcntl.flock(LOCK_EX | LOCK_NB)` elsewhere); raises `LockError` when another holder exists.
  - `load_config(path)` resolves a relative `stt.vocabulary_file` against `path.parent`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lock.py`:

```python
from pathlib import Path

import pytest

from jabberscribe.lock import LockError, instance_lock, lock_path


def test_lock_file_sits_next_to_the_database(tmp_path: Path) -> None:
    assert lock_path(tmp_path / "data" / "js.db") == tmp_path / "data" / "jabberscribe.lock"


def test_second_holder_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    with instance_lock(db):
        with pytest.raises(LockError, match="another JabberScribe instance"):
            with instance_lock(db):
                pass


def test_lock_is_released_after_the_block(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    with instance_lock(db):
        pass

    with instance_lock(db):
        pass


def test_lock_is_released_when_the_block_raises(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    with pytest.raises(RuntimeError):
        with instance_lock(db):
            raise RuntimeError("boom")

    with instance_lock(db):
        pass
```

Append to the end of `tests/test_config.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_lock.py tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jabberscribe.lock'`; `test_relative_vocabulary_file_is_relative_to_the_config_file` fails with `WindowsPath('custom_vocabulary.txt') == ...`.

- [ ] **Step 3: Create `jabberscribe/lock.py`**

```python
"""Single-instance lock.

claim_next reclaims RUNNING rows as crash leftovers, which is only safe while
one process works the database. `run`, `process` and `purge` therefore hold an
exclusive OS lock on <db_path parent>/jabberscribe.lock; a second instance
fails fast instead of transcribing the same call twice. The OS releases the
lock when the process dies, so a crash never leaves a stale lock behind.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

LOCK_NAME = "jabberscribe.lock"


class LockError(RuntimeError):
    """Another JabberScribe instance holds the lock."""


def lock_path(db_path: Path) -> Path:
    return db_path.parent / LOCK_NAME


def _acquire(fh: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fh: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def instance_lock(db_path: Path) -> Iterator[None]:
    """Hold the instance lock for the duration of the block. Raises LockError if it is taken."""
    path = lock_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+b")
    try:
        _acquire(fh)
    except OSError as exc:
        fh.close()
        raise LockError(f"another JabberScribe instance is running (lock held: {path})") from exc
    try:
        yield
    finally:
        _release(fh)
        fh.close()
```

- [ ] **Step 4: Resolve the vocabulary path against the config file**

In `jabberscribe/config.py`, replace:

```python
    model: str
    vocabulary_file: Path | None = None
```

with:

```python
    model: str
    #: A relative path is relative to the config file, not the working directory.
    vocabulary_file: Path | None = None
```

and replace:

```python
    try:
        return Config(**raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}: {exc}") from exc
```

with:

```python
    try:
        cfg = Config(**raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}: {exc}") from exc
    # A Windows service runs in C:\Windows\System32; resolving against the config file keeps the glossary found.
    vocabulary = cfg.stt.vocabulary_file
    if vocabulary is not None and not vocabulary.is_absolute():
        cfg.stt.vocabulary_file = path.parent / vocabulary
    return cfg
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_lock.py tests/test_config.py -q`
Expected: all pass (4 in test_lock).

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (211 passed).

- [ ] **Step 7: Commit**

```bash
git add jabberscribe/lock.py jabberscribe/config.py tests/test_lock.py tests/test_config.py
git commit -m "feat(lock): single-instance lock; resolve vocabulary next to the config" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 18: CLI and end-to-end integration

Replaces original Task 11. Fixes audit I1 (`run` dies on any exception), I2 (`process` drains the whole queue and releases every waiting conference; two instances), I5/§2.1.5 (nothing runs `purge`), I7 (doctor only lists models), M6 (`process` exits 0 when the job did not finish), M7 (`doctor` reports the vocabulary), C3 (`retry` command), and improvement 8-lite (`status`).

**Files:**
- Create: `jabberscribe/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above. Specifically, imported into `cli` by name so tests can monkeypatch them: `make_client` (Task 11), `scan_once` (Task 9), `purge` (Task 16); and `cli.time.sleep`. Also `settle` (Task 14), `run_job`, `run_once` (Task 15), `instance_lock`, `LockError` (Task 17), `JobStore`, `SchemaError`, statuses (Task 10), `post`, `TransientError` (Task 11), `LiteLLMTranscriber`, `SttError`, `build_prompt`, `load_vocabulary` (Task 11), `LiteLLMSummarizer`, `strip_fences` (Task 12), `parse_sidecar`, `SidecarError` (Task 8), `GroupConfig`, `load_config`, `ConfigError` (committed).
- Produces:
  - `Check(name: str, ok: bool, detail: str)`; `CHAT_PROBE: str`; `doctor(cfg: Config, client: httpx.Client) -> list[Check]` — check names, in order: `ffmpeg`, `litellm` (models listed), `litellm.chat` (a real JSON-mode user-role completion that must answer `{"ok": true}`), `litellm.transcription` (a 1 s ffmpeg sine tone through the transcription route with the production prompt; must return `segments`), `stt.vocabulary_file`, `paths.drop_root/inbox`, `paths.drop_root/quarantine`, `paths.work_dir`, `paths.out_root`, `paths.db_path parent`.
  - `main(argv: list[str] | None = None) -> int` with subcommands:
    - `doctor` → 0 if every check passes, else 1.
    - `process <audio> <sidecar>` (locked) → ingests the pair unless its `job_key` is known, releases only its own conference immediately (`max_wait_seconds=0`), runs only its own job (or its primary) via `run_job`; prints `<job_key>: <status> -> <out_dir>`; 0 only when the job ends DONE or GROUPED.
    - `run [--once]` (locked) → each poll: `scan_once`, `settle`, `run_once`, and `purge` on the first poll of each UTC day; an exception is logged and the loop continues; `--once` returns 1 if its poll raised.
    - `purge` (locked) → prints counts; 1 if any error.
    - `retry <job_key>` | `retry --failed` → `JobStore.requeue`; 1 if a named job is not a failed primary.
    - `status` → counts per status, oldest WAITING/QUEUED age, retrying (`~`) and failed (`!`) jobs; 1 while any failed primary exists.
  - Exit code 2 for a bad config or a database from another schema version; 1 when the lock is held.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
import json
import shutil
from pathlib import Path

import httpx
import pytest

from jabberscribe import cli
from jabberscribe.cli import CHAT_PROBE, doctor, main
from jabberscribe.jobs import DONE, FAILED, QUEUED, JobStore
from jabberscribe.lock import instance_lock
from jabberscribe.output import ACTIONS_FILE, RESULT_FILE, SUMMARY_FILE, TRANSCRIPT_FILE

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

SUMMARY_ANSWER = json.dumps(
    {
        "summary": "סיכום.",
        "action_items": [{"task": "לשלוח את הדוח", "owner": "דנה", "due": "מחר", "source_ts": "00:00:00"}],
    },
    ensure_ascii=False,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(handler))


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


def _chat(content: object) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


class FakeLiteLLM:
    """Routes the three LiteLLM endpoints JabberScribe uses."""

    def __init__(self, models: tuple[str, ...] = ("whisper-he", "gemma-3"), probe_answer: object = '{"ok": true}'):
        self.models = models
        self.probe_answer = probe_answer
        self.transcriptions = 0
        self.chats: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if path == "/v1/audio/transcriptions":
            request.read()
            self.transcriptions += 1
            return httpx.Response(200, json={"segments": [{"start": 0.0, "end": 2.0, "text": "דנה תשלח את הדוח מחר"}]})
        if path == "/v1/chat/completions":
            body = json.loads(request.content)
            self.chats.append(body)
            if CHAT_PROBE in body["messages"][0]["content"]:
                return _chat(self.probe_answer)
            return _chat(SUMMARY_ANSWER)
        return httpx.Response(404)


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "paths:\n"
        f"  drop_root: {(tmp_path / 'drop').as_posix()}\n"
        f"  work_dir: {(tmp_path / 'work').as_posix()}\n"
        f"  out_root: {(tmp_path / 'out').as_posix()}\n"
        f"  db_path: {(tmp_path / 'js.db').as_posix()}\n"
        "watcher:\n"
        "  min_age_seconds: 0\n"
        "litellm:\n"
        "  base_url: http://litellm.test\n"
        "stt:\n"
        "  model: whisper-he\n"
        "summary:\n"
        "  model: gemma-3\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fake_litellm(monkeypatch) -> FakeLiteLLM:
    server = FakeLiteLLM()
    monkeypatch.setattr(cli, "make_client", lambda litellm_cfg: _client(server))
    return server


def _checks(cfg, server) -> dict:
    return {c.name: c for c in doctor(cfg, _client(server))}


@needs_ffmpeg
def test_doctor_passes_against_a_working_server(cfg) -> None:
    server = FakeLiteLLM()

    checks = _checks(cfg, server)

    assert all(c.ok for c in checks.values()), [c for c in checks.values() if not c.ok]
    assert server.transcriptions == 1
    probe = server.chats[0]
    assert [m["role"] for m in probe["messages"]] == ["user"]
    assert probe["response_format"] == {"type": "json_object"}
    assert cfg.paths.out_root.is_dir()


def test_doctor_names_a_missing_model(cfg) -> None:
    checks = _checks(cfg, FakeLiteLLM(models=("whisper-he",)))

    assert not checks["litellm"].ok
    assert "gemma-3" in checks["litellm"].detail


@pytest.mark.parametrize("answer", [None, "not json", '{"ok": false}'])
def test_doctor_fails_when_the_chat_answer_is_unusable(cfg, answer) -> None:
    assert not _checks(cfg, FakeLiteLLM(probe_answer=answer))["litellm.chat"].ok


def test_doctor_reports_a_rejected_chat_request(cfg) -> None:
    def reject_chat(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(400, text="System role not supported")
        return FakeLiteLLM()(request)

    check = _checks(cfg, reject_chat)["litellm.chat"]

    assert not check.ok
    assert "400" in check.detail


@needs_ffmpeg
def test_doctor_reports_a_transcription_route_without_segments(cfg) -> None:
    def no_segments(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions":
            request.read()
            return httpx.Response(200, json={"text": "שלום"})
        return FakeLiteLLM()(request)

    check = _checks(cfg, no_segments)["litellm.transcription"]

    assert not check.ok
    assert "verbose_json" in check.detail


def test_doctor_reports_an_unreachable_server(cfg) -> None:
    checks = _checks(cfg, _refuse)

    assert not checks["litellm"].ok
    assert "refused" in checks["litellm"].detail
    assert not checks["litellm.chat"].ok


def test_doctor_reports_a_missing_vocabulary_file(cfg, tmp_path) -> None:
    cfg.stt.vocabulary_file = tmp_path / "missing.txt"

    check = _checks(cfg, FakeLiteLLM())["stt.vocabulary_file"]

    assert not check.ok
    assert "missing.txt" in check.detail


def test_main_doctor_fails_when_litellm_is_down(cfg_file, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "make_client", lambda litellm_cfg: _client(_refuse))

    code = main(["--config", str(cfg_file), "doctor"])

    assert code == 1
    assert "[FAIL] litellm" in capsys.readouterr().out


def test_main_reports_bad_config_without_traceback(tmp_path: Path, capsys) -> None:
    code = main(["--config", str(tmp_path / "missing.yaml"), "doctor"])

    assert code == 2
    assert "not found" in capsys.readouterr().err


@needs_ffmpeg
def test_process_produces_outputs_end_to_end(tmp_path, cfg_file, fake_litellm, make_wav, make_sidecar, capsys) -> None:
    audio = make_wav(tmp_path / "src" / "call.wav", channels=2, seconds=2.0)
    sidecar = make_sidecar(tmp_path / "src" / "call.json", call_id="e2e", tracks="dual")

    code = main(["--config", str(cfg_file), "process", str(audio), str(sidecar)])

    assert code == 0
    out = tmp_path / "out" / "2026" / "10" / "e2e_1042"
    result = json.loads((out / RESULT_FILE).read_text(encoding="utf-8"))
    assert result["transcript"][0]["text"] == "דנה תשלח את הדוח מחר"
    assert result["action_items"][0]["owner"] == "דנה"
    for name in ("recording.wav", TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE):
        assert (out / name).is_file()
    assert "e2e_1042: done" in capsys.readouterr().out


@needs_ffmpeg
def test_reprocessing_the_same_call_is_a_no_op(tmp_path, cfg_file, fake_litellm, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "src" / "call.wav", channels=2)
    sidecar = make_sidecar(tmp_path / "src" / "call.json", call_id="twice", tracks="dual")

    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 0
    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 0

    assert fake_litellm.transcriptions == 1
    assert len(JobStore(tmp_path / "js.db").list_all()) == 1


@needs_ffmpeg
def test_process_does_not_wait_on_a_conference_copy(tmp_path, cfg_file, fake_litellm, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "src" / "conf.wav", channels=2)
    sidecar = make_sidecar(tmp_path / "src" / "conf.json", call_id="m1", conference_id="conf-1", tracks="dual")

    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 0

    assert (tmp_path / "out" / "2026" / "10" / "m1_1042" / RESULT_FILE).is_file()


@needs_ffmpeg
def test_process_leaves_other_queued_jobs_alone(tmp_path, cfg_file, fake_litellm, make_wav, make_sidecar) -> None:
    """`process` is a manual tool; it must not drain the service's queue."""
    make_wav(tmp_path / "drop" / "inbox" / "other.wav")
    make_sidecar(tmp_path / "drop" / "inbox" / "other.json", call_id="other")
    audio = make_wav(tmp_path / "src" / "mine.wav")
    sidecar = make_sidecar(tmp_path / "src" / "mine.json", call_id="mine")

    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 0

    store = JobStore(tmp_path / "js.db")
    assert store.get("mine_1042").status == DONE
    assert store.get("other_1042").status == QUEUED
    assert fake_litellm.transcriptions == 1


@needs_ffmpeg
def test_process_exits_non_zero_when_the_job_did_not_finish(tmp_path, cfg_file, monkeypatch, make_wav, make_sidecar):
    def stt_down(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions":
            return httpx.Response(503)
        return FakeLiteLLM()(request)

    monkeypatch.setattr(cli, "make_client", lambda litellm_cfg: _client(stt_down))
    audio = make_wav(tmp_path / "src" / "call.wav")
    sidecar = make_sidecar(tmp_path / "src" / "call.json", call_id="down")

    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 1
    assert JobStore(tmp_path / "js.db").get("down_1042").status == QUEUED


def test_process_refuses_to_run_beside_another_instance(tmp_path, cfg_file, make_wav, make_sidecar, capsys) -> None:
    audio = make_wav(tmp_path / "src" / "call.wav")
    sidecar = make_sidecar(tmp_path / "src" / "call.json")

    with instance_lock(tmp_path / "js.db"):
        code = main(["--config", str(cfg_file), "process", str(audio), str(sidecar)])

    assert code == 1
    assert "another JabberScribe instance" in capsys.readouterr().err


class _Stop(Exception):
    pass


def _stop_after(polls: int):
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= polls:
            raise _Stop

    return sleep


@needs_ffmpeg
def test_run_survives_a_failing_poll(tmp_path, cfg_file, fake_litellm, monkeypatch, make_wav, make_sidecar) -> None:
    make_wav(tmp_path / "drop" / "inbox" / "a.wav")
    make_sidecar(tmp_path / "drop" / "inbox" / "a.json", call_id="svc")
    real_scan = cli.scan_once
    calls = {"n": 0}

    def flaky_scan(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("share unavailable")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(cli, "scan_once", flaky_scan)
    monkeypatch.setattr(cli.time, "sleep", _stop_after(2))

    with pytest.raises(_Stop):
        main(["--config", str(cfg_file), "run"])

    assert JobStore(tmp_path / "js.db").get("svc_1042").status == DONE


def test_run_purges_once_a_day(cfg_file, fake_litellm, monkeypatch) -> None:
    purges: list[object] = []
    real_purge = cli.purge

    def counting_purge(*args, **kwargs):
        purges.append(kwargs["now"])
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(cli, "purge", counting_purge)
    monkeypatch.setattr(cli.time, "sleep", _stop_after(3))

    with pytest.raises(_Stop):
        main(["--config", str(cfg_file), "run"])

    assert len(purges) == 1


def test_run_once_reports_a_failing_poll(cfg_file, fake_litellm, monkeypatch) -> None:
    def broken_scan(*args, **kwargs):
        raise OSError("share unavailable")

    monkeypatch.setattr(cli, "scan_once", broken_scan)

    assert main(["--config", str(cfg_file), "run", "--once"]) == 1


def _failed_job(tmp_path: Path, key: str = "f_1042") -> JobStore:
    store = JobStore(tmp_path / "js.db")
    store.init_schema()
    store.create(
        job_key=key,
        call_id="f",
        conference_id=None,
        audio_path=tmp_path / "out" / key / "recording.wav",
        out_dir=tmp_path / "out" / key,
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )
    store.record_attempt(key, "stt: HTTP 413")
    store.set_status(key, FAILED)
    return store


def test_retry_requeues_a_failed_job(tmp_path, cfg_file, capsys) -> None:
    _failed_job(tmp_path)

    assert main(["--config", str(cfg_file), "retry", "f_1042"]) == 0

    job = JobStore(tmp_path / "js.db").get("f_1042")
    assert (job.status, job.attempts) == (QUEUED, 0)
    assert "f_1042: requeued" in capsys.readouterr().out


def test_retry_failed_requeues_every_failed_job(tmp_path, cfg_file) -> None:
    _failed_job(tmp_path, "a_1")
    _failed_job(tmp_path, "b_2")

    assert main(["--config", str(cfg_file), "retry", "--failed"]) == 0

    store = JobStore(tmp_path / "js.db")
    assert {store.get("a_1").status, store.get("b_2").status} == {QUEUED}


def test_retry_refuses_a_job_that_did_not_fail(tmp_path, cfg_file, capsys) -> None:
    store = _failed_job(tmp_path)
    store.set_status("f_1042", DONE)

    assert main(["--config", str(cfg_file), "retry", "f_1042"]) == 1
    assert "nothing requeued" in capsys.readouterr().err


def test_status_lists_counts_and_failures(tmp_path, cfg_file, capsys) -> None:
    _failed_job(tmp_path)

    code = main(["--config", str(cfg_file), "status"])

    out = capsys.readouterr().out
    assert code == 1
    assert "failed: 1" in out
    assert "! f_1042: stt: HTTP 413" in out


def test_status_is_zero_when_nothing_failed(cfg_file, capsys) -> None:
    assert main(["--config", str(cfg_file), "status"]) == 0
    assert "queued: 0" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cli.py -q`
Expected: FAIL — `ImportError: cannot import name 'cli' from 'jabberscribe'`.

- [ ] **Step 3: Create `jabberscribe/cli.py`**

```python
"""Command-line entry point.

`doctor` exists so a bad deployment fails loudly at install time rather than
silently at 2 a.m.: it sends a real chat completion and a real transcription
through LiteLLM, not just a model listing.

`run`, `process` and `purge` hold the single-instance lock. `run` never dies on
a bad poll: each iteration logs its error and the loop carries on. Operator
output is English, like the logs; user-facing files are Hebrew.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from jabberscribe.audit import AuditLog
from jabberscribe.config import Config, ConfigError, GroupConfig, load_config
from jabberscribe.group import settle
from jabberscribe.jobs import DONE, FAILED, GROUPED, QUEUED, RUNNING, WAITING, JobStore, SchemaError
from jabberscribe.llm import TransientError, make_client, post
from jabberscribe.lock import LockError, instance_lock
from jabberscribe.pipeline import run_job, run_once
from jabberscribe.retention import purge
from jabberscribe.sidecar import SidecarError, parse_sidecar
from jabberscribe.stt import LiteLLMTranscriber, SttError, build_prompt, load_vocabulary
from jabberscribe.summarize import LiteLLMSummarizer, strip_fences
from jabberscribe.watcher import scan_once

log = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("config/jabberscribe.yaml")

CHAT_PROBE = 'Reply with exactly this JSON object and nothing else: {"ok": true}'
STATUS_ORDER = (WAITING, QUEUED, RUNNING, DONE, GROUPED, FAILED)


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
        proc = subprocess.run(
            [exe, "-version"], capture_output=True, encoding="utf-8", errors="replace", timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("ffmpeg", False, f"{exe}: {exc}")
    if proc.returncode != 0:
        return Check("ffmpeg", False, f"{exe} exited {proc.returncode}")
    first_line = proc.stdout.splitlines()[0] if proc.stdout else exe
    return Check("ffmpeg", True, first_line)


def _check_models(cfg: Config, client: httpx.Client) -> Check:
    """The server must be reachable and serve both configured models."""
    try:
        response = client.get("/v1/models")
        response.raise_for_status()
        served = {m["id"] for m in response.json()["data"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return Check("litellm", False, f"{cfg.litellm.base_url}: {exc}")
    missing = [m for m in (cfg.stt.model, cfg.summary.model) if m not in served]
    if missing:
        return Check("litellm", False, f"{cfg.litellm.base_url} does not serve: {', '.join(missing)}")
    return Check("litellm", True, f"{cfg.litellm.base_url}: {cfg.stt.model}, {cfg.summary.model}")


def _check_chat(cfg: Config, client: httpx.Client) -> Check:
    """The summary request shape (user role, JSON mode, max_tokens) must be accepted and answered."""
    try:
        response = post(
            client,
            "/v1/chat/completions",
            json={
                "model": cfg.summary.model,
                "temperature": 0,
                "max_tokens": 20,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": CHAT_PROBE}],
            },
        )
        answer = json.loads(strip_fences(response.json()["choices"][0]["message"]["content"]))
    except (httpx.HTTPError, TransientError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        return Check("litellm.chat", False, f"{cfg.summary.model}: {exc}")
    if answer != {"ok": True}:
        return Check("litellm.chat", False, f"{cfg.summary.model}: unexpected answer {answer!r}")
    return Check("litellm.chat", True, f"{cfg.summary.model}: JSON answer received")


def _check_transcription(cfg: Config, client: httpx.Client) -> Check:
    """A one-second tone must go through the transcription route with our prompt and verbose_json."""
    prompt = build_prompt(load_vocabulary(cfg.stt.vocabulary_file))
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.ogg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(probe),
                ],
                capture_output=True,
                timeout=60,
                check=True,
            )
            segments = LiteLLMTranscriber(client, cfg.stt.model, prompt).transcribe(probe)
        except (OSError, subprocess.SubprocessError) as exc:
            return Check("litellm.transcription", False, f"cannot make the probe tone: {exc}")
        except (TransientError, SttError) as exc:
            return Check("litellm.transcription", False, f"{cfg.stt.model}: {exc}")
    return Check("litellm.transcription", True, f"{cfg.stt.model}: {len(segments)} segment(s), prompt accepted")


def _check_vocabulary(cfg: Config) -> Check:
    path = cfg.stt.vocabulary_file
    if path is None:
        return Check("stt.vocabulary_file", True, "not configured")
    vocabulary = load_vocabulary(path)
    if vocabulary is None:
        return Check("stt.vocabulary_file", False, f"missing or empty: {path}")
    return Check("stt.vocabulary_file", True, f"{path}: {len(vocabulary.split(', '))} terms")


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


def doctor(cfg: Config, client: httpx.Client) -> list[Check]:
    """Verify the environment. Creates missing directories as a side effect."""
    return [
        _check_ffmpeg(),
        _check_models(cfg, client),
        _check_chat(cfg, client),
        _check_transcription(cfg, client),
        _check_vocabulary(cfg),
        _check_dir("paths.drop_root/inbox", cfg.paths.inbox),
        _check_dir("paths.drop_root/quarantine", cfg.paths.quarantine),
        _check_dir("paths.work_dir", cfg.paths.work_dir),
        _check_dir("paths.out_root", cfg.paths.out_root),
        _check_dir("paths.db_path parent", cfg.paths.db_path.parent),
    ]


def _ensure_dirs(cfg: Config) -> None:
    paths = cfg.paths
    for path in (paths.inbox, paths.quarantine, paths.work_dir, paths.out_root, paths.db_path.parent):
        path.mkdir(parents=True, exist_ok=True)


def _open(cfg: Config) -> tuple[JobStore, AuditLog]:
    # The job store first: it refuses a database from another version before anything writes to it.
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path)
    audit.init_schema()
    return store, audit


def _workers(cfg: Config, client: httpx.Client) -> tuple[LiteLLMTranscriber, LiteLLMSummarizer]:
    prompt = build_prompt(load_vocabulary(cfg.stt.vocabulary_file))
    summarizer = LiteLLMSummarizer(client, cfg.summary.model, cfg.summary.max_chunk_chars)
    return LiteLLMTranscriber(client, cfg.stt.model, prompt), summarizer


def _process(cfg: Config, store: JobStore, audit: AuditLog, audio: Path, sidecar_path: Path) -> int:
    """Run one recording end to end: only its own job, and only its own conference is released."""
    try:
        key = parse_sidecar(sidecar_path.read_text(encoding="utf-8-sig")).job_key
    except (SidecarError, OSError, UnicodeDecodeError) as exc:
        print(f"invalid sidecar {sidecar_path}: {exc}", file=sys.stderr)
        return 1
    if store.get(key) is None:
        shutil.copy2(audio, cfg.paths.inbox / audio.name)
        shutil.copy2(sidecar_path, cfg.paths.inbox / sidecar_path.name)
        # min_age 0: a human handing us one file is not racing a recorder.
        result = scan_once(cfg, store, audit, min_age_seconds=0)
        if store.get(key) is None:
            reason = f"quarantined {result.quarantined}, deferred {result.deferred}"
            print(f"{key}: not ingested ({reason})", file=sys.stderr)
            return 1
    job = store.get(key)
    if job.status == WAITING:
        # Do not wait for other copies: release this conference now.
        group = GroupConfig(settle_seconds=cfg.group.settle_seconds, max_wait_seconds=0)
        immediate = cfg.model_copy(update={"group": group})
        settle(immediate, store, audit, datetime.now(UTC), conference_id=job.conference_id)
        job = store.get(key)
    run_job(job.grouped_into or key, cfg, store, *_workers(cfg, make_client(cfg.litellm)))
    job = store.get(key)
    print(f"{key}: {job.status} -> {job.out_dir}")
    return 0 if job.status in (DONE, GROUPED) else 1


def _report_purge(cfg: Config, store: JobStore, audit: AuditLog) -> int:
    result = purge(cfg, store, audit, now=datetime.now(UTC))
    print(f"audio deleted: {len(result.audio_deleted)}")
    print(f"text deleted: {len(result.text_deleted)}")
    print(f"leftovers swept: {len(result.swept)}")
    for problem in result.errors:
        print(f"  ! {problem}", file=sys.stderr)
    return 1 if result.errors else 0


def _serve(cfg: Config, store: JobStore, audit: AuditLog, once: bool) -> int:
    transcriber, summarizer = _workers(cfg, make_client(cfg.litellm))
    last_purge: date | None = None
    while True:
        ok = True
        try:
            scan_once(cfg, store, audit)
            settle(cfg, store, audit, datetime.now(UTC))
            run_once(cfg, store, transcriber, summarizer)
            today = datetime.now(UTC).date()
            if last_purge != today:
                result = purge(cfg, store, audit, now=datetime.now(UTC))
                for problem in result.errors:
                    log.error("purge: %s", problem)
                last_purge = today
        except Exception:
            # One bad poll (a locked file, a full disk, a bug) must not stop the service.
            log.exception("poll failed; continuing")
            ok = False
        if once:
            return 0 if ok else 1
        time.sleep(cfg.watcher.poll_seconds)


def _retry(store: JobStore, job_key: str | None, all_failed: bool) -> int:
    if all_failed:
        keys = [j.job_key for j in store.list_by_status(FAILED) if j.grouped_into is None]
        if not keys:
            print("no failed jobs")
    else:
        keys = [job_key]
    refused = 0
    for key in keys:
        if store.requeue(key):
            print(f"{key}: requeued")
        else:
            print(f"{key}: not a failed primary job; nothing requeued", file=sys.stderr)
            refused += 1
    return 1 if refused else 0


def _status(store: JobStore) -> int:
    """Counts by status, the oldest waiting and queued job, and every retrying or failed job.

    Exits 1 while any job is FAILED, so a scheduled task can alert on it.
    """
    jobs = store.list_all()
    counts = Counter(j.status for j in jobs)
    for status in STATUS_ORDER:
        print(f"{status}: {counts[status]}")
    now = datetime.now(UTC)
    for status in (WAITING, QUEUED):
        pending = [j for j in jobs if j.status == status]
        if pending:
            oldest = min(pending, key=lambda j: j.created_at)
            minutes = (now - datetime.fromisoformat(oldest.created_at)).total_seconds() / 60
            print(f"oldest {status}: {oldest.job_key} for {minutes:.0f} min")
    for job in jobs:
        if job.status == QUEUED and job.next_attempt_at:
            print(f"  ~ {job.job_key}: attempt {job.attempts}, next at {job.next_attempt_at}: {job.last_error}")
        if job.status == FAILED and job.grouped_into is None:
            print(f"  ! {job.job_key}: {job.last_error}")
    return 1 if any(j.status == FAILED and j.grouped_into is None for j in jobs) else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jabberscribe")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to jabberscribe.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="verify environment and LiteLLM routes; create missing directories")

    process_cmd = sub.add_parser("process", help="process one recording from a path pair")
    process_cmd.add_argument("audio", type=Path)
    process_cmd.add_argument("sidecar", type=Path)

    run_cmd = sub.add_parser("run", help="watch the inbox and process jobs; purge once a day")
    run_cmd.add_argument("--once", action="store_true", help="single pass, then exit")

    sub.add_parser("purge", help="delete audio and text past their retention window")

    retry_cmd = sub.add_parser("retry", help="give a failed job a fresh set of attempts")
    target = retry_cmd.add_mutually_exclusive_group(required=True)
    target.add_argument("job_key", nargs="?")
    target.add_argument("--failed", action="store_true", help="every failed job")

    sub.add_parser("status", help="job counts, backlog age, retrying and failed jobs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "doctor":
        checks = doctor(cfg, make_client(cfg.litellm))
        for check in checks:
            print(f"[{'OK ' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        return 0 if all(c.ok for c in checks) else 1

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _ensure_dirs(cfg)
    try:
        store, audit = _open(cfg)
    except SchemaError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "retry":
        return _retry(store, args.job_key, args.failed)
    if args.command == "status":
        return _status(store)

    try:
        with instance_lock(cfg.paths.db_path):
            if args.command == "process":
                return _process(cfg, store, audit, args.audio, args.sidecar)
            if args.command == "run":
                return _serve(cfg, store, audit, args.once)
            if args.command == "purge":
                return _report_purge(cfg, store, audit)
    except LockError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -q`
Expected: 25 passed.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: all pass (236 passed).

- [ ] **Step 6: Commit**

```bash
git add jabberscribe/cli.py tests/test_cli.py
git commit -m "feat(cli): deep doctor probes, scoped process, resilient run, retry and status" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 19: Shipping config, live check, docs

Replaces original Task 12, updated for everything above. Also records the C1 deviation and the new failure rules in the spec, marks the original plan as superseded for Tasks 8–12, and fixes its Task 3 Step 8 text (audit M13). Adds the README notes for LiteLLM logging (I5(b)), backups and VSS (I4(e)), and running as a Windows service (M7).

**Files:**
- Modify (full rewrite): `config/jabberscribe.yaml`, `README.md`
- Modify: `tests/test_config.py` (append one test), `docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md` (§5, §6, §7, §8), `docs/superpowers/plans/2026-10-07-v2-pipeline.md` (header, Task 3 Step 8)
- Create: `tests/test_live.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a shipped config that loads (vocabulary relative to `config/`), an opt-in live test against the real LiteLLM server, and v2 docs.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_config.py`:

```python
def test_shipped_config_is_valid() -> None:
    shipped = Path(__file__).resolve().parent.parent / "config" / "jabberscribe.yaml"

    cfg = load_config(shipped)

    assert cfg.retention.audio_days == 90
    assert cfg.retention.text_days == 365
    assert cfg.stt.vocabulary_file == shipped.parent / "custom_vocabulary.txt"
    assert cfg.stt.vocabulary_file.is_file()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_config.py::test_shipped_config_is_valid -q`
Expected: FAIL — `ConfigError` (the shipped file still has the v1 `audio_store`/`pipeline` keys).

- [ ] **Step 3: Rewrite `config/jabberscribe.yaml`**

```yaml
# JabberScribe v2 configuration. Secrets come from environment variables, never here:
#   JABBERSCRIBE_LITELLM_KEY   LiteLLM API key (leave unset if the server has no auth)
# Run the service with an absolute --config path: a Windows service starts in C:\Windows\System32.
paths:
  drop_root: D:/jabberscribe/drop          # the recorder writes into drop_root/inbox
  work_dir: D:/jabberscribe/work
  out_root: D:/jabberscribe/out            # out/<YYYY>/<MM>/<call>/ -- readable company-wide in the MVP
  db_path: D:/jabberscribe/jabberscribe-v2.db   # a fresh file: v2 refuses databases from other versions

watcher:
  poll_seconds: 30
  min_age_seconds: 15

# A conference group is the copies of one conference_id whose time spans overlap
# (within settle_seconds). It is processed once quiet for settle_seconds, or once
# its first copy has waited max_wait_seconds. A later copy that is longer and ends
# more than settle_seconds after the processed one replaces it and is reprocessed.
group:
  settle_seconds: 60
  max_wait_seconds: 300

litellm:
  base_url: http://litellm.corp.local:4000   # without a trailing /v1
  timeout_seconds: 600

# Model names are LiteLLM model_name values. `jabberscribe doctor` sends a real
# transcription and a real chat completion to both -- use the names your server lists.
stt:
  model: whisper-he
  vocabulary_file: custom_vocabulary.txt    # relative to this file

summary:
  model: gemma-3
  max_chunk_chars: 12000                    # longer transcripts are summarized in chunks, then merged

retention:
  audio_days: 90
  text_days: 365
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest tests/test_config.py -q`
Expected: all pass.

- [ ] **Step 5: Create the opt-in live test**

Create `tests/test_live.py`:

```python
"""Checks against the real LiteLLM server. Skipped unless pointed at one:

    JABBERSCRIBE_LIVE_CONFIG=config/jabberscribe.yaml JABBERSCRIBE_LIVE_CLIP=clip.wav pytest -m slow

The clip should be a short real Hebrew recording. The transcription runs with
the production prompt, so a server that rejects the prompt or verbose_json fails here.
"""

import os
from pathlib import Path

import pytest

from jabberscribe.audio import prepare_for_stt
from jabberscribe.cli import doctor
from jabberscribe.config import load_config
from jabberscribe.llm import make_client
from jabberscribe.stt import LiteLLMTranscriber, Segment, build_prompt, load_vocabulary
from jabberscribe.summarize import LiteLLMSummarizer

pytestmark = pytest.mark.slow


def _hebrew(text: str) -> bool:
    return any("א" <= ch <= "ת" for ch in text)


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> tuple:
    config, clip = os.environ.get("JABBERSCRIBE_LIVE_CONFIG"), os.environ.get("JABBERSCRIBE_LIVE_CLIP")
    if not config or not clip:
        pytest.skip("set JABBERSCRIBE_LIVE_CONFIG and JABBERSCRIBE_LIVE_CLIP")
    cfg = load_config(Path(config))
    client = make_client(cfg.litellm)
    audio = prepare_for_stt(Path(clip), tmp_path_factory.mktemp("live"))
    prompt = build_prompt(load_vocabulary(cfg.stt.vocabulary_file))
    segments = LiteLLMTranscriber(client, cfg.stt.model, prompt).transcribe(audio)
    return cfg, client, segments


def test_doctor_probes_pass(live) -> None:
    cfg, client, _segments = live

    failed = [c for c in doctor(cfg, client) if c.name.startswith("litellm") and not c.ok]

    assert failed == []


def test_transcribes_hebrew_with_timestamps(live) -> None:
    _cfg, _client, segments = live

    assert segments
    assert any(_hebrew(s.text) for s in segments)
    assert all(isinstance(s, Segment) and s.end >= s.start for s in segments)


def test_summarizes_in_hebrew(live) -> None:
    cfg, client, segments = live

    summary = LiteLLMSummarizer(client, cfg.summary.model, cfg.summary.max_chunk_chars).summarize(segments)

    assert summary is not None
    assert _hebrew(summary.text)
```

Run: `python -m pytest tests/test_live.py -q`
Expected: 3 skipped (no live environment variables set).

- [ ] **Step 6: Update the spec**

In `docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md`, make these four replacements.

§5 — replace:

```markdown
`line_owner.user` is the identity the SSO web app will match against.
The dedup key is `(call_id, line_owner.extension)`.
```

with:

```markdown
`line_owner.user` is the identity the SSO web app will match against; a sidecar without it is
accepted with a warning. `schema_version` must be `2`. `started_at` must carry a UTC offset, and a
sidecar whose `started_at` is more than one day in the future is quarantined (a recorder clock fault
would otherwise create a call retention never purges). `line_owner.extension` is trimmed.
The dedup key is `(call_id, line_owner.extension)`.
```

§6 — replace:

```markdown
CUCM forks each participating line separately, so a 10-person conference yields up to
10 recordings. Recordings that share a `conference_id` form one group:

- Wait until the group is quiet (no new copy for `group.settle_seconds`, default 60s)
  or until the ≤15-minute latency budget forces a decision.
- Pick one copy to transcribe — the longest, ties broken by earliest start.
- Write one output and record every member's `line_owner` as an owner of it.
- A copy arriving after the group is processed is attached as an owner without
  reprocessing.
```

with:

```markdown
CUCM forks each participating line separately, so a 10-person conference yields up to
10 recordings. Recordings that share a `conference_id` **and whose time spans overlap**
(within `group.settle_seconds`) form one group. A reused `conference_id`, such as a
recurring Meet-Me number, therefore starts a new group:

- Wait until the group is quiet (no new copy for `group.settle_seconds`, default 60s)
  or until its first copy has waited `group.max_wait_seconds` (default 300s).
- Pick one copy to transcribe — the longest, ties broken by earliest start.
- Write one output and record every member's `line_owner` as an owner of it.
- A copy arriving after the group is processed is attached as an owner without
  reprocessing — **unless** it is longer and ends more than `group.settle_seconds`
  after the processed copy. Then it replaces the processed copy: it is transcribed
  from scratch, the earlier copy's text outputs are deleted (audited as `superseded`),
  and the group still ends with one output.
- A failed copy never keeps its group: the next-longest copy is processed instead.

**Deviation pending owner sign-off.** The replacement rule means a meeting can be
transcribed more than once (extra GPU time). Without it, the first participant to hang
up decides the transcript, and everyone gets a truncated copy of a longer meeting.
```

§7 — replace:

```markdown
prompt that shows fillers to bias it toward keeping them. This is best effort, not a
guarantee.
```

with:

```markdown
prompt that shows fillers to bias it toward keeping them. This is best effort, not a
guarantee. Segments Whisper most likely invented are dropped: repetition loops
(`compression_ratio` > 2.4), silence (`no_speech_prob` > 0.6 with `avg_logprob` < -1),
and echoes of the prompt.
```

§8 — replace:

```markdown
| `audio` | Retry; corrupt input → quarantine |
| `stt` | Retry with backoff (LiteLLM down/overloaded); job stays queued |
| `summarize` | Retry once; then degrade to "summary unavailable" and continue |
| `output` | Retry; failure is a bug and fails loudly |
```

with:

```markdown
| `audio` | Retry with backoff; after 3 attempts (e.g. corrupt input) → `failed` for a human to inspect and `jabberscribe retry` |
| `stt` | LiteLLM down or overloaded (transport error, 429, 5xx): retry with backoff (30 s doubling, at most 30 min) for as long as it lasts; the job stays queued. Other errors (4xx, bad response): `failed` after 3 attempts |
| `summarize` | Invalid model output: retry once, then degrade to "summary unavailable" and continue. LiteLLM down or overloaded: retry with backoff, as for `stt`. Other 4xx (wrong model name, rejected parameter): `failed` after 3 attempts, loudly |
| `output` | Retry with backoff; failure is a bug and fails loudly (`failed` after 3 attempts) |
```

- [ ] **Step 7: Mark the original plan and fix its ingest order (M13)**

In `docs/superpowers/plans/2026-10-07-v2-pipeline.md`, replace:

```markdown
**Spec:** `docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md`
```

with:

```markdown
**Spec:** `docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md`

> **Superseded in part (2026-10-08):** Tasks 1–7 of this plan were executed as written. Tasks 8–12 were replaced
> before execution by `.superpowers/plan/2026-10-08-v2-hardened-tasks.md` (Tasks 8–19), which also hardens the
> modules from Tasks 2–7. Do not execute Tasks 8–12 below. Task 3 Step 8 shows the copy-before-create ingest order
> committed in `d245a3b`.
```

and, inside Task 3 Step 8's `scan_once` body, replace:

```python
        out_dir = out_dir_for(cfg.paths.out_root, sidecar)
        stored_audio = out_dir / f"recording{audio.suffix}"
        created = store.create(
            job_key=sidecar.job_key,
            call_id=sidecar.call_id,
            conference_id=sidecar.conference_id,
            audio_path=stored_audio,
            out_dir=out_dir,
            sidecar_json=sidecar.raw,
            started_at=sidecar.started_at,
            duration_sec=sidecar.duration_sec,
        )
        if not created:
            # Already known. Drop the duplicate rather than reprocess it.
            log.info("duplicate %s, discarding inbox copy", sidecar.job_key)
            audio.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            skipped.append(sidecar.job_key)
            continue

        out_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(audio), str(stored_audio))
        sidecar_path.unlink(missing_ok=True)
        enqueued.append(sidecar.job_key)
        log.info("enqueued %s", sidecar.job_key)
```

with (the order committed in `d245a3b`: copy into place, then create the row, then clear the inbox):

```python
        out_dir = out_dir_for(cfg.paths.out_root, sidecar)
        stored_audio = out_dir / f"recording{audio.suffix}"
        if store.get(sidecar.job_key) is not None:
            # Already known. Drop the duplicate rather than reprocess it.
            log.info("duplicate %s, discarding inbox copy", sidecar.job_key)
            audio.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            skipped.append(sidecar.job_key)
            continue

        # Every step is safe to repeat: a crash before create() leaves the inbox
        # pair in place and the next scan redoes the copy. The recording is never
        # deleted from the inbox before a job row references a complete copy.
        out_dir.mkdir(parents=True, exist_ok=True)
        part = stored_audio.with_suffix(stored_audio.suffix + ".part")
        shutil.copyfile(audio, part)
        part.replace(stored_audio)
        store.create(
            job_key=sidecar.job_key,
            call_id=sidecar.call_id,
            conference_id=sidecar.conference_id,
            audio_path=stored_audio,
            out_dir=out_dir,
            sidecar_json=sidecar.raw,
            started_at=sidecar.started_at,
            duration_sec=sidecar.duration_sec,
        )
        audio.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        enqueued.append(sidecar.job_key)
        log.info("enqueued %s", sidecar.job_key)
```

- [ ] **Step 8: Rewrite `README.md`**

````markdown
# JabberScribe

On-premises call and conference transcription for Jabber telephony.
Every call's recording lands in a drop folder; JabberScribe transcribes it in
strict verbatim, writes a Hebrew summary and action items, and files everything
in a per-call output folder.

**Nothing leaves the corporate network.** Transcription (Whisper / ivrit.ai)
and summarization (Gemma) both run behind the on-prem LiteLLM server.

## Status

v2 MVP pipeline. Capture — CUCM media forking to a SIPREC recorder — is
designed (see the spec) but not built; until it is, recordings are dropped in
by hand or by any recorder that follows the drop contract.

```bash
jabberscribe --config D:/jabberscribe/jabberscribe.yaml doctor     # ffmpeg, paths, real LiteLLM chat + transcription probes
jabberscribe --config ... process call.wav call.json   # one recording, end to end (only that job)
jabberscribe --config ... run                          # watch the inbox; purges once a day
jabberscribe --config ... purge                        # delete past-retention audio and text now
jabberscribe --config ... status                       # backlog, retrying and failed jobs (exit 1 if any failed)
jabberscribe --config ... retry <job_key>              # give a failed job fresh attempts
jabberscribe --config ... retry --failed               # ... every failed job
```

Secrets come from the environment, never from config: `JABBERSCRIBE_LITELLM_KEY`.

## Running as a service

Run `jabberscribe run` under a service wrapper (NSSM or Task Scheduler "at
startup", restart on failure) with an **absolute** `--config` path: a Windows
service starts in `C:\Windows\System32`. Relative paths inside the config
(`stt.vocabulary_file`) resolve against the config file.

Only one instance may work the database. `run`, `process` and `purge` hold
`<db_path folder>/jabberscribe.lock`; a second one exits with an error. Stop
the service before running `process` or `purge` by hand.

A poll that fails (a locked file, an unreachable share) is logged and the loop
carries on. When LiteLLM is down or overloaded, jobs wait and retry with
backoff (30 s doubling, at most 30 min) for as long as the outage lasts; other
errors fail a job after 3 attempts. Check `jabberscribe status` and use
`jabberscribe retry` once the cause is fixed.

## LiteLLM

`litellm.base_url` is the server root, e.g. `http://litellm.corp.local:4000`
(a trailing `/v1` is tolerated). `jabberscribe doctor` sends a one-line JSON
chat completion and a one-second transcription with the production prompt, so
a wrong model name, a chat template that rejects the request, or a route
without `verbose_json` segments fails at install time.

**Turn LiteLLM message logging off for these routes.** Otherwise the proxy
keeps full transcripts and summaries outside JabberScribe's retention purge:
set `litellm_settings.turn_off_message_logging: true`, do not enable
`store_prompts_in_spend_logs`, attach no logging callbacks (Langfuse, S3, ...)
and no response cache to the Whisper and Gemma routes.

## Output

`out/<YYYY>/<MM>/<call>/` holds `recording.wav`, `transcript.md`,
`summary.md`, `actions.md`, and `result.json` (everything, structured, with
owners, models and stage timings — the contract for the future web app). The
Markdown files are wrapped in `<div dir="rtl">` so Hebrew renders right to left.

A conference produces one output owned by every participating line. If a
longer copy of the meeting arrives after a shorter one was processed, the
longer copy is transcribed and replaces the earlier output (audited as
`superseded`).

**Access:** in the MVP the output folder is readable company-wide. This is an
accepted risk until the SSO web app ships; restrict the share's ACL if that
changes.

## Retention

Audio 90 days, text 365 days, counted from the later of the call's start and
its arrival. The daily purge also removes leftover STT copies in `work/`,
quarantined pairs and inbox orphans older than 90 days, and strips call
metadata from job rows past 365 days. Every deletion is audited in the
database's `audit_log` table. Backups and Volume Shadow Copies ("Previous
Versions") of the share keep deleted audio; align their retention with these
windows.

## Drop contract

The recorder writes `<name>.wav.part`, renames it to `<name>.wav`, then writes
the `<name>.json` sidecar **last**. See spec §5 for the sidecar schema
(`schema_version: 2`, `started_at` with a UTC offset). A malformed sidecar, or
one whose `started_at` is more than a day in the future, sends the pair to
`quarantine/` with a reason file.

## Tests

```bash
python -m pytest -q                       # unit + integration (fake LiteLLM), needs ffmpeg
JABBERSCRIBE_LIVE_CONFIG=config/jabberscribe.yaml JABBERSCRIBE_LIVE_CLIP=clip.wav python -m pytest -m slow
```

See [the v2 spec](docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md),
[the v2 pipeline plan](docs/superpowers/plans/2026-10-07-v2-pipeline.md) (Tasks 1–7)
and the hardened plan `.superpowers/plan/2026-10-08-v2-hardened-tasks.md` (Tasks 8–19).

## Language

Hebrew-primary audio with mixed-in English technical terms. User-facing output
is Hebrew (RTL); code, comments, commits, logs and CLI output are English.
````

- [ ] **Step 9: Final verification**

Run: `python -m pytest -q`
Expected: 237 passed, 3 skipped (only `tests/test_live.py` skipped).

Run: `python -m ruff check .`
Expected: `All checks passed!`

Run: `pip install -e . ; jabberscribe --config config/jabberscribe.yaml doctor`
Expected: `ffmpeg`, `stt.vocabulary_file` and `paths.*` lines show `[OK ]` (directories under `D:/jabberscribe` are created); the three `litellm*` lines show `[FAIL]` unless the real server is reachable — set `litellm.base_url` and the model names to real values to make them green.

- [ ] **Step 10: Commit**

```bash
git add config/jabberscribe.yaml README.md tests/test_config.py tests/test_live.py docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md docs/superpowers/plans/2026-10-07-v2-pipeline.md
git commit -m "docs: v2 shipping config, live LiteLLM check, README, spec and plan updates" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Audit coverage

| Finding | Task | | Finding | Task |
|---|---|---|---|---|
| C1 truncated conference | 14 (re-election), 19 (spec deviation) | | M1 naive `started_at` | 8 |
| C2 reused `conference_id` | 14 | | M2 `job_key` collisions | not addressed |
| C3 outage FAILs calls | 10, 11, 12, 15, 18 (`retry`) | | M3 extension not trimmed | 8 |
| I1 crash on one bad pair / poll | 9, 18 | | M4 literal BOM | 8 |
| I2 two workers | 15 (`run_job`), 17, 18 | | M5 ffmpeg stderr code page | 11 |
| I3 FAILED primary | 10, 14 | | M6 `process` exit code | 18 |
| I4 audio leftovers | 9, 11, 15, 16, 19 (VSS note) | | M7 CWD-relative paths | 17, 18, 19 |
| I5 metadata / LiteLLM logs | 16, 19 (README) | | M8 unaudited deletions | 9 |
| I6 recorder clock | 9, 16 | | M9 owner lost before DONE | 14 |
| I7 summary silently never works | 12, 18 (`doctor`) | | M10 sidecar parse outside `try` | 15 |
| I8 context window | 12 | | M11 tests on silence | 11 |
| I9 transient summary loss | 12, 15 | | M12 empty assertion | 14 |
| I10 prompt order, hallucinations | 11 | | M13 plan ingest order | 19 |
| I11 locked `result.json` | 13, 14 | | M14 `timestamp_granularities[]` | not addressed |
| §7 timings | 13, 15 | | M15 `/v1` suffix | 11 |
| §2.1.5 purge never runs | 18 | | M16 schema version | 10 (`user_version` only) |
