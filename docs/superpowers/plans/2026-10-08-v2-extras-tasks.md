# JabberScribe v2 Extras Implementation Plan (rev2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the buildable v2 extras on top of the hardened MVP: near/far speaker labels for dual-track calls, bracket cues as a separate layer, legal hold, admin alerting, and a heartbeat with hang-up-to-output latency percentiles. The extras that need owner decisions get design notes at the end, with no code.

**Architecture:** Same single service. The stage order becomes `audio → stt → cues → summarize → output`. The `stt` stage transcribes each channel of a dual-track call separately and merges the labelled segments by start time (`speakers.py`). The new `cues` stage runs an optional local CPU audio-event tagger behind a `Tagger` Protocol (`cues.py`). It never fails a job. The job store gains in-place schema migrations (v2 → v3 legal hold, v3 → v4 latency). `run` writes `heartbeat.json` and sends rate-limited webhook alerts (`health.py`, `alerts.py`).

**Tech Stack:** Python ≥3.12, pydantic 2, PyYAML, httpx (`httpx.MockTransport` in tests), SQLite (stdlib), ffmpeg/ffprobe on PATH, pytest, ruff. Optional extra `cues`: `panns-inference`, `torch`, `numpy` (CPU only).

**Base: feat/v2-pipeline e49c0cb** (`docs(spec): mark MVP pipeline implemented`). This revision is written against the code at that commit, not against the text of `docs/superpowers/plans/2026-10-08-v2-hardened-tasks.md`: review fixes after that plan changed `jobs.py` (`transient_failures`, guarded `finish`/`retry_later`/`fail`, `ACTIVE`, an in-place `ALTER` for `transient_failures`), `pipeline.py` (`clock=` seam, output `OSError` → transient), `group.py` (rewritten around `_winner`/`_merge`/`_discard_all`), `retention.py` (`ACTIVE` guards, per-job `try`), `summarize.py` (`SummaryUnavailable`), `cli.py` (per-phase isolation, `_utcnow` seam, `_read_vocabulary`), `output.py` (`summary_error`) and `config.py` (`overlap_slack_seconds`). Every step was applied mechanically, task by task, to an export of e49c0cb (`git archive e49c0cb`): each "replace X with Y" anchor occurs exactly once in its file when the task runs, every full-file block starts from the file's content at that point, and after each task `python -m pytest -q` and `python -m ruff check .` gave the counts written in that task (Windows, Python 3.12, ffmpeg on PATH). Before Task E1, confirm the base: `git rev-parse HEAD` on `feat/v2-pipeline` is `e49c0cb...`, `python -m pytest -q` reports `312 passed, 3 skipped` (the 3 are `tests/test_live.py`), and `python -m ruff check .` passes. If any file you are about to edit differs from e49c0cb, stop and reconcile first.

Real suite counts after each task: E1 326, E2 336, E3 359, E4 377, E5 386, E6 402, E7 403 passed (always 3 skipped). Without ffmpeg on PATH the ffmpeg-marked tests skip instead and the passed counts drop.

**Branch:** work on a new branch `feat/v2-extras`, created from the branch that holds the finished hardened plan (`feat/v2-pipeline`), in its own worktree (superpowers:using-git-worktrees).

**Spec:** `docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md` §2.2. **Audit:** `.superpowers/audit/expert-audit.md` §4.

**Out of scope (design notes only, at the end):** SSO web app, email notifications, PII redaction, full-text search, calendar enrichment, conference diarization.

## Defaults chosen without the owner — confirm

The owner was not available. Each item below is a binding default for this plan. Each one can be changed later in one place.

1. **Channel convention:** in a dual-track recording, channel 0 is the near end (the recorded line) and channel 1 is the far end. It is configurable as `stt.near_channel` because SIPREC recorders differ.
2. **Speaker names:** labels use the real CUCM `display_name` values from the sidecar. The near end is `line_owner.display_name` and the far end of a 1:1 call is `parties[0].display_name`. The fallbacks are the generic `צד א` / `צד ב`. A conference's far channel is the bridge mix, so it is labelled `משתתפים`, with no diarization.
3. **Speaker split is on by default** (`stt.split_channels: true`). It costs two STT calls per dual-track call. A channel whose peak stays below −50 dBFS is not sent to Whisper, because a dead leg invites hallucinations. If a `dual` sidecar comes with audio that cannot be split (e.g. mono), the call falls back to the mixed downmix instead of failing.
4. **Action-item owner from speaker labels — "owner only when stated" is not loosened.** A first-person commitment ("אני אשלח") counts as a stated owner only when the line's speaker label is a real name, i.e. a `display_name` from the sidecar. The generic fallback labels `צד א`, `צד ב` and `משתתפים` are not names: a first-person commitment on such a line states no owner, and the owner stays `null` unless a name is said in the call. The summary prompt says exactly this, names the three generic labels, and a test (`test_generic_speaker_labels_never_state_an_owner`) keeps the prompt in step with `speakers.py`.
5. **`result.json` stays `schema_version: 1`.** The new fields (`transcript[].speaker`, `cues`, `cues_available`) are additive. `speaker` is always present and is `null` on mixed-track calls.
6. **Cue tagger:** PANNs Cnn14 (AudioSet, 527 classes) on the CPU, as the optional extra `pip install .[cues]`. No PyTorch- or ONNX-free tagger can tell laughter or music from speech, so the model dependency is unavoidable. The checkpoint (`Cnn14_mAP=0.431.pth`, about 330 MB) and the AudioSet label CSV must be placed by hand, because nothing is downloaded at runtime. Without the extra, the checkpoint or the label file, the `cues` stage is skipped and logged, and the call goes out without cues.
7. **Cue taxonomy and tuning:** six Hebrew labels: `[צחוק]`, `[רעש רקע]`, `[מוזיקה]`, `[שקט]`, `[הקלדה]`, `[צלצול]`. The tagger scores 2 s windows at threshold 0.3. `[שקט]` is emitted only for ≥ 6 s, because short pauses are normal. Tagging runs on the whole recording, both channels downmixed. `cues.enabled` defaults to `true`.
8. **Cues layer:** `transcript.md` stays strict verbatim and untouched. `transcript_cues.md` is always written: it shows cues interleaved when they are available, and a notice when they are not. It counts as a text file for retention and superseding. `doctor` reports the tagger as information only and never fails on it.
9. **Database migration:** an existing v2 database is upgraded in place (`ALTER TABLE ... ADD COLUMN`; v2 → v3 → v4, one transaction per step). A migration skips a column that is already present, and the base's pre-release patch (add `transient_failures` when missing) is kept, so every v2 database the base accepts is upgraded. This replaces the base's "refuse any other version" for v2 and v3. Any other version is still refused.
10. **Legal hold policy:** anyone who can run the CLI on the server can set or clear a hold, and the audit actor is that OS account. `hold` needs a non-blank `--reason`; for `unhold` it is optional. A hold on any copy of a conference holds every copy. A hold also stops conference re-election from deleting the earlier, shorter output: it is kept and audited as `superseded`. `hold`/`unhold` do not take the instance lock (like `retry`).
11. **Alerts — disabled by default:** with no `alerts.webhook_url` in config and no `JABBERSCRIBE_ALERT_WEBHOOK_URL` in the environment (the shipped config has `webhook_url: null`), nothing is sent and `run` does not probe LiteLLM. When enabled: a generic JSON POST that includes a `text` field, so Teams and Slack incoming webhooks render it as-is. The URL comes from config `alerts.webhook_url`, or from env `JABBERSCRIBE_ALERT_WEBHOOK_URL`, which takes precedence because such URLs often embed a secret. An optional bearer token comes from env `JABBERSCRIBE_ALERT_TOKEN`. Each kind is sent at most once per hour, and the send times persist in `<db folder>/alerts.json`. Payloads carry kinds, counts and job keys, never names or call content. **If the webhook is outside the corporate network (Teams/Slack cloud), job keys (call id + extension) leave it.** When alerts are enabled, `run` probes LiteLLM (`GET /v1/models`, 10 s timeout) on every poll. A `job_failed` alert fires only for primaries that newly failed. The `backlog` alert fires when the oldest unfinished job arrived more than `alerts.backlog_minutes` (15) ago.
12. **Heartbeat and latency:** `run` writes `<db folder>/heartbeat.json` after every poll. Each output records `output_at` and `latency_sec` (hang-up to output) in the job row. `status` prints nearest-rank p50/p95 over the outputs written in the last 24 h, plus the heartbeat age.

## Global Constraints

- Everything stays on-prem: STT and summary go only to the configured LiteLLM `base_url`. The cue tagger runs locally on the CPU, and no model or label file is downloaded at runtime.
- Secrets come from environment variables, never config: `JABBERSCRIBE_LITELLM_KEY`, `JABBERSCRIBE_ALERT_WEBHOOK_URL`, `JABBERSCRIBE_ALERT_TOKEN`.
- User-facing output (Markdown files, summary, action items, speaker labels, cue labels) is Hebrew, with English technical terms kept as spoken. Code, comments, commits, logs and CLI operator output are English.
- `transcript.md` is strict verbatim: fillers, false starts and repetitions are kept, with no cleanup. Cues never enter it. Speaker labels prefix a line as `[HH:MM:SS] <speaker>: text` only when the speaker is known.
- A cues failure, or a missing tagger, never fails a job. A summary failure never costs the transcript. These rules are unchanged from the base.
- Retention: audio 90 days, text 365 days, unchanged. A job on legal hold, together with every copy of its conference, is skipped by purge until the hold is released. Every hold and release is audited with the OS account as actor.
- Every file the service writes goes through `.part` + rename (`output.write_atomic`), including `heartbeat.json` and `alerts.json`.
- `run`, `process` and `purge` hold the single-instance lock. `hold`, `unhold`, `retry` and `status` do not.
- Windows host. Never put a literal U+FEFF in source.
- Python 3.12. Line length 120. The ruff rules `E, F, I, UP, B` must pass (`python -m ruff check .`).
- Every commit message ends with the trailer line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (pass it as a second `-m`).

## File Map

| File | Task | Responsibility |
|---|---|---|
| `jabberscribe/jobs.py` | E1, E2, E5 (edit) | In-place migrations; `legal_hold`/`hold_reason`; `is_held`; `output_at`/`latency_sec`; status counts; `cues` stage in `STAGE_ORDER` |
| `jabberscribe/audit.py` | E1 (edit) | `LEGAL_HOLD_SET`, `LEGAL_HOLD_RELEASED` |
| `jabberscribe/retention.py` | E1, E3 (edit) | Skip held calls; sweep per-channel STT copies |
| `jabberscribe/group.py` | E1 (edit) | `_discard_all` keeps a held loser's outputs when `_merge` supersedes it |
| `jabberscribe/health.py` | E2 (create) | `heartbeat.json`, latency percentiles |
| `jabberscribe/audio.py` | E3 (rewrite) | Per-channel Opus encode, channel peak level, `STT_GLOB` |
| `jabberscribe/speakers.py` | E3 (create) | Speaker labels from the sidecar, STT inputs per route, labelled merge |
| `jabberscribe/stt.py` | E3 (edit) | `Segment.speaker`, `segment_line` |
| `jabberscribe/summarize.py` | E3 (edit) | Speaker-aware transcript text and prompt rule |
| `jabberscribe/cues.py` | E4 (create) | `Cue`, `Tagger`, AudioSet→Hebrew mapping, windowed decode, PANNs tagger, lazy loader |
| `jabberscribe/output.py` | E3, E5 (edit) | Speaker lines; `transcript_cues.md`; `cues` in `result.json` |
| `jabberscribe/pipeline.py` | E2, E3, E5 (edit) | Record latency; per-channel STT; `cues` stage |
| `jabberscribe/alerts.py` | E6 (create) | Rate-limited webhook alerts, job/backlog checks, LiteLLM probe |
| `jabberscribe/config.py` | E3, E4, E6 (edit) | `stt.split_channels`, `stt.near_channel`, `cues.*`, `alerts.*` |
| `jabberscribe/cli.py` | E1, E2, E5, E6 (edit) | `hold`/`unhold`; heartbeat; latency in `status`; tagger wiring; alerts |
| `pyproject.toml` | E4 (edit) | Optional extra `cues` |
| `tests/conftest.py` | E1 (edit) | Shared `cfg_file` fixture |
| `config/jabberscribe.yaml`, `README.md`, spec, `docs/superpowers/plans/2026-10-08-v2-extras-tasks.md` | E7 | Shipping config, docs, tracked copy of this plan |

---

### Task E1: Schema migrations and legal hold

Spec §2.2 does not list this; audit §4a item 2. One column pair, an audited CLI, purge and supersede both respect the hold. Also introduces in-place schema migrations, which Task E2 extends.

**Files:**
- Modify: `jabberscribe/jobs.py`, `jabberscribe/audit.py`, `jabberscribe/retention.py`, `jabberscribe/group.py`, `jabberscribe/cli.py`, `tests/conftest.py`
- Test (create): `tests/test_jobs_migration.py`, `tests/test_hold.py`

**Interfaces:**
- Consumes (base e49c0cb): `JobStore`, `Job` (last field `transient_failures: int`, no default), `SchemaError`, `SCHEMA_VERSION = 2`, `utcnow`, `_row_to_job`, `init_schema` with its `transient_failures` patch (`jobs.py`); `AuditLog.record(job_key, action, detail)`; `purge(cfg, store, audit, now) -> PurgeResult` with its per-job `try` and `ACTIVE` guards (`retention.py`); `settle`, `_merge`, `_discard_all(cfg, audit, olds, new_key)`, `_discard_outputs` (`group.py`); `cli.main`, `_status(store)` (exit 1 on unresolved failures), `_report_purge`.
- Produces:
  - `jobs.SCHEMA_VERSION = 3`; `jobs.MIGRATIONS: dict[int, tuple[tuple[str, str], ...]]` (key = version the columns upgrade *from*; value = `(column, type and default)` pairs, a present column is skipped). `JobStore.init_schema()` upgrades a database at any version in `MIGRATIONS` in place, one transaction per step (`JobStore._migrate`); any other non-current version still raises `SchemaError` ("... point paths.db_path at a fresh file").
  - `Job` gains trailing fields `legal_hold: bool = False`, `hold_reason: str | None = None` (after `transient_failures`).
  - `JobStore.hold(job_key: str, reason: str) -> bool` (False: unknown job); `JobStore.release_hold(job_key: str) -> bool` (False: not on hold); `JobStore.is_held(job_key: str) -> bool` (the job, its primary, a fellow member, or one of its members is held); `JobStore.held_jobs() -> list[Job]`.
  - `audit.LEGAL_HOLD_SET = "legal_hold_set"`, `audit.LEGAL_HOLD_RELEASED = "legal_hold_released"`.
  - `PurgeResult` gains a trailing field `held: tuple[str, ...] = ()` (held jobs that are past audio retention and were kept).
  - `group._discard_all(cfg, store, audit, olds, new_key)` (gains `store`): a held loser's outputs are kept and audited `superseded` with `outputs kept under legal hold`.
  - CLI: `jabberscribe hold <job_key> --reason TEXT` → 0, or 1 for an unknown job or a blank reason. `jabberscribe unhold <job_key> [--reason TEXT]` → 0, or 1 when the job is not on hold. `status` lists `  h <job_key>: on legal hold: <reason>` after the failed jobs; a hold never changes its exit code. `purge` prints `on legal hold (kept): N`.
  - fixture `cfg_file(tmp_path) -> Path`: a YAML config equivalent to the `cfg` fixture (same `tmp_path` folders and `js.db`).

- [ ] **Step 1: Add the shared `cfg_file` fixture**

Append to the end of `tests/conftest.py`:

```python
@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    """The `cfg` fixture's configuration as a YAML file, for tests that drive cli.main."""
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
```

(`tests/test_cli.py` defines its own `cfg_file`; a module fixture shadows the conftest one, so nothing changes there.)

- [ ] **Step 2: Write the failing tests**

Create `tests/test_jobs_migration.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from jabberscribe.jobs import SCHEMA_VERSION, JobStore, SchemaError

#: The jobs table exactly as schema version 2 (the hardened MVP, feat/v2-pipeline e49c0cb) created it.
V2_SCHEMA = """
CREATE TABLE jobs (
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
  transient_failures INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  next_attempt_at TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
"""


def _v2_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V2_SCHEMA)
    conn.execute(
        "INSERT INTO jobs (job_key, call_id, status, stage, audio_path, out_dir, sidecar_json, started_at,"
        " duration_sec, created_at, updated_at) VALUES ('old_1042', 'old', 'done', 'output', '/a.wav', '/out',"
        " '{}', '2026-10-07T14:03:11+03:00', 5, '2026-10-07T11:03:11+00:00', '2026-10-07T11:20:00+00:00')"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


def _user_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_a_v2_database_is_upgraded_in_place(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)

    store = JobStore(db)
    store.init_schema()

    job = store.get("old_1042")
    assert (job.status, job.legal_hold, job.hold_reason, job.transient_failures) == ("done", False, None, 0)
    assert _user_version(db) == SCHEMA_VERSION


def test_a_pre_release_v2_database_without_transient_failures_is_upgraded_too(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    conn = sqlite3.connect(db)
    conn.executescript(V2_SCHEMA.replace("  transient_failures INTEGER NOT NULL DEFAULT 0,\n", ""))
    conn.execute("PRAGMA user_version = 2")
    conn.close()

    store = JobStore(db)
    store.init_schema()

    assert _user_version(db) == SCHEMA_VERSION
    conn = sqlite3.connect(db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    conn.close()
    assert {"transient_failures", "legal_hold", "hold_reason"} <= columns


def test_an_upgraded_database_opens_again(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)
    JobStore(db).init_schema()

    store = JobStore(db)
    store.init_schema()

    assert store.get("old_1042") is not None


def test_a_newer_database_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    JobStore(db).init_schema()
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(SchemaError, match="fresh file"):
        JobStore(db).init_schema()
```

Create `tests/test_hold.py`:

