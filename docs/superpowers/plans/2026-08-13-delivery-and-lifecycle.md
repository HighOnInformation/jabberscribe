# JabberScribe Delivery & Lifecycle — Implementation Plan (Plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the transcript Plan 1 produces and deliver it — a restricted Confluence Data Center page per call, an at-most-once email to the participants, a full audit trail, and a retention purge.

**Architecture:** Four new stages appended to the existing checkpointed pipeline (`render`, `publish`, `notify`) plus a scheduled `purge` command. Each is activated by adding its name to `pipeline.stages`; config for a stage is required only when that stage is enabled. All I/O clients are injectable so every test runs against doubles with no network.

**Tech Stack:** Python 3.12, httpx (Confluence DC REST), stdlib `smtplib`/`email`, SQLite, pytest, ruff.

## Global Constraints

- **On-premises only.** Confluence Data Center and the internal SMTP relay. No Atlassian Cloud, no external mail provider.
- **Python 3.12**, ruff `line-length = 120`, `ruff check . && ruff format --check . && pytest` clean before every commit.
- **Hebrew output, English code.** Page and mail bodies render RTL Hebrew; identifiers, comments, logs, and commits in English.
- **Secrets from the environment only:** `JABBERSCRIBE_CONFLUENCE_PAT`, `JABBERSCRIBE_SMTP_USER`, `JABBERSCRIBE_SMTP_PASSWORD`. Never in YAML, never in git.
- **Publishing is default-closed.** Every page gets read restrictions limited to the call's participants plus the configured compliance group. `attach_audio` defaults to `false`.
- **Every publish, mail, and deletion writes an `audit_log` row.** A record-everything policy must be able to answer "who saw this call".
- **`notify` is at-most-once.** Never auto-retry a send that may have been delivered.
- **No test may touch the network.** Confluence and SMTP are injected doubles.
- **Git:** one branch per task, named in the task. Merge to `main` with `--no-ff` when tests pass.

## Out of scope

`enrich` (the local-Ollama Hebrew summary) is **not** in this plan. Every render
path already accepts `summary=None` and prints "סיכום אינו זמין", which is exactly
the degraded behaviour spec §7.3 requires — so enrich can be added later without
touching anything built here. Judging Hebrew summary quality needs real
transcripts to judge against, and this plan is what produces them.

## File Structure

| File | Responsibility |
|---|---|
| `jabberscribe/render.py` | Transcript + summary → Confluence XHTML, mail subject/body, plain-text transcript. Pure functions. |
| `jabberscribe/audit.py` | Append-only `audit_log` table; `AuditLog.record()` / `.entries()` |
| `jabberscribe/confluence.py` | `ConfluenceClient` — thin REST wrapper (create/update/restrict/url) |
| `jabberscribe/publish.py` | `publish_call()` — idempotent create-or-update plus restrictions |
| `jabberscribe/notify.py` | `notify_call()` — compose and send at-most-once |
| `jabberscribe/retention.py` | `purge()` — delete aged audio and pages, audited |
| `jabberscribe/config.py` | + `ConfluenceConfig`, `MailConfig`, `RetentionConfig`, stage↔config cross-validation |
| `jabberscribe/jobs.py` | + `set_page_id`, `mark_notified`, `set_summary_path`, `list_all` |
| `jabberscribe/pipeline.py` | + `render`/`publish`/`notify` stage handlers |
| `jabberscribe/cli.py` | + `purge` and `replay` subcommands |
| `tests/test_render.py`, `test_audit.py`, `test_publish.py`, `test_notify.py`, `test_retention.py` | One per module |

---

### Task 1: Config for the outer stages, with stage↔config validation

**Branch:** `feat/delivery-config`

**Files:**
- Modify: `jabberscribe/config.py`, `config/jabberscribe.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `ConfluenceConfig`: `base_url: str`, `space_key: str`, `parent_page_id: str`, `compliance_group: str | None = None`, `attach_audio: bool = False`
  - `MailConfig`: `smtp_host: str`, `smtp_port: int = 25`, `use_tls: bool = False`, `from_address: str`, `compliance_bcc: str | None = None`, `ops_alert_to: str | None = None`, `fallback_to: str`
  - `RetentionConfig`: `audio_days: int = 90`, `page_days: int = 365`
  - `Config.confluence: ConfluenceConfig | None = None`, `Config.mail: MailConfig | None = None`, `Config.retention: RetentionConfig = RetentionConfig()`
  - `Config` model validator: enabling `publish` without a `confluence:` section, or `notify` without a `mail:` section, raises — surfaced as `ConfigError`.

- [ ] **Step 1: Branch**

```bash
cd /c/Users/meirh/Git/jabberscribe
git checkout -b feat/delivery-config
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_config.py`:

```python
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
    extra = (
        "\npipeline:\n  stages: [audio, stt, render, publish, notify]\n" + CONFLUENCE_YAML + MAIL_YAML
    )

    cfg = load_config(_write(tmp_path, extra))

    assert cfg.pipeline.stages == ("audio", "stt", "render", "publish", "notify")
    assert cfg.mail.smtp_port == 25
```

- [ ] **Step 3: Run, confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Expected: FAIL — `AttributeError` / no `confluence` attribute.

- [ ] **Step 4: Implement**

In `jabberscribe/config.py`, add `model_validator` to the pydantic import, then add these classes above `Config`:

```python
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
```

Replace the `Config` class body with:

```python
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
```

- [ ] **Step 5: Run tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Expected: all pass.

- [ ] **Step 6: Extend the shipped config template**

Append to `config/jabberscribe.yaml`:

```yaml
# Required once 'publish' is in pipeline.stages.
# PAT comes from JABBERSCRIBE_CONFLUENCE_PAT, never from this file.
# confluence:
#   base_url: https://wiki.corp.local
#   space_key: CALLS
#   parent_page_id: "123456"
#   compliance_group: callrec-compliance
#   attach_audio: false

# Required once 'notify' is in pipeline.stages.
# Credentials come from JABBERSCRIBE_SMTP_USER / JABBERSCRIBE_SMTP_PASSWORD.
# mail:
#   smtp_host: smtp.corp.local
#   smtp_port: 25
#   use_tls: false
#   from_address: jabberscribe@corp.local
#   compliance_bcc: callrec-archive@corp.local
#   ops_alert_to: it-ops@corp.local
#   fallback_to: it-ops@corp.local

retention:
  audio_days: 90
  page_days: 365
```

- [ ] **Step 7: Lint, test, commit, merge**

```bash
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m pytest -q
git add -A && git commit -m "feat(config): delivery config with stage-to-config validation"
git checkout main && git merge --no-ff feat/delivery-config -m "Merge branch 'feat/delivery-config'"
```

---

### Task 2: Audit log

**Branch:** `feat/audit`

**Files:**
- Create: `jabberscribe/audit.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Produces:
  - Action constants: `PUBLISHED = "published"`, `UPDATED = "updated"`, `MAILED = "mailed"`, `PURGED_AUDIO = "purged_audio"`, `PURGED_PAGE = "purged_page"`, `QUARANTINED = "quarantined"`
  - `AuditEntry` frozen dataclass: `call_id: str`, `action: str`, `detail: str`, `actor: str`, `at: str`
  - `AuditLog(db_path: Path, actor: str | None = None)` with `.init_schema()`, `.record(call_id, action, detail="") -> None`, `.entries(call_id: str | None = None) -> list[AuditEntry]`, `.close()`
  - `actor` defaults to the OS account running the service.

- [ ] **Step 1: Branch**

```bash
git checkout -b feat/audit
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_audit.py`:

```python
from pathlib import Path

from jabberscribe.audit import MAILED, PUBLISHED, AuditLog


def _log(tmp_path: Path) -> AuditLog:
    log = AuditLog(tmp_path / "js.db", actor="svc-jabberscribe")
    log.init_schema()
    return log


def test_record_and_read_back(tmp_path: Path) -> None:
    log = _log(tmp_path)

    log.record("c1", PUBLISHED, "page 998")

    entries = log.entries("c1")
    assert len(entries) == 1
    assert entries[0].action == PUBLISHED
    assert entries[0].detail == "page 998"
    assert entries[0].actor == "svc-jabberscribe"
    assert entries[0].at


def test_entries_are_scoped_by_call(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.record("c1", PUBLISHED, "")
    log.record("c2", MAILED, "meir@corp.local")

    assert [e.call_id for e in log.entries("c2")] == ["c2"]
    assert len(log.entries()) == 2


def test_log_is_append_only_and_ordered(tmp_path: Path) -> None:
    log = _log(tmp_path)
    for i in range(3):
        log.record("c1", PUBLISHED, f"attempt {i}")

    assert [e.detail for e in log.entries("c1")] == ["attempt 0", "attempt 1", "attempt 2"]


def test_actor_defaults_to_os_account(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "js.db")
    log.init_schema()

    log.record("c1", PUBLISHED, "")

    assert log.entries("c1")[0].actor


def test_shares_a_database_with_the_job_store(tmp_path: Path) -> None:
    """Audit rows and job rows must live in one file so they cannot diverge."""
    from jabberscribe.jobs import JobStore

    db = tmp_path / "js.db"
    store = JobStore(db)
    store.init_schema()
    log = AuditLog(db)
    log.init_schema()

    store.create(
        call_id="c1",
        audio_path=Path("/a.wav"),
        sidecar_json="{}",
        kind="call",
        started_at="2026-08-12T14:03:11+03:00",
        duration_sec=5,
    )
    log.record("c1", PUBLISHED, "page 1")

    assert store.get("c1") is not None
    assert len(log.entries("c1")) == 1


def test_init_schema_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    AuditLog(db).init_schema()
    log = AuditLog(db)
    log.init_schema()

    log.record("c1", PUBLISHED, "")
    assert len(log.entries("c1")) == 1
```

