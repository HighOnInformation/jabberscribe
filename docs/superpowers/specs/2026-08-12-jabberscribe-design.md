# JabberScribe — Technical Specification

**Date:** 2026-08-12
**Status:** Approved. Core (§13 steps 1-5) implemented; delivery and lifecycle pending.
**Author:** Design session (brainstorming skill)

---

## 1. Purpose

Transcribe recorded Jabber calls and conferences, then deliver each transcript to
Confluence and to the participants by email — entirely inside the corporate network.

The service turns a recorded conversation into two artifacts:

1. A Confluence Data Center page, one per call, holding the timestamped transcript and
   (optionally) a Hebrew summary with decisions and action items.
2. An email to the participants and a compliance mailbox, carrying the same content.

## 2. Constraints

These are decisions already made, not open questions. Every one of them narrows the
design, and the design below is the direct consequence.

| Constraint | Consequence |
|---|---|
| **On-premises only** — no audio, transcript, or summary may leave the network | STT is local (faster-whisper); summarization is local (Ollama); Confluence is Data Center, not Cloud; mail goes through the internal relay. No cloud STT/LLM provider appears anywhere in the design. |
| **Compliance mode** — every call of every enrolled user is recorded | Legal/HR sign-off and an external-party recording announcement are hard pre-conditions (§3). Publishing is restricted by default and a retention purge is mandatory, not optional. |
| **Pilot volume** — under 10 calls/day, roughly 3 audio-hours | One box, one serial worker, SQLite as the queue. No message broker, no worker pool, no GPU. |
| **Hebrew-primary audio** with mixed-in English technical terms | The Hebrew-specialised `ivrit.ai` Whisper model, a Hebrew glossary prompt, and RTL output. Local Hebrew *summarization* is the weakest link, so it is optional and degradable. |
| **Capture path undecided** | Capture is out of scope behind a file-drop contract (§4). The service is fully buildable and testable before the capture question is answered. |

### 2.1 Non-goals

Deliberately excluded, with reasons — these are not oversights:

- **Recording/capture itself.** Out of scope by design (§4, §5).
- **Neural diarization (pyannote).** Channel separation handles the dual-track case
  exactly and for free; single-track speaker identification is a research problem, and
  pyannote adds a heavy dependency plus a gated model. Revisit only if single-track
  capture wins and speaker labels prove necessary.
- **A web UI.** Confluence *is* the reading surface. A second one would duplicate it.
- **Real-time / live transcription.** Batch, post-call only.
- **Translation.** Transcripts stay in the spoken language.
- **Call analytics** (scoring, objection detection, CRM sync). That is CallSight's job,
  a separate product with a separate purpose.

## 3. Pre-conditions before production use

Compliance-mode recording is a legal posture, not just a feature flag. The following
must be true before the service processes real calls. They are listed here because the
system's safety depends on them and no amount of code substitutes for them.

1. **Written legal/HR authorization** for recording employee calls, and an employee
   notice covering the enrolled population.
2. **An announcement to external parties** that the call is recorded, delivered by the
   telephony platform (CUCM annunciator or equivalent), not by this service.
3. **A named data owner** and an agreed retention period for audio and for transcripts.
4. **A restricted Confluence space** created, with a compliance group defined.
5. **Security sign-off** on the recording host's disk encryption and ACLs.

## 4. System boundary — the drop contract

Capture and transcription meet at a directory. That is the entire coupling.

```
<drop_root>/
  inbox/
    20260812T140311_8f2a1c.wav     # audio, written first (as .part, then renamed)
    20260812T140311_8f2a1c.json    # metadata sidecar, written LAST
  quarantine/                       # malformed inputs, for human inspection
```

**Readiness rule:** the sidecar's appearance signals that the audio is complete. The
recorder writes audio to `<name>.wav.part`, renames it to `<name>.wav`, and only then
writes the sidecar. This gives atomic handoff with no locking and no shared state. A
secondary guard ignores files whose mtime is newer than `watcher.min_age_seconds`,
catching a recorder that died mid-write.

### 4.1 Sidecar schema

```json
{
  "schema_version": 1,
  "call_id": "8f2a1c4e-...",
  "source": "cucm-bib",
  "kind": "call",
  "started_at": "2026-08-12T14:03:11+03:00",
  "ended_at": "2026-08-12T14:16:43+03:00",
  "duration_sec": 812,
  "subject": null,
  "participants": [
    {
      "display_name": "מאיר חדד",
      "uri": "mhadad@corp.local",
      "extension": "1042",
      "email": "mhadad@corp.local",
      "role": "caller"
    }
  ],
  "audio": {
    "tracks": "dual",
    "codec": "pcm_s16le",
    "sample_rate": 8000,
    "channels": 2
  }
}
```