```python
import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from jabberscribe.audit import LEGAL_HOLD_RELEASED, LEGAL_HOLD_SET, PURGED_AUDIO, SUPERSEDED, AuditLog
from jabberscribe.cli import main
from jabberscribe.group import settle
from jabberscribe.jobs import DONE, SCRUBBED_SIDECAR
from jabberscribe.output import RESULT_FILE, TEXT_FILES, TRANSCRIPT_FILE
from jabberscribe.retention import purge
from jabberscribe.watcher import scan_once

# Matches the make_sidecar default started_at.
STARTED = datetime(2026, 10, 7, 14, 3, 11, tzinfo=timezone(timedelta(hours=3)))


def _job(store, key: str, *, conference_id: str | None = None) -> None:
    store.create(
        job_key=key,
        call_id=key.split("_")[0],
        conference_id=conference_id,
        audio_path=Path(f"/out/{key}/recording.wav"),
        out_dir=Path(f"/out/{key}"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )


def _set_created(cfg, key: str, created: datetime) -> None:
    conn = sqlite3.connect(cfg.paths.db_path)
    conn.execute("UPDATE jobs SET created_at = ? WHERE job_key = ?", (created.isoformat(timespec="seconds"), key))
    conn.commit()
    conn.close()


def _ingested_call(cfg, store, audit, make_wav, make_sidecar):
    make_wav(cfg.paths.inbox / "r.wav")
    make_sidecar(cfg.paths.inbox / "r.json", call_id="r1")
    scan_once(cfg, store, audit, min_age_seconds=0)
    _set_created(cfg, "r1_1042", STARTED)
    # A finished call: purge keeps the text of a job still in the pipeline (ACTIVE) whatever its age.
    store.set_status("r1_1042", DONE)
    job = store.get("r1_1042")
    for name in TEXT_FILES:
        (job.out_dir / name).write_text("x", encoding="utf-8")
    return job


def test_hold_and_release(store) -> None:
    _job(store, "a_1")

    assert store.hold("a_1", "תביעה 2026-17") is True
    job = store.get("a_1")
    assert (job.legal_hold, job.hold_reason) == (True, "תביעה 2026-17")
    assert store.is_held("a_1")
    assert [j.job_key for j in store.held_jobs()] == ["a_1"]

    assert store.release_hold("a_1") is True
    job = store.get("a_1")
    assert (job.legal_hold, job.hold_reason) == (False, None)
    assert not store.is_held("a_1")


def test_unknown_or_unheld_jobs_are_refused(store) -> None:
    assert store.hold("nope_1", "x") is False
    _job(store, "a_1")

    assert store.release_hold("a_1") is False


def test_a_hold_on_any_copy_covers_the_whole_conference(store) -> None:
    for key in ("p_1", "m_2", "n_3"):
        _job(store, key, conference_id="conf-1")
    _job(store, "x_4")
    store.group_into("m_2", "p_1")
    store.group_into("n_3", "p_1")

    store.hold("m_2", "member held")
    assert [store.is_held(k) for k in ("p_1", "m_2", "n_3", "x_4")] == [True, True, True, False]

    store.release_hold("m_2")
    store.hold("p_1", "primary held")
    assert [store.is_held(k) for k in ("p_1", "m_2", "n_3", "x_4")] == [True, True, True, False]


def test_purge_keeps_a_held_call_past_both_retention_windows(cfg, store, audit, make_wav, make_sidecar) -> None:
    job = _ingested_call(cfg, store, audit, make_wav, make_sidecar)
    store.hold(job.job_key, "litigation")

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=400))

    assert result.held == (job.job_key,)
    assert (result.audio_deleted, result.text_deleted) == ((), ())
    assert job.audio_path.is_file()
    assert (job.out_dir / RESULT_FILE).is_file()
    assert store.get(job.job_key).sidecar_json != SCRUBBED_SIDECAR


def test_purge_resumes_once_the_hold_is_released(cfg, store, audit, make_wav, make_sidecar) -> None:
    job = _ingested_call(cfg, store, audit, make_wav, make_sidecar)
    store.hold(job.job_key, "litigation")
    purge(cfg, store, audit, now=STARTED + timedelta(days=400))
    store.release_hold(job.job_key)

    result = purge(cfg, store, audit, now=STARTED + timedelta(days=400))

    assert (result.audio_deleted, result.text_deleted, result.held) == ((job.job_key,), (job.job_key,), ())
    assert PURGED_AUDIO in [e.action for e in audit.entries(job.job_key)]


def _drop(cfg, store, audit, make_wav, make_sidecar, leg: str, ext: str, **extra) -> str:
    make_wav(cfg.paths.inbox / f"{leg}.wav")
    make_sidecar(cfg.paths.inbox / f"{leg}.json", call_id=leg, extension=ext, conference_id="conf-1", **extra)
    scan_once(cfg, store, audit, min_age_seconds=0)
    return f"{leg}_{ext}"


def test_a_held_primary_keeps_its_outputs_when_a_longer_copy_replaces_it(
    cfg, store, audit, make_wav, make_sidecar
) -> None:
    start = "2026-10-07T14:00:00+03:00"
    leaver = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at=start, duration_sec=480)
    _set_created(cfg, leaver, datetime.now(UTC) - timedelta(seconds=61))
    settle(cfg, store, audit, datetime.now(UTC))
    out_dir = store.get(leaver).out_dir
    (out_dir / RESULT_FILE).write_text(json.dumps({"owners": []}), encoding="utf-8")
    (out_dir / TRANSCRIPT_FILE).write_text("x", encoding="utf-8")
    store.set_status(leaver, DONE)
    store.hold(leaver, "litigation")

    host = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at=start, duration_sec=3600)
    result = settle(cfg, store, audit, datetime.now(UTC))

    assert result.superseded == (leaver,)
    assert result.released == (host,)
    assert (out_dir / RESULT_FILE).is_file()
    assert (out_dir / TRANSCRIPT_FILE).is_file()
    entry = audit.entries(leaver)[-1]
    assert entry.action == SUPERSEDED
    assert "legal hold" in entry.detail


def test_hold_command_sets_the_hold_and_audits_the_actor(cfg_file, store, tmp_path, capsys) -> None:
    _job(store, "a_1")

    assert main(["--config", str(cfg_file), "hold", "a_1", "--reason", "תביעה 17"]) == 0

    assert "a_1: on legal hold" in capsys.readouterr().out
    assert store.get("a_1").legal_hold is True
    entry = AuditLog(tmp_path / "js.db").entries("a_1")[-1]
    assert (entry.action, entry.detail) == (LEGAL_HOLD_SET, "תביעה 17")
    assert entry.actor


def test_unhold_command_releases_and_audits(cfg_file, store, tmp_path, capsys) -> None:
    _job(store, "a_1")
    main(["--config", str(cfg_file), "hold", "a_1", "--reason", "x"])

    assert main(["--config", str(cfg_file), "unhold", "a_1", "--reason", "case closed"]) == 0

    assert store.get("a_1").legal_hold is False
    entry = AuditLog(tmp_path / "js.db").entries("a_1")[-1]
    assert (entry.action, entry.detail) == (LEGAL_HOLD_RELEASED, "case closed")
    assert main(["--config", str(cfg_file), "unhold", "a_1"]) == 1
    assert "not on legal hold" in capsys.readouterr().err


def test_hold_refuses_an_unknown_job_and_a_blank_reason(cfg_file, store, capsys) -> None:
    _job(store, "a_1")

    assert main(["--config", str(cfg_file), "hold", "nope_1", "--reason", "x"]) == 1
    assert main(["--config", str(cfg_file), "hold", "a_1", "--reason", "   "]) == 1

    err = capsys.readouterr().err
    assert "unknown job" in err
    assert "reason is required" in err
    assert store.get("a_1").legal_hold is False


def test_status_lists_held_calls(cfg_file, store, capsys) -> None:
    _job(store, "a_1")
    store.hold("a_1", "litigation")

    assert main(["--config", str(cfg_file), "status"]) == 0

    assert "h a_1: on legal hold: litigation" in capsys.readouterr().out
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python -m pytest tests/test_jobs_migration.py tests/test_hold.py -q`
Expected: FAIL. `test_hold.py` fails at collection with `ImportError: cannot import name 'LEGAL_HOLD_RELEASED' from 'jabberscribe.audit'`. In `test_jobs_migration.py` (2 failed, 2 passed), `test_a_v2_database_is_upgraded_in_place` fails with `AttributeError: 'Job' object has no attribute 'legal_hold'` and `test_a_pre_release_v2_database_without_transient_failures_is_upgraded_too` fails on the missing `legal_hold`/`hold_reason` columns. `test_an_upgraded_database_opens_again` and `test_a_newer_database_is_refused` pass; they are regression guards for the migration.

- [ ] **Step 4: Add the migrations and hold columns to `jabberscribe/jobs.py`**

The base's last `Job` field is the non-default `transient_failures`, so the new defaulted fields go after it. The base already patches one pre-release v2 database in place (it adds `transient_failures` when missing, after the `CREATE TABLE IF NOT EXISTS`). That patch stays as it is. The migrations run before it and add each column only when it is missing, so a pre-release v2 database (no `transient_failures`) and the base test `test_init_schema_adds_transient_failures_to_an_early_v2_database` (which builds its "early" table from the current `_SCHEMA`, hold columns included) both upgrade cleanly.

In `jabberscribe/jobs.py`, replace:

```python
#: Stored in PRAGMA user_version. A database written by any other version is refused.
SCHEMA_VERSION = 2
```

with:

```python
#: Stored in PRAGMA user_version. Versions listed in MIGRATIONS are upgraded in place; any other is refused.
SCHEMA_VERSION = 3

#: Columns that upgrade a database from the keyed version to the next one: (name, type and default).
#: A column that is already there is skipped, so a pre-release database that has it upgrades cleanly.
MIGRATIONS: dict[int, tuple[tuple[str, str], ...]] = {
    2: (
        ("legal_hold", "INTEGER NOT NULL DEFAULT 0"),
        ("hold_reason", "TEXT"),
    ),
}
```

and replace:

```python
  next_attempt_at TEXT,
  created_at      TEXT NOT NULL,
```

with:

```python
  next_attempt_at TEXT,
  legal_hold      INTEGER NOT NULL DEFAULT 0,
  hold_reason     TEXT,
  created_at      TEXT NOT NULL,
```

and replace:

```python
    transient_failures: int


def _row_to_job(row: sqlite3.Row) -> Job:
```

with:

```python
    transient_failures: int
    legal_hold: bool = False
    hold_reason: str | None = None


def _row_to_job(row: sqlite3.Row) -> Job:
```

and replace:

```python
        transient_failures=row["transient_failures"],
    )
```

with:

```python
        transient_failures=row["transient_failures"],
        legal_hold=bool(row["legal_hold"]),
        hold_reason=row["hold_reason"],
    )
```

and replace:

```python
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
```

with:

```python
    def init_schema(self) -> None:
        """Create the tables, upgrade a known older database in place, or refuse anything else.

        CREATE TABLE IF NOT EXISTS would silently keep an older jobs table and
        fail later with a cryptic column error, so the version is checked first.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        has_jobs = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'jobs'").fetchone()
        if has_jobs and version != SCHEMA_VERSION and version not in MIGRATIONS:
            raise SchemaError(
                f"database schema version {version} is not {SCHEMA_VERSION}; point paths.db_path at a fresh file"
            )
        if has_jobs:
            while version != SCHEMA_VERSION:
                self._migrate(version)
                version += 1
        self._conn.executescript(_SCHEMA)
```

and replace:

```python
    def create(
        self,
        *,
```

with:

```python
    def _migrate(self, version: int) -> None:
        """Upgrade from `version` to the next version in one transaction."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            present = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            for name, definition in MIGRATIONS[version]:
                if name not in present:
                    self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            self._conn.execute(f"PRAGMA user_version = {version + 1}")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def create(
        self,
        *,
```

and replace:

```python
    def list_all(self) -> list[Job]:
```

with:

```python
    def hold(self, job_key: str, reason: str) -> bool:
        """Put a job on legal hold, or replace the reason of its hold. Returns False for an unknown job."""
        cur = self._conn.execute(
            "UPDATE jobs SET legal_hold = 1, hold_reason = ?, updated_at = ? WHERE job_key = ?",
            (reason, utcnow(), job_key),
        )
        return cur.rowcount == 1

    def release_hold(self, job_key: str) -> bool:
        """Lift a legal hold. Returns False unless the job was on hold."""
        cur = self._conn.execute(
            "UPDATE jobs SET legal_hold = 0, hold_reason = NULL, updated_at = ? WHERE job_key = ? AND legal_hold = 1",
            (utcnow(), job_key),
        )
        return cur.rowcount == 1

    def is_held(self, job_key: str) -> bool:
        """True when this job, its primary, a fellow member, or one of its members is on hold.

        A conference is one call: a hold on any copy of it holds every copy.
        hand_over keeps groups one level deep, so these four relations cover a whole group.
        """
        row = self._conn.execute(
            "SELECT 1 FROM jobs h, jobs k WHERE k.job_key = ? AND h.legal_hold = 1 AND ("
            " h.job_key = k.job_key OR h.job_key = k.grouped_into OR h.grouped_into = k.job_key"
            " OR (k.grouped_into IS NOT NULL AND h.grouped_into = k.grouped_into)) LIMIT 1",
            (job_key,),
        ).fetchone()
        return row is not None

    def held_jobs(self) -> list[Job]:
        rows = self._conn.execute("SELECT * FROM jobs WHERE legal_hold = 1 ORDER BY created_at, rowid").fetchall()
        return [_row_to_job(r) for r in rows]

    def list_all(self) -> list[Job]:
```

- [ ] **Step 5: Add the audit actions**

In `jabberscribe/audit.py`, replace:

```python
SCRUBBED_METADATA = "scrubbed_metadata"
```

with:

```python
SCRUBBED_METADATA = "scrubbed_metadata"
LEGAL_HOLD_SET = "legal_hold_set"
LEGAL_HOLD_RELEASED = "legal_hold_released"
```

- [ ] **Step 6: Make purge skip held calls**

In `jabberscribe/retention.py`, replace:

```python
text retention a row keeps no call metadata. Every deletion is audited.
"""
```

with:

```python
text retention a row keeps no call metadata. Every deletion is audited.

A call on legal hold -- and every copy of its conference -- is skipped
entirely until the hold is released (`jabberscribe unhold`).
"""
```

and replace:

```python
    swept: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
```

with:

```python
    swept: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: Calls past audio retention that were kept because of a legal hold.
    held: tuple[str, ...] = ()
```

and replace:

```python
    audio_deleted: list[str] = []
    text_deleted: list[str] = []
    errors: list[str] = []
```

with:

```python
    audio_deleted: list[str] = []
    text_deleted: list[str] = []
    errors: list[str] = []
    held: list[str] = []
```

The hold check goes inside the per-job `try`, so a database error on it is reported like the others.

and replace:

```python
        try:
            if age > cfg.retention.audio_days and job.audio_path.is_file():
```

with:

```python
        try:
            if store.is_held(job.job_key):
                # Checked per job, right before deleting, so a hold set during a purge still counts.
                # An active held job is not failed for its audio either: the audio is kept.
                if age > cfg.retention.audio_days:
                    held.append(job.job_key)
                continue
            if age > cfg.retention.audio_days and job.audio_path.is_file():
```

and replace:

```python
    log.info(
        "purge deleted %d audio file(s), text for %d call(s), %d leftover file(s)",
        len(audio_deleted),
        len(text_deleted),
        len(swept),
    )
    return PurgeResult(tuple(audio_deleted), tuple(text_deleted), tuple(swept), tuple(errors))
```

with:

```python
    log.info(
        "purge deleted %d audio file(s), text for %d call(s), %d leftover file(s); %d call(s) kept on legal hold",
        len(audio_deleted),
        len(text_deleted),
        len(swept),
        len(held),
    )
    return PurgeResult(tuple(audio_deleted), tuple(text_deleted), tuple(swept), tuple(errors), tuple(held))
```

- [ ] **Step 7: Keep a held primary's outputs when it is superseded**

The base supersedes through `_merge`, which discards every loser's outputs via `_discard_all` before the database changes. A held loser's outputs are skipped there and audited instead; every primary that is not the winner is still handed over as before.

In `jabberscribe/group.py`, replace:

```python
def _discard_all(cfg: Config, audit: AuditLog, olds: list[Job], new_key: str) -> bool:
    # Try every job, even after one fails, so the retry has less left to do.
    return all([_discard_outputs(cfg, audit, old, new_key) for old in olds])
```

with:

```python
def _discard_all(cfg: Config, store: JobStore, audit: AuditLog, olds: list[Job], new_key: str) -> bool:
    """Discard the outputs of every superseded primary, except one on legal hold: its outputs are evidence.

    The new primary writes its own folder, so a held loser's files stay where they are.
    """
    done: list[bool] = []
    # Try every job, even after one fails, so the retry has less left to do.
    for old in olds:
        if store.is_held(old.job_key):
            audit.record(old.job_key, SUPERSEDED, f"replaced by {new_key}; outputs kept under legal hold")
            done.append(True)
        else:
            done.append(_discard_outputs(cfg, audit, old, new_key))
    return all(done)
```

and replace:

```python
    if not _discard_all(cfg, audit, losers, winner.job_key):
```

with:

```python
    if not _discard_all(cfg, store, audit, losers, winner.job_key):
```

- [ ] **Step 8: Add `hold` and `unhold` to the CLI**

In `jabberscribe/cli.py`, replace:

```python
from jabberscribe.audit import AuditLog
```

with:

```python
from jabberscribe.audit import LEGAL_HOLD_RELEASED, LEGAL_HOLD_SET, AuditLog
```

and replace:

```python
    print(f"leftovers swept: {len(result.swept)}")
```

with:

```python
    print(f"leftovers swept: {len(result.swept)}")
    print(f"on legal hold (kept): {len(result.held)}")
```

and replace:

```python
def _status(store: JobStore) -> int:
```

with:

```python
def _hold(store: JobStore, audit: AuditLog, job_key: str, reason: str) -> int:
    """Put a call on legal hold. The audit row's actor is the OS account running the command."""
    reason = reason.strip()
    if not reason:
        print("a reason is required for a legal hold", file=sys.stderr)
        return 1
    if not store.hold(job_key, reason):
        print(f"{job_key}: unknown job", file=sys.stderr)
        return 1
    audit.record(job_key, LEGAL_HOLD_SET, reason)
    print(f"{job_key}: on legal hold; purge skips it and every copy of its conference")
    return 0


def _unhold(store: JobStore, audit: AuditLog, job_key: str, reason: str) -> int:
    if not store.release_hold(job_key):
        print(f"{job_key}: not on legal hold; nothing released", file=sys.stderr)
        return 1
    audit.record(job_key, LEGAL_HOLD_RELEASED, reason.strip())
    print(f"{job_key}: legal hold released; retention applies again")
    return 0


def _status(store: JobStore) -> int:
```

and replace:

```python
            unresolved = unresolved or primary is None or primary.status != DONE
    return 1 if unresolved else 0
```

with:

```python
            unresolved = unresolved or primary is None or primary.status != DONE
    for job in store.held_jobs():
        # Information only: a hold never changes the exit code.
        print(f"  h {job.job_key}: on legal hold: {job.hold_reason}")
    return 1 if unresolved else 0
```

and replace:

```python
    sub.add_parser("status", help="job counts, backlog age, retrying and failed jobs")
    return parser
```

with:

```python
    sub.add_parser("status", help="job counts, backlog age, retrying, failed and held jobs")

    hold_cmd = sub.add_parser("hold", help="put a call on legal hold: purge keeps it until unhold")
    hold_cmd.add_argument("job_key")
    hold_cmd.add_argument("--reason", required=True, help="why, e.g. the case number (audited)")

    unhold_cmd = sub.add_parser("unhold", help="release a legal hold; retention applies again")
    unhold_cmd.add_argument("job_key")
    unhold_cmd.add_argument("--reason", default="", help="why (audited)")
    return parser
```

and replace:

```python
    if args.command == "status":
        return _status(store)
```

with:

```python
    if args.command == "status":
        return _status(store)
    if args.command == "hold":
        return _hold(store, audit, args.job_key, args.reason)
    if args.command == "unhold":
        return _unhold(store, audit, args.job_key, args.reason)
```

- [ ] **Step 9: Run the new tests**

Run: `python -m pytest tests/test_jobs_migration.py tests/test_hold.py -q`
Expected: PASS (14 passed: 4 + 10).

- [ ] **Step 10: Run the full suite and lint**

Run: `python -m pytest -q`
Expected: `326 passed, 3 skipped` (base 312 + 14). The base `test_init_schema_refuses_an_older_database` still passes: version 0 is not in `MIGRATIONS`. The base `test_init_schema_adds_transient_failures_to_an_early_v2_database` still passes: its hold columns are already present, so the migration skips them.

Run: `python -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 11: Commit**

```bash
git add jabberscribe/jobs.py jabberscribe/audit.py jabberscribe/retention.py jabberscribe/group.py jabberscribe/cli.py tests/conftest.py tests/test_jobs_migration.py tests/test_hold.py
git commit -m "feat(hold): audited legal hold honoured by purge and supersede; in-place schema migration" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task E2: Heartbeat and hang-up-to-output latency

Lets operators see that the service is alive and that it meets the spec's ≤ 15 min latency. `result.json` already carries `timings.hangup_to_output_sec` (base `pipeline.py`). This task also stores it in the job row so `status` can report p50/p95 without opening a year of result files.

**Files:**
- Create: `jabberscribe/health.py`
- Modify: `jabberscribe/jobs.py`, `jabberscribe/pipeline.py`, `jabberscribe/cli.py`, `tests/test_pipeline.py` (append), `tests/test_jobs_migration.py` (append)
- Test (create): `tests/test_health.py`

**Interfaces:**
- Consumes: `MIGRATIONS`, `SCHEMA_VERSION`, `Job`, `JobStore` (E1); `iso`, `utcnow`, `ACTIVE` (base `jobs.py`); `write_atomic` (`output.py`); `process_job` output stage, its `OSError` → `TransientError` `try`, and `timings["hangup_to_output_sec"]` (base `pipeline.py`); `cli._serve` (per-phase isolation, daily purge block), `cli._utcnow`, `_status` (base, E1).
- Produces:
  - `jobs.SCHEMA_VERSION = 4`; `MIGRATIONS[3] = (("output_at", "TEXT"), ("latency_sec", "REAL"))`. `Job` gains trailing fields `output_at: str | None = None`, `latency_sec: float | None = None`.
  - `JobStore.record_output(job_key: str, latency_sec: float) -> None` (stamps `output_at = utcnow()`); `JobStore.latencies_since(since: datetime) -> list[float]` (ascending); `JobStore.status_counts() -> dict[str, int]`; `JobStore.oldest_pending_created_at() -> str | None` (min `created_at` over `ACTIVE`: QUEUED, RUNNING, WAITING).
  - `health`: `HEARTBEAT_FILE = "heartbeat.json"`; `LATENCY_WINDOW = timedelta(hours=24)`; `heartbeat_path(db_path: Path) -> Path` (`db_path.parent / HEARTBEAT_FILE`); `pending_age_sec(store, now) -> float | None`; `write_heartbeat(path: Path, store: JobStore, now: datetime, *, polls: int, last_poll_ok: bool) -> None`; `read_heartbeat(path: Path) -> dict | None`; `percentile(values: list[float], pct: float) -> float` (nearest rank); `latency_summary(store: JobStore, now: datetime) -> str`.
  - `heartbeat.json` keys: `last_poll_at` (ISO UTC), `pid`, `polls`, `last_poll_ok`, `counts` (status → count), `oldest_pending_age_sec` (float or null).
  - The pipeline output stage calls `store.record_output(job_key, timings["hangup_to_output_sec"])` once `write_outputs` succeeded. `run` writes the heartbeat after every poll, phases and purge included, stamped with `cli._utcnow()` (a write failure is logged, never fatal). `status` prints `latency 24h (hang-up to output): N call(s), p50 X.X min, p95 Y.Y min` (or `... no calls`) and `heartbeat: S s ago, poll N, last poll ok|FAILED` (or `heartbeat: none ...`). `cli._status(cfg: Config, store: JobStore) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_health.py`:

```python
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.cli import main
from jabberscribe.health import (
    HEARTBEAT_FILE,
    heartbeat_path,
    latency_summary,
    percentile,
    read_heartbeat,
    write_heartbeat,
)
from jabberscribe.jobs import DONE, QUEUED


def _job(store, key: str) -> None:
    store.create(
        job_key=key,
        call_id=key.split("_")[0],
        conference_id=None,
        audio_path=Path(f"/out/{key}/recording.wav"),
        out_dir=Path(f"/out/{key}"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )


def _finished(store, latencies: tuple[float, ...]) -> None:
    for index, latency in enumerate(latencies):
        key = f"k{index}_1"
        _job(store, key)
        store.record_output(key, latency)
        store.set_status(key, DONE)


def test_percentile_is_nearest_rank() -> None:
    assert percentile([5.0], 95) == 5.0
    assert [percentile([900.0, 60.0, 120.0], p) for p in (50, 95)] == [120.0, 900.0]
    assert percentile([float(n) for n in range(1, 101)], 95) == 95.0


def test_latencies_are_read_back_for_a_time_window(store) -> None:
    _job(store, "a_1")
    _job(store, "b_2")

    store.record_output("a_1", 300.0)

    assert store.latencies_since(datetime.now(UTC) - timedelta(hours=24)) == [300.0]
    assert store.latencies_since(datetime.now(UTC) + timedelta(minutes=1)) == []
    job = store.get("a_1")
    assert job.latency_sec == 300.0
    assert job.output_at is not None


def test_latency_summary(store) -> None:
    assert latency_summary(store, datetime.now(UTC)) == "latency 24h (hang-up to output): no calls"

    _finished(store, (60.0, 120.0, 900.0))

    assert latency_summary(store, datetime.now(UTC)).endswith("3 call(s), p50 2.0 min, p95 15.0 min")


def test_status_counts_and_oldest_pending(store) -> None:
    _job(store, "a_1")
    _job(store, "b_2")
    store.set_status("b_2", DONE)

    assert store.status_counts() == {QUEUED: 1, DONE: 1}
    assert store.oldest_pending_created_at() == store.get("a_1").created_at

    store.set_status("a_1", DONE)
    assert store.oldest_pending_created_at() is None


def test_heartbeat_round_trip(store, tmp_path: Path) -> None:
    _job(store, "a_1")
    path = heartbeat_path(tmp_path / "js.db")

    write_heartbeat(path, store, datetime.now(UTC), polls=7, last_poll_ok=False)

    beat = read_heartbeat(path)
    assert path == tmp_path / HEARTBEAT_FILE
    assert (beat["polls"], beat["last_poll_ok"], beat["counts"]) == (7, False, {QUEUED: 1})
    assert beat["oldest_pending_age_sec"] >= 0
    assert list(tmp_path.glob("*.part")) == []


def test_an_unreadable_heartbeat_reads_as_none(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")

    assert read_heartbeat(tmp_path / "missing.json") is None
    assert read_heartbeat(bad) is None


def test_run_once_writes_a_heartbeat(cfg_file, tmp_path: Path) -> None:
    assert main(["--config", str(cfg_file), "run", "--once"]) == 0

    beat = json.loads((tmp_path / HEARTBEAT_FILE).read_text(encoding="utf-8"))
    assert (beat["polls"], beat["last_poll_ok"], beat["counts"], beat["oldest_pending_age_sec"]) == (1, True, {}, None)


def test_status_shows_latency_and_heartbeat(cfg_file, store, capsys) -> None:
    _finished(store, (60.0, 120.0, 900.0))

    assert main(["--config", str(cfg_file), "status"]) == 0
    out = capsys.readouterr().out
    assert "3 call(s), p50 2.0 min, p95 15.0 min" in out
    assert "heartbeat: none" in out

    main(["--config", str(cfg_file), "run", "--once"])
    capsys.readouterr()
    main(["--config", str(cfg_file), "status"])
    assert "last poll ok" in capsys.readouterr().out
```

Append to the end of `tests/test_pipeline.py`:

```python
def test_output_records_the_latency_for_status(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    job = store.get(key)
    assert job.latency_sec == _result(job)["timings"]["hangup_to_output_sec"]
    assert job.output_at is not None
```

Append to the end of `tests/test_jobs_migration.py`:

```python
def test_upgrade_adds_the_latency_columns(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    _v2_database(db)

    store = JobStore(db)
    store.init_schema()

    job = store.get("old_1042")
    assert (job.output_at, job.latency_sec) == (None, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_health.py tests/test_pipeline.py tests/test_jobs_migration.py -q`
Expected: FAIL. `test_health.py` fails at collection with `ModuleNotFoundError: No module named 'jabberscribe.health'`. `test_output_records_the_latency_for_status` fails with `AttributeError: 'Job' object has no attribute 'latency_sec'` and `test_upgrade_adds_the_latency_columns` with `AttributeError: 'Job' object has no attribute 'output_at'` (2 failed, 31 passed outside `test_health.py`).

- [ ] **Step 3: Add the latency columns and queries to `jabberscribe/jobs.py`**

In `jabberscribe/jobs.py`, replace:

```python
SCHEMA_VERSION = 3
```

with:

```python
SCHEMA_VERSION = 4
```

and replace:

```python
        ("hold_reason", "TEXT"),
    ),
}
```

with:

```python
        ("hold_reason", "TEXT"),
    ),
    3: (
        ("output_at", "TEXT"),
        ("latency_sec", "REAL"),
    ),
}
```

and replace:

```python
  hold_reason     TEXT,
  created_at      TEXT NOT NULL,
```

with:

```python
  hold_reason     TEXT,
  output_at       TEXT,
  latency_sec     REAL,
  created_at      TEXT NOT NULL,
```

and replace:

```python
    legal_hold: bool = False
    hold_reason: str | None = None
```

with:

```python
    legal_hold: bool = False
    hold_reason: str | None = None
    #: When the outputs were last written, and how long after hang-up (for `status`).
    output_at: str | None = None
    latency_sec: float | None = None
```

and replace:

```python
        hold_reason=row["hold_reason"],
    )
```

with:

```python
        hold_reason=row["hold_reason"],
        output_at=row["output_at"],
        latency_sec=row["latency_sec"],
    )
```

and replace:

```python
    def list_all(self) -> list[Job]:
```

with:

```python
    def record_output(self, job_key: str, latency_sec: float) -> None:
        """Remember that the outputs were written now, `latency_sec` after the call ended."""
        now = utcnow()
        self._conn.execute(
            "UPDATE jobs SET output_at = ?, latency_sec = ?, updated_at = ? WHERE job_key = ?",
            (now, latency_sec, now, job_key),
        )

    def latencies_since(self, since: datetime) -> list[float]:
        """Hang-up-to-output latencies of the outputs written at or after `since`, ascending."""
        rows = self._conn.execute(
            "SELECT latency_sec FROM jobs WHERE output_at >= ? AND latency_sec IS NOT NULL ORDER BY latency_sec",
            (iso(since),),
        ).fetchall()
        return [float(r["latency_sec"]) for r in rows]

    def status_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def oldest_pending_created_at(self) -> str | None:
        """Arrival time of the oldest job still in the pipeline (ACTIVE: QUEUED, RUNNING or WAITING)."""
        row = self._conn.execute(
            "SELECT MIN(created_at) AS oldest FROM jobs WHERE status IN (?, ?, ?)", ACTIVE
        ).fetchone()
        return row["oldest"]

    def list_all(self) -> list[Job]:
```

- [ ] **Step 4: Create `jabberscribe/health.py`**

```python
"""Liveness and latency for operators.

`run` rewrites heartbeat.json next to the database after every poll, so an
external monitor (a scheduled task, a file-age sensor) can alarm when it goes
stale -- a hung service writes nothing, so it cannot report itself. `status`
reports the hang-up-to-output latency percentiles that the spec's 15-minute
budget is measured against.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from jabberscribe.jobs import JobStore, iso
from jabberscribe.output import write_atomic

HEARTBEAT_FILE = "heartbeat.json"
LATENCY_WINDOW = timedelta(hours=24)


def heartbeat_path(db_path: Path) -> Path:
    return db_path.parent / HEARTBEAT_FILE


def pending_age_sec(store: JobStore, now: datetime) -> float | None:
    """Seconds since the oldest unfinished job arrived, or None when nothing is pending."""
    oldest = store.oldest_pending_created_at()
    if oldest is None:
        return None
    return round((now - datetime.fromisoformat(oldest)).total_seconds(), 1)


def write_heartbeat(path: Path, store: JobStore, now: datetime, *, polls: int, last_poll_ok: bool) -> None:
    beat = {
        "last_poll_at": iso(now),
        "pid": os.getpid(),
        "polls": polls,
        "last_poll_ok": last_poll_ok,
        "counts": store.status_counts(),
        "oldest_pending_age_sec": pending_age_sec(store, now),
    }
    write_atomic(path, json.dumps(beat, indent=2))


def read_heartbeat(path: Path) -> dict | None:
    """The last heartbeat, or None when there is none or it cannot be read."""
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        datetime.fromisoformat(beat["last_poll_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return beat if isinstance(beat, dict) else None


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty list: an observed value, never an interpolation."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def latency_summary(store: JobStore, now: datetime) -> str:
    latencies = store.latencies_since(now - LATENCY_WINDOW)
    if not latencies:
        return "latency 24h (hang-up to output): no calls"
    p50, p95 = percentile(latencies, 50) / 60, percentile(latencies, 95) / 60
    return f"latency 24h (hang-up to output): {len(latencies)} call(s), p50 {p50:.1f} min, p95 {p95:.1f} min"
```

- [ ] **Step 5: Record the latency at the output stage**

In the base, `write_outputs` sits in a `try` that turns an `OSError` into a `TransientError`. The latency is recorded after that `try`, only once every file is written. Like `complete_stage`, the write is unguarded.

In `jabberscribe/pipeline.py`, replace:

```python
                    raise TransientError(str(exc)) from exc
```

with:

```python
                    raise TransientError(str(exc)) from exc
                store.record_output(job.job_key, timings["hangup_to_output_sec"])
```

- [ ] **Step 6: Write the heartbeat in `run` and report in `status`**

In `jabberscribe/cli.py`, replace:

```python
from jabberscribe.group import requeue_failed, settle
```

with:

```python
from jabberscribe.group import requeue_failed, settle
from jabberscribe.health import heartbeat_path, latency_summary, read_heartbeat, write_heartbeat
```

and replace:

```python
    last_purge: date | None = None
    while True:
```

with:

```python
    last_purge: date | None = None
    polls = 0
    while True:
```

and replace:

```python
                log.exception("purge failed; next attempt tomorrow")
                ok = False
        if once:
            return 0 if ok else 1
        time.sleep(cfg.watcher.poll_seconds)
```

with:

```python
                log.exception("purge failed; next attempt tomorrow")
                ok = False
        polls += 1
        _beat(cfg, store, polls, ok)
        if once:
            return 0 if ok else 1
        time.sleep(cfg.watcher.poll_seconds)


def _beat(cfg: Config, store: JobStore, polls: int, ok: bool) -> None:
    try:
        write_heartbeat(heartbeat_path(cfg.paths.db_path), store, _utcnow(), polls=polls, last_poll_ok=ok)
    except Exception:
        # A monitor will notice the stale heartbeat; the service itself carries on.
        log.exception("cannot write the heartbeat")
```

and replace:

```python
def _status(store: JobStore) -> int:
    """Counts by status, the oldest waiting and queued job, and every retrying or failed job.
```

with:

```python
def _heartbeat_line(cfg: Config, now: datetime) -> str:
    beat = read_heartbeat(heartbeat_path(cfg.paths.db_path))
    if beat is None:
        return "heartbeat: none (is `jabberscribe run` running?)"
    age = (now - datetime.fromisoformat(beat["last_poll_at"])).total_seconds()
    state = "ok" if beat.get("last_poll_ok") else "FAILED"
    return f"heartbeat: {age:.0f} s ago, poll {beat.get('polls')}, last poll {state}"


def _status(cfg: Config, store: JobStore) -> int:
    """Counts by status, the oldest waiting and queued job, latency, heartbeat, and every retrying or failed job.
```

and replace:

```python
            print(f"oldest {status}: {oldest.job_key} for {minutes:.0f} min")
```

with:

```python
            print(f"oldest {status}: {oldest.job_key} for {minutes:.0f} min")
    print(latency_summary(store, now))
    print(_heartbeat_line(cfg, now))
```

and replace:

```python
        return _status(store)
```

with:

```python
        return _status(cfg, store)
```

- [ ] **Step 7: Run the new tests**

Run: `python -m pytest tests/test_health.py tests/test_pipeline.py tests/test_jobs_migration.py -q`
Expected: PASS (41 passed).

- [ ] **Step 8: Run the full suite and lint**

Run: `python -m pytest -q`
Expected: `336 passed, 3 skipped`. The base `test_status_*` tests still pass: the latency and heartbeat lines are added after the `oldest ...` lines and do not change the exit code.

Run: `python -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add jabberscribe/health.py jabberscribe/jobs.py jabberscribe/pipeline.py jabberscribe/cli.py tests/test_health.py tests/test_pipeline.py tests/test_jobs_migration.py
git commit -m "feat(health): heartbeat file and hang-up-to-output latency percentiles in status" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task E3: Near/far speaker labels for dual-track calls

Spec §2.2 extra 4 (near/far half); audit §4b. Each channel of a dual-track call is transcribed separately (two STT calls) and labelled from the sidecar; the segments are merged by start time. Mixed-track calls are unchanged. Conference diarization stays out of scope (design notes).

**Files:**
- Create: `jabberscribe/speakers.py`
- Modify (full rewrite): `jabberscribe/audio.py`
- Modify: `jabberscribe/config.py`, `jabberscribe/stt.py`, `jabberscribe/summarize.py`, `jabberscribe/output.py`, `jabberscribe/pipeline.py`, `jabberscribe/retention.py`
- Modify tests: `tests/test_pipeline.py` (`_enqueue` default route; append), `tests/test_output.py` (one assertion; append), `tests/test_cli.py` (one assertion), `tests/test_summarize.py` (append), `tests/test_config.py` (append)
- Test (create): `tests/test_speakers.py`

**Interfaces:**
- Consumes (base e49c0cb unless noted): `Sidecar`, `Party` (`line_owner`, `parties`, `kind`, `conference_id`, `tracks`); `Segment`, `Transcriber`, `format_ts`; `_run_ffmpeg`, `prepare_for_stt`, `STT_FILENAME`, `AudioError` (`audio.py`, whose full rewrite below keeps them); `transcript_text`, `INSTRUCTIONS`; `_render_transcript`; `process_job` stages and `_delete_stt_audio` (E2); the retention STT sweep, which skips `ACTIVE` jobs' work folders.
- Produces:
  - `SttConfig.split_channels: bool = True`, `SttConfig.near_channel: Literal[0, 1] = 0`.
  - `stt.Segment` gains a trailing field `speaker: str | None = None`; `stt.segment_line(segment: Segment) -> str` → `[HH:MM:SS] text` or `[HH:MM:SS] <speaker>: text`.
  - `audio`: `STT_GLOB = "stt*.ogg*"` (every STT copy, `.part` included); `channel_filename(channel: int) -> str` (`stt-ch<N>.ogg`); `prepare_channel_for_stt(src: Path, work_dir: Path, channel: int, ffmpeg: str = "ffmpeg") -> Path`; `channel_peak_db(src: Path, channel: int, ffmpeg: str = "ffmpeg") -> float`; `channel_count(src: Path, ffprobe: str = "ffprobe") -> int`. `prepare_for_stt`, `STT_FILENAME`, `AudioError`, `TARGET_RATE` unchanged.
  - `speakers`: `NEAR_FALLBACK = "צד א"`, `FAR_FALLBACK = "צד ב"`, `CONFERENCE_FAR = "משתתפים"`, `SILENT_CHANNEL_DB = -50.0`; `SttInput = tuple[Path, str | None]`; `speaker_labels(sidecar: Sidecar) -> tuple[str, str]` (near, far); `stt_inputs(audio: Path, work: Path, sidecar: Sidecar, cfg: SttConfig) -> list[SttInput]`; `transcribe_inputs(transcriber: Transcriber, inputs: list[SttInput]) -> list[Segment]`.
  - `transcript.md` lines and the summary transcript text use `segment_line`; `result.json` `transcript[]` items carry `speaker` (null for mixed). `INSTRUCTIONS` gains two speaker rules: a first-person commitment states the owner only on a line labelled with a real name; the generic labels `צד א`, `צד ב`, `משתתפים` never state one (default 4). The pipeline's `audio` and `stt` stages call `stt_inputs`; the STT copies (`STT_GLOB`) are deleted after transcription and swept by retention.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_speakers.py`:

```python
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest

from jabberscribe.audio import (
    STT_FILENAME,
    STT_GLOB,
    channel_count,
    channel_filename,
    channel_peak_db,
    prepare_channel_for_stt,
)
from jabberscribe.config import SttConfig
from jabberscribe.sidecar import parse_sidecar
from jabberscribe.speakers import (
    CONFERENCE_FAR,
    FAR_FALLBACK,
    NEAR_FALLBACK,
    speaker_labels,
    stt_inputs,
    transcribe_inputs,
)
from jabberscribe.stt import Segment

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

STT = SttConfig(model="whisper-he")


def _sidecar(tmp_path: Path, make_sidecar, **fields):
    return parse_sidecar(make_sidecar(tmp_path / "s.json", **fields).read_text(encoding="utf-8"))


def _wav_with_silent_far_end(path: Path, seconds: float = 1.0, rate: int = 8000) -> Path:
    """Channel 0 carries a tone, channel 1 digital silence: a far end that never spoke."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    for i in range(int(seconds * rate)):
        frames += struct.pack("<hh", int(12000 * math.sin(2 * math.pi * 440 * i / rate)), 0)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    return path


class ByFile:
    """Fake transcriber: answers each file name with canned segments."""

    def __init__(self, answers: dict[str, list[Segment]]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def transcribe(self, audio: Path) -> list[Segment]:
        self.calls.append(audio.name)
        return self.answers[audio.name]


def test_one_to_one_call_uses_both_display_names(tmp_path, make_sidecar) -> None:
    assert speaker_labels(_sidecar(tmp_path, make_sidecar)) == ("מאיר", "דנה")


def test_missing_names_fall_back_to_generic_labels(tmp_path, make_sidecar) -> None:
    sidecar = _sidecar(tmp_path, make_sidecar, line_owner={"extension": "1042"}, parties=[{"extension": "2210"}])

    assert speaker_labels(sidecar) == (NEAR_FALLBACK, FAR_FALLBACK)


def test_a_call_with_several_parties_gets_a_generic_far_label(tmp_path, make_sidecar) -> None:
    parties = [{"extension": "2210", "display_name": "דנה"}, {"extension": "3000", "display_name": "יוסי"}]

    assert speaker_labels(_sidecar(tmp_path, make_sidecar, parties=parties)) == ("מאיר", FAR_FALLBACK)


def test_a_conference_far_end_is_the_participants(tmp_path, make_sidecar) -> None:
    sidecar = _sidecar(tmp_path, make_sidecar, conference_id="conf-1")

    assert speaker_labels(sidecar) == ("מאיר", CONFERENCE_FAR)


def test_channels_are_labelled_and_merged_by_start_time() -> None:
    transcriber = ByFile(
        {
            "near.ogg": [Segment(0.0, 2.0, "שלום"), Segment(5.0, 6.0, "כן")],
            "far.ogg": [Segment(2.5, 4.0, "היי, מה נשמע")],
        }
    )

    merged = transcribe_inputs(transcriber, [(Path("near.ogg"), "מאיר"), (Path("far.ogg"), "דנה")])

    assert merged == [
        Segment(0.0, 2.0, "שלום", "מאיר"),
        Segment(2.5, 4.0, "היי, מה נשמע", "דנה"),
        Segment(5.0, 6.0, "כן", "מאיר"),
    ]


def test_downmix_segments_stay_unlabelled() -> None:
    transcriber = ByFile({STT_FILENAME: [Segment(0.0, 1.0, "שלום")]})

    assert transcribe_inputs(transcriber, [(Path(STT_FILENAME), None)]) == [Segment(0.0, 1.0, "שלום")]


@needs_ffmpeg
def test_mixed_track_call_is_one_downmix(tmp_path, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)

    inputs = stt_inputs(audio, tmp_path / "work", _sidecar(tmp_path, make_sidecar, tracks="mixed"), STT)

    assert inputs == [(tmp_path / "work" / STT_FILENAME, None)]


@needs_ffmpeg
def test_dual_track_call_is_one_file_per_channel(tmp_path, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)
    work = tmp_path / "work"

    inputs = stt_inputs(audio, work, _sidecar(tmp_path, make_sidecar, tracks="dual"), STT)

    assert inputs == [(work / channel_filename(0), "מאיר"), (work / channel_filename(1), "דנה")]
    assert all(path.stat().st_size > 0 for path, _ in inputs)
    assert sorted(p.name for p in work.glob(STT_GLOB)) == ["stt-ch0.ogg", "stt-ch1.ogg"]


@needs_ffmpeg
def test_near_channel_setting_swaps_the_labels(tmp_path, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)
    work = tmp_path / "work"
    cfg = SttConfig(model="whisper-he", near_channel=1)

    inputs = stt_inputs(audio, work, _sidecar(tmp_path, make_sidecar, tracks="dual"), cfg)

    assert inputs == [(work / channel_filename(1), "מאיר"), (work / channel_filename(0), "דנה")]


@needs_ffmpeg
def test_split_can_be_switched_off(tmp_path, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)
    cfg = SttConfig(model="whisper-he", split_channels=False)

    inputs = stt_inputs(audio, tmp_path / "work", _sidecar(tmp_path, make_sidecar, tracks="dual"), cfg)

    assert inputs == [(tmp_path / "work" / STT_FILENAME, None)]


@needs_ffmpeg
def test_a_silent_channel_is_not_transcribed(tmp_path, make_sidecar) -> None:
    audio = _wav_with_silent_far_end(tmp_path / "call.wav")
    work = tmp_path / "work"

    inputs = stt_inputs(audio, work, _sidecar(tmp_path, make_sidecar, tracks="dual"), STT)

    assert inputs == [(work / channel_filename(0), "מאיר")]
    assert not (work / channel_filename(1)).exists()


@needs_ffmpeg
def test_channel_peak_tells_a_tone_from_silence(tmp_path) -> None:
    audio = _wav_with_silent_far_end(tmp_path / "call.wav")

    assert channel_peak_db(audio, 0) > -20.0
    assert channel_peak_db(audio, 1) < -80.0


@needs_ffmpeg
def test_channel_count(tmp_path, make_wav) -> None:
    assert channel_count(make_wav(tmp_path / "stereo.wav", channels=2)) == 2
    assert channel_count(make_wav(tmp_path / "mono.wav", channels=1)) == 1


@needs_ffmpeg
def test_channel_encode_is_reused(tmp_path, make_wav) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=2)
    first = prepare_channel_for_stt(audio, tmp_path / "work", 1)
    stamp = first.stat().st_mtime_ns

    assert prepare_channel_for_stt(audio, tmp_path / "work", 1).stat().st_mtime_ns == stamp
    assert list((tmp_path / "work").glob("*.part")) == []


@needs_ffmpeg
def test_a_mono_file_labelled_dual_falls_back_to_the_downmix(tmp_path, make_wav, make_sidecar, caplog) -> None:
    audio = make_wav(tmp_path / "call.wav", channels=1)

    inputs = stt_inputs(audio, tmp_path / "work", _sidecar(tmp_path, make_sidecar, tracks="dual"), STT)

    assert inputs == [(tmp_path / "work" / STT_FILENAME, None)]
    assert "downmix" in caplog.text
```