- [ ] **Step 3: Run, confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_audit.py -q`
Expected: FAIL — no module `jabberscribe.audit`.

- [ ] **Step 4: Implement `jabberscribe/audit.py`**

```python
"""Append-only audit trail.

A record-everything recording policy guarantees somebody will eventually ask who
saw a given call. That answer has to exist, so every publish, mail, and deletion
writes a row here. Rows are never updated or deleted.
"""

from __future__ import annotations

import getpass
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PUBLISHED = "published"
UPDATED = "updated"
MAILED = "mailed"
PURGED_AUDIO = "purged_audio"
PURGED_PAGE = "purged_page"
QUARANTINED = "quarantined"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id TEXT NOT NULL,
  action  TEXT NOT NULL,
  detail  TEXT,
  actor   TEXT NOT NULL,
  at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_call ON audit_log (call_id, id);
"""


@dataclass(frozen=True)
class AuditEntry:
    call_id: str
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

    def record(self, call_id: str, action: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO audit_log (call_id, action, detail, actor, at) VALUES (?, ?, ?, ?, ?)",
            (call_id, action, detail, self._actor, datetime.now(UTC).isoformat(timespec="seconds")),
        )

    def entries(self, call_id: str | None = None) -> list[AuditEntry]:
        if call_id is None:
            rows = self._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM audit_log WHERE call_id = ? ORDER BY id", (call_id,)).fetchall()
        return [
            AuditEntry(r["call_id"], r["action"], r["detail"] or "", r["actor"], r["at"]) for r in rows
        ]
```

- [ ] **Step 5: Run, lint, commit, merge**

```bash
.venv/Scripts/python.exe -m pytest tests/test_audit.py -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m pytest -q
git add -A && git commit -m "feat(audit): append-only trail for publish, mail, and deletion"
git checkout main && git merge --no-ff feat/audit -m "Merge branch 'feat/audit'"
```

---

### Task 3: Render

**Branch:** `feat/render`

**Files:**
- Create: `jabberscribe/render.py`
- Test: `tests/test_render.py`

**Interfaces:**
- Consumes: `sidecar.Sidecar`, `stt.Segment`
- Produces:
  - `load_transcript(path: Path) -> list[Segment]`
  - `format_timestamp(seconds: float) -> str` → `"H:MM:SS"` when an hour or more, else `"M:SS"`
  - `render_page_title(sidecar: Sidecar) -> str`
  - `render_transcript_text(segments: list[Segment]) -> str`
  - `render_confluence_body(sidecar, segments, summary: str | None, audio_note: str | None) -> str`
  - `render_mail(sidecar, summary: str | None, page_url: str | None) -> tuple[str, str]` → `(subject, text_body)`
  - `RenderedCall` frozen dataclass: `title: str`, `body_xhtml: str`, `mail_subject: str`, `mail_body: str`, `transcript_text: str`
  - `render_call(sidecar, segments, summary=None, page_url=None, audio_note=None) -> RenderedCall`

- [ ] **Step 1: Branch**

```bash
git checkout -b feat/render
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_render.py`:

```python
import json
from pathlib import Path

from jabberscribe.render import (
    format_timestamp,
    load_transcript,
    render_call,
    render_confluence_body,
    render_page_title,
    render_transcript_text,
)
from jabberscribe.sidecar import parse_sidecar
from jabberscribe.stt import Segment

SIDECAR_JSON = json.dumps(
    {
        "call_id": "8f2a1c",
        "kind": "call",
        "source": "cucm-bib",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 3672,
        "participants": [
            {"display_name": "מאיר חדד", "email": "meir@corp.local", "extension": "1042", "role": "caller"},
            {"display_name": "Support", "extension": "1099", "role": "callee"},
        ],
        "audio": {"tracks": "dual", "sample_rate": 8000, "channels": 2},
    }
)

SEGMENTS = [
    Segment(0.0, 2.5, "שלום, מה המצב?", "near"),
    Segment(2.9, 6.0, "הכל טוב, תודה", "far"),
]


def _sidecar():
    return parse_sidecar(SIDECAR_JSON)


def test_format_timestamp_short_and_long() -> None:
    assert format_timestamp(0) == "0:00"
    assert format_timestamp(65.4) == "1:05"
    assert format_timestamp(3672) == "1:01:12"


def test_page_title_carries_date_participants_and_duration() -> None:
    title = render_page_title(_sidecar())

    assert "2026-08-12" in title
    assert "14:03" in title
    assert "מאיר חדד" in title
    assert "1:01:12" in title


def test_transcript_text_is_timestamped_and_labelled() -> None:
    text = render_transcript_text(SEGMENTS)

    assert "[0:00] near: שלום, מה המצב?" in text
    assert "[0:02] far: הכל טוב, תודה" in text


def test_transcript_text_omits_speaker_when_absent() -> None:
    text = render_transcript_text([Segment(0.0, 1.0, "שלום")])

    assert text.strip() == "[0:00] שלום"


def test_confluence_body_is_rtl_and_contains_metadata() -> None:
    body = render_confluence_body(_sidecar(), SEGMENTS, summary=None, audio_note=None)

    assert 'dir="rtl"' in body
    assert "8f2a1c" in body
    assert "cucm-bib" in body
    assert "מאיר חדד" in body
    assert "שלום, מה המצב?" in body


def test_confluence_body_escapes_markup_in_content() -> None:
    """Transcribed speech and display names are untrusted text, not markup."""
    hostile = [Segment(0.0, 1.0, "<script>alert('x')</script> & more", "near")]

    body = render_confluence_body(_sidecar(), hostile, summary=None, audio_note=None)

    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&amp; more" in body


def test_confluence_body_notes_a_missing_summary() -> None:
    body = render_confluence_body(_sidecar(), SEGMENTS, summary=None, audio_note=None)

    assert "סיכום אינו זמין" in body


def test_confluence_body_includes_summary_when_present() -> None:
    body = render_confluence_body(_sidecar(), SEGMENTS, summary="הלקוח ביקש הצעת מחיר", audio_note=None)

    assert "הלקוח ביקש הצעת מחיר" in body
    assert "סיכום אינו זמין" not in body


def test_confluence_body_includes_audio_note_when_given() -> None:
    body = render_confluence_body(_sidecar(), SEGMENTS, summary=None, audio_note="D:/audio/8f2a1c.wav")

    assert "D:/audio/8f2a1c.wav" in body


def test_mail_subject_and_body(tmp_path: Path) -> None:
    rendered = render_call(_sidecar(), SEGMENTS, summary="סיכום", page_url="https://wiki/x/998")

    assert rendered.mail_subject.startswith("[Call] 2026-08-12 14:03")
    assert "מאיר חדד" in rendered.mail_subject
    assert "סיכום" in rendered.mail_body
    assert "https://wiki/x/998" in rendered.mail_body


def test_mail_body_states_when_no_page_exists() -> None:
    rendered = render_call(_sidecar(), SEGMENTS, summary=None, page_url=None)

    assert "https://" not in rendered.mail_body


def test_load_transcript_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "transcript.json"
    path.write_text(
        json.dumps(
            {
                "call_id": "8f2a1c",
                "segments": [{"start": 0.0, "end": 1.0, "text": "שלום", "speaker": "near"}],
            }
        ),
        encoding="utf-8",
    )

    segments = load_transcript(path)

    assert segments == [Segment(0.0, 1.0, "שלום", "near")]


def test_render_call_produces_every_artifact() -> None:
    rendered = render_call(_sidecar(), SEGMENTS)

    assert rendered.title
    assert rendered.body_xhtml
    assert rendered.mail_subject
    assert rendered.mail_body
    assert "שלום, מה המצב?" in rendered.transcript_text
```

- [ ] **Step 3: Run, confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_render.py -q`
Expected: FAIL — no module `jabberscribe.render`.

- [ ] **Step 4: Implement `jabberscribe/render.py`**

```python
"""Rendering: transcript data to human-readable artifacts.

Pure functions, no I/O beyond reading the transcript file. Everything here is
snapshot-testable, which matters because these are the only outputs a human ever
reads.