- `call_id` — globally unique, supplied by the capture source. **The deduplication key.**
- `source` — `cucm-bib` | `endpoint-agent` | `xmpp-bot` | `import`. Recorded for audit;
  the service's behavior does not branch on it.
- `kind` — `call` | `conference`. Selects the rendering template only.
- `tracks` — `dual` means near-end and far-end are separate channels, which yields exact
  speaker labels. `mixed` means a single mixed track, for which v1 produces no speaker
  labels (§5.3).
- `participants[].email` — the mail recipient list. May be empty; §9.3 covers the
  fallback.

Unknown fields are ignored; a missing required field sends the pair to `quarantine/`
with a reason file. Required: `call_id`, `started_at`, `duration_sec`, `audio.tracks`.

### 4.2 Capture options — documented, not chosen

Whichever is selected, only the recorder changes; nothing downstream does.

**Option 1 — CUCM Built-in-Bridge / SIPREC media forking.** The supported enterprise
route. The phone or softphone forks media to a recording server, which writes the drop
files.
*Pre-conditions:* Jabber registered to on-prem CUCM (not Webex Calling); CUCM admin able
to configure recording profiles and a recording-enabled device pool; a SIPREC-capable
recorder.
*Strengths:* covers every user and every conference without touching endpoints; survives
client upgrades; the only option that genuinely satisfies "record everything"; carries
authoritative participant metadata from CUCM.
*Weaknesses:* requires telephony-team cooperation and licensing.

**Option 2 — Windows endpoint agent.** A per-machine agent detects an active Jabber call
and records WASAPI loopback (far end) plus microphone (near end) as two channels.
*Pre-conditions:* ability to deploy software to user machines; a reliable call-state
signal.
*Strengths:* no telephony-team dependency; pilots on one machine in a day; naturally
dual-track, so speaker labeling is exact.
*Weaknesses:* per-user rollout; brittle against client updates; misses calls when the
agent is not running — which **conflicts with a record-everything policy**. Acceptable
for a pilot, not for a compliance guarantee.

**Option 3 — XMPP/Jitsi conference bot.** A bot joins the conference as a participant and
records the mixed stream.
*Pre-conditions:* a self-hosted bridge we control.
*Strengths:* clean, supported, no endpoint software.
*Weaknesses:* conferences only; the bot is visible in the roster; irrelevant if the
backend is Cisco-hosted.

**Option 4 — Import from an existing recording platform.** If a call-recording system is
already deployed, a small exporter maps its output into the drop contract. Cheapest path
if it exists; check before building anything.

> **Open action:** determine the telephony backend (on-prem CUCM vs. cloud) and whether a
> recording platform already exists. Option 1 is the presumed target because it is the
> only one consistent with compliance-mode coverage.

## 5. Architecture

One Python service on one on-prem host. A folder watcher feeds a SQLite-backed job
table; a serial worker walks each job through idempotent stages.

```
capture (out of scope)
      │  writes audio + sidecar
      ▼
   inbox/ ──► watcher ──► jobs (SQLite, queued)
                              │
                              ▼
        audio ──► stt ──► [enrich] ──► render ──► publish ──► notify
                                                (Confluence)  (SMTP)
                              │
                              ▼
                       jobs (done) ──► retention (scheduled)
```

Serial, single-worker execution is the correct amount of machinery at this volume, not a
compromise (see §11 for the throughput math).

### 5.1 Modules

Each has one purpose, an explicit dependency set, and is testable in isolation.

| Module | Purpose | Depends on |
|---|---|---|
| `config` | pydantic settings from YAML + env; validates on startup and refuses to run with placeholder secrets | — |
| `jobs` | SQLite job store: rows, stage checkpoints, attempts, status transitions. Data access only, no business logic. **The queue seam.** | sqlite3 |
| `watcher` | Scan `inbox/`, apply readiness + mtime guards, validate sidecar, dedup by `call_id`, enqueue or quarantine | `jobs`, `config` |
| `audio` | ffmpeg → 16 kHz mono float; loudness-normalize; split dual-track into per-speaker channels | ffmpeg |
| `stt` | `Transcriber` protocol; `WhisperLocal` implementation returning timestamped segments | faster-whisper |
| `diarize` | Assign speaker labels from channel identity when `tracks: dual`. When `tracks: mixed`, emit **no** speaker labels (§5.3) | — |
| `enrich` | Optional Hebrew summary / decisions / action items via local Ollama | httpx → Ollama |
| `render` | Transcript + summary → Confluence Storage Format XHTML, and text/HTML mail bodies. Pure functions. | — |
| `publish` | Confluence DC REST: create-or-update page, apply restrictions | httpx |
| `notify` | Compose and send via internal SMTP relay | smtplib |
| `retention` | Scheduled purge of audio and pages per policy, audited | `jobs`, `publish` |
| `audit` | Append-only log of every publish, mail, and deletion | sqlite3 |
| `cli` | `run`, `process <file>`, `replay <call_id>`, `purge`, `doctor` | all |