In `tests/test_pipeline.py`, replace:

```python
def _enqueue(cfg, store, audit, make_wav, make_sidecar, *, call_id="abc", extension="1042", **extra) -> str:
    make_wav(cfg.paths.inbox / f"{call_id}{extension}.wav", channels=2)
    make_sidecar(
        cfg.paths.inbox / f"{call_id}{extension}.json", call_id=call_id, extension=extension, tracks="dual", **extra
    )
```

with:

```python
def _enqueue(
    cfg, store, audit, make_wav, make_sidecar, *, call_id="abc", extension="1042", tracks="mixed", **extra
) -> str:
    # Two-channel audio either way. "mixed" keeps the one-downmix route these tests were written for;
    # the dual-track speaker route has its own tests below.
    make_wav(cfg.paths.inbox / f"{call_id}{extension}.wav", channels=2)
    make_sidecar(
        cfg.paths.inbox / f"{call_id}{extension}.json", call_id=call_id, extension=extension, tracks=tracks, **extra
    )
```

Append to the end of `tests/test_pipeline.py`:

```python
class ChannelTranscriber(FakeTranscriber):
    """Answers each channel file with one line named after it, the far end one second later."""

    def transcribe(self, audio: Path) -> list[Segment]:
        self.calls.append(audio)
        start = 0.0 if audio.name == "stt-ch0.ogg" else 1.0
        return [Segment(start, start + 0.5, audio.stem)]


def test_dual_track_call_is_labelled_near_and_far(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar, tracks="dual")
    transcriber = ChannelTranscriber()
    summarizer = FakeSummarizer()

    run_once(cfg, store, transcriber, summarizer)

    job = store.get(key)
    assert [p.name for p in transcriber.calls] == ["stt-ch0.ogg", "stt-ch1.ogg"]
    transcript = _result(job)["transcript"]
    assert [(s["speaker"], s["text"]) for s in transcript] == [("מאיר", "stt-ch0"), ("דנה", "stt-ch1")]
    assert "[00:00:00] מאיר: stt-ch0" in (job.out_dir / TRANSCRIPT_FILE).read_text(encoding="utf-8")
    assert [s.speaker for s in summarizer.seen] == ["מאיר", "דנה"]
    assert list((cfg.paths.work_dir / key).glob("stt*.ogg*")) == []


def test_mixed_track_call_has_no_speakers(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    assert [s["speaker"] for s in _result(store.get(key))["transcript"]] == [None, None]
```

In `tests/test_output.py`, replace:

```python
    assert result["transcript"][1] == {"start": 61.0, "end": 62.5, "text": "נדבר מחר"}
```

with:

```python
    assert result["transcript"][1] == {"start": 61.0, "end": 62.5, "text": "נדבר מחר", "speaker": None}
```

Append to the end of `tests/test_output.py`:

```python
def test_labelled_segments_name_their_speaker(tmp_path: Path, make_sidecar) -> None:
    sidecar = parse_sidecar(make_sidecar(tmp_path / "s.json", call_id="gc1").read_text(encoding="utf-8"))
    out = tmp_path / "out"

    write_outputs(
        out,
        sidecar=sidecar,
        segments=[Segment(0.0, 1.0, "שלום", "מאיר"), Segment(2.0, 3.0, "היי", "דנה")],
        summary=SUMMARY,
        owners=[sidecar.line_owner],
        models=MODELS,
        recording=out / "recording.wav",
    )

    text = _read(out / TRANSCRIPT_FILE)
    assert "[00:00:00] מאיר: שלום" in text
    assert "[00:00:02] דנה: היי" in text
    assert json.loads(_read(out / RESULT_FILE))["transcript"][0]["speaker"] == "מאיר"
```

In `tests/test_cli.py`, replace:

```python
    assert fake_litellm.transcriptions == 1
    assert len(JobStore(tmp_path / "js.db").list_all()) == 1
```

with:

```python
    assert fake_litellm.transcriptions == 2  # one per channel of the dual-track call, and only once
    assert len(JobStore(tmp_path / "js.db").list_all()) == 1
```

Append to the end of `tests/test_summarize.py`:

```python
def test_transcript_text_names_known_speakers() -> None:
    segments = [Segment(0.0, 1.0, "שלום", "מאיר"), Segment(2.0, 3.0, "היי")]

    assert transcript_text(segments) == "[00:00:00] מאיר: שלום\n[00:00:02] היי"


def test_prompt_explains_speaker_names() -> None:
    server = Server(GOOD_JSON)

    _summarizer(server).summarize([Segment(0.0, 1.0, "אני אשלח את הדוח", "דנה")])

    content = server.requests[0]["messages"][0]["content"]
    assert "[00:00:00] דנה: אני אשלח את הדוח" in content
    assert "first person" in content


def test_generic_speaker_labels_never_state_an_owner() -> None:
    """Owner only when stated: "I will" on a fallback-labelled line names nobody."""
    from jabberscribe.speakers import CONFERENCE_FAR, FAR_FALLBACK, NEAR_FALLBACK
    from jabberscribe.summarize import INSTRUCTIONS

    generic_rule = next(line for line in INSTRUCTIONS.splitlines() if "generic labels" in line)
    for label in (NEAR_FALLBACK, FAR_FALLBACK, CONFERENCE_FAR):
        assert label in generic_rule
    assert "real name" in INSTRUCTIONS
```

Append to the end of `tests/test_config.py`:

```python
def test_speaker_split_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, BASE))

    assert (cfg.stt.split_channels, cfg.stt.near_channel) == (True, 0)


def test_near_channel_must_be_zero_or_one(tmp_path: Path) -> None:
    data = copy.deepcopy(BASE)
    data["stt"]["near_channel"] = 2

    with pytest.raises(ConfigError, match="near_channel"):
        load_config(_write(tmp_path, data))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_speakers.py tests/test_pipeline.py tests/test_output.py tests/test_summarize.py tests/test_config.py tests/test_cli.py -q`
Expected: FAIL. `test_speakers.py` fails at collection with `ImportError: cannot import name 'STT_GLOB' from 'jabberscribe.audio'`. The new output, summarize and pipeline tests fail with `TypeError: Segment.__init__() takes 4 positional arguments but 5 were given`, or with `KeyError: 'speaker'`. `test_speaker_split_defaults` fails with `AttributeError`. `test_reprocessing_the_same_call_is_a_no_op` fails with `assert 1 == 2`, `test_generic_speaker_labels_never_state_an_owner` with `ModuleNotFoundError: No module named 'jabberscribe.speakers'`, and the base `test_result_json_contents` on the new `speaker` key (outside `test_speakers.py`: 9 failed, 119 passed). `test_near_channel_must_be_zero_or_one` already passes, because the strict config refuses unknown keys; it guards the `Literal[0, 1]` bound.

- [ ] **Step 3: Add the speaker settings to the config**

In `jabberscribe/config.py`, replace:

```python
from pathlib import Path
```

with:

```python
from pathlib import Path
from typing import Literal
```

and replace:

```python
    #: A relative path is relative to the config file, not the working directory.
    vocabulary_file: Path | None = None
```

with:

```python
    #: A relative path is relative to the config file, not the working directory.
    vocabulary_file: Path | None = None
    #: Dual-track calls: transcribe each channel on its own and label who spoke (two STT calls per call).
    split_channels: bool = True
    #: The channel of a dual-track recording that carries the recorded line (the near end).
    near_channel: Literal[0, 1] = 0
```

- [ ] **Step 4: Give segments a speaker**

In `jabberscribe/stt.py`, replace:

```python
@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
```

with:

```python
@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    #: Who spoke, when the channel tells us (dual-track calls); None for a downmix.
    speaker: str | None = None
```

and replace:

```python
def is_hallucination(raw: dict, text: str, prompt: str) -> bool:
```

with:

```python
def segment_line(segment: Segment) -> str:
    """One transcript line: `[HH:MM:SS] text`, or `[HH:MM:SS] speaker: text` when the speaker is known."""
    words = f"{segment.speaker}: {segment.text}" if segment.speaker else segment.text
    return f"[{format_ts(segment.start)}] {words}"


def is_hallucination(raw: dict, text: str, prompt: str) -> bool:
```

- [ ] **Step 5: Rewrite `jabberscribe/audio.py`**

```python
"""Audio preprocessing via ffmpeg.

The STT endpoint gets compact mono files: resampled to 16 kHz,
loudness-normalized, and encoded as Opus so an hour-long call stays around
15 MB -- well under typical transcription upload limits.

A call is sent either as one downmix of all channels (stt.ogg) or, for a
dual-track call with speaker labels on, as one file per channel
(stt-ch0.ogg, stt-ch1.ogg) so each end is transcribed on its own (speakers.py).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

STT_FILENAME = "stt.ogg"
#: Every STT copy a job can leave in its work folder: the downmix, the channel files, and .part leftovers.
STT_GLOB = "stt*.ogg*"
TARGET_RATE = 16000
_LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
_TIMEOUT_SECONDS = 1800
_MAX_VOLUME = re.compile(r"max_volume: (-?[\d.]+|-inf) dB")


class AudioError(RuntimeError):
    """Audio could not be prepared."""


def channel_filename(channel: int) -> str:
    return f"stt-ch{channel}.ogg"


def _run_ffmpeg(args: list[str]) -> str:
    """Run ffmpeg; return its stderr, where it also reports measurements."""
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
    return proc.stderr or ""


def _encode(src: Path, dest: Path, audio_filter: str, ffmpeg: str) -> Path:
    """Encode `src` through `audio_filter` as mono 16 kHz Opus at `dest`.

    Idempotent: an existing non-empty output is reused, which is what makes the
    stage safe to retry after a crash.
    """
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
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
                "-af", audio_filter, "-ac", "1", "-ar", str(TARGET_RATE),
                "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(partial),
            ]
        )
    except AudioError:
        # A half-written Opus file is still the caller's voice; it must not linger.
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)
    return dest


def prepare_for_stt(src: Path, work_dir: Path, ffmpeg: str = "ffmpeg") -> Path:
    """Encode all channels of `src`, downmixed, as work_dir/stt.ogg."""
    return _encode(src, work_dir / STT_FILENAME, _LOUDNORM, ffmpeg)


def prepare_channel_for_stt(src: Path, work_dir: Path, channel: int, ffmpeg: str = "ffmpeg") -> Path:
    """Encode one channel of `src` as work_dir/stt-ch<channel>.ogg."""
    return _encode(src, work_dir / channel_filename(channel), f"pan=mono|c0=c{channel},{_LOUDNORM}", ffmpeg)


def channel_count(src: Path, ffprobe: str = "ffprobe") -> int:
    """Number of channels in the first audio stream of `src`."""
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    args = [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels", "-of", "csv=p=0"]
    try:
        proc = subprocess.run(
            [*args, str(src)], capture_output=True, encoding="utf-8", errors="replace", timeout=60, check=False
        )
    except FileNotFoundError as exc:
        raise AudioError(f"ffprobe not found: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffprobe timed out") from exc
    found = (proc.stdout or "").strip()
    if proc.returncode != 0 or not found.isdigit():
        raise AudioError(f"ffprobe cannot read the channels of {src}: {(proc.stderr or '').strip()[:200]}")
    return int(found)


def channel_peak_db(src: Path, channel: int, ffmpeg: str = "ffmpeg") -> float:
    """Loudest sample of one channel in dBFS, before any normalization. Digital silence is about -91."""
    if not src.is_file():
        raise AudioError(f"source audio not found: {src}")
    stderr = _run_ffmpeg(
        [ffmpeg, "-nostdin", "-i", str(src), "-af", f"pan=mono|c0=c{channel},volumedetect", "-f", "null", "-"]
    )
    match = _MAX_VOLUME.search(stderr)
    if match is None:
        raise AudioError(f"ffmpeg reported no level for channel {channel} of {src}")
    return float(match.group(1))
```

- [ ] **Step 6: Create `jabberscribe/speakers.py`**

```python
"""Near/far-end speaker labels for dual-track calls.

The recorder captures each end of a call on its own channel, so who said what
is known without diarization: each channel is transcribed on its own, every
segment is labelled with that channel's speaker, and the two transcripts are
merged by start time. Labels come from the sidecar -- the recorded line's
display name for the near end, the other party's for the far end of a 1:1
call, generic labels when a name is missing. A conference's far channel is the
bridge mix of everyone else, so it is labelled as a group; telling those
voices apart would need diarization.

A channel without signal is not sent to Whisper at all: on silence it invents
words, and the STT hallucination filters catch most but not all of them.
Labels are personal data, so they are never logged.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from jabberscribe.audio import AudioError, channel_count, channel_peak_db, prepare_channel_for_stt, prepare_for_stt
from jabberscribe.config import SttConfig
from jabberscribe.sidecar import Sidecar
from jabberscribe.stt import Segment, Transcriber

log = logging.getLogger(__name__)

NEAR_FALLBACK = "צד א"
FAR_FALLBACK = "צד ב"
CONFERENCE_FAR = "משתתפים"
#: A channel whose loudest sample stays below this carries no speech: a dead or muted leg.
SILENT_CHANNEL_DB = -50.0

#: One file to transcribe and the speaker its segments are labelled with (None: an unlabelled downmix).
SttInput = tuple[Path, str | None]


def _name(value: str | None, fallback: str) -> str:
    return (value or "").strip() or fallback


def speaker_labels(sidecar: Sidecar) -> tuple[str, str]:
    """(near, far) labels for a dual-track recording."""
    near = _name(sidecar.line_owner.display_name, NEAR_FALLBACK)
    if sidecar.kind == "conference" or sidecar.conference_id is not None:
        return near, CONFERENCE_FAR
    if len(sidecar.parties) == 1:
        return near, _name(sidecar.parties[0].display_name, FAR_FALLBACK)
    return near, FAR_FALLBACK


def stt_inputs(audio: Path, work: Path, sidecar: Sidecar, cfg: SttConfig) -> list[SttInput]:
    """Prepare the files the stt stage sends to Whisper. Idempotent; the audio stage calls it first.

    A dual-track call that cannot be split (a mono file behind a "dual"
    sidecar) falls back to the downmix: wrong metadata must not cost the
    transcript, nor put every word in the near end's mouth. Truly broken audio
    fails in the downmix as well, as before.
    """
    if not (cfg.split_channels and sidecar.tracks == "dual"):
        return [(prepare_for_stt(audio, work), None)]
    near, far = speaker_labels(sidecar)
    inputs: list[SttInput] = []
    try:
        channels = channel_count(audio)
        if channels != 2:
            raise AudioError(f"expected 2 channels, found {channels}")
        for channel, label in ((cfg.near_channel, near), (1 - cfg.near_channel, far)):
            peak = channel_peak_db(audio, channel)
            if peak < SILENT_CHANNEL_DB:
                log.info("channel %d of %s is silent (%.1f dB); not transcribed", channel, audio, peak)
                continue
            inputs.append((prepare_channel_for_stt(audio, work, channel), label))
    except AudioError as exc:
        log.warning("cannot split %s into channels (%s); transcribing the downmix instead", audio, exc)
        return [(prepare_for_stt(audio, work), None)]
    return inputs


def transcribe_inputs(transcriber: Transcriber, inputs: list[SttInput]) -> list[Segment]:
    """Transcribe every input, label its segments, and merge them by start time (near end first on a tie)."""
    segments: list[Segment] = []
    for path, label in inputs:
        found = transcriber.transcribe(path)
        segments += found if label is None else [replace(s, speaker=label) for s in found]
    return sorted(segments, key=lambda s: (s.start, s.end))
```

- [ ] **Step 7: Use speaker lines in the summary and the transcript file**

In `jabberscribe/summarize.py`, replace:

```python
from jabberscribe.stt import Segment, format_ts
```

with:

```python
from jabberscribe.stt import Segment, segment_line
```

and replace:

```python
- source_ts: the timestamp of the transcript line the item comes from, copied exactly.
```

with:

```python
- source_ts: the timestamp of the transcript line the item comes from, copied exactly.
- A line may name its speaker before a colon. When a speaker named by a real name commits to a task in the
  first person ("אני אשלח", "I will send it"), that name is the stated owner.
- צד א, צד ב and משתתפים are generic labels, not names: a first-person commitment on such a line does not
  state an owner. Leave owner null unless a name is said in the call.
```

and replace:

```python
    return "\n".join(f"[{format_ts(s.start)}] {s.text}" for s in segments)
```

with:

```python
    return "\n".join(segment_line(s) for s in segments)
```

In `jabberscribe/output.py`, replace:

```python
from jabberscribe.stt import Segment, format_ts
```

with:

```python
from jabberscribe.stt import Segment, segment_line
```

and replace:

```python
    lines = [f"[{format_ts(s.start)}] {s.text}" for s in segments]
```

with:

```python
    lines = [segment_line(s) for s in segments]
```

- [ ] **Step 8: Route the pipeline's audio and stt stages through `speakers`**

In `jabberscribe/pipeline.py`, replace:

```python
from jabberscribe.audio import STT_FILENAME, prepare_for_stt
```

with:

```python
from jabberscribe.audio import STT_GLOB
```

and replace:

```python
from jabberscribe.sidecar import Sidecar, parse_sidecar
```

with:

```python
from jabberscribe.sidecar import Sidecar, parse_sidecar
from jabberscribe.speakers import stt_inputs, transcribe_inputs
```

and replace:

```python
    (work / STT_FILENAME).unlink(missing_ok=True)
    (work / f"{STT_FILENAME}.part").unlink(missing_ok=True)
```

with:

```python
    for leftover in work.glob(STT_GLOB):
        leftover.unlink(missing_ok=True)
```

and replace:

```python
            if stage == "audio":
                prepare_for_stt(job.audio_path, work)
            elif stage == "stt":
                _write_segments(segments_path, transcriber.transcribe(prepare_for_stt(job.audio_path, work)))
                _delete_stt_audio(work)
```

with:

```python
            if stage == "audio":
                stt_inputs(job.audio_path, work, sidecar, cfg.stt)
            elif stage == "stt":
                inputs = stt_inputs(job.audio_path, work, sidecar, cfg.stt)
                _write_segments(segments_path, transcribe_inputs(transcriber, inputs))
                _delete_stt_audio(work)
```

- [ ] **Step 9: Sweep every STT copy in retention**

In `jabberscribe/retention.py`, replace:

```python
from jabberscribe.audio import STT_FILENAME
```

with:

```python
from jabberscribe.audio import STT_GLOB
```

and replace:

```python
        p for d in cfg.paths.work_dir.glob("*") if d.name not in active_keys for p in d.glob(f"{STT_FILENAME}*")
```

with:

```python
        p for d in cfg.paths.work_dir.glob("*") if d.name not in active_keys for p in d.glob(STT_GLOB)
```

- [ ] **Step 10: Run the new tests**

Run: `python -m pytest tests/test_speakers.py tests/test_pipeline.py tests/test_output.py tests/test_summarize.py tests/test_config.py tests/test_cli.py tests/test_retention.py tests/test_audio.py -q`
Expected: PASS (167 passed with ffmpeg on PATH; the ffmpeg-marked tests skip without it).

- [ ] **Step 11: Run the full suite and lint**

Run: `python -m pytest -q`
Expected: `359 passed, 3 skipped` (with ffmpeg on PATH).

Run: `python -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 12: Commit**

```bash
git add jabberscribe/audio.py jabberscribe/speakers.py jabberscribe/config.py jabberscribe/stt.py jabberscribe/summarize.py jabberscribe/output.py jabberscribe/pipeline.py jabberscribe/retention.py tests/test_speakers.py tests/test_pipeline.py tests/test_output.py tests/test_cli.py tests/test_summarize.py tests/test_config.py
git commit -m "feat(speakers): per-channel transcription with near/far labels for dual-track calls" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task E4: Audio-event tagger module (bracket cues)

Spec §2.2 extra 3; audit §4b. This task builds the cue layer's engine behind a `Tagger` Protocol: AudioSet classes mapped to Hebrew cues, a windowed ffmpeg decode, the PANNs tagger (optional extra, loaded lazily), and a loader that returns `None`, with a logged reason, when the stage must be skipped. Task E5 wires it into the pipeline. All tests here use plain data or ffmpeg; none needs PyTorch.