All interpolated content is HTML-escaped. Transcribed speech and directory
display names are untrusted text: a caller who says "less-than script" must not
be able to inject markup into a wiki page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path

from jabberscribe.sidecar import Sidecar
from jabberscribe.stt import Segment

_NO_SUMMARY_HE = "סיכום אינו זמין"
_TRANSCRIPT_HEADING_HE = "תמלול"
_SUMMARY_HEADING_HE = "סיכום"
_DETAILS_HEADING_HE = "פרטי השיחה"


@dataclass(frozen=True)
class RenderedCall:
    title: str
    body_xhtml: str
    mail_subject: str
    mail_body: str
    transcript_text: str


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def load_transcript(path: Path) -> list[Segment]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        Segment(
            start=float(s["start"]),
            end=float(s["end"]),
            text=s["text"],
            speaker=s.get("speaker"),
        )
        for s in payload.get("segments", [])
    ]


def _participant_names(sidecar: Sidecar) -> str:
    names = [p.display_name or p.extension or p.uri or "?" for p in sidecar.participants]
    return ", ".join(names) if names else "unknown"


def _local_time(sidecar: Sidecar) -> datetime | None:
    try:
        return datetime.fromisoformat(sidecar.started_at)
    except ValueError:
        return None


def _date_and_time(sidecar: Sidecar) -> tuple[str, str]:
    stamp = _local_time(sidecar)
    if stamp is None:
        return sidecar.started_at, ""
    return stamp.strftime("%Y-%m-%d"), stamp.strftime("%H:%M")


def render_page_title(sidecar: Sidecar) -> str:
    date, time = _date_and_time(sidecar)
    duration = format_timestamp(sidecar.duration_sec)
    return f"{date} {time} — {_participant_names(sidecar)} ({duration})"


def render_transcript_text(segments: list[Segment]) -> str:
    lines = []
    for seg in segments:
        stamp = format_timestamp(seg.start)
        prefix = f"[{stamp}] {seg.speaker}: " if seg.speaker else f"[{stamp}] "
        lines.append(f"{prefix}{seg.text}")
    return "\n".join(lines) + "\n"


def _metadata_rows(sidecar: Sidecar, audio_note: str | None) -> list[tuple[str, str]]:
    date, time = _date_and_time(sidecar)
    rows = [
        ("מזהה שיחה", sidecar.call_id),
        ("תאריך", f"{date} {time}"),
        ("משך", format_timestamp(sidecar.duration_sec)),
        ("סוג", sidecar.kind),
        ("מקור ההקלטה", sidecar.source),
        ("משתתפים", _participant_names(sidecar)),
    ]
    if sidecar.subject:
        rows.append(("נושא", sidecar.subject))
    if audio_note:
        rows.append(("קובץ אודיו", audio_note))
    return rows


def render_confluence_body(
    sidecar: Sidecar,
    segments: list[Segment],
    summary: str | None,
    audio_note: str | None,
) -> str:
    rows = "".join(
        f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>" for label, value in _metadata_rows(sidecar, audio_note)
    )
    summary_html = f"<p>{escape(summary)}</p>" if summary else f"<p><em>{escape(_NO_SUMMARY_HE)}</em></p>"
    transcript_html = "".join(
        "<p><strong>[{stamp}]{speaker}</strong> {text}</p>".format(
            stamp=format_timestamp(seg.start),
            speaker=f" {escape(seg.speaker)}:" if seg.speaker else "",
            text=escape(seg.text),
        )
        for seg in segments
    )
    return (
        f'<div dir="rtl">'
        f"<h2>{escape(_DETAILS_HEADING_HE)}</h2>"
        f"<table><tbody>{rows}</tbody></table>"
        f"<h2>{escape(_SUMMARY_HEADING_HE)}</h2>{summary_html}"
        f"<h2>{escape(_TRANSCRIPT_HEADING_HE)}</h2>{transcript_html}"
        f"</div>"
    )


def render_mail(sidecar: Sidecar, summary: str | None, page_url: str | None) -> tuple[str, str]:
    date, time = _date_and_time(sidecar)
    subject = f"[Call] {date} {time} — {_participant_names(sidecar)}"
    lines = [
        f"{_DETAILS_HEADING_HE}: {_participant_names(sidecar)}",
        f"{date} {time}, {format_timestamp(sidecar.duration_sec)}",
        "",
        f"{_SUMMARY_HEADING_HE}:",
        summary or _NO_SUMMARY_HE,
        "",
    ]
    if page_url:
        lines.append(page_url)
    lines.append("")
    lines.append("התמלול המלא מצורף.")
    return subject, "\n".join(lines)


def render_call(
    sidecar: Sidecar,
    segments: list[Segment],
    summary: str | None = None,
    page_url: str | None = None,
    audio_note: str | None = None,
) -> RenderedCall:
    subject, body = render_mail(sidecar, summary, page_url)
    return RenderedCall(
        title=render_page_title(sidecar),
        body_xhtml=render_confluence_body(sidecar, segments, summary, audio_note),
        mail_subject=subject,
        mail_body=body,
        transcript_text=render_transcript_text(segments),
    )
```

- [ ] **Step 5: Run, lint, commit, merge**

```bash
.venv/Scripts/python.exe -m pytest tests/test_render.py -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m pytest -q
git add -A && git commit -m "feat(render): Confluence XHTML and mail bodies, all content escaped"
git checkout main && git merge --no-ff feat/render -m "Merge branch 'feat/render'"
```

---

### Task 4: Confluence client and publish stage

**Branch:** `feat/publish`

**Files:**
- Create: `jabberscribe/confluence.py`, `jabberscribe/publish.py`
- Modify: `jabberscribe/jobs.py` (add `set_page_id`), `jabberscribe/pipeline.py`
- Test: `tests/test_publish.py`

**Interfaces:**
- Produces:
  - `ConfluenceError(RuntimeError)`
  - `ConfluenceClient(base_url: str, pat: str, http: httpx.Client | None = None)` with `.create_page(space_key, parent_id, title, body) -> str`, `.update_page(page_id, title, body) -> None`, `.set_read_restrictions(page_id, usernames: list[str], group: str | None) -> None`, `.page_url(page_id) -> str`, `.close()`
  - `publish.publish_call(job, cfg, store, audit, client, rendered) -> str` returning the page id
  - `jobs.JobStore.set_page_id(call_id, page_id) -> None`
- REST endpoints (Confluence Data Center v1 API, `Authorization: Bearer <PAT>`):
  - create: `POST {base}/rest/api/content`
  - read version: `GET {base}/rest/api/content/{id}?expand=version`
  - update: `PUT {base}/rest/api/content/{id}` with `version.number = current + 1`
  - restrict: `PUT {base}/rest/api/content/{id}/restriction`

- [ ] **Step 1: Branch and add the httpx dependency**

```bash
git checkout -b feat/publish
```

In `pyproject.toml`, add `"httpx>=0.27"` to `dependencies`, then:

```bash
.venv/Scripts/python.exe -m pip install -e ".[dev,stt]"
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_publish.py`:

```python
import json
from pathlib import Path

import httpx
import pytest

from jabberscribe.audit import PUBLISHED, UPDATED, AuditLog
from jabberscribe.confluence import ConfluenceClient, ConfluenceError
from jabberscribe.jobs import JobStore
from jabberscribe.publish import publish_call
from jabberscribe.render import render_call
from jabberscribe.sidecar import parse_sidecar

SIDECAR = json.dumps(
    {
        "call_id": "pub1",
        "kind": "call",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 60,
        "participants": [
            {"display_name": "מאיר", "email": "meir@corp.local", "uri": "mhadad@corp.local", "role": "caller"}
        ],
        "audio": {"tracks": "mixed"},
    }
)