`process <file>` runs one recording end-to-end without the watcher — the primary
development and diagnostic entry point. `doctor` verifies ffmpeg, the STT model, the
Confluence PAT, and SMTP reachability, so a bad deployment fails loudly at install time
rather than silently at 2 a.m.

### 5.2 STT configuration

Ported from CallSight's benchmarked configuration, which won a Hebrew STT bake-off
against cloud providers:

- Model `ivrit-ai/whisper-large-v3-turbo-ct2` via `faster-whisper` (CTranslate2)
- `compute_type="int8"` on CPU, `float16` when a CUDA device is present
- A Hebrew glossary as `initial_prompt`, from `config/custom_vocabulary.txt`
- `condition_on_previous_text=False` — prevents Hebrew repetition-loop degeneration
- VAD filtering on, to skip silence in hold-heavy calls

The `Transcriber` protocol exists so the model can be swapped without touching the
pipeline. Exactly **one** implementation ships; the protocol is a seam, not a plugin
framework.

### 5.3 Speaker labels: exact or absent

With `tracks: dual`, near-end and far-end arrive on separate channels. Each channel is
transcribed independently and its segments merged by timestamp, so speaker attribution is
a fact derived from channel identity — not an inference. It is exactly correct, costs
nothing, and needs no model.

With `tracks: mixed`, v1 emits **no speaker labels at all** — just timestamped text.
Guessing turns from pause length produces labels that are confidently wrong, which is
worse than none: a reader trusts an attribution that a compliance transcript should never
fabricate. If mixed-track capture wins and attribution turns out to matter, that is the
point to reconsider real diarization — as a measured decision, not a guess baked into v1.

This is also a reason to prefer a dual-track capture source (Options 1 and 2 in §4.2)
where the choice exists.

## 6. Data model

```sql
CREATE TABLE jobs (
  call_id         TEXT PRIMARY KEY,          -- dedup key from the sidecar
  status          TEXT NOT NULL,             -- queued|running|done|failed|needs_review
  stage           TEXT NOT NULL,             -- last completed stage
  audio_path      TEXT NOT NULL,
  sidecar_json    TEXT NOT NULL,             -- verbatim sidecar, for replay
  kind            TEXT NOT NULL,
  started_at      TEXT NOT NULL,
  duration_sec    INTEGER NOT NULL,
  transcript_path TEXT,
  summary_path    TEXT,
  confluence_page_id TEXT,                   -- set once; makes publish idempotent
  notified_at     TEXT,                      -- set once; makes notify at-most-once
  attempts        INTEGER NOT NULL DEFAULT 0,
  last_error      TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE audit_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id    TEXT NOT NULL,
  action     TEXT NOT NULL,      -- published|updated|mailed|purged_audio|purged_page|quarantined
  detail     TEXT,               -- recipients, page id, path — whatever the action needs
  actor      TEXT NOT NULL,      -- service account name
  at         TEXT NOT NULL
);
```

`sidecar_json` is stored verbatim so `replay` never depends on the original file still
existing after retention has purged it.

The `audit_log` is append-only and exists to answer "who saw this call" — a question a
record-everything policy guarantees will be asked.

## 7. Stage semantics

Every stage is idempotent and re-entrant. On restart, the worker resumes at the stage
after `jobs.stage`; it never re-transcribes work already done.

| Stage | Output | On failure |
|---|---|---|
| `audio` | normalized WAV(s) in the work dir | Retry; malformed audio → `quarantine` + `failed` |
| `stt` | transcript JSON (segments with start/end/text/speaker) | Retry with backoff |
| `enrich` | summary JSON | **Degrade** — log, mark summary unavailable, continue |
| `render` | Confluence XHTML + mail bodies | Retry; a render failure is a bug, so it fails loudly |
| `publish` | `confluence_page_id` | Retry; safe because create-or-update is keyed on the stored page id |
| `notify` | `notified_at` | **No auto-retry** — see §7.2 |