**Why an extra and not a built-in default:** telling laughter, music, typing and ringing apart from speech needs a trained audio model. Every usable AudioSet tagger (PANNs, YAMNet, BEATs, AST) needs PyTorch, TensorFlow or ONNX Runtime. A dependency-free heuristic could find only silence. So the model is the optional extra `pip install .[cues]`, and the stage is skipped when the extra is absent.

**Files:**
- Create: `jabberscribe/cues.py`
- Modify: `jabberscribe/config.py`, `pyproject.toml`, `tests/test_config.py` (append)
- Test (create): `tests/test_cues.py`

**Interfaces:**
- Consumes: `_Strict`, `Config`, `load_config` (base `config.py`).
- Produces:
  - `config.CuesConfig(enabled: bool = True, model_path: Path | None = None, threshold: float = 0.3)` (0 < threshold < 1); `Config.cues: CuesConfig = CuesConfig()`. `load_config` resolves a relative `cues.model_path` against the config file's folder.
  - `cues.Cue(start: float, end: float, label: str)` (frozen dataclass); `cues.Tagger` Protocol: `tag(self, audio: Path) -> list[Cue]`; `cues.CueError(RuntimeError)`.
  - Labels: `LAUGHTER = "[צחוק]"`, `NOISE = "[רעש רקע]"`, `MUSIC = "[מוזיקה]"`, `SILENCE = "[שקט]"`, `TYPING = "[הקלדה]"`, `RINGING = "[צלצול]"`; `AUDIOSET_CUES: dict[str, str]`.
  - Constants: `SAMPLE_RATE = 32000`, `WINDOW_SECONDS = 2.0`, `BATCH_WINDOWS = 16`, `MIN_SILENCE_SECONDS = 6.0`, `PANNS_LABELS: Path`, `PANNS_MIN_CHECKPOINT_BYTES = 300_000_000`.
  - `cues_from_scores(windows: Iterable[dict[str, float]], window_seconds: float, threshold: float) -> list[Cue]` (sorted by start, then label).
  - `read_windows(audio: Path, window_seconds: float = WINDOW_SECONDS, ffmpeg: str = "ffmpeg") -> Iterator[bytes]` (mono 32 kHz float32 little-endian; raises `CueError`).
  - `PannsTagger(model_path: Path, threshold: float)` implementing `Tagger`.
  - `unavailable_reason(cfg: CuesConfig) -> str | None`; `load_tagger(cfg: CuesConfig) -> Tagger | None`.
  - `pyproject.toml` optional extra `cues = ["panns-inference>=0.1.1", "torch>=2.2", "numpy>=1.26"]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cues.py`:

```python
import shutil
from pathlib import Path

import pytest

from jabberscribe import cues
from jabberscribe.config import CuesConfig
from jabberscribe.cues import (
    LAUGHTER,
    MUSIC,
    SAMPLE_RATE,
    SILENCE,
    Cue,
    CueError,
    cues_from_scores,
    load_tagger,
    read_windows,
    unavailable_reason,
)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

WINDOW_BYTES = 2 * SAMPLE_RATE * 4


def test_consecutive_windows_merge_into_one_cue() -> None:
    windows = [{"Laughter": 0.8}, {"Giggle": 0.5}, {"Speech": 0.9}]

    assert cues_from_scores(windows, 2.0, 0.3) == [Cue(0.0, 4.0, LAUGHTER)]


def test_weak_and_unmapped_classes_are_ignored() -> None:
    assert cues_from_scores([{"Laughter": 0.2, "Speech": 0.99, "Dog": 0.9}], 2.0, 0.3) == []


def test_overlapping_labels_are_separate_cues() -> None:
    windows = [{"Music": 0.6}, {"Music": 0.6, "Laughter": 0.4}, {}]

    assert cues_from_scores(windows, 2.0, 0.3) == [Cue(0.0, 4.0, MUSIC), Cue(2.0, 4.0, LAUGHTER)]


def test_a_cue_still_open_at_the_end_closes_there() -> None:
    assert cues_from_scores([{}, {"Music": 0.9}], 2.0, 0.3) == [Cue(2.0, 4.0, MUSIC)]


def test_only_long_silences_are_cued() -> None:
    short = [{"Silence": 0.9}, {"Silence": 0.9}, {}]
    long = [{"Silence": 0.9}, {"Silence": 0.9}, {"Silence": 0.9}]

    assert cues_from_scores(short, 2.0, 0.3) == []
    assert cues_from_scores(long, 2.0, 0.3) == [Cue(0.0, 6.0, SILENCE)]


@needs_ffmpeg
def test_read_windows_streams_32khz_float_windows(tmp_path: Path, make_wav) -> None:
    chunks = list(read_windows(make_wav(tmp_path / "a.wav", seconds=5.0)))

    assert len(chunks) == 3
    assert [len(c) for c in chunks[:2]] == [WINDOW_BYTES, WINDOW_BYTES]
    # Resampling 8 kHz to 32 kHz may shift the length by a few samples.
    assert abs(len(chunks[2]) - WINDOW_BYTES // 2) <= 4 * 256


@needs_ffmpeg
def test_read_windows_rejects_undecodable_audio(tmp_path: Path) -> None:
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"this is not audio")

    with pytest.raises(CueError, match="ffmpeg exited"):
        list(read_windows(junk))


def test_read_windows_rejects_missing_audio(tmp_path: Path) -> None:
    with pytest.raises(CueError, match="not found"):
        list(read_windows(tmp_path / "missing.wav"))


def test_disabled_cues_load_no_tagger() -> None:
    assert load_tagger(CuesConfig(enabled=False)) is None


def test_a_missing_checkpoint_skips_the_stage_with_a_reason(tmp_path: Path, caplog) -> None:
    assert load_tagger(CuesConfig(model_path=tmp_path / "cnn14.pth")) is None
    assert "cues stage skipped" in caplog.text
    assert "checkpoint not found" in caplog.text


def test_no_model_path_is_a_reason(tmp_path: Path) -> None:
    assert "model_path" in unavailable_reason(CuesConfig())


def test_a_small_checkpoint_is_refused_before_panns_would_download_another(tmp_path: Path) -> None:
    checkpoint = tmp_path / "cnn14.pth"
    checkpoint.write_bytes(b"x")

    assert "smaller" in unavailable_reason(CuesConfig(model_path=checkpoint))


def test_a_missing_extra_skips_the_stage(tmp_path: Path, monkeypatch) -> None:
    checkpoint = tmp_path / "cnn14.pth"
    checkpoint.write_bytes(b"x")
    monkeypatch.setattr(cues, "PANNS_MIN_CHECKPOINT_BYTES", 1)
    monkeypatch.setattr(cues, "_extra_installed", lambda: False)

    assert "pip install .[cues]" in unavailable_reason(CuesConfig(model_path=checkpoint))


def test_a_missing_label_file_skips_the_stage(tmp_path: Path, monkeypatch) -> None:
    checkpoint = tmp_path / "cnn14.pth"
    checkpoint.write_bytes(b"x")
    monkeypatch.setattr(cues, "PANNS_MIN_CHECKPOINT_BYTES", 1)
    monkeypatch.setattr(cues, "_extra_installed", lambda: True)
    monkeypatch.setattr(cues, "PANNS_LABELS", tmp_path / "panns_data" / "class_labels_indices.csv")

    assert "label file" in unavailable_reason(CuesConfig(model_path=checkpoint))


def test_a_tagger_that_fails_to_load_skips_the_stage(tmp_path: Path, monkeypatch, caplog) -> None:
    checkpoint = tmp_path / "cnn14.pth"
    checkpoint.write_bytes(b"x")
    monkeypatch.setattr(cues, "unavailable_reason", lambda cfg: None)

    def broken(model_path: Path, threshold: float):
        raise RuntimeError("torch not importable")

    monkeypatch.setattr(cues, "PannsTagger", broken)

    assert load_tagger(CuesConfig(model_path=checkpoint)) is None
    assert "failed to load" in caplog.text
```

Append to the end of `tests/test_config.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cues.py tests/test_config.py -q`
Expected: FAIL. `test_cues.py` fails at collection with `ImportError: cannot import name 'cues' from 'jabberscribe'`. `test_cues_defaults` fails with `AttributeError: 'Config' object has no attribute 'cues'` and `test_relative_cues_model_path_is_relative_to_the_config_file` with `ConfigError` (`cues`: extra inputs are not permitted) (2 failed, 15 passed in `test_config.py`). `test_cues_threshold_must_be_a_probability` already passes, because the strict config refuses the unknown `cues` key; it guards the `0 < threshold < 1` bound.

- [ ] **Step 3: Add the cues config**

In `jabberscribe/config.py`, replace:

```python
from pydantic import BaseModel, ConfigDict, ValidationError
```

with:

```python
from pydantic import BaseModel, ConfigDict, Field, ValidationError
```

and replace:

```python
class RetentionConfig(_Strict):
```

with:

```python
class CuesConfig(_Strict):
    #: Tag non-speech events ([צחוק], [מוזיקה], ...) into a separate layer. Needs `pip install .[cues]`.
    enabled: bool = True
    #: The PANNs Cnn14 checkpoint (Cnn14_mAP=0.431.pth). Relative paths are relative to the config file.
    model_path: Path | None = None
    #: A label is cued in a window when one of its AudioSet classes scores at least this.
    threshold: float = Field(default=0.3, gt=0, lt=1)


class RetentionConfig(_Strict):
```

and replace:

```python
    retention: RetentionConfig = RetentionConfig()
```

with:

```python
    retention: RetentionConfig = RetentionConfig()
    cues: CuesConfig = CuesConfig()
```

and replace:

```python
    if vocabulary is not None and not vocabulary.is_absolute():
        cfg.stt.vocabulary_file = path.parent / vocabulary
    return cfg
```

with:

```python
    if vocabulary is not None and not vocabulary.is_absolute():
        cfg.stt.vocabulary_file = path.parent / vocabulary
    model = cfg.cues.model_path
    if model is not None and not model.is_absolute():
        cfg.cues.model_path = path.parent / model
    return cfg
```

- [ ] **Step 4: Declare the optional extra**

In `pyproject.toml`, replace:

```toml
dev = ["pytest>=8.0", "ruff>=0.6"]
```

with:

```toml
dev = ["pytest>=8.0", "ruff>=0.6"]
# The bracket-cue tagger (PANNs on the CPU). Without it the cues stage is skipped.
cues = ["panns-inference>=0.1.1", "torch>=2.2", "numpy>=1.26"]
```

- [ ] **Step 5: Create `jabberscribe/cues.py`**

```python
"""Bracket cues: non-speech audio events as a separate layer.

A local audio-event tagger (PANNs Cnn14, trained on AudioSet) scores the
recording in two-second windows on the CPU. Selected AudioSet classes become
Hebrew cues -- [צחוק], [מוזיקה], ... -- with start and end times. They stay
apart from the strict-verbatim transcript: result.json carries them as
`cues`, and transcript_cues.md shows them interleaved for reading.
transcript.md is never touched. Intonation is out of reach of this approach.

The tagger needs PyTorch and a 330 MB checkpoint, so it is the optional extra
`pip install .[cues]`, imported only when a tagger is built. Without the extra,
the checkpoint or the AudioSet label file -- or with cues disabled --
load_tagger returns None, says why in the log, and the pipeline skips the stage.
Nothing is ever downloaded: the checkpoint and the label file are placed by hand.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from jabberscribe.config import CuesConfig

log = logging.getLogger(__name__)

LAUGHTER = "[צחוק]"
NOISE = "[רעש רקע]"
MUSIC = "[מוזיקה]"
SILENCE = "[שקט]"
TYPING = "[הקלדה]"
RINGING = "[צלצול]"

#: AudioSet class names, spelled as in panns_inference.labels, and the cue each one raises. Others are ignored.
AUDIOSET_CUES: dict[str, str] = {
    "Laughter": LAUGHTER,
    "Baby laughter": LAUGHTER,
    "Giggle": LAUGHTER,
    "Snicker": LAUGHTER,
    "Belly laugh": LAUGHTER,
    "Chuckle, chortle": LAUGHTER,
    "Music": MUSIC,
    "Noise": NOISE,
    "Static": NOISE,
    "Environmental noise": NOISE,
    "Hubbub, speech noise, speech babble": NOISE,
    "Silence": SILENCE,
    "Typing": TYPING,
    "Computer keyboard": TYPING,
    "Typewriter": TYPING,
    "Telephone bell ringing": RINGING,
    "Ringtone": RINGING,
}

#: PANNs models are trained on 32 kHz audio.
SAMPLE_RATE = 32000
WINDOW_SECONDS = 2.0
#: Windows per model call: bounded memory, reasonable CPU throughput.
BATCH_WINDOWS = 16
#: Pauses are normal in conversation; only a long one deserves a cue.
MIN_SILENCE_SECONDS = 6.0
#: panns_inference reads its label list from here when imported, and tries to download it (wget) when missing.
PANNS_LABELS = Path.home() / "panns_data" / "class_labels_indices.csv"
#: panns_inference re-downloads a checkpoint smaller than this instead of loading it.
PANNS_MIN_CHECKPOINT_BYTES = 300_000_000


class CueError(RuntimeError):
    """The recording could not be decoded for tagging."""


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    label: str


class Tagger(Protocol):
    def tag(self, audio: Path) -> list[Cue]: ...


def cues_from_scores(windows: Iterable[dict[str, float]], window_seconds: float, threshold: float) -> list[Cue]:
    """Turn per-window class probabilities into cues.

    Window i covers [i * window_seconds, (i + 1) * window_seconds) and maps AudioSet
    class names to probabilities. A label is present in a window when any of its
    classes reaches `threshold`; consecutive windows with the label merge into one cue.
    """
    open_since: dict[str, float] = {}
    found: list[Cue] = []
    end = 0.0
    for index, scores in enumerate(windows):
        start, end = index * window_seconds, (index + 1) * window_seconds
        present = {AUDIOSET_CUES[name] for name, p in scores.items() if name in AUDIOSET_CUES and p >= threshold}
        for label in [name for name in open_since if name not in present]:
            found.append(Cue(open_since.pop(label), start, label))
        for label in present:
            open_since.setdefault(label, start)
    found += [Cue(began, end, label) for label, began in open_since.items()]
    kept = [c for c in found if c.label != SILENCE or c.end - c.start >= MIN_SILENCE_SECONDS]
    return sorted(kept, key=lambda c: (c.start, c.label))


def read_windows(audio: Path, window_seconds: float = WINDOW_SECONDS, ffmpeg: str = "ffmpeg") -> Iterator[bytes]:
    """Decode `audio` to mono 32 kHz float32 PCM and yield it one window at a time (the last may be short).

    Streaming keeps memory flat: an hour of 32 kHz float audio is 460 MB.
    """
    if not audio.is_file():
        raise CueError(f"audio not found: {audio}")
    size = int(window_seconds * SAMPLE_RATE) * 4
    args = [ffmpeg, "-nostdin", "-v", "error", "-i", str(audio), "-ac", "1", "-ar", str(SAMPLE_RATE)]
    try:
        proc = subprocess.Popen([*args, "-f", "f32le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise CueError(f"cannot start ffmpeg: {exc}") from exc
    with proc:
        stdout = proc.stdout
        assert stdout is not None
        while chunk := stdout.read(size):
            yield chunk
    if proc.returncode != 0:
        raise CueError(f"ffmpeg exited {proc.returncode} while decoding {audio}")


class PannsTagger:
    """PANNs Cnn14 audio tagging on the CPU. Importing torch happens here, never at module import."""

    def __init__(self, model_path: Path, threshold: float) -> None:
        import numpy
        from panns_inference import AudioTagging, labels

        self._np = numpy
        self._model = AudioTagging(checkpoint_path=str(model_path), device="cpu")
        self._labels: list[str] = list(labels)
        self._threshold = threshold

    def tag(self, audio: Path) -> list[Cue]:
        np = self._np
        full = int(WINDOW_SECONDS * SAMPLE_RATE)
        scores: list[dict[str, float]] = []
        batch: list = []
        for chunk in read_windows(audio):
            samples = np.frombuffer(chunk, dtype=np.float32)
            batch.append(np.pad(samples, (0, full - len(samples))))
            if len(batch) == BATCH_WINDOWS:
                scores += self._score(np.stack(batch))
                batch = []
        if batch:
            scores += self._score(np.stack(batch))
        return cues_from_scores(scores, WINDOW_SECONDS, self._threshold)

    def _score(self, batch) -> list[dict[str, float]]:
        clipwise, _embedding = self._model.inference(batch)
        return [
            {name: float(p) for name, p in zip(self._labels, row, strict=True) if name in AUDIOSET_CUES}
            for row in clipwise
        ]


def _extra_installed() -> bool:
    return importlib.util.find_spec("panns_inference") is not None


def unavailable_reason(cfg: CuesConfig) -> str | None:
    """Why the tagger cannot run here, or None when it can. Never imports torch."""
    if not cfg.enabled:
        return "disabled in config (cues.enabled: false)"
    if cfg.model_path is None:
        return "no cues.model_path configured"
    if not cfg.model_path.is_file():
        return f"model checkpoint not found: {cfg.model_path}"
    if cfg.model_path.stat().st_size < PANNS_MIN_CHECKPOINT_BYTES:
        return f"checkpoint is smaller than Cnn14_mAP=0.431.pth; panns_inference would download one: {cfg.model_path}"
    if not _extra_installed():
        return "the optional extra is not installed: pip install .[cues]"
    if not PANNS_LABELS.is_file():
        return f"AudioSet label file missing (panns_inference would download it): {PANNS_LABELS}"
    return None


def load_tagger(cfg: CuesConfig) -> Tagger | None:
    """The configured tagger, or None (logged with the reason) when the cues stage must be skipped."""
    reason = unavailable_reason(cfg)
    if reason is not None:
        log.warning("cues stage skipped: %s", reason)
        return None
    try:
        return PannsTagger(cfg.model_path, cfg.threshold)
    except Exception:
        log.exception("cues stage skipped: the tagger failed to load")
        return None
```

- [ ] **Step 6: Run the new tests**

Run: `python -m pytest tests/test_cues.py tests/test_config.py -q`
Expected: PASS (32 passed with ffmpeg on PATH).

- [ ] **Step 7: Smoke-check the real tagger (only on a host with the extra and the checkpoint)**

This step needs `pip install .[cues]`, the checkpoint `Cnn14_mAP=0.431.pth` copied from an internal share (e.g. into `config/models/`), and `class_labels_indices.csv` copied into `%USERPROFILE%\panns_data\` for the account that runs the check. Skip it on a development box without them. Task E5's tests use a fake tagger.

Run: `python -c "import importlib.util, pathlib; p = pathlib.Path(importlib.util.find_spec('panns_inference').origin).parent; print('panns_data' in (p / 'config.py').read_text(), '3e8' in (p / 'inference.py').read_text())"`
Expected: `True True`. The installed package must read its label file from `~/panns_data` and must re-download a checkpoint smaller than 3e8 bytes; `PANNS_LABELS` and `PANNS_MIN_CHECKPOINT_BYTES` guard both. If either prints `False`, open those two files and update the two constants to match before going on.

Run: `python -c "from pathlib import Path; from jabberscribe.config import CuesConfig; from jabberscribe.cues import load_tagger; t = load_tagger(CuesConfig(model_path=Path('config/models/Cnn14_mAP=0.431.pth'))); print(t.tag(Path('clip.wav')))"`, with `clip.wav` a short real call.
Expected: a list of `Cue(...)`. It may be empty for a quiet call. The run must not traceback and must not attempt a download.

- [ ] **Step 8: Run the full suite and lint**

Run: `python -m pytest -q`
Expected: `377 passed, 3 skipped`.

Run: `python -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add jabberscribe/cues.py jabberscribe/config.py pyproject.toml tests/test_cues.py tests/test_config.py
git commit -m "feat(cues): AudioSet tagger behind a Tagger protocol, optional PANNs extra loaded lazily" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task E5: The `cues` stage and the cue layer in the outputs

Wires Task E4's tagger into the pipeline as a new checkpointed stage between `stt` and `summarize`. It writes `transcript_cues.md` and adds `cues` to `result.json`. A missing or failing tagger degrades the stage; it never fails the job.

**Files:**
- Modify: `jabberscribe/jobs.py`, `jabberscribe/pipeline.py`, `jabberscribe/output.py`, `jabberscribe/cli.py`, `tests/test_jobs.py` (one assertion), `tests/test_pipeline.py` (four assertions)
- Test (create): `tests/test_cues_stage.py`