class FakeConfluence:
    """Records every call so tests can assert on the REST conversation."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.restrictions: list[dict] = []
        self.next_id = "998"

    def create_page(self, space_key, parent_id, title, body) -> str:
        self.created.append({"space": space_key, "parent": parent_id, "title": title, "body": body})
        return self.next_id

    def update_page(self, page_id, title, body) -> None:
        self.updated.append({"id": page_id, "title": title, "body": body})

    def set_read_restrictions(self, page_id, usernames, group) -> None:
        self.restrictions.append({"id": page_id, "users": usernames, "group": group})

    def page_url(self, page_id) -> str:
        return f"https://wiki.corp.local/pages/viewpage.action?pageId={page_id}"


def _cfg(tmp_path: Path):
    from jabberscribe.config import Config

    return Config(
        paths={
            "drop_root": tmp_path / "drop",
            "work_dir": tmp_path / "work",
            "audio_store": tmp_path / "audio",
            "db_path": tmp_path / "js.db",
        },
        pipeline={"stages": ("audio", "stt", "render", "publish")},
        confluence={
            "base_url": "https://wiki.corp.local",
            "space_key": "CALLS",
            "parent_page_id": "123",
            "compliance_group": "callrec-compliance",
        },
    )


def _job(cfg, store):
    store.create(
        call_id="pub1",
        audio_path=cfg.paths.audio_store / "pub1.wav",
        sidecar_json=SIDECAR,
        kind="call",
        started_at="2026-08-12T14:03:11+03:00",
        duration_sec=60,
    )
    return store.get("pub1")


def _fixture(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path, actor="svc")
    audit.init_schema()
    job = _job(cfg, store)
    rendered = render_call(parse_sidecar(SIDECAR), [])
    return cfg, store, audit, job, rendered


def test_publish_creates_page_and_stores_id(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    client = FakeConfluence()

    page_id = publish_call(job, cfg, store, audit, client, rendered)

    assert page_id == "998"
    assert len(client.created) == 1
    assert client.created[0]["space"] == "CALLS"
    assert client.created[0]["parent"] == "123"
    assert store.get("pub1").confluence_page_id == "998"
    assert [e.action for e in audit.entries("pub1")] == [PUBLISHED]


def test_publish_applies_read_restrictions(tmp_path: Path) -> None:
    """Default-closed: a page nobody was granted stays invisible."""
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    client = FakeConfluence()

    publish_call(job, cfg, store, audit, client, rendered)

    assert client.restrictions[0]["group"] == "callrec-compliance"
    assert "mhadad@corp.local" in client.restrictions[0]["users"]


def test_republish_updates_the_same_page(tmp_path: Path) -> None:
    """Idempotency: a retry must never create a second page."""
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    client = FakeConfluence()
    publish_call(job, cfg, store, audit, client, rendered)

    refreshed = store.get("pub1")
    page_id = publish_call(refreshed, cfg, store, audit, client, rendered)

    assert page_id == "998"
    assert len(client.created) == 1
    assert len(client.updated) == 1
    assert [e.action for e in audit.entries("pub1")] == [PUBLISHED, UPDATED]


def test_publish_records_page_id_even_if_restrictions_fail(tmp_path: Path) -> None:
    """A stored page id we cannot restrict is still a page we must not duplicate."""
    cfg, store, audit, job, rendered = _fixture(tmp_path)

    class Failing(FakeConfluence):
        def set_read_restrictions(self, page_id, usernames, group) -> None:
            raise ConfluenceError("restriction API refused")

    with pytest.raises(ConfluenceError):
        publish_call(job, cfg, store, audit, Failing(), rendered)

    assert store.get("pub1").confluence_page_id == "998"


def test_attach_audio_note_is_absent_by_default(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    client = FakeConfluence()

    publish_call(job, cfg, store, audit, client, rendered)

    assert "pub1.wav" not in client.created[0]["body"]


# --- ConfluenceClient wire-level tests, against a mock transport (no network) ---


def _client(handler) -> ConfluenceClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://wiki.corp.local")
    return ConfluenceClient("https://wiki.corp.local", "PAT123", http=http)


def test_client_create_page_posts_storage_format() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "555"})

    page_id = _client(handler).create_page("CALLS", "123", "T", "<p>hi</p>")

    assert page_id == "555"
    assert seen["url"].endswith("/rest/api/content")
    assert seen["auth"] == "Bearer PAT123"
    assert seen["body"]["body"]["storage"]["representation"] == "storage"
    assert seen["body"]["ancestors"] == [{"id": "123"}]


def test_client_update_page_increments_version() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"id": "555", "version": {"number": 4}})
        return httpx.Response(200, json={"id": "555"})

    _client(handler).update_page("555", "T", "<p>hi</p>")

    assert json.loads(calls[-1].content)["version"] == {"number": 5}


def test_client_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(ConfluenceError, match="403"):
        _client(handler).create_page("CALLS", "123", "T", "<p>x</p>")


def test_client_page_url_is_stable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={})

    assert _client(handler).page_url("998").endswith("pageId=998")
```

- [ ] **Step 3: Run, confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_publish.py -q`
Expected: FAIL — no module `jabberscribe.confluence`.

- [ ] **Step 4: Implement `jabberscribe/confluence.py`**

```python
"""Thin Confluence Data Center REST client.

Data Center, not Cloud: the v1 `/rest/api` surface with a Personal Access Token.
Cloud's v2 API differs enough that this class would need replacing, which is why
it stays thin and behind an interface the publish stage injects.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 30.0


class ConfluenceError(RuntimeError):
    """Confluence rejected a request or was unreachable."""