### 7.1 Retry policy

Exponential backoff (30 s, 2 min, 10 min), 3 attempts, then `status=failed` and an alert
mail to the ops address. Inputs that can never succeed — bad sidecar, undecodable audio —
skip retries and go straight to `quarantine/` with a `.reason.txt` beside them.

### 7.2 Email is the at-most-once hazard

Confluence is safely retryable: `confluence_page_id` is stored, so a retry updates the
existing page instead of creating a second one.

Email has no equivalent handle. A crash between `send` and the commit of `notified_at`
cannot be distinguished from a crash before the send, and auto-retrying re-mails the
participants. Therefore `notify` runs **last**, and an ambiguous outcome sets
`status=needs_review` for a human to resolve. Under-notifying is recoverable;
spamming participants with duplicate transcripts of their own calls is not.

### 7.3 Degraded publish

If `enrich` fails — Ollama down, out of memory, or unusable Hebrew output — the job
publishes the transcript with a visible "summary unavailable" note and **succeeds**.
Local Hebrew summarization is the known weak link in this design; it must never cost the
user their transcript.

## 8. Security and compliance

- A dedicated service account owns the process. `inbox/`, the audio store, and the
  SQLite database are ACL'd to it alone and live on an encrypted volume.
- The Confluence Personal Access Token and SMTP credentials come from environment
  variables. `config` refuses to start if either is still a placeholder.
- Published pages land under a configured parent in a **restricted space**, and each page
  gets read restrictions limited to the call's participants plus a named compliance
  group. Default-closed: a page nobody was granted stays invisible.
- Audio is **linked, not attached**, to the Confluence page by default
  (`publish.attach_audio: false`). Attaching voice recordings to a wiki multiplies the
  exposure surface for no reading benefit.
- Every publish, mail, and deletion writes an `audit_log` row.
- `retention` purges audio after `retention.audio_days` and pages after
  `retention.page_days`, logging each deletion. Retention is part of v1, not a follow-up:
  a record-everything system without a delete policy is a liability.

## 9. Configuration

`config/jabberscribe.yaml` for behavior, environment variables for secrets.

```yaml
paths:
  drop_root: D:/jabberscribe/drop
  work_dir: D:/jabberscribe/work
  audio_store: D:/jabberscribe/audio

watcher:
  poll_seconds: 30
  min_age_seconds: 15

pipeline:
  stages: [audio, stt]    # see 9.0 -- the activation switch

stt:
  model: ivrit-ai/whisper-large-v3-turbo-ct2
  compute_type: int8
  device: auto            # auto|cpu|cuda
  vocabulary_file: config/custom_vocabulary.txt

enrich:
  enabled: false          # default OFF for v1 — transcript first
  ollama_url: http://localhost:11434
  model: gemma3:12b

confluence:
  base_url: https://wiki.corp.local
  space_key: CALLS
  parent_page_id: "123456"
  compliance_group: callrec-compliance
  attach_audio: false

mail:
  smtp_host: smtp.corp.local
  smtp_port: 25
  from_address: jabberscribe@corp.local
  compliance_bcc: callrec-archive@corp.local
  ops_alert_to: it-ops@corp.local
  fallback_to: it-ops@corp.local

retention:
  audio_days: 90
  page_days: 365
```

Secrets: `JABBERSCRIBE_CONFLUENCE_PAT`, `JABBERSCRIBE_SMTP_USER`,
`JABBERSCRIBE_SMTP_PASSWORD`.

### 9.0 Staged activation

`pipeline.stages` lists the stages this deployment runs, in order. It is the
activation switch for the whole system: the core (`audio`, `stt`) runs on its
own, and each outer capability comes on by adding its stage name once that stage
exists. Nothing else changes — the resume logic in §7 already walks a stage list
rather than a fixed sequence.

Three properties make this safe rather than merely convenient:

- **Validated at load.** Unknown stages, duplicates, and out-of-order lists are
  rejected by config validation, not discovered at runtime.
- **Reported at startup.** A stage that is enabled but not yet implemented
  raises at once and `doctor` flags it, so activating something early tells you
  immediately instead of failing partway through someone's call.
- **Incremental, not retroactive.** Because stages checkpoint per job, adding a
  stage only performs the new work. Calls already transcribed are not
  re-transcribed when publishing is switched on later.

### 9.1 Confluence page shape

One page per call, titled `{date} {time} — {participant names} ({duration})`, under
`parent_page_id`. Body, in order: a metadata table (participants, times, duration,
source, `call_id`); the summary section when available; the timestamped transcript;
a footer linking the audio and naming the retention date. Content is `dir="rtl"` for
Hebrew.