**Interfaces:**
- Consumes: `Cue`, `Tagger`, `load_tagger(cfg.cues)`, `unavailable_reason(cfg.cues)`, `LAUGHTER`, `MUSIC` (E4); `segment_line`, `format_ts` (E3, base `stt.py`); `process_job`, `_run(job, cfg, store, transcriber, summarizer, clock)`, `run_once(..., now=None, clock=_utcnow)`, `run_job(..., clock=_utcnow)` (base `pipeline.py`, E2, E3); `write_outputs` (with `summary_error`), `TEXT_FILES` (base `output.py`, E3); `COPY_STAGES` (base `jobs.py`, unchanged); `cli._workers`, `_serve`, `_process`, `doctor` (base, E2).
- Produces:
  - `jobs.STAGE_ORDER = ("audio", "stt", "cues", "summarize", "output")`. A job checkpointed at `stt` by an older build runs `cues` next. A job already past `cues` has no `cues.json` and goes out with cues unavailable.
  - `pipeline.CUES_JSON = "cues.json"` (in `work/<job_key>/`; JSON list of cues, or `null` when unavailable). The signatures are now `process_job(job, cfg, store, transcriber, summarizer, tagger: Tagger | None = None) -> Path`, `run_once(cfg, store, transcriber, summarizer, now: datetime | None = None, clock=_utcnow, tagger: Tagger | None = None) -> int` and `run_job(job_key, cfg, store, transcriber, summarizer, clock=_utcnow, tagger: Tagger | None = None) -> bool`; callers pass `tagger=` by name. `timings` gains `cues_sec`.
  - `output.CUES_TRANSCRIPT_FILE = "transcript_cues.md"` (always written; part of `TEXT_FILES`, so retention and superseding remove it too); `output.CUES_UNAVAILABLE` (Hebrew notice). `write_outputs(..., timings=None, cues: list[Cue] | None = None)`. `result.json` gains `"cues_available": bool` and `"cues": [{"start", "end", "label"}]`.
  - `cli._workers(cfg, client) -> tuple[LiteLLMTranscriber, LiteLLMSummarizer, Tagger | None]`. `doctor` gains a `cues` check that is information only: it is always `ok`, with detail `tagger available` or `skipped: <reason>`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cues_stage.py`:

```python
import json
import shutil
from pathlib import Path

import httpx
import pytest

from jabberscribe.cli import doctor
from jabberscribe.cues import LAUGHTER, MUSIC, Cue
from jabberscribe.jobs import DONE
from jabberscribe.output import CUES_TRANSCRIPT_FILE, CUES_UNAVAILABLE, RESULT_FILE, TRANSCRIPT_FILE, write_outputs
from jabberscribe.pipeline import run_once
from jabberscribe.sidecar import parse_sidecar
from jabberscribe.stt import Segment
from jabberscribe.watcher import scan_once

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

SEGMENTS = [Segment(0.0, 1.0, "אה, שלום"), Segment(3.0, 4.0, "חחח, נכון")]
CUES = [Cue(2.0, 4.0, LAUGHTER), Cue(10.0, 14.0, MUSIC)]


def _write(tmp_path: Path, make_sidecar, cues: list[Cue] | None) -> Path:
    sidecar = parse_sidecar(make_sidecar(tmp_path / "s.json").read_text(encoding="utf-8"))
    out = tmp_path / "out"
    write_outputs(
        out,
        sidecar=sidecar,
        segments=SEGMENTS,
        summary=None,
        owners=[sidecar.line_owner],
        models={"stt": "whisper-he", "summary": "gemma-3"},
        recording=out / "recording.wav",
        cues=cues,
    )
    return out


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_cues_are_interleaved_in_their_own_file(tmp_path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, CUES)

    lines = [line for line in _text(out / CUES_TRANSCRIPT_FILE).splitlines() if line.startswith("[")]

    assert lines == ["[00:00:00] אה, שלום", "[00:00:02] [צחוק]", "[00:00:03] חחח, נכון", "[00:00:10] [מוזיקה]"]
    assert _text(out / CUES_TRANSCRIPT_FILE).startswith('<div dir="rtl">\n\n#')


def test_the_verbatim_transcript_never_carries_cues(tmp_path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, CUES)

    assert LAUGHTER not in _text(out / TRANSCRIPT_FILE)
    assert MUSIC not in _text(out / TRANSCRIPT_FILE)


def test_result_json_carries_the_cues(tmp_path, make_sidecar) -> None:
    result = json.loads(_text(_write(tmp_path, make_sidecar, CUES) / RESULT_FILE))

    assert result["cues_available"] is True
    assert result["cues"] == [
        {"start": 2.0, "end": 4.0, "label": LAUGHTER},
        {"start": 10.0, "end": 14.0, "label": MUSIC},
    ]


def test_unavailable_cues_are_said_so(tmp_path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, None)

    text = _text(out / CUES_TRANSCRIPT_FILE)
    assert CUES_UNAVAILABLE in text
    assert "[00:00:00] אה, שלום" in text
    result = json.loads(_text(out / RESULT_FILE))
    assert (result["cues_available"], result["cues"]) == (False, [])


def test_a_call_without_events_is_not_unavailable(tmp_path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, [])

    assert CUES_UNAVAILABLE not in _text(out / CUES_TRANSCRIPT_FILE)
    assert json.loads(_text(out / RESULT_FILE))["cues_available"] is True


class FakeTranscriber:
    def transcribe(self, audio: Path) -> list[Segment]:
        return [Segment(0.0, 1.5, "אה, שלום")]


class FakeSummarizer:
    def summarize(self, segments: list[Segment]) -> None:
        return None


class FakeTagger:
    def __init__(self) -> None:
        self.seen: list[Path] = []

    def tag(self, audio: Path) -> list[Cue]:
        self.seen.append(audio)
        return [Cue(0.5, 2.0, LAUGHTER)]


class ExplodingTagger:
    def tag(self, audio: Path) -> list[Cue]:
        raise RuntimeError("out of memory")


def _enqueue(cfg, store, audit, make_wav, make_sidecar) -> str:
    make_wav(cfg.paths.inbox / "c.wav", channels=2)
    make_sidecar(cfg.paths.inbox / "c.json", call_id="cue")
    scan_once(cfg, store, audit, min_age_seconds=0)
    return "cue_1042"


def _result(store, key: str) -> dict:
    return json.loads(_text(store.get(key).out_dir / RESULT_FILE))


@needs_ffmpeg
def test_the_cues_stage_tags_the_recording(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)
    tagger = FakeTagger()

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer(), tagger=tagger)

    job = store.get(key)
    assert job.status == DONE
    assert tagger.seen == [job.audio_path]
    result = _result(store, key)
    assert result["cues"] == [{"start": 0.5, "end": 2.0, "label": LAUGHTER}]
    assert "cues_sec" in result["timings"]


@needs_ffmpeg
def test_a_failing_tagger_never_fails_the_job(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer(), tagger=ExplodingTagger())

    job = store.get(key)
    assert (job.status, job.attempts) == (DONE, 0)
    assert _result(store, key)["cues_available"] is False


@needs_ffmpeg
def test_without_a_tagger_the_stage_is_skipped(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _enqueue(cfg, store, audit, make_wav, make_sidecar)

    run_once(cfg, store, FakeTranscriber(), FakeSummarizer())

    job = store.get(key)
    assert job.status == DONE
    assert _result(store, key)["cues_available"] is False
    assert (job.out_dir / CUES_TRANSCRIPT_FILE).is_file()


def test_doctor_reports_the_tagger_without_failing(cfg) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(refuse))

    check = {c.name: c for c in doctor(cfg, client)}["cues"]

    assert check.ok
    assert check.detail == "skipped: no cues.model_path configured"
```

In `tests/test_jobs.py`, replace:

```python
    assert tuple(walked) == STAGE_ORDER == ("audio", "stt", "summarize", "output")
```

with:

```python
    assert tuple(walked) == STAGE_ORDER == ("audio", "stt", "cues", "summarize", "output")
```

In `tests/test_pipeline.py`, replace:

```python
    assert set(timings) == {"audio_sec", "stt_sec", "summarize_sec", "hangup_to_output_sec"}
```

with:

```python
    assert set(timings) == {"audio_sec", "stt_sec", "cues_sec", "summarize_sec", "hangup_to_output_sec"}
```

and replace:

```python
    assert (crashed.status, crashed.stage, crashed.attempts) == (QUEUED, "stt", 1)
```

with:

```python
    assert (crashed.status, crashed.stage, crashed.attempts) == (QUEUED, "cues", 1)
```

and replace:

```python
    assert (waiting.status, waiting.stage, waiting.attempts, waiting.transient_failures) == (QUEUED, "stt", 0, 1)
```

with:

```python
    assert (waiting.status, waiting.stage, waiting.attempts, waiting.transient_failures) == (QUEUED, "cues", 0, 1)
```

and replace:

```python
    assert (store.get(longest).status, store.get(longest).stage) == (QUEUED, "stt")
```

with:

```python
    assert (store.get(longest).status, store.get(longest).stage) == (QUEUED, "cues")
```

(These three base tests check the checkpoint a summarize failure leaves: it is now `cues`, the stage before `summarize`. `COPY_STAGES` stays `(QUEUED, "audio")`: a failure after `stt` -- in `cues`, `summarize` or `output` -- is still the system's, not the copy's, so it never triggers conference re-election.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cues_stage.py tests/test_jobs.py tests/test_pipeline.py -q`
Expected: FAIL. `test_cues_stage.py` fails at collection with `ImportError: cannot import name 'CUES_TRANSCRIPT_FILE' from 'jabberscribe.output'`. `test_next_stage_walks_fixed_order`, `test_result_records_stage_timings_and_latency`, `test_resume_after_crash_does_not_retranscribe`, `test_transient_summary_outage_resumes_at_summarize` and `test_summary_rejection_fails_only_the_primary_of_a_conference` fail on the missing `cues` stage (5 failed, 63 passed in `test_jobs.py` + `test_pipeline.py`).

- [ ] **Step 3: Add the stage**

In `jabberscribe/jobs.py`, replace:

```python
STAGE_ORDER: tuple[str, ...] = ("audio", "stt", "summarize", "output")
```

with:

```python
STAGE_ORDER: tuple[str, ...] = ("audio", "stt", "cues", "summarize", "output")
```

- [ ] **Step 4: Write the cue layer in `jabberscribe/output.py`**

In `jabberscribe/output.py`, replace:

```python
The Markdown bodies are wrapped in <div dir="rtl">: mixed Hebrew and English
lines and tables render scrambled in most viewers without it.
"""
```

with:

```python
The Markdown bodies are wrapped in <div dir="rtl">: mixed Hebrew and English
lines and tables render scrambled in most viewers without it.

transcript.md is strict verbatim and never carries cues. transcript_cues.md is
the same transcript with the bracket cues ([צחוק], ...) interleaved by time.
"""
```

and replace:

```python
from jabberscribe.jobs import utcnow
```

with:

```python
from jabberscribe.cues import Cue
from jabberscribe.jobs import utcnow
```

and replace:

```python
from jabberscribe.stt import Segment, segment_line
```

with:

```python
from jabberscribe.stt import Segment, format_ts, segment_line
```

and replace:

```python
ACTIONS_FILE = "actions.md"
RESULT_FILE = "result.json"
TEXT_FILES = (TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE, RESULT_FILE)

SUMMARY_UNAVAILABLE = "הסיכום אינו זמין עבור שיחה זו."
```

with:

```python
ACTIONS_FILE = "actions.md"
CUES_TRANSCRIPT_FILE = "transcript_cues.md"
RESULT_FILE = "result.json"
TEXT_FILES = (TRANSCRIPT_FILE, CUES_TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE, RESULT_FILE)

SUMMARY_UNAVAILABLE = "הסיכום אינו זמין עבור שיחה זו."
CUES_UNAVAILABLE = "סימוני האירועים אינם זמינים עבור שיחה זו."
```

and replace:

```python
def _render_summary(summary: Summary | None) -> str:
```

with:

```python
def _render_cues_transcript(segments: list[Segment], cues: list[Cue] | None) -> str:
    # A cue sorts before a segment that starts at the same moment: "[צחוק]" then what was said.
    lines = [(c.start, 0, f"[{format_ts(c.start)}] {c.label}") for c in cues or []]
    lines += [(s.start, 1, segment_line(s)) for s in segments]
    body = "\n\n".join(text for _, _, text in sorted(lines))
    notice = "" if cues is not None else CUES_UNAVAILABLE + "\n\n"
    return _rtl("# תמליל עם סימוני אירועים\n\n" + notice + body + "\n")


def _render_summary(summary: Summary | None) -> str:
```

and replace:

```python
    timings: dict[str, float] | None = None,
) -> Path:
    """Write every output file for one call. Returns the result.json path.

    `timings` holds per-stage seconds and the hang-up-to-output latency (see pipeline.py).
    `summary_error` says why the summary is unavailable, when the reason is known.
    """
    write_atomic(out_dir / TRANSCRIPT_FILE, _render_transcript(segments))
    write_atomic(out_dir / SUMMARY_FILE, _render_summary(summary))
    write_atomic(out_dir / ACTIONS_FILE, _render_actions(summary))
```

with:

```python
    timings: dict[str, float] | None = None,
    cues: list[Cue] | None = None,
) -> Path:
    """Write every output file for one call. Returns the result.json path.

    `timings` holds per-stage seconds and the hang-up-to-output latency (see pipeline.py).
    `summary_error` says why the summary is unavailable, when the reason is known.
    `cues` is None when the cue layer is unavailable, [] when the call had no events.
    """
    write_atomic(out_dir / TRANSCRIPT_FILE, _render_transcript(segments))
    write_atomic(out_dir / CUES_TRANSCRIPT_FILE, _render_cues_transcript(segments, cues))
    write_atomic(out_dir / SUMMARY_FILE, _render_summary(summary))
    write_atomic(out_dir / ACTIONS_FILE, _render_actions(summary))
```

and replace:

```python
        "action_items": [asdict(i) for i in summary.action_items] if summary else [],
```

with:

```python
        "action_items": [asdict(i) for i in summary.action_items] if summary else [],
        "cues_available": cues is not None,
        "cues": [asdict(c) for c in cues or []],
```

- [ ] **Step 5: Run the stage in `jabberscribe/pipeline.py`**

In `jabberscribe/pipeline.py`, replace:

```python
Stages run in the fixed order audio -> stt -> summarize -> output, and each one
checkpoints in the job store. A restart resumes at the first incomplete stage,
so a crash after transcription never transcribes again.
```

with:

```python
Stages run in the fixed order audio -> stt -> cues -> summarize -> output, and
each one checkpoints in the job store. A restart resumes at the first
incomplete stage, so a crash after transcription never transcribes again.

The cues stage never fails a job: without a tagger, or when tagging fails,
the call goes out with its cue layer marked unavailable.
```

and replace:

```python
from jabberscribe.config import Config
```

with:

```python
from jabberscribe.config import Config
from jabberscribe.cues import Cue, Tagger
```

and replace:

```python
TIMINGS_FILE = "timings.json"
```

with:

```python
TIMINGS_FILE = "timings.json"
CUES_JSON = "cues.json"
```

and replace:

```python
def _read_segments(path: Path) -> list[Segment]:
    return [Segment(**s) for s in json.loads(path.read_text(encoding="utf-8"))]
```

with:

```python
def _read_segments(path: Path) -> list[Segment]:
    return [Segment(**s) for s in json.loads(path.read_text(encoding="utf-8"))]


def _write_cues(path: Path, cues: list[Cue] | None) -> None:
    payload = None if cues is None else [asdict(c) for c in cues]
    write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_cues(path: Path) -> list[Cue] | None:
    """None when the layer is unavailable -- including a job that passed the cues stage before it existed."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return None if data is None else [Cue(**c) for c in data]


def _tag(tagger: Tagger | None, audio: Path, job_key: str) -> list[Cue] | None:
    """The recording's cues, or None when there is no tagger or it fails. Cues never fail a job."""
    if tagger is None:
        log.info("%s: no tagger; cues skipped", job_key)
        return None
    try:
        return tagger.tag(audio)
    except Exception:
        log.exception("%s: tagging failed; the call goes out without cues", job_key)
        return None
```

and replace:

```python
    transcriber: Transcriber,
    summarizer: Summarizer,
) -> Path:
```

with:

```python
    transcriber: Transcriber,
    summarizer: Summarizer,
    tagger: Tagger | None = None,
) -> Path:
```

and replace:

```python
    timings_path = work / TIMINGS_FILE
    stage = job.stage
```

with:

```python
    timings_path = work / TIMINGS_FILE
    cues_path = work / CUES_JSON
    stage = job.stage
```

and replace:

```python
                _delete_stt_audio(work)
            elif stage == "summarize":
```

with:

```python
                _delete_stt_audio(work)
            elif stage == "cues":
                _write_cues(cues_path, _tag(tagger, job.audio_path, job.job_key))
            elif stage == "summarize":
```

and replace:

The cue checkpoint is read with the other checkpoints, before the `try` that turns an output-side `OSError` into a `TransientError`: a corrupt `cues.json` is a real failure, not an outage.

and replace:

```python
                summary, summary_error = _read_summary(summary_path)
```

with:

```python
                summary, summary_error = _read_summary(summary_path)
                cues = _read_cues(cues_path)
```

and replace:

```python
                        recording=job.audio_path,
                        timings=timings,
```

with:

```python
                        recording=job.audio_path,
                        timings=timings,
                        cues=cues,
```

and replace:

```python
    summarizer: Summarizer,
    clock: Callable[[], datetime],
) -> None:
    try:
        process_job(job, cfg, store, transcriber, summarizer)
```

with:

```python
    summarizer: Summarizer,
    clock: Callable[[], datetime],
    tagger: Tagger | None = None,
) -> None:
    try:
        process_job(job, cfg, store, transcriber, summarizer, tagger)
```

and replace:

```python
    now: datetime | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> int:
```

with:

```python
    now: datetime | None = None,
    clock: Callable[[], datetime] = _utcnow,
    tagger: Tagger | None = None,
) -> int:
```

and replace:

```python
        _run(job, cfg, store, transcriber, summarizer, clock)
    return processed
```

with:

```python
        _run(job, cfg, store, transcriber, summarizer, clock, tagger)
    return processed
```

and replace:

```python
    summarizer: Summarizer,
    clock: Callable[[], datetime] = _utcnow,
) -> bool:
    """Process one job now, due or not (the `process` command). Returns False if it was not runnable."""
    job = store.claim(job_key)
    if job is None:
        return False
    _run(job, cfg, store, transcriber, summarizer, clock)
```

with:

```python
    summarizer: Summarizer,
    clock: Callable[[], datetime] = _utcnow,
    tagger: Tagger | None = None,
) -> bool:
    """Process one job now, due or not (the `process` command). Returns False if it was not runnable."""
    job = store.claim(job_key)
    if job is None:
        return False
    _run(job, cfg, store, transcriber, summarizer, clock, tagger)
```

- [ ] **Step 6: Build the tagger in the CLI and report it in `doctor`**

In `jabberscribe/cli.py`, replace:

```python
from jabberscribe.config import Config, ConfigError, load_config
```

with:

```python
from jabberscribe.config import Config, ConfigError, load_config
from jabberscribe.cues import Tagger, load_tagger, unavailable_reason
```

and replace:

```python
def _check_dir(name: str, path: Path) -> Check:
```

with:

```python
def _check_cues(cfg: Config) -> Check:
    """Information only: the cue layer is optional, so a missing tagger never fails doctor."""
    reason = unavailable_reason(cfg.cues)
    return Check("cues", True, "tagger available" if reason is None else f"skipped: {reason}")


def _check_dir(name: str, path: Path) -> Check:
```

and replace:

```python
        _check_vocabulary(cfg),
```

with:

```python
        _check_vocabulary(cfg),
        _check_cues(cfg),
```

and replace:

```python
def _workers(cfg: Config, client: httpx.Client) -> tuple[LiteLLMTranscriber, LiteLLMSummarizer]:
    prompt = build_prompt(_read_vocabulary(cfg))
    summarizer = LiteLLMSummarizer(client, cfg.summary.model, cfg.summary.max_chunk_chars)
    return LiteLLMTranscriber(client, cfg.stt.model, prompt), summarizer
```

with:

```python
def _workers(cfg: Config, client: httpx.Client) -> tuple[LiteLLMTranscriber, LiteLLMSummarizer, Tagger | None]:
    prompt = build_prompt(_read_vocabulary(cfg))
    summarizer = LiteLLMSummarizer(client, cfg.summary.model, cfg.summary.max_chunk_chars)
    return LiteLLMTranscriber(client, cfg.stt.model, prompt), summarizer, load_tagger(cfg.cues)
```

and replace:

```python
    transcriber, summarizer = _workers(cfg, make_client(cfg.litellm))
```

with:

```python
    transcriber, summarizer, tagger = _workers(cfg, make_client(cfg.litellm))
```

and replace:

```python
            ("process", lambda: run_once(cfg, store, transcriber, summarizer)),
```

with:

```python
            ("process", lambda: run_once(cfg, store, transcriber, summarizer, tagger=tagger)),
```

The base `run_job` takes `clock` as its sixth parameter, so `_process` must pass the tagger by name; unpacking `*_workers(...)` would hand the tagger to `clock`.

and replace:

```python
    run_job(target_key, cfg, store, *_workers(cfg, make_client(cfg.litellm)))
```

with:

```python
    transcriber, summarizer, tagger = _workers(cfg, make_client(cfg.litellm))
    run_job(target_key, cfg, store, transcriber, summarizer, tagger=tagger)
```

- [ ] **Step 7: Run the new tests**

Run: `python -m pytest tests/test_cues_stage.py tests/test_jobs.py tests/test_pipeline.py tests/test_output.py tests/test_cli.py tests/test_retention.py tests/test_group.py -q`
Expected: PASS (178 passed with ffmpeg on PATH). The base retention and group tests still pass with `transcript_cues.md` added to `TEXT_FILES`.

- [ ] **Step 8: Run the full suite and lint**

Run: `python -m pytest -q`
Expected: `386 passed, 3 skipped`.