class ConfluenceClient:
    def __init__(self, base_url: str, pat: str, http: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._owns_http = http is None
        self._http = http or httpx.Client(base_url=self._base, timeout=_TIMEOUT)
        self._headers = {"Authorization": f"Bearer {pat}", "Content-Type": "application/json"}

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._http.request(method, f"{self._base}{path}", headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ConfluenceError(f"{method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise ConfluenceError(f"{method} {path} returned {response.status_code}: {response.text[:300]}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ConfluenceError(f"{method} {path} returned non-JSON body") from exc

    def create_page(self, space_key: str, parent_id: str, title: str, body: str) -> str:
        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "ancestors": [{"id": parent_id}],
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        data = self._request("POST", "/rest/api/content", json=payload)
        page_id = data.get("id")
        if not page_id:
            raise ConfluenceError("create_page response contained no page id")
        log.info("created Confluence page %s", page_id)
        return str(page_id)

    def _current_version(self, page_id: str) -> int:
        data = self._request("GET", f"/rest/api/content/{page_id}?expand=version")
        try:
            return int(data["version"]["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfluenceError(f"cannot read version of page {page_id}") from exc

    def update_page(self, page_id: str, title: str, body: str) -> None:
        payload = {
            "id": page_id,
            "type": "page",
            "title": title,
            "version": {"number": self._current_version(page_id) + 1},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }
        self._request("PUT", f"/rest/api/content/{page_id}", json=payload)
        log.info("updated Confluence page %s", page_id)

    def set_read_restrictions(self, page_id: str, usernames: list[str], group: str | None) -> None:
        restrictions: dict[str, list[dict]] = {"user": [{"type": "known", "username": u} for u in usernames]}
        if group:
            restrictions["group"] = [{"type": "group", "name": group}]
        payload = [{"operation": "read", "restrictions": restrictions}]
        self._request("PUT", f"/rest/api/content/{page_id}/restriction", json=payload)
        log.info("restricted page %s to %d user(s) and group %s", page_id, len(usernames), group)

    def page_url(self, page_id: str) -> str:
        return f"{self._base}/pages/viewpage.action?pageId={page_id}"
```

- [ ] **Step 5: Add `set_page_id` to `jabberscribe/jobs.py`**

Insert after `set_transcript_path`:

```python
    def set_page_id(self, call_id: str, page_id: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET confluence_page_id = ?, updated_at = ? WHERE call_id = ?",
            (page_id, utcnow(), call_id),
        )

    def set_summary_path(self, call_id: str, path: Path) -> None:
        self._conn.execute(
            "UPDATE jobs SET summary_path = ?, updated_at = ? WHERE call_id = ?",
            (str(path), utcnow(), call_id),
        )

    def mark_notified(self, call_id: str, at: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET notified_at = ?, updated_at = ? WHERE call_id = ?",
            (at, utcnow(), call_id),
        )
```

- [ ] **Step 6: Implement `jabberscribe/publish.py`**

```python
"""Publish a call to Confluence, idempotently and default-closed.

Idempotency rests on one stored fact: confluence_page_id. If it is set, the call
already has a page and we update it; a retry can never create a duplicate. The id
is stored the instant the page exists, before restrictions are applied, because a
page we failed to restrict is still a page we must not create twice.
"""

from __future__ import annotations

import logging

from jabberscribe.audit import PUBLISHED, UPDATED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import Job, JobStore
from jabberscribe.render import RenderedCall
from jabberscribe.sidecar import parse_sidecar

log = logging.getLogger(__name__)


def _restriction_users(sidecar) -> list[str]:
    """Confluence usernames for the call's participants.

    The sidecar's `uri` is the closest thing to a directory identity a recorder
    can supply; the local part is used when it looks like an address. Anyone we
    cannot resolve simply is not granted -- the compliance group still has read
    access, so a page is never orphaned.
    """
    users: list[str] = []
    for participant in sidecar.participants:
        identity = participant.uri or participant.email
        if not identity:
            continue
        users.append(identity)
    return users


def publish_call(
    job: Job,
    cfg: Config,
    store: JobStore,
    audit: AuditLog,
    client,
    rendered: RenderedCall,
) -> str:
    if cfg.confluence is None:  # pragma: no cover - config validation prevents this
        raise ValueError("publish stage requires a confluence: config section")

    sidecar = parse_sidecar(job.sidecar_json)
    page_id = job.confluence_page_id

    if page_id:
        client.update_page(page_id, rendered.title, rendered.body_xhtml)
        audit.record(job.call_id, UPDATED, f"page {page_id}")
    else:
        page_id = client.create_page(
            cfg.confluence.space_key,
            cfg.confluence.parent_page_id,
            rendered.title,
            rendered.body_xhtml,
        )
        # Store before restricting: an unrestricted page is a problem, but a
        # duplicated page is a worse one.
        store.set_page_id(job.call_id, page_id)
        audit.record(job.call_id, PUBLISHED, f"page {page_id}")

    client.set_read_restrictions(page_id, _restriction_users(sidecar), cfg.confluence.compliance_group)
    return page_id
```

- [ ] **Step 7: Run tests, lint, commit, merge**

```bash
.venv/Scripts/python.exe -m pytest tests/test_publish.py -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m pytest -q
git add -A && git commit -m "feat(publish): idempotent restricted Confluence DC publishing"
git checkout main && git merge --no-ff feat/publish -m "Merge branch 'feat/publish'"
```

---

### Task 5: Notify — at-most-once email

**Branch:** `feat/notify`

**Files:**
- Create: `jabberscribe/notify.py`
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces:
  - `MailError(RuntimeError)` — retryable; nothing was delivered
  - `AmbiguousSendError(RuntimeError)` — the message may have been delivered; do not retry
  - `build_message(cfg, rendered, to, transcript_name) -> EmailMessage` — Bcc is an envelope recipient only, never a header, so participants never see the archive address; that is why it is not a parameter here
  - `send_via_smtp(cfg, message, recipients) -> None`
  - `notify_call(job, cfg, store, audit, rendered, sender=send_via_smtp) -> bool` — returns False when already notified

**Semantics.** Spec §7.2 says an ambiguous send must not be auto-retried. This refines it usefully: a failure to *connect* delivered nothing and is safely retryable (`MailError`), while a failure raised *during* the send may have delivered and is not (`AmbiguousSendError` → `needs_review`). Distinguishing the two avoids sending every call to a human when the relay is merely down, without ever risking duplicate mail.

- [ ] **Step 1: Branch**

```bash
git checkout -b feat/notify
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_notify.py`:

```python
import json
import smtplib
from pathlib import Path

import pytest

from jabberscribe.audit import MAILED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import NEEDS_REVIEW, JobStore
from jabberscribe.notify import AmbiguousSendError, MailError, build_message, notify_call
from jabberscribe.render import render_call
from jabberscribe.sidecar import parse_sidecar

SIDECAR = json.dumps(
    {
        "call_id": "n1",
        "kind": "call",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 60,
        "participants": [
            {"display_name": "מאיר", "email": "meir@corp.local", "role": "caller"},
            {"display_name": "Support", "email": "support@corp.local", "role": "callee"},
        ],
        "audio": {"tracks": "mixed"},
    }
)

NO_EMAIL_SIDECAR = json.dumps(
    {
        "call_id": "n2",
        "kind": "call",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 60,
        "participants": [{"display_name": "Unknown", "extension": "1099"}],
        "audio": {"tracks": "mixed"},
    }
)


def _cfg(tmp_path: Path) -> Config:
    return Config(
        paths={
            "drop_root": tmp_path / "drop",
            "work_dir": tmp_path / "work",
            "audio_store": tmp_path / "audio",
            "db_path": tmp_path / "js.db",
        },
        pipeline={"stages": ("audio", "stt", "render", "notify")},
        mail={
            "smtp_host": "smtp.corp.local",
            "from_address": "jabberscribe@corp.local",
            "compliance_bcc": "archive@corp.local",
            "fallback_to": "it-ops@corp.local",
        },
    )


def _fixture(tmp_path: Path, sidecar_json: str = SIDECAR):
    cfg = _cfg(tmp_path)
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path, actor="svc")
    audit.init_schema()
    sidecar = parse_sidecar(sidecar_json)
    store.create(
        call_id=sidecar.call_id,
        audio_path=cfg.paths.audio_store / f"{sidecar.call_id}.wav",
        sidecar_json=sidecar_json,
        kind="call",
        started_at=sidecar.started_at,
        duration_sec=60,
    )
    rendered = render_call(sidecar, [], page_url="https://wiki/x/1")
    return cfg, store, audit, store.get(sidecar.call_id), rendered


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def __call__(self, cfg, message, recipients) -> None:
        self.sent.append((message, recipients))


def test_notify_sends_to_participants_and_bcc(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    sender = Recorder()

    assert notify_call(job, cfg, store, audit, rendered, sender=sender) is True

    message, recipients = sender.sent[0]
    assert set(recipients) == {"meir@corp.local", "support@corp.local", "archive@corp.local"}
    assert message["To"] == "meir@corp.local, support@corp.local"
    assert message["From"] == "jabberscribe@corp.local"
    assert store.get("n1").notified_at
    assert [e.action for e in audit.entries("n1")] == [MAILED]


def test_notify_is_at_most_once(tmp_path: Path) -> None:
    """The whole point: never mail participants a second copy."""
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    sender = Recorder()
    notify_call(job, cfg, store, audit, rendered, sender=sender)

    again = notify_call(store.get("n1"), cfg, store, audit, rendered, sender=sender)

    assert again is False
    assert len(sender.sent) == 1


def test_transcript_is_attached(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    sender = Recorder()

    notify_call(job, cfg, store, audit, rendered, sender=sender)

    message, _ = sender.sent[0]
    names = [part.get_filename() for part in message.iter_attachments()]
    assert "n1.transcript.txt" in names


def test_missing_participant_emails_fall_back(tmp_path: Path) -> None:
    """An unresolved recipient must not strand a transcript."""
    cfg, store, audit, job, rendered = _fixture(tmp_path, NO_EMAIL_SIDECAR)
    sender = Recorder()

    assert notify_call(job, cfg, store, audit, rendered, sender=sender) is True

    message, recipients = sender.sent[0]
    assert "it-ops@corp.local" in recipients
    assert "1099" in message.get_body(preferencelist=("plain",)).get_content()


def test_connection_failure_is_retryable_and_does_not_mark_notified(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)

    def refuse(cfg_, message, recipients):
        raise MailError("connection refused")

    with pytest.raises(MailError):
        notify_call(job, cfg, store, audit, rendered, sender=refuse)

    assert store.get("n1").notified_at is None
    assert store.get("n1").status != NEEDS_REVIEW
    assert audit.entries("n1") == []


def test_ambiguous_send_marks_needs_review_and_never_retries(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)

    def disconnect(cfg_, message, recipients):
        raise AmbiguousSendError("server disconnected mid-DATA")

    with pytest.raises(AmbiguousSendError):
        notify_call(job, cfg, store, audit, rendered, sender=disconnect)

    refreshed = store.get("n1")
    assert refreshed.status == NEEDS_REVIEW
    assert refreshed.notified_at is None
    assert "disconnected" in refreshed.last_error


def test_build_message_carries_subject_and_body(tmp_path: Path) -> None:
    cfg, _store, _audit, _job, rendered = _fixture(tmp_path)

    message = build_message(cfg, rendered, ["a@corp.local"], "n1.transcript.txt")

    assert message["Subject"] == rendered.mail_subject
    assert "https://wiki/x/1" in message.get_body(preferencelist=("plain",)).get_content()


def test_smtp_exceptions_map_to_the_right_error_types(monkeypatch, tmp_path: Path) -> None:
    """Connect failures are retryable; failures during send are not."""
    from jabberscribe import notify

    cfg, _store, _audit, _job, rendered = _fixture(tmp_path)
    message = build_message(cfg, rendered, ["a@corp.local"], "n1.transcript.txt")

    class ConnectFails:
        def __init__(self, *a, **k):
            raise smtplib.SMTPConnectError(421, "no")

    monkeypatch.setattr(notify.smtplib, "SMTP", ConnectFails)
    with pytest.raises(MailError):
        notify.send_via_smtp(cfg, message, ["a@corp.local"])

    class SendFails:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_message(self, *a, **k):
            raise smtplib.SMTPServerDisconnected("mid-DATA")

    monkeypatch.setattr(notify.smtplib, "SMTP", SendFails)
    with pytest.raises(AmbiguousSendError):
        notify.send_via_smtp(cfg, message, ["a@corp.local"])
```

- [ ] **Step 3: Run, confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_notify.py -q`
Expected: FAIL — no module `jabberscribe.notify`.

- [ ] **Step 4: Implement `jabberscribe/notify.py`**

```python
"""Email delivery, at most once.

Confluence is safely retryable because a stored page id makes a retry an update.
Email has no such handle: a crash between the send and the commit of notified_at
is indistinguishable from a crash before the send, and guessing wrong re-mails
the participants their own call.

So the failure modes are separated. Failing to connect delivered nothing and is
retryable (MailError). Failing during the send may have delivered, so the job
goes to needs_review for a human (AmbiguousSendError) and is never retried
automatically. Under-notifying is recoverable; duplicate transcripts are not.
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage

from jabberscribe.audit import MAILED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import NEEDS_REVIEW, Job, JobStore
from jabberscribe.render import RenderedCall
from jabberscribe.sidecar import parse_sidecar

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    """Nothing was delivered. Safe to retry."""


class AmbiguousSendError(RuntimeError):
    """The message may have been delivered. Never retry automatically."""


def build_message(cfg: Config, rendered: RenderedCall, to: list[str], transcript_name: str) -> EmailMessage:
    """Compose the message.

    The compliance Bcc is deliberately absent: it is passed to the SMTP envelope
    as an extra recipient instead, so participants never see the archive address.
    """
    if cfg.mail is None:  # pragma: no cover - config validation prevents this
        raise ValueError("notify stage requires a mail: config section")
    message = EmailMessage()
    message["Subject"] = rendered.mail_subject
    message["From"] = cfg.mail.from_address
    message["To"] = ", ".join(to)
    message.set_content(rendered.mail_body)
    message.add_attachment(
        rendered.transcript_text.encode("utf-8"),
        maintype="text",
        subtype="plain",
        filename=transcript_name,
    )
    return message


def send_via_smtp(cfg: Config, message: EmailMessage, recipients: list[str]) -> None:
    if cfg.mail is None:  # pragma: no cover
        raise ValueError("notify stage requires a mail: config section")
    mail = cfg.mail
    try:
        smtp = smtplib.SMTP(mail.smtp_host, mail.smtp_port, timeout=60)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"cannot connect to {mail.smtp_host}:{mail.smtp_port}: {exc}") from exc

    with smtp:
        try:
            if mail.use_tls:
                smtp.starttls()
            user = os.environ.get("JABBERSCRIBE_SMTP_USER")
            password = os.environ.get("JABBERSCRIBE_SMTP_PASSWORD")
            if user and password:
                smtp.login(user, password)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailError(f"SMTP handshake failed: {exc}") from exc
        try:
            smtp.send_message(message, to_addrs=recipients)
        except (OSError, smtplib.SMTPException) as exc:
            raise AmbiguousSendError(f"send may have partially completed: {exc}") from exc


def notify_call(
    job: Job,
    cfg: Config,
    store: JobStore,
    audit: AuditLog,
    rendered: RenderedCall,
    sender=send_via_smtp,
) -> bool:
    """Mail the transcript. Returns False if this call was already notified."""
    if cfg.mail is None:  # pragma: no cover - config validation prevents this
        raise ValueError("notify stage requires a mail: config section")
    if job.notified_at:
        log.info("%s: already notified at %s, skipping", job.call_id, job.notified_at)
        return False

    sidecar = parse_sidecar(job.sidecar_json)
    to = list(sidecar.emails)
    body = rendered.mail_body
    if not to:
        # No resolvable recipient must not strand a transcript.
        to = [cfg.mail.fallback_to]
        # Name AND extension: whoever picks this up needs enough to identify the
        # participant in the directory, and the extension is often the only handle.
        unresolved = ", ".join(
            f"{p.display_name or '?'} (ext {p.extension or '-'})" for p in sidecar.participants
        )
        body = f"{body}\n\nNo participant email could be resolved. Participants: {unresolved}\n"
        rendered = RenderedCall(
            title=rendered.title,
            body_xhtml=rendered.body_xhtml,
            mail_subject=rendered.mail_subject,
            mail_body=body,
            transcript_text=rendered.transcript_text,
        )

    recipients = list(to)
    if cfg.mail.compliance_bcc:
        recipients.append(cfg.mail.compliance_bcc)

    message = build_message(cfg, rendered, to, f"{job.call_id}.transcript.txt")

    try:
        sender(cfg, message, recipients)
    except AmbiguousSendError as exc:
        store.set_status(job.call_id, NEEDS_REVIEW, last_error=str(exc))
        log.error("%s: ambiguous send, needs review: %s", job.call_id, exc)
        raise

    store.mark_notified(job.call_id, datetime.now(UTC).isoformat(timespec="seconds"))
    audit.record(job.call_id, MAILED, ", ".join(recipients))
    log.info("%s: mailed %d recipient(s)", job.call_id, len(recipients))
    return True
```

- [ ] **Step 5: Run, lint, commit, merge**

```bash
.venv/Scripts/python.exe -m pytest tests/test_notify.py -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m pytest -q
git add -A && git commit -m "feat(notify): at-most-once email with retryable vs ambiguous failures"
git checkout main && git merge --no-ff feat/notify -m "Merge branch 'feat/notify'"
```

---

### Task 6: Wire the stages into the pipeline

**Branch:** `feat/delivery-stages`

**Files:**
- Modify: `jabberscribe/pipeline.py`, `jabberscribe/cli.py`
- Test: `tests/test_pipeline_delivery.py`

**Interfaces:**
- `pipeline.IMPLEMENTED_STAGES` becomes `("audio", "stt", "render", "publish", "notify")`
- `pipeline.process_job(job, cfg, store, transcriber, *, audit=None, confluence=None, mail_sender=None) -> Path`
- Stage behaviour: `render` builds a `RenderedCall` and caches nothing on disk; `publish` calls `publish_call`; `notify` calls `notify_call`.

- [ ] **Step 1: Branch**

```bash
git checkout -b feat/delivery-stages
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_pipeline_delivery.py`:

```python
import json
from pathlib import Path

import pytest

from jabberscribe.audit import MAILED, PUBLISHED, AuditLog
from jabberscribe.jobs import DONE, NEEDS_REVIEW, JobStore
from jabberscribe.notify import AmbiguousSendError
from jabberscribe.pipeline import process_job
from jabberscribe.stt import Segment
from jabberscribe.watcher import scan_once


class FakeTranscriber:
    def transcribe(self, wav: Path) -> list[Segment]:
        return [Segment(0.0, 1.5, "שלום")]


class FakeConfluence:
    def __init__(self) -> None:
        self.created, self.updated, self.restrictions = [], [], []

    def create_page(self, space_key, parent_id, title, body) -> str:
        self.created.append(title)
        return "777"

    def update_page(self, page_id, title, body) -> None:
        self.updated.append(page_id)

    def set_read_restrictions(self, page_id, usernames, group) -> None:
        self.restrictions.append(page_id)

    def page_url(self, page_id) -> str:
        return f"https://wiki/x/{page_id}"


def _delivery_cfg(cfg, stages=("audio", "stt", "render", "publish", "notify")):
    """Build a delivery-enabled config.

    Note the explicit model instances: `model_copy(update=...)` does NOT validate,
    so handing it plain dicts would leave dicts in place and every later
    `cfg.confluence.space_key` would fail on a dict.
    """
    from jabberscribe.config import ConfluenceConfig, MailConfig, PipelineConfig

    return cfg.model_copy(
        update={
            "pipeline": PipelineConfig(stages=stages),
            "confluence": ConfluenceConfig(
                base_url="https://wiki.corp.local",
                space_key="CALLS",
                parent_page_id="1",
                compliance_group="grp",
            ),
            "mail": MailConfig(
                smtp_host="smtp.corp.local",
                from_address="js@corp.local",
                fallback_to="ops@corp.local",
            ),
        }
    )


def _setup(cfg, make_wav, make_sidecar):
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path, actor="svc")
    audit.init_schema()
    make_wav(cfg.paths.inbox / "in.wav")
    make_sidecar(cfg.paths.inbox / "in.json", call_id="d1")
    scan_once(cfg, store, min_age_seconds=0)
    return store, audit


def test_full_pipeline_publishes_and_mails(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg)
    store, audit = _setup(staged, make_wav, make_sidecar)
    client, sent = FakeConfluence(), []

    process_job(
        store.claim_next(), staged, store, FakeTranscriber(),
        audit=audit, confluence=client, mail_sender=lambda c, m, r: sent.append(r),
    )

    job = store.get("d1")
    assert job.status == DONE
    assert job.confluence_page_id == "777"
    assert job.notified_at
    assert client.restrictions == ["777"]
    assert len(sent) == 1
    assert {e.action for e in audit.entries("d1")} == {PUBLISHED, MAILED}


def test_rerun_neither_duplicates_page_nor_remails(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg)
    store, audit = _setup(staged, make_wav, make_sidecar)
    client, sent = FakeConfluence(), []
    args = dict(audit=audit, confluence=client, mail_sender=lambda c, m, r: sent.append(r))
    process_job(store.claim_next(), staged, store, FakeTranscriber(), **args)

    store.set_status("d1", "running")
    store.complete_stage("d1", "render")
    process_job(store.get("d1"), staged, store, FakeTranscriber(), **args)

    assert len(client.created) == 1
    assert client.updated == ["777"]
    assert len(sent) == 1


def test_ambiguous_mail_leaves_job_needing_review(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg)
    store, audit = _setup(staged, make_wav, make_sidecar)

    def disconnect(c, m, r):
        raise AmbiguousSendError("mid-DATA")

    with pytest.raises(AmbiguousSendError):
        process_job(
            store.claim_next(), staged, store, FakeTranscriber(),
            audit=audit, confluence=FakeConfluence(), mail_sender=disconnect,
        )

    assert store.get("d1").status == NEEDS_REVIEW


def test_publish_body_contains_the_transcript(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg, stages=("audio", "stt", "render", "publish"))
    store, audit = _setup(staged, make_wav, make_sidecar)

    class Capturing(FakeConfluence):
        body = ""

        def create_page(self, space_key, parent_id, title, body) -> str:
            Capturing.body = body
            return "777"

    process_job(store.claim_next(), staged, store, FakeTranscriber(), audit=audit, confluence=Capturing())

    assert "שלום" in Capturing.body
    assert 'dir="rtl"' in Capturing.body


def test_transcript_json_is_still_written(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg)
    store, audit = _setup(staged, make_wav, make_sidecar)

    path = process_job(
        store.claim_next(), staged, store, FakeTranscriber(),
        audit=audit, confluence=FakeConfluence(), mail_sender=lambda c, m, r: None,
    )

    assert json.loads(path.read_text(encoding="utf-8"))["call_id"] == "d1"
```

- [ ] **Step 3: Run, confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_pipeline_delivery.py -q`
Expected: FAIL — `process_job()` got an unexpected keyword argument `audit`.

- [ ] **Step 4: Rewrite `process_job` in `jabberscribe/pipeline.py`**

Replace the imports and `IMPLEMENTED_STAGES`, then the stage loop:

```python
from jabberscribe.audit import AuditLog
from jabberscribe.notify import notify_call
from jabberscribe.publish import publish_call
from jabberscribe.render import load_transcript, render_call

IMPLEMENTED_STAGES: tuple[str, ...] = ("audio", "stt", "render", "publish", "notify")
```

Replace `process_job` with:

```python
def process_job(
    job: Job,
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    *,
    audit: AuditLog | None = None,
    confluence=None,
    mail_sender=None,
) -> Path:
    """Run `job` from its checkpoint to the end of the configured pipeline.

    Returns the transcript path. Raises on failure after recording the attempt,
    leaving the retry decision to the caller.
    """
    from jabberscribe.sidecar import parse_sidecar

    sidecar = parse_sidecar(job.sidecar_json)
    stages = cfg.pipeline.stages
    work = _work_dir(cfg, job.call_id)
    transcript_path = work / "transcript.json"
    stage = job.stage
    rendered = None
    page_url = None

    try:
        while (stage := next_stage(stage, stages)) is not None:
            log.info("%s: stage %s", job.call_id, stage)
            if stage == "audio":
                prepare(job.audio_path, work, sidecar.tracks)
            elif stage == "stt":
                tracks = prepare(job.audio_path, work, sidecar.tracks)
                per_track = {track.label: transcriber.transcribe(track.path) for track in tracks}
                write_transcript(transcript_path, merge_tracks(per_track), job.call_id)
                store.set_transcript_path(job.call_id, transcript_path)
            elif stage == "render":
                current = store.get(job.call_id) or job
                if current.confluence_page_id and confluence is not None:
                    page_url = confluence.page_url(current.confluence_page_id)
                audio_note = str(job.audio_path) if cfg.confluence and cfg.confluence.attach_audio else None
                rendered = render_call(
                    sidecar,
                    load_transcript(transcript_path),
                    summary=None,
                    page_url=page_url,
                    audio_note=audio_note,
                )
            elif stage == "publish":
                if confluence is None:
                    raise ValueError("publish stage requires a Confluence client")
                if rendered is None:
                    rendered = render_call(sidecar, load_transcript(transcript_path))
                current = store.get(job.call_id) or job
                page_id = publish_call(current, cfg, store, _audit_or_raise(audit), confluence, rendered)
                # Re-render so the mail carries the page link.
                rendered = render_call(
                    sidecar,
                    load_transcript(transcript_path),
                    summary=None,
                    page_url=confluence.page_url(page_id),
                )
            elif stage == "notify":
                if rendered is None:
                    rendered = render_call(sidecar, load_transcript(transcript_path))
                current = store.get(job.call_id) or job
                kwargs = {"sender": mail_sender} if mail_sender is not None else {}
                notify_call(current, cfg, store, _audit_or_raise(audit), rendered, **kwargs)
            else:
                raise StageNotImplementedError(
                    f"stage {stage!r} is enabled in pipeline.stages but not implemented yet;"
                    f" implemented stages are {IMPLEMENTED_STAGES}"
                )
            store.complete_stage(job.call_id, stage)
    except Exception as exc:
        store.record_attempt(job.call_id, str(exc))
        log.exception("%s: stage %s failed", job.call_id, stage)
        raise

    store.set_status(job.call_id, DONE)
    log.info("%s: done", job.call_id)
    return transcript_path
```

An ambiguous send raises out of `notify_call`, so the `DONE` line is unreachable
in that case and needs no guard — `run_once` sees `needs_review` and leaves the
job alone, and `claim_next` will not re-claim it because `needs_review` is not a
runnable status.

Add above `process_job`:

```python
def _audit_or_raise(audit: AuditLog | None) -> AuditLog:
    if audit is None:
        raise ValueError("delivery stages require an AuditLog")
    return audit
```

Add `NEEDS_REVIEW` to the `jabberscribe.jobs` import line.

- [ ] **Step 5: Update `run_once` and the CLI to build the real clients**

Replace `run_once` in `pipeline.py`:

```python
def run_once(
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    *,
    audit: AuditLog | None = None,
    confluence=None,
) -> int:
    """Process every runnable job once. Returns how many were attempted."""
    processed = 0
    while (job := store.claim_next()) is not None:
        processed += 1
        try:
            process_job(job, cfg, store, transcriber, audit=audit, confluence=confluence)
        except Exception:
            refreshed = store.get(job.call_id)
            if refreshed is not None and refreshed.status == NEEDS_REVIEW:
                log.error("%s: left for human review", job.call_id)
            elif refreshed is not None and refreshed.attempts >= MAX_ATTEMPTS:
                store.set_status(job.call_id, FAILED)
                log.error("%s: giving up after %d attempts", job.call_id, refreshed.attempts)
            else:
                store.set_status(job.call_id, QUEUED)
                break
    return processed
```

In `cli.py`, add these imports:

```python
import os

from jabberscribe.audit import AuditLog
from jabberscribe.confluence import ConfluenceClient
```

Add this helper next to `_transcriber`:

```python
def _confluence(cfg: Config) -> ConfluenceClient | None:
    """Build a Confluence client when publishing is enabled.

    The PAT comes from the environment only. Refusing to start without it beats
    discovering it missing when the first call reaches the publish stage.
    """
    if cfg.confluence is None or "publish" not in cfg.pipeline.stages:
        return None
    pat = os.environ.get("JABBERSCRIBE_CONFLUENCE_PAT")
    if not pat:
        raise ConfigError("publish is enabled but JABBERSCRIBE_CONFLUENCE_PAT is not set")
    return ConfluenceClient(cfg.confluence.base_url, pat)


def _audit(cfg: Config) -> AuditLog:
    audit = AuditLog(cfg.paths.db_path)
    audit.init_schema()
    return audit
```

In both the `process` and `run` command bodies, replace the `run_once(cfg, store, _transcriber(cfg))` call with:

```python
        run_once(cfg, store, _transcriber(cfg), audit=_audit(cfg), confluence=_confluence(cfg))
```

Wrap the `_confluence(cfg)` construction so a missing PAT exits 2 rather than raising:

```python
        try:
            confluence = _confluence(cfg)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
```

and pass `confluence=confluence`.

- [ ] **Step 6: Run everything, lint, commit, merge**

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add -A && git commit -m "feat(pipeline): activate render, publish, and notify stages"
git checkout main && git merge --no-ff feat/delivery-stages -m "Merge branch 'feat/delivery-stages'"
```

---

### Task 7: Retention purge

**Branch:** `feat/retention`

**Files:**
- Create: `jabberscribe/retention.py`
- Modify: `jabberscribe/jobs.py` (add `list_all`), `jabberscribe/cli.py` (add `purge`)
- Test: `tests/test_retention.py`

**Interfaces:**
- Produces:
  - `PurgeResult` frozen dataclass: `audio_deleted: tuple[str, ...]`, `pages_deleted: tuple[str, ...]`, `errors: tuple[str, ...]`
  - `purge(cfg, store, audit, now: datetime, confluence=None) -> PurgeResult`
  - `jobs.JobStore.list_all() -> list[Job]`
  - `confluence.ConfluenceClient.delete_page(page_id) -> None`

- [ ] **Step 1: Branch**

```bash
git checkout -b feat/retention
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_retention.py`:

```python
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audit import PURGED_AUDIO, PURGED_PAGE, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import JobStore
from jabberscribe.retention import purge

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


class FakeConfluence:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_page(self, page_id: str) -> None:
        self.deleted.append(page_id)


def _cfg(tmp_path: Path, audio_days: int = 90, page_days: int = 365) -> Config:
    return Config(
        paths={
            "drop_root": tmp_path / "drop",
            "work_dir": tmp_path / "work",
            "audio_store": tmp_path / "audio",
            "db_path": tmp_path / "js.db",
        },
        retention={"audio_days": audio_days, "page_days": page_days},
    )


def _job(cfg, store, call_id: str, age_days: int, page_id: str | None = None) -> Path:
    started = (NOW - timedelta(days=age_days)).isoformat()
    audio = cfg.paths.audio_store / f"{call_id}.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFFfake")
    store.create(
        call_id=call_id,
        audio_path=audio,
        sidecar_json=json.dumps({"call_id": call_id}),
        kind="call",
        started_at=started,
        duration_sec=10,
    )
    if page_id:
        store.set_page_id(call_id, page_id)
    return audio


def _fixture(tmp_path: Path, **kw):
    cfg = _cfg(tmp_path, **kw)
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path, actor="svc")
    audit.init_schema()
    return cfg, store, audit


def test_audio_older_than_retention_is_deleted(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path)
    old = _job(cfg, store, "old", age_days=100)
    fresh = _job(cfg, store, "fresh", age_days=10)

    result = purge(cfg, store, audit, now=NOW)

    assert result.audio_deleted == ("old",)
    assert not old.exists()
    assert fresh.exists()
    assert [e.action for e in audit.entries("old")] == [PURGED_AUDIO]


def test_audio_exactly_at_the_boundary_is_kept(tmp_path: Path) -> None:
    """Retention is 'older than N days', so day N itself survives."""
    cfg, store, audit = _fixture(tmp_path, audio_days=90)
    boundary = _job(cfg, store, "boundary", age_days=90)

    result = purge(cfg, store, audit, now=NOW)

    assert result.audio_deleted == ()
    assert boundary.exists()


def test_pages_older_than_retention_are_deleted(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path, page_days=365)
    _job(cfg, store, "ancient", age_days=400, page_id="900")
    _job(cfg, store, "recent", age_days=10, page_id="901")
    client = FakeConfluence()

    result = purge(cfg, store, audit, now=NOW, confluence=client)

    assert result.pages_deleted == ("ancient",)
    assert client.deleted == ["900"]
    assert PURGED_PAGE in [e.action for e in audit.entries("ancient")]


def test_pages_are_left_alone_without_a_client(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path, page_days=365)
    _job(cfg, store, "ancient", age_days=400, page_id="900")

    result = purge(cfg, store, audit, now=NOW, confluence=None)

    assert result.pages_deleted == ()


def test_already_deleted_audio_is_not_an_error(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path)
    audio = _job(cfg, store, "gone", age_days=100)
    audio.unlink()

    result = purge(cfg, store, audit, now=NOW)

    assert result.errors == ()
    assert result.audio_deleted == ()


def test_a_failing_page_delete_is_reported_not_raised(tmp_path: Path) -> None:
    cfg, store, audit = _fixture(tmp_path, page_days=365)
    _job(cfg, store, "ancient", age_days=400, page_id="900")

    class Failing:
        def delete_page(self, page_id: str) -> None:
            raise RuntimeError("wiki says no")

    result = purge(cfg, store, audit, now=NOW, confluence=Failing())

    assert result.pages_deleted == ()
    assert any("ancient" in e for e in result.errors)


def test_unparseable_started_at_is_skipped_safely(tmp_path: Path) -> None:
    """Never delete on the basis of a date we could not read."""
    cfg, store, audit = _fixture(tmp_path)
    audio = cfg.paths.audio_store / "weird.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF")
    store.create(
        call_id="weird",
        audio_path=audio,
        sidecar_json="{}",
        kind="call",
        started_at="not-a-date",
        duration_sec=1,
    )

    result = purge(cfg, store, audit, now=NOW)

    assert result.audio_deleted == ()
    assert audio.exists()
    assert any("weird" in e for e in result.errors)
```

- [ ] **Step 3: Run, confirm failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_retention.py -q`
Expected: FAIL — no module `jabberscribe.retention`.

- [ ] **Step 4: Add `list_all` to `jobs.py` and `delete_page` to `confluence.py`**

In `jabberscribe/jobs.py`:

```python
    def list_all(self) -> list[Job]:
        rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()
        return [_row_to_job(r) for r in rows]
```

In `jabberscribe/confluence.py`:

```python
    def delete_page(self, page_id: str) -> None:
        self._request("DELETE", f"/rest/api/content/{page_id}")
        log.info("deleted Confluence page %s", page_id)
```

- [ ] **Step 5: Implement `jabberscribe/retention.py`**

```python
"""Retention purge.

A record-everything policy without a delete policy is a liability, so this ships
with v1 rather than after it. Two independent clocks: audio is bulky and
sensitive so it goes early; the transcript page lives longer because it is the
business record.

The rule is "older than N days", so the boundary day itself survives -- and a
row whose start date cannot be parsed is never deleted. Refusing to act on a
date we could not read is the only safe default when the action is deletion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from jabberscribe.audit import PURGED_AUDIO, PURGED_PAGE, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import Job, JobStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PurgeResult:
    audio_deleted: tuple[str, ...] = ()
    pages_deleted: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _age_days(job: Job, now: datetime) -> float | None:
    try:
        started = datetime.fromisoformat(job.started_at)
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=now.tzinfo)
    return (now - started) / timedelta(days=1)


def purge(
    cfg: Config,
    store: JobStore,
    audit: AuditLog,
    now: datetime,
    confluence=None,
) -> PurgeResult:
    """Delete aged audio and pages. Every deletion is audited."""
    audio_deleted: list[str] = []
    pages_deleted: list[str] = []
    errors: list[str] = []

    for job in store.list_all():
        age = _age_days(job, now)
        if age is None:
            errors.append(f"{job.call_id}: cannot parse started_at {job.started_at!r}, skipping")
            continue

        if age > cfg.retention.audio_days:
            path = Path(job.audio_path)
            if path.is_file():
                try:
                    path.unlink()
                except OSError as exc:
                    errors.append(f"{job.call_id}: cannot delete audio: {exc}")
                else:
                    audio_deleted.append(job.call_id)
                    audit.record(job.call_id, PURGED_AUDIO, str(path))

        if job.confluence_page_id and age > cfg.retention.page_days and confluence is not None:
            try:
                confluence.delete_page(job.confluence_page_id)
            except Exception as exc:
                errors.append(f"{job.call_id}: cannot delete page {job.confluence_page_id}: {exc}")
            else:
                pages_deleted.append(job.call_id)
                audit.record(job.call_id, PURGED_PAGE, f"page {job.confluence_page_id}")

    log.info("purge deleted %d audio file(s) and %d page(s)", len(audio_deleted), len(pages_deleted))
    return PurgeResult(tuple(audio_deleted), tuple(pages_deleted), tuple(errors))
```

- [ ] **Step 6: Add the `purge` subcommand to `cli.py`**

Register it in `_build_parser`:

```python
    sub.add_parser("purge", help="delete audio and pages past their retention window")
```

Handle it in `main`, before the final `raise`:

```python
    if args.command == "purge":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        from datetime import UTC, datetime

        from jabberscribe.retention import purge

        store = JobStore(cfg.paths.db_path)
        store.init_schema()
        try:
            confluence = _confluence(cfg)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        result = purge(cfg, store, _audit(cfg), now=datetime.now(UTC), confluence=confluence)
        print(f"audio deleted: {len(result.audio_deleted)}")
        print(f"pages deleted: {len(result.pages_deleted)}")
        for problem in result.errors:
            print(f"  ! {problem}", file=sys.stderr)
        return 1 if result.errors else 0
```

- [ ] **Step 7: Run, lint, commit, merge**

```bash
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff format . && .venv/Scripts/python.exe -m ruff check .
git add -A && git commit -m "feat(retention): audited purge of aged audio and pages"
git checkout main && git merge --no-ff feat/retention -m "Merge branch 'feat/retention'"
```

- [ ] **Step 8: Update README and the shipped config**

In `README.md`, mark `render`, `publish`, and `notify` as available in the stage table, and add `jabberscribe purge` to the command list.

```bash
git add -A && git commit -m "docs(readme): delivery stages available"
```

---

## Definition of Done for Plan 2

1. `pipeline.stages: [audio, stt, render, publish, notify]` runs a call end to end against test doubles.
2. Re-running a completed call creates no second page and sends no second email.
3. Every page carries read restrictions for its participants plus the compliance group.
4. All transcribed content is HTML-escaped in the page body.
5. A connection failure is retried; an ambiguous send sets `needs_review` and is never retried.
6. A call with no resolvable participant email still gets delivered, to `mail.fallback_to`.
7. `jabberscribe purge` deletes past-retention audio and pages, auditing each, and never acts on an unparseable date.
8. Enabling `publish` without a `confluence:` section, or `notify` without `mail:`, is rejected at config load.
9. `ruff check . && ruff format --check . && pytest` clean; no test touches the network.
