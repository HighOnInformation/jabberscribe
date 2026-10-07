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
jabberscribe --config ... process call.wav call.json   # one recording, end to end (only that job; exit 0 only if it is DONE)
jabberscribe --config ... run                          # watch the inbox; purges once a day
jabberscribe --config ... purge                        # delete past-retention audio and text now
jabberscribe --config ... status                       # backlog, retrying and failed jobs (exit 1 if any failure is unresolved)
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
backoff (30 s doubling, at most 30 min) for as long as the outage lasts; such
transient failures (transport errors, HTTP 5xx/429, locked output files) are
counted separately and never fail a job. Other errors fail a job after 3 attempts. Check `jabberscribe status` and use
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
longer copy of the meeting arrives after a shorter one was processed (it ends
more than `group.settle_seconds` later), the longer copy is transcribed and
replaces the earlier output (audited as `superseded`). Copies are grouped by
`conference_id` and overlapping time spans (within `group.overlap_slack_seconds`,
default 5 s, so back-to-back meetings on one bridge stay apart); a copy that bridges several groups
merges them, and the longest copy wins.

**Access:** in the MVP the output folder is readable company-wide. This is an
accepted risk until the SSO web app ships; restrict the share's ACL if that
changes.

## Retention

Audio 90 days, text 365 days, counted from the later of the call's start and
its arrival. The daily purge also removes leftover STT copies in `work/`,
quarantined pairs and inbox orphans older than 90 days, and, past 365 days,
replaces a job row's stored sidecar (parties, names, users) with `{}`. The row
itself stays: its `job_key` (call id and extension), `conference_id`, paths,
start time, duration and last error are kept, as are the `audit_log` rows
naming the `job_key`. A job still active after the audio window
has its audio deleted and is marked failed (audited); work folders and STT
copies of active jobs are never swept. Every deletion is audited in the
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
and [the hardened plan](docs/superpowers/plans/2026-10-08-v2-hardened-tasks.md) (Tasks 8–19).

## Language

Hebrew-primary audio with mixed-in English technical terms. User-facing output
is Hebrew (RTL); code, comments, commits, logs and CLI output are English.