Run: `python -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add jabberscribe/jobs.py jabberscribe/pipeline.py jabberscribe/output.py jabberscribe/cli.py tests/test_cues_stage.py tests/test_jobs.py tests/test_pipeline.py
git commit -m "feat(cues): cues stage between stt and summarize; transcript_cues.md and result.json cues" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task E6: Admin alerting over a webhook

Audit §4a item 1. The service is unattended, so a person must hear about four things: a failed call, a backlog older than the latency budget, an unreachable LiteLLM, and a purge that could not delete something. Alerts go out as one JSON POST each, at most once per hour per kind.

**Files:**
- Create: `jabberscribe/alerts.py`
- Modify: `jabberscribe/config.py`, `jabberscribe/cli.py`, `tests/test_config.py` (append)
- Test (create): `tests/test_alerts.py`

**Interfaces:**
- Consumes: `JobStore.list_by_status`, `FAILED`, `iso` (base `jobs.py`); `JobStore.oldest_pending_created_at` (E2); `write_atomic` (base `output.py`); `PurgeResult.errors` (base `retention.py`); `cli._serve` (per-phase isolation, purge block), `cli._utcnow`, `_report_purge`, `main` doctor branch (base, E2, E5); `httpx`.
- Produces:
  - `config.AlertsConfig(webhook_url: str | None = None, backlog_minutes: int = 15)`; `Config.alerts: AlertsConfig = AlertsConfig()`.
  - `alerts`: `URL_ENV = "JABBERSCRIBE_ALERT_WEBHOOK_URL"`, `TOKEN_ENV = "JABBERSCRIBE_ALERT_TOKEN"`, `STATE_FILE = "alerts.json"`, `RATE_LIMIT = timedelta(hours=1)`, `TIMEOUT_SECONDS = 10.0`, `MAX_KEYS = 5`; kinds `JOB_FAILED = "job_failed"`, `BACKLOG = "backlog"`, `LITELLM_DOWN = "litellm_down"`, `PURGE_ERRORS = "purge_errors"`.
  - `Alerter(url: str, state_path: Path, client: httpx.Client | None = None, token: str | None = None)`. Its methods are `send(kind: str, message: str, now: datetime) -> bool` (True only when delivered; never raises), `failed_seen -> set[str]` (property) and `remember_failed(keys: set[str]) -> None`.
  - `make_alerter(cfg: Config, client: httpx.Client | None = None) -> Alerter | None` (None when no URL in env or config); `check_jobs(store: JobStore, alerter: Alerter, now: datetime, backlog_minutes: int) -> None`; `probe_litellm(client: httpx.Client) -> str | None`.
  - Payload: `{"text": "JabberScribe <kind>: <message>", "source": "jabberscribe", "kind", "message", "host", "at"}`; header `Authorization: Bearer <JABBERSCRIBE_ALERT_TOKEN>` when that variable is set.
  - CLI: alerting is off unless a URL is set (`make_alerter` returns None). When an alerter is configured, `run` checks jobs and probes LiteLLM after every poll, in its own isolated block after the purge (an exception is logged and marks the poll failed). `run` and `purge` alert on purge errors. `doctor` alerts `litellm_down` when any `litellm*` check fails. Names imported into `cli` (`make_alerter`, `probe_litellm`) can be monkeypatched in tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_alerts.py`:

```python
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from jabberscribe import cli
from jabberscribe.alerts import (
    BACKLOG,
    JOB_FAILED,
    LITELLM_DOWN,
    PURGE_ERRORS,
    TOKEN_ENV,
    URL_ENV,
    Alerter,
    check_jobs,
    make_alerter,
    probe_litellm,
)
from jabberscribe.jobs import FAILED
from jabberscribe.retention import PurgeResult

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class Hook:
    """Fake webhook: records every POST and answers with `status`."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[dict] = []
        self.urls: list[str] = []
        self.auth: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(json.loads(request.content))
        self.urls.append(str(request.url))
        self.auth = request.headers.get("authorization")
        return httpx.Response(self.status)

    @property
    def kinds(self) -> list[str]:
        return [p["kind"] for p in self.posts]


def _alerter(tmp_path: Path, hook: Hook, token: str | None = None) -> Alerter:
    client = httpx.Client(transport=httpx.MockTransport(hook))
    return Alerter("http://hooks.test/alert", tmp_path / "alerts.json", client, token)


def _job(store, key: str) -> None:
    store.create(
        job_key=key,
        call_id=key.split("_")[0],
        conference_id=None,
        audio_path=Path(f"/out/{key}/recording.wav"),
        out_dir=Path(f"/out/{key}"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )


def _fail(store, key: str) -> None:
    _job(store, key)
    store.record_attempt(key, "stt: HTTP 413")
    store.set_status(key, FAILED)


def _age(cfg, key: str, minutes: int) -> None:
    created = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    conn = sqlite3.connect(cfg.paths.db_path)
    conn.execute("UPDATE jobs SET created_at = ? WHERE job_key = ?", (created, key))
    conn.commit()
    conn.close()


def test_send_posts_json_with_a_text_field(tmp_path: Path) -> None:
    hook = Hook()

    assert _alerter(tmp_path, hook).send(BACKLOG, "oldest call waited 20 min", T0) is True

    post = hook.posts[0]
    assert post["text"] == "JabberScribe backlog: oldest call waited 20 min"
    assert (post["source"], post["kind"], post["at"]) == ("jabberscribe", BACKLOG, "2026-10-07T12:00:00+00:00")
    assert post["host"]
    assert hook.auth is None


def test_each_kind_is_sent_at_most_once_an_hour(tmp_path: Path) -> None:
    hook = Hook()
    alerter = _alerter(tmp_path, hook)

    assert alerter.send(BACKLOG, "a", T0) is True
    assert alerter.send(BACKLOG, "b", T0 + timedelta(minutes=30)) is False
    assert alerter.send(JOB_FAILED, "c", T0 + timedelta(minutes=31)) is True
    assert alerter.send(BACKLOG, "d", T0 + timedelta(minutes=61)) is True

    assert hook.kinds == [BACKLOG, JOB_FAILED, BACKLOG]


def test_the_rate_limit_survives_a_restart(tmp_path: Path) -> None:
    hook = Hook()
    _alerter(tmp_path, hook).send(BACKLOG, "a", T0)

    assert _alerter(tmp_path, hook).send(BACKLOG, "b", T0 + timedelta(minutes=5)) is False
    assert list(tmp_path.glob("*.part")) == []


def test_an_undelivered_alert_is_tried_again(tmp_path: Path) -> None:
    hook = Hook(status=500)
    alerter = _alerter(tmp_path, hook)

    assert alerter.send(BACKLOG, "a", T0) is False
    hook.status = 200
    assert alerter.send(BACKLOG, "a", T0 + timedelta(minutes=1)) is True


def test_an_unreachable_webhook_never_raises(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(refuse))

    assert Alerter("http://hooks.test/alert", tmp_path / "alerts.json", client).send(BACKLOG, "a", T0) is False


def test_url_and_token_come_from_the_environment(cfg, monkeypatch) -> None:
    hook = Hook()
    monkeypatch.setenv(URL_ENV, "http://relay.test/hook")
    monkeypatch.setenv(TOKEN_ENV, "s3cret")

    alerter = make_alerter(cfg, httpx.Client(transport=httpx.MockTransport(hook)))
    alerter.send(BACKLOG, "a", T0)

    assert hook.urls == ["http://relay.test/hook"]
    assert hook.auth == "Bearer s3cret"


def test_alerting_is_off_without_a_url(cfg, monkeypatch) -> None:
    monkeypatch.delenv(URL_ENV, raising=False)

    assert make_alerter(cfg) is None


def test_the_configured_url_is_used_when_the_environment_has_none(cfg, monkeypatch) -> None:
    monkeypatch.delenv(URL_ENV, raising=False)
    cfg.alerts.webhook_url = "http://config.test/hook"
    hook = Hook()

    make_alerter(cfg, httpx.Client(transport=httpx.MockTransport(hook))).send(BACKLOG, "a", T0)

    assert hook.urls == ["http://config.test/hook"]


def test_a_new_failure_is_alerted_once(store, tmp_path: Path) -> None:
    hook = Hook()
    alerter = _alerter(tmp_path, hook)
    _fail(store, "a_1")

    check_jobs(store, alerter, T0, 15)
    check_jobs(store, alerter, T0 + timedelta(hours=2), 15)

    assert hook.kinds == [JOB_FAILED]
    assert "a_1" in hook.posts[0]["message"]

    _fail(store, "b_2")
    check_jobs(store, alerter, T0 + timedelta(hours=3), 15)

    assert hook.kinds == [JOB_FAILED, JOB_FAILED]
    assert "b_2" in hook.posts[1]["message"]
    assert "a_1" not in hook.posts[1]["message"]


def test_a_rate_limited_failure_is_alerted_later(store, tmp_path: Path) -> None:
    hook = Hook()
    alerter = _alerter(tmp_path, hook)
    _fail(store, "a_1")
    check_jobs(store, alerter, T0, 15)
    _fail(store, "b_2")

    check_jobs(store, alerter, T0 + timedelta(minutes=10), 15)
    check_jobs(store, alerter, T0 + timedelta(minutes=70), 15)

    assert hook.kinds == [JOB_FAILED, JOB_FAILED]
    assert "b_2" in hook.posts[1]["message"]


def test_a_backlog_older_than_the_threshold_is_alerted(cfg, store, tmp_path: Path) -> None:
    hook = Hook()
    alerter = _alerter(tmp_path, hook)
    _job(store, "young_1")

    check_jobs(store, alerter, datetime.now(UTC), 15)
    assert hook.kinds == []

    _job(store, "old_2")
    _age(cfg, "old_2", 20)
    check_jobs(store, alerter, datetime.now(UTC), 15)

    assert hook.kinds == [BACKLOG]
    assert "20 min" in hook.posts[0]["message"]


def test_probe_litellm() -> None:
    def client(handler) -> httpx.Client:
        return httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(handler))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert probe_litellm(client(lambda r: httpx.Response(200, json={"data": []}))) is None
    assert probe_litellm(client(lambda r: httpx.Response(503))) == "HTTP 503"
    assert "refused" in probe_litellm(client(refuse))


def test_run_alerts_when_litellm_is_down(cfg_file, tmp_path: Path, monkeypatch) -> None:
    hook = Hook()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli, "make_alerter", lambda cfg: _alerter(tmp_path, hook))
    monkeypatch.setattr(
        cli,
        "make_client",
        lambda litellm_cfg: httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(refuse)),
    )

    assert cli.main(["--config", str(cfg_file), "run", "--once"]) == 0

    assert hook.kinds == [LITELLM_DOWN]
    assert "refused" in hook.posts[0]["message"]


def test_purge_command_alerts_on_errors(cfg_file, tmp_path: Path, monkeypatch) -> None:
    hook = Hook()
    monkeypatch.setattr(cli, "make_alerter", lambda cfg: _alerter(tmp_path, hook))
    monkeypatch.setattr(cli, "purge", lambda *args, **kwargs: PurgeResult(errors=("k_1: cannot delete audio",)))

    assert cli.main(["--config", str(cfg_file), "purge"]) == 1

    assert hook.kinds == [PURGE_ERRORS]
    assert "k_1: cannot delete audio" in hook.posts[0]["message"]


def test_doctor_alerts_when_litellm_checks_fail(cfg_file, tmp_path: Path, monkeypatch) -> None:
    hook = Hook()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli, "make_alerter", lambda cfg: _alerter(tmp_path, hook))
    monkeypatch.setattr(
        cli,
        "make_client",
        lambda litellm_cfg: httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(refuse)),
    )

    assert cli.main(["--config", str(cfg_file), "doctor"]) == 1

    assert hook.kinds == [LITELLM_DOWN]
    assert hook.posts[0]["message"].startswith("doctor: litellm:")
```

Append to the end of `tests/test_config.py`:

```python
def test_alerts_are_off_by_default(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, BASE))

    assert (cfg.alerts.webhook_url, cfg.alerts.backlog_minutes) == (None, 15)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_alerts.py tests/test_config.py -q`
Expected: FAIL. `test_alerts.py` fails at collection with `ModuleNotFoundError: No module named 'jabberscribe.alerts'`. `test_alerts_are_off_by_default` fails with `AttributeError: 'Config' object has no attribute 'alerts'`.

- [ ] **Step 3: Add the alerts config**

In `jabberscribe/config.py`, replace:

```python
class RetentionConfig(_Strict):
```

with:

```python
class AlertsConfig(_Strict):
    #: Where alerts are POSTed (JSON). None disables alerting. JABBERSCRIBE_ALERT_WEBHOOK_URL overrides it.
    webhook_url: str | None = None
    #: Alert when the oldest unfinished call arrived longer ago than this.
    backlog_minutes: int = 15


class RetentionConfig(_Strict):
```

and replace:

```python
    cues: CuesConfig = CuesConfig()
```

with:

```python
    cues: CuesConfig = CuesConfig()
    alerts: AlertsConfig = AlertsConfig()
```

- [ ] **Step 4: Create `jabberscribe/alerts.py`**

```python
"""Operator alerts over a generic webhook.

The service is unattended, so a failed call, a growing backlog, an
unreachable LiteLLM or a purge that could not delete must reach a person.
Each alert is one JSON POST to one webhook -- an internal relay, a monitoring
system, or a Teams/Slack incoming webhook, which render the `text` field as is.

Each kind is sent at most once an hour. The send times persist in alerts.json
next to the database, so a restarting service does not spam. Payloads carry
kinds, counts and job keys -- never names or call content. Sending never
raises: an alert that cannot be delivered is logged and tried again at the
next opportunity.

The URL comes from JABBERSCRIBE_ALERT_WEBHOOK_URL when set (such URLs often
embed a secret), else from alerts.webhook_url; JABBERSCRIBE_ALERT_TOKEN, when
set, is sent as a bearer token.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from jabberscribe.config import Config
from jabberscribe.jobs import FAILED, JobStore, iso
from jabberscribe.output import write_atomic

log = logging.getLogger(__name__)

URL_ENV = "JABBERSCRIBE_ALERT_WEBHOOK_URL"
TOKEN_ENV = "JABBERSCRIBE_ALERT_TOKEN"
STATE_FILE = "alerts.json"
RATE_LIMIT = timedelta(hours=1)
TIMEOUT_SECONDS = 10.0
#: How many job keys one alert lists; the rest are counted.
MAX_KEYS = 5

JOB_FAILED = "job_failed"
BACKLOG = "backlog"
LITELLM_DOWN = "litellm_down"
PURGE_ERRORS = "purge_errors"


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    sent, seen = data.get("sent"), data.get("failed_seen")
    return {"sent": sent if isinstance(sent, dict) else {}, "failed_seen": seen if isinstance(seen, list) else []}


class Alerter:
    def __init__(
        self, url: str, state_path: Path, client: httpx.Client | None = None, token: str | None = None
    ) -> None:
        self._url = url
        self._state_path = state_path
        self._client = client or httpx.Client(timeout=TIMEOUT_SECONDS)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._state = _load_state(state_path)

    def send(self, kind: str, message: str, now: datetime) -> bool:
        """POST one alert unless `kind` went out within the last hour. True only when it was delivered."""
        last = self._state["sent"].get(kind)
        if last is not None and now - datetime.fromisoformat(last) < RATE_LIMIT:
            log.info("alert %s suppressed by the rate limit: %s", kind, message)
            return False
        payload = {
            "text": f"JabberScribe {kind}: {message}",
            "source": "jabberscribe",
            "kind": kind,
            "message": message,
            "host": socket.gethostname(),
            "at": iso(now),
        }
        try:
            response = self._client.post(self._url, json=payload, headers=self._headers)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("alert %s not delivered (%s): %s", kind, exc, message)
            return False
        self._state["sent"][kind] = iso(now)
        self._save()
        log.warning("alert %s sent: %s", kind, message)
        return True

    @property
    def failed_seen(self) -> set[str]:
        """Failed jobs already alerted, so each failure is announced once."""
        return set(self._state["failed_seen"])

    def remember_failed(self, keys: set[str]) -> None:
        self._state["failed_seen"] = sorted(keys)
        self._save()

    def _save(self) -> None:
        try:
            write_atomic(self._state_path, json.dumps(self._state, indent=2))
        except OSError as exc:
            log.error("cannot save the alert state %s: %s", self._state_path, exc)


def make_alerter(cfg: Config, client: httpx.Client | None = None) -> Alerter | None:
    """The configured alerter, or None when no webhook URL is set (alerting off)."""
    url = os.environ.get(URL_ENV) or cfg.alerts.webhook_url
    if not url:
        return None
    return Alerter(url, cfg.paths.db_path.parent / STATE_FILE, client, os.environ.get(TOKEN_ENV))


def _listed(keys: list[str]) -> str:
    shown = ", ".join(keys[:MAX_KEYS])
    return shown if len(keys) <= MAX_KEYS else f"{shown} and {len(keys) - MAX_KEYS} more"


def check_jobs(store: JobStore, alerter: Alerter, now: datetime, backlog_minutes: int) -> None:
    """Alert on calls that newly failed and on a backlog older than `backlog_minutes`."""
    failed = {j.job_key for j in store.list_by_status(FAILED) if j.grouped_into is None}
    new = sorted(failed - alerter.failed_seen)
    if new:
        message = f"{len(new)} call(s) failed: {_listed(new)}; see `jabberscribe status`"
        if alerter.send(JOB_FAILED, message, now):
            alerter.remember_failed(failed)
    elif failed != alerter.failed_seen:
        # Retried or purged jobs leave the set, so a second failure of the same call is announced again.
        alerter.remember_failed(failed)

    oldest = store.oldest_pending_created_at()
    if oldest is not None:
        waited = now - datetime.fromisoformat(oldest)
        if waited > timedelta(minutes=backlog_minutes):
            alerter.send(BACKLOG, f"the oldest unfinished call has waited {waited.total_seconds() / 60:.0f} min", now)


def probe_litellm(client: httpx.Client) -> str | None:
    """None when LiteLLM answers its model listing, else what went wrong.

    A short timeout of its own: the shared client waits up to litellm.timeout_seconds (10 min) for a transcription,
    and a hung server must not stall every poll that long.
    """
    try:
        response = client.get("/v1/models", timeout=TIMEOUT_SECONDS)
    except httpx.TransportError as exc:
        return str(exc) or type(exc).__name__
    if response.status_code == 429 or response.status_code >= 500:
        return f"HTTP {response.status_code}"
    return None
```

- [ ] **Step 5: Wire the alerts into the CLI**

The base `_serve` runs scan, settle and process as isolated phases, then the daily purge in its own `try`, then (E2) the heartbeat. The alert checks become one more isolated block after the purge, so they also see jobs the purge just failed; an exception in them is logged and marks the poll failed, like any phase. Every timestamp goes through the base `_utcnow()` seam.

In `jabberscribe/cli.py`, replace:

```python
from jabberscribe.audit import LEGAL_HOLD_RELEASED, LEGAL_HOLD_SET, AuditLog
```

with:

```python
from jabberscribe.alerts import LITELLM_DOWN, PURGE_ERRORS, Alerter, check_jobs, make_alerter, probe_litellm
from jabberscribe.audit import LEGAL_HOLD_RELEASED, LEGAL_HOLD_SET, AuditLog
```

and replace:

```python
def _report_purge(cfg: Config, store: JobStore, audit: AuditLog) -> int:
    result = purge(cfg, store, audit, now=datetime.now(UTC))
```

with:

```python
def _alert_purge(alerter: Alerter | None, errors: tuple[str, ...]) -> None:
    if alerter is not None and errors:
        alerter.send(PURGE_ERRORS, f"{len(errors)} purge problem(s); first: {errors[0]}", _utcnow())


def _report_purge(cfg: Config, store: JobStore, audit: AuditLog) -> int:
    result = purge(cfg, store, audit, now=datetime.now(UTC))
    _alert_purge(make_alerter(cfg), result.errors)
```

and replace:

```python
def _serve(cfg: Config, store: JobStore, audit: AuditLog, once: bool) -> int:
    transcriber, summarizer, tagger = _workers(cfg, make_client(cfg.litellm))
    last_purge: date | None = None
```

with:

```python
def _watch(cfg: Config, store: JobStore, alerter: Alerter, client: httpx.Client) -> None:
    """After a poll: alert on new failures, an old backlog, and an unreachable LiteLLM."""
    now = _utcnow()
    check_jobs(store, alerter, now, cfg.alerts.backlog_minutes)
    problem = probe_litellm(client)
    if problem is not None:
        alerter.send(LITELLM_DOWN, f"{cfg.litellm.base_url}: {problem}", now)


def _serve(cfg: Config, store: JobStore, audit: AuditLog, once: bool) -> int:
    client = make_client(cfg.litellm)
    transcriber, summarizer, tagger = _workers(cfg, client)
    alerter = make_alerter(cfg)
    last_purge: date | None = None
```

and replace:

```python
                for problem in result.errors:
                    log.error("purge: %s", problem)
            except Exception:
                log.exception("purge failed; next attempt tomorrow")
                ok = False
```

with:

```python
                for problem in result.errors:
                    log.error("purge: %s", problem)
                _alert_purge(alerter, result.errors)
            except Exception:
                log.exception("purge failed; next attempt tomorrow")
                ok = False
        if alerter is not None:
            try:
                _watch(cfg, store, alerter, client)
            except Exception:
                log.exception("alert checks failed; continuing")
                ok = False
```

and replace:

```python
        for check in checks:
            print(f"[{'OK ' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        return 0 if all(c.ok for c in checks) else 1
```

with:

```python
        for check in checks:
            print(f"[{'OK ' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        down = [c for c in checks if c.name.startswith("litellm") and not c.ok]
        alerter = make_alerter(cfg)
        if alerter is not None and down:
            details = "; ".join(f"{c.name}: {c.detail}" for c in down)
            alerter.send(LITELLM_DOWN, f"doctor: {details}", _utcnow())
        return 0 if all(c.ok for c in checks) else 1
```

- [ ] **Step 6: Run the new tests**