Page identity is `call_id` → `confluence_page_id`, stored in `jobs`. A reprocessed call
updates its page; it never spawns a duplicate.

### 9.2 Email shape

Subject `[Call] {date} {time} — {participants}`. Body carries the summary when available
plus a link to the Confluence page; the full transcript is attached as
`{call_id}.transcript.txt`. To: participant emails from the sidecar. Bcc:
`compliance_bcc`.

### 9.3 Recipient resolution

Participant emails come from the sidecar. When absent — likely with capture sources that
report only an extension — the mail goes to `mail.fallback_to` with the missing
identities named in the body, and the job succeeds. An unresolved recipient must not
strand a transcript. Directory (LDAP) lookup is a deliberate later addition, not v1.

## 10. Testing strategy

The drop contract makes the entire service testable with fixture files and **no Jabber
involvement whatsoever** — the single most valuable property of this design while the
capture question is open.

- **Unit:** audio preprocessing against synthetic tone WAVs generated in-test; renderers
  as golden snapshots; sidecar validation across a table of malformed inputs; job
  state-machine transitions including simulated crash-and-resume at every stage;
  retention date arithmetic.
- **Integration:** full pipeline over a 30-second Hebrew sample, with `aiosmtpd` as a
  fake SMTP sink and a mocked Confluence API. Asserts page body, restriction calls,
  recipients, and audit rows.
- **Idempotency:** run the same `call_id` twice; assert exactly one page, one mail, and
  one job row.
- **STT accuracy:** WER against a short golden clip with a configured threshold, marked
  `slow` so CI never downloads the model.

Definition of done for v1:

1. `jabberscribe process sample.wav` produces a Hebrew transcript, a Confluence page, and
   an email against the test doubles.
2. Killing the worker mid-transcription and restarting resumes without re-transcribing.
3. Reprocessing the same `call_id` produces no duplicate page and no second email.
4. A malformed sidecar lands in `quarantine/` with a reason and no job row.
5. `doctor` reports green against a real Confluence DC instance and SMTP relay.

## 11. Sizing

~3 audio-hours/day. `large-v3-turbo` at int8 on 8 CPU cores runs roughly 1.5–3×
realtime, so a full day's audio takes **1–2 hours of wall clock** — ample headroom on a
plain CPU box, with no GPU required. A CUDA GPU (even an RTX 4060 16 GB, ~15× realtime)
becomes necessary only above roughly 30 audio-hours/day.

Storage: 8 kHz mono PCM is ~58 MB/hour; at 3 hours/day and 90-day retention that is
about **16 GB** of audio at steady state. Transcripts are negligible.

Scaling past pilot is: replace the SQLite job table with a real broker and run multiple
workers. Nothing else in the design changes — that is what the `jobs` seam buys.

## 12. Relationship to CallSight

`C:\Users\meirh\Git\CallSight` solves an adjacent problem (Hebrew sales-call
intelligence) and its STT work is proven. JabberScribe **ports** the pieces that
transfer — the audio preprocessor, the `whisper_local` model configuration, and
transcript post-processing — rather than depending on the package, because CallSight is
coupled to Firestore, Cloud Run, and cloud LLM routing, all of which this project's
on-prem constraint forbids. Roughly 300 lines are copied; the framework is not.

## 13. Implementation order

Each step is independently verifiable, and every step before 7 needs no Confluence, no
SMTP, and no Jabber.

1. `config` + `cli` skeleton + `doctor`
2. `jobs` store and state machine (with crash/resume tests)
3. `watcher` + sidecar validation + quarantine
4. `audio` preprocessing
5. `stt` local Whisper + `diarize` channel-based speaker labels
6. `render` (transcript → XHTML + mail bodies)
7. `publish` Confluence DC, with restrictions and idempotency
8. `notify` SMTP, at-most-once
9. `audit` + `retention`
10. `enrich` local Ollama summary, behind the flag

## 14. Open questions

Tracked, none blocking implementation:

1. **Telephony backend** — on-prem CUCM or cloud? Decides the capture option (§4.2).
2. **Existing recording platform?** If one exists, Option 4 replaces building a recorder.
3. **Retention periods** — the 90/365-day defaults need the data owner's sign-off.
4. **Conference participant metadata** — how complete a roster the chosen capture source
   can supply; affects page titles and recipient resolution.
5. **Hebrew summary quality** from `gemma3:12b` — measurable only against real
   transcripts, which is why `enrich` ships disabled.