Run: `python -m pytest tests/test_alerts.py tests/test_config.py tests/test_cli.py -q`
Expected: PASS (71 passed with ffmpeg on PATH). The base CLI tests are unaffected: their config has no `alerts.webhook_url`. If the test environment sets `JABBERSCRIBE_ALERT_WEBHOOK_URL`, unset it first.

- [ ] **Step 7: Run the full suite and lint**

Run: `python -m pytest -q`
Expected: `402 passed, 3 skipped`.

Run: `python -m ruff check .`
Expected: `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add jabberscribe/alerts.py jabberscribe/config.py jabberscribe/cli.py tests/test_alerts.py tests/test_config.py
git commit -m "feat(alerts): rate-limited webhook alerts for failures, backlog, LiteLLM outages and purge errors" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task E7: Shipping config and docs for the extras

**Files:**
- Modify: `config/jabberscribe.yaml`, `README.md`, `docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md` (§2.2), `tests/test_config.py` (append)

**Interfaces:**
- Consumes: `SttConfig.split_channels`, `near_channel` (E3); `CuesConfig` (E4); `AlertsConfig` (E6); CLI `hold`/`unhold` (E1); heartbeat and `status` (E2).
- Produces: a shipped config with every extra setting spelled out, the operator documentation, and the spec status of the extras.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_config.py`:

```python
def test_shipped_config_spells_out_the_extras() -> None:
    shipped = Path(__file__).resolve().parent.parent / "config" / "jabberscribe.yaml"

    cfg = load_config(shipped)

    assert (cfg.stt.split_channels, cfg.stt.near_channel) == (True, 0)
    assert cfg.cues.enabled is True
    assert cfg.cues.model_path == shipped.parent / "models" / "Cnn14_mAP=0.431.pth"
    assert cfg.cues.threshold == 0.3
    assert (cfg.alerts.webhook_url, cfg.alerts.backlog_minutes) == (None, 15)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_config.py::test_shipped_config_spells_out_the_extras -q`
Expected: FAIL with `assert None == WindowsPath('.../config/models/Cnn14_mAP=0.431.pth')`.

- [ ] **Step 3: Extend `config/jabberscribe.yaml`**

In `config/jabberscribe.yaml`, replace:

```yaml
# JabberScribe v2 configuration. Secrets come from environment variables, never here:
#   JABBERSCRIBE_LITELLM_KEY   LiteLLM API key (leave unset if the server has no auth)
```

with:

```yaml
# JabberScribe v2 configuration. Secrets come from environment variables, never here:
#   JABBERSCRIBE_LITELLM_KEY         LiteLLM API key (leave unset if the server has no auth)
#   JABBERSCRIBE_ALERT_WEBHOOK_URL   alert webhook; overrides alerts.webhook_url (such URLs often embed a secret)
#   JABBERSCRIBE_ALERT_TOKEN         optional bearer token for the alert webhook
```

and replace:

```yaml
  db_path: D:/jabberscribe/jabberscribe-v2.db   # a fresh file: v2 refuses databases from other versions
```

with:

```yaml
  db_path: D:/jabberscribe/jabberscribe-v2.db   # v2 upgrades its own older schema in place; refuses any other
```

and replace:

```yaml
stt:
  model: whisper-he
  vocabulary_file: custom_vocabulary.txt    # relative to this file
```

with:

```yaml
stt:
  model: whisper-he
  vocabulary_file: custom_vocabulary.txt    # relative to this file
  # Dual-track calls: transcribe each channel separately (two STT calls) and label who spoke,
  # near end = the recorded line's display name, far end = the other party's (generic labels if missing).
  split_channels: true
  near_channel: 0                           # the channel that carries the recorded line; check your recorder
```

and replace:

```yaml
retention:
  audio_days: 90
  text_days: 365
```

with:

```yaml
retention:
  audio_days: 90
  text_days: 365

# Bracket cues ([צחוק], [מוזיקה], ...) as a separate layer: result.json "cues" and transcript_cues.md;
# transcript.md stays strict verbatim. Needs `pip install .[cues]`, the PANNs checkpoint at model_path,
# and class_labels_indices.csv in %USERPROFILE%\panns_data\ of the service account. Nothing is downloaded:
# without any of them the cues stage is skipped (logged) and calls go out without cues.
cues:
  enabled: true
  model_path: models/Cnn14_mAP=0.431.pth   # relative to this file; models/ is git-ignored
  threshold: 0.3

# Operator alerts: one JSON POST per kind (job_failed, backlog, litellm_down, purge_errors),
# at most once an hour each. Payloads carry job keys and counts, never names or call content.
alerts:
  webhook_url: null                         # null = off; or set JABBERSCRIBE_ALERT_WEBHOOK_URL
  backlog_minutes: 15
```

- [ ] **Step 4: Run it to verify it passes**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS (19 passed).

- [ ] **Step 5: Update `README.md`**

In `README.md`, replace:

```markdown
v2 MVP pipeline. Capture — CUCM media forking to a SIPREC recorder — is
```

with:

```markdown
v2 MVP pipeline plus the first extras: near/far speaker labels, bracket cues,
legal hold, alerts and a heartbeat. Capture — CUCM media forking to a SIPREC recorder — is
```

and replace:

```markdown
jabberscribe --config ... status                       # backlog, retrying and failed jobs (exit 1 if any failure is unresolved)
```

with:

```markdown
jabberscribe --config ... status                       # backlog, latency p50/p95, heartbeat, retrying, failed and held jobs (exit 1 if any failure is unresolved)
```

and replace:

```markdown
jabberscribe --config ... retry --failed               # ... every failed job
```

with:

```markdown
jabberscribe --config ... retry --failed               # ... every failed job
jabberscribe --config ... hold <job_key> --reason "…"  # legal hold: purge keeps the call and its conference
jabberscribe --config ... unhold <job_key>             # release it; retention applies again
```

and replace:

```markdown
Secrets come from the environment, never from config: `JABBERSCRIBE_LITELLM_KEY`.
```

with:

```markdown
Secrets come from the environment, never from config: `JABBERSCRIBE_LITELLM_KEY`,
`JABBERSCRIBE_ALERT_WEBHOOK_URL`, `JABBERSCRIBE_ALERT_TOKEN`.
```

and replace:

```markdown
`out/<YYYY>/<MM>/<call>/` holds `recording.wav`, `transcript.md`,
`summary.md`, `actions.md`, and `result.json` (everything, structured, with
owners, models and stage timings — the contract for the future web app). The
Markdown files are wrapped in `<div dir="rtl">` so Hebrew renders right to left.
```

with:

```markdown
`out/<YYYY>/<MM>/<call>/` holds `recording.wav`, `transcript.md`,
`transcript_cues.md`, `summary.md`, `actions.md`, and `result.json`
(everything, structured, with owners, models, stage timings, speakers and cues
— the contract for the future web app). The Markdown files are wrapped in
`<div dir="rtl">` so Hebrew renders right to left.

**Speaker labels.** A dual-track call is transcribed one channel at a time,
and every line names its speaker: `[00:03:12] מאיר חדד: ...`. The near end
(channel `stt.near_channel`) is the recorded line's `display_name`; the far end
is the other party's on a 1:1 call, `משתתפים` on a conference (no
diarization), and `צד א` / `צד ב` when a name is missing. A silent channel is
not transcribed. Mixed-track calls carry no labels. `stt.split_channels: false`
turns this off (one STT call per call instead of two).

**Bracket cues.** With `pip install .[cues]`, the PANNs checkpoint at
`cues.model_path` and the AudioSet label file in the service account's
`%USERPROFILE%\panns_data\`, a local CPU tagger marks `[צחוק]`, `[רעש רקע]`,
`[מוזיקה]`, `[שקט]` (6 s or longer), `[הקלדה]` and `[צלצול]`. Cues go to
`result.json` (`cues`, `cues_available`) and `transcript_cues.md`;
`transcript.md` stays strict verbatim. Without the extra the stage is skipped
(see `doctor`'s `cues` line) and nothing fails.
```

and replace:

```markdown
Versions") of the share keep deleted audio; align their retention with these
windows.
```

with:

```markdown
Versions") of the share keep deleted audio; align their retention with these
windows.

**Legal hold.** `jabberscribe hold <job_key> --reason "<case>"` exempts a call
— and every copy of its conference — from purge until `jabberscribe unhold`.
A held conference's earlier output is also kept when a longer copy replaces
it. Every hold and release is audited with the OS account that ran it;
`status` lists the held calls.

## Monitoring

`run` rewrites `heartbeat.json` next to the database after every poll (last
poll time, poll count, job counts, age of the oldest unfinished call); alarm on
its age with any file-age monitor. `status` adds the hang-up-to-output latency
of the last 24 hours (p50 and p95; the budget is 15 min) and the heartbeat age.

Set `alerts.webhook_url` (or `JABBERSCRIBE_ALERT_WEBHOOK_URL`) to get a JSON
POST, with a `text` field Teams and Slack render as is, when a call fails,
when a call has waited more than `alerts.backlog_minutes`, when LiteLLM does
not answer (`run` probes it every poll; `doctor` alerts too), or when the
purge cannot delete something. Each kind is sent at most once an hour. Alerts
carry job keys and counts, never names or call content — but a cloud webhook
does take the job keys (call id + extension) outside the network.
```

and replace:

```markdown
[the v2 pipeline plan](docs/superpowers/plans/2026-10-07-v2-pipeline.md) (Tasks 1–7)
and [the hardened plan](docs/superpowers/plans/2026-10-08-v2-hardened-tasks.md) (Tasks 8–19).
```

with:

```markdown
[the v2 pipeline plan](docs/superpowers/plans/2026-10-07-v2-pipeline.md) (Tasks 1–7),
[the hardened plan](docs/superpowers/plans/2026-10-08-v2-hardened-tasks.md) (Tasks 8–19)
and [the extras plan](docs/superpowers/plans/2026-10-08-v2-extras-tasks.md) (Tasks E1–E7).
```

Then track this plan where the README link points, as the base did for the hardened plan: copy this file to `docs/superpowers/plans/2026-10-08-v2-extras-tasks.md`, unchanged.

- [ ] **Step 6: Record the extras' status in the spec**

In `docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md`, replace:

```markdown
4. **Speaker labels** — near/far end from the two channels, or diarization.
```

with:

```markdown
4. **Speaker labels** — near/far end from the two channels, or diarization.

Status (extras plan, 2026-10-08): bracket cues (as a separate layer,
`transcript_cues.md`; `transcript.md` stays strict verbatim) and near/far
speaker labels for dual-track calls are built, together with legal hold,
admin alerts and a heartbeat. Conference diarization, the SSO web app and
email wait for the owner decisions listed in the extras plan's design notes.
```

- [ ] **Step 7: Final verification**

Run: `python -m pytest -q`
Expected: `403 passed, 3 skipped`; the 3 skipped are `tests/test_live.py`.

Run: `python -m ruff check .`
Expected: `All checks passed!`

Run: `pip install -e . ; jabberscribe --config config/jabberscribe.yaml doctor`
Expected: the base `doctor` lines, plus `[OK ] cues: skipped: model checkpoint not found: ...\config\models\Cnn14_mAP=0.431.pth`. The line reads `tagger available` on a host with the extra, the checkpoint and the label file.

- [ ] **Step 8: Commit**

```bash
git add config/jabberscribe.yaml README.md docs/superpowers/specs/2026-10-07-jabberscribe-v2-design.md docs/superpowers/plans/2026-10-08-v2-extras-tasks.md tests/test_config.py
git commit -m "docs: shipping config and README for speaker labels, cues, legal hold, alerts and heartbeat" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Design notes — needs owner decisions

These extras are designed here but **not planned as code**. Each one needs owner decisions first, listed at the end of its section. When the owner has decided, each section becomes its own plan.

### D1. SSO web app (spec §2.2 extra 1)

**What it is:** a read-only web app where each user sees only the calls on their own line. It replaces the company-wide share.

**Shape (buildable once the decisions are made):**
- **A separate process** (`jabberscribe-web`) that never writes to the pipeline's database. The pipeline keeps its single-writer lock, and the web app gets read access to `out_root` and nothing else.
- **A read model** in its own SQLite file (`web.db`), filled by an indexer that rescans `result.json` files by mtime every N seconds. It has three tables:
  - `call(job_key PK, started_at, kind, duration_sec, summary, path)`
  - `call_owner(job_key, user)`, one row per `owners[].user`
  - `web_audit(user, job_key, action, at)`
  
  A row whose `result.json` has disappeared, because retention purged it, is dropped. That keeps the read model inside the 365-day text window.
- **Authorization rule:** user U may open call C only if U ∈ `{o.user for o in C.owners}`. The rule is enforced in the SQL of every query, never by filtering after the fact. It matches on `line_owner.user` captured at record time, as the audit recommends. Matching on the extension breaks on shared lines and on reassigned extensions.
- **Read-access audit** (audit §4a item 6) from day one: every page view, audio playback and download writes a `web_audit` row.
- **Pages:** call list (date, duration, other party, summary preview), call page (summary, action items, transcript with speaker labels, the cue layer as a toggle), and audio playback with HTTP Range while the recording still exists (90 days). Pages are Hebrew and RTL.

**Stack options:**

| Option | For | Against |
|---|---|---|
| **FastAPI + Entra ID OIDC** (authlib or MSAL; Jinja2 server-rendered pages) — **recommended** | Same Python stack and models as the pipeline. MFA and conditional access come from Entra. Standard OIDC code flow. | The browser must reach `login.microsoftonline.com`, and the server fetches Entra's signing keys. That is auth metadata leaving the network, not call content. |
| FastAPI + AD FS (OIDC) | Fully on-prem identity | Needs AD FS to be deployed and maintained |
| ASP.NET Core + Windows auth (Kerberos/IWA) on IIS | Seamless sign-in on domain PCs; native on a Windows host | A second language and runtime to maintain; harder off-domain |
| Reverse-proxy auth header (IIS/ARR → app) | Least app code | Easy to misconfigure into a spoofable header |

**Decisions needed:**
1. Identity provider and protocol: Entra ID OIDC (recommended), AD FS, or IWA.
2. Identity binding: which token claim matches the sidecar's `line_owner.user` (`sAMAccountName` such as `mhadad`, versus the UPN). For Entra, this is `onprem_sam_account_name`, or the local part of `preferred_username`.
3. Privileged access: managers, assistants or delegates on shared lines, compliance (including a legal-hold UI), and admins. Who may see calls that are not on their own line?
4. Whether the company-wide share is revoked at launch, so that only the service account and the web app can read `out_root`, and how existing MVP folders are handled.
5. Hosting: same server or a separate one, TLS certificate, and the public URL. Email (D2) needs that URL.
6. Whether users may download recordings, or only play them back.
7. How long the read-access audit is kept.

### D2. Email notifications (spec §2.2 extra 2)

**Recommendation: link-only.** The message says that a new call summary is ready and links to the call in the web app. A summary in the mailbox would escape both the 365-day retention and the web app's access control. The subject line should be generic, without party names.

**Shape:** a separate notifier loop with an outbox table `notification(job_key, user, status, attempts, next_attempt_at)`, filled when a call reaches DONE (one row per owner). A mail outage then delays notices but never blocks the pipeline. Retries reuse the pipeline's backoff, and each send is audited. The v1 `notify.py` pattern can be ported. It depends on D1 for the link format.

**Decisions needed:**
1. Mail relay and sender identity: an Exchange connector or an SMTP relay, its authentication, and the From address.
2. Recipient resolution: `line_owner.user` → mail address, through AD/LDAP or Graph.
3. Content policy: link-only (recommended) or a summary in the body.
4. Frequency and opt-out: per call or a daily digest, and per-user opt-out.
5. Conferences: notify every participating owner, or the organizer only.

### D3. PII and PCI redaction (audit §4a item 4)

**The problem:** spoken Israeli ID numbers, card numbers and bank accounts end up in a transcript that, in the MVP, is readable company-wide. If any line takes payments, the recordings pull the system into PCI scope.

**Shape options:**
- **A detection pass** after `stt`: validators for Israeli ID numbers (9 digits with a check digit), card numbers (13–19 digits, Luhn) and Israeli bank account formats. They first need number normalization, because Whisper writes spoken numbers both as digits and as Hebrew words (`ארבע חמש שש`).
- **Where redaction applies:**
  - (a) As a layer: `result.json` gains `redactions: [{segment, span, class}]`, the web app renders the redacted view, and the verbatim text stays under tighter access.
  - (b) In place: `transcript.md` loses strict verbatim for those spans.
  - The summary needs the same pass, because the LLM can repeat numbers.
- **Audio muting:** possible with ffmpeg over the matched time ranges. Segment timestamps would mute whole sentences, so word-level timestamps (`timestamp_granularities[]=word`) are needed. Their availability through LiteLLM is unverified (audit M14).

**Decisions needed:**
1. The data classes to redact.
2. Layer or in place.
3. Whether to mute the audio.
4. Whether any recorded line takes card payments. If so, recording those lines at all may need pause/resume or an exclusion, which is a capture-side decision.
5. How many false positives are acceptable.

### D4. Full-text search (audit §4a item 5)

**Shape:** SQLite FTS5 with `tokenize='trigram'` (SQLite ≥ 3.34; the bundled Python 3.12 SQLite qualifies) in the web app's read model. Trigram suits Hebrew: prefixes (ה, ו, ב, ל, מ, ש, כ) attach to words, so word-token search misses `והדוח` when the query is `דוח`, while trigram substring matching finds it. Queries need at least 3 characters.
- **Table:** `call_text(job_key UNINDEXED, start UNINDEXED, text)`, with one row per segment so a hit links to its timestamp. The summary and action items are indexed as extra rows.
- **Owner filtering is mandatory in the SQL:** `... JOIN call_owner USING (job_key) WHERE call_owner.user = :user`.
- **Normalization at both index and query time:** strip niqqud and map final letters (ך→כ, ם→מ, ן→נ, ף→פ, ץ→צ). This is needed because trigram matching is literal.
- **Retention:** index rows go when the call's text is purged. The indexer drops calls whose `result.json` has disappeared. Without that, the index would outlive the 365-day window.

**Decisions needed:**
1. Search scope: transcript only, or the summary and actions too.
2. Whether a compliance role may search across all calls.
3. Whether search queries are audited, which is recommended because queries can reveal intent.

### D5. Calendar enrichment (audit §4a item 8)

**Shape:** match a conference to its Outlook meeting to get the title, organizer and invitees. The match is a meeting whose join number equals the conference bridge, or a meeting in the line owner's calendar overlapping `started_at` ± 10 min. The result is stored as `result.json` `meeting: {title, organizer, invitees}`. The title also gives the summary prompt context. Invitees may serve as naming hints, never as speaker attribution.

**Access options:**
- Microsoft Graph `calendarView` with application permission `Calendars.Read`. That permission is tenant-wide, so it should be narrowed with an `ApplicationAccessPolicy` to the mailboxes of recorded lines.
- EWS against on-prem Exchange.

**Decisions needed:**
1. Whether calendar access is allowed at all, and through Graph or EWS.
2. Which calendars: the organizer's only, or every owner's.
3. Whether private meetings are excluded, which is recommended.
4. Whether invitee lists may be stored in `result.json`, which adds more personal data under the 365-day retention.

### D6. Conference diarization (spec §2.2 extra 4, second half)

**Shape:** the far channel of a conference is the bridge mix, so telling its voices apart needs diarization. pyannote 3.x needs a GPU and a gated Hugging Face licence, and LiteLLM does not serve it. It would run as a separate HTTP service on the GPU host. The output would be speaker turns `דובר 1`, `דובר 2`, … that replace `משתתפים` on the far channel.

**Decisions needed:**
1. The model and its hosting.
2. Whether diarized speakers may ever be mapped to named participants, which is an accuracy and HR risk if a quote is attributed to the wrong person.
3. The added GPU cost.

### Also recommended by the audit, not planned here

- A quality harness: 20–50 real clips, measuring CER/WER, filler retention and action-item precision, before any model or prompt change.
- A vocabulary feedback loop into `custom_vocabulary.txt`.

Neither needs an owner decision. Both are good next plans.

## Coverage

| Requirement (controller brief) | Task |
|---|---|
| E-a speaker labels: per-channel STT, merge by start, sidecar names, fallbacks `צד א`/`צד ב`, conference `משתתפים`, mixed unchanged, `Segment.speaker`, transcript lines, `result.json` speaker, summary prompt, `stt.split_channels`, per-channel hallucination filters (via `LiteLLMTranscriber` per file) | E3 |
| E-b cues as a separate layer: `Tagger` Protocol, optional extra loaded lazily, skipped and logged when absent, fake tagger in tests, `cues` in `result.json`, `transcript_cues.md`, Hebrew labels, stage order `audio, stt, cues, summarize, output`, failures degrade | E4, E5 |
| E-c alerts: webhook POST JSON, `alerts.webhook_url` (None = off), FAILED / backlog > 15 min / LiteLLM down at doctor and run / purge errors, one per kind per hour, secrets via env | E6 |
| E-d legal hold: column, `hold --reason` / `unhold`, purge skips, audited with actor | E1 |
| E-e heartbeat each iteration (last poll, counts, oldest queued age), hang-up→output latency in timings (already in the base; now also stored), `status` p50/p95 over 24 h | E2 |
| Design-only: SSO web app (indexer, `owners[].user` rule, stack options, FastAPI + Entra ID OIDC recommended, decisions), email (link-only), PII redaction, FTS5 trigram, calendar enrichment | Design notes D1–D5 (+ D6) |
| Shipping config, README, spec status | E7 |
