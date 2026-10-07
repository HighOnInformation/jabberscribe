# JabberScribe v2 — Technical Specification

**Date:** 2026-10-07
**Status:** Agreed in a grilling session; not yet planned or implemented.
**Supersedes:** [2026-08-12-jabberscribe-design.md](2026-08-12-jabberscribe-design.md)

---

## 1. Summary

JabberScribe records every Jabber call and conference in the company, transcribes it
in strict verbatim through the on-prem LiteLLM server, and produces a Hebrew meeting
summary and action items. The MVP writes these as files in a per-call folder within
~15 minutes of hang-up. A per-user SSO web app, email, non-verbal cues, and speaker
labels are extras that come after.

## 2. Scope

### 2.1 MVP

1. **Capture** — CUCM forks every call's media to an open-source SIPREC recorder,
   which drops dual-channel audio plus metadata into the pipeline.
2. **Transcribe** — Whisper / ivrit.ai via LiteLLM: strict verbatim words with
   timestamps, fillers kept.
3. **Summarize** — Gemma via LiteLLM: Hebrew summary and action items.
4. **Output** — one folder per call: recording, Markdown for humans, JSON for machines.
5. **Lifecycle** — job store with checkpoint/resume, dedup, retention purge, audit.

### 2.2 Extras, in order

1. **SSO web app** — each line owner sees only their own calls. Replaces Confluence.
2. **Email** — notification with a link to the call in the web app.
3. **Bracket cues** — `[laughs]`, `[noise]`, etc. from a local audio-event tagger merged
   by timestamp. Intonation cues are out of reach of this approach.
4. **Speaker labels** — near/far end from the two channels, or diarization.

### 2.3 Non-goals

- Live (in-call) transcription.
- Any processing outside the corporate network.
- Confluence publishing (replaced by the web app).

## 3. Decisions

| Topic | Decision |
|---|---|
| Recording policy | Every call, company-wide, including conferences. Policy is approved (legal/HR). |
| Telephony backend | **Assumed** on-prem CUCM. Unconfirmed — see §10. |
| Capture | CUCM Built-in-Bridge / recording profile → open-source SIPREC recorder (OpenSIPS + rtpengine, or drachtio `siprec-recording-server`). |
| STT | Whisper / ivrit.ai served by the on-prem LiteLLM server. |
| Summary LLM | The Gemma model already deployed on LiteLLM. |
| Output language | Hebrew, English technical terms kept as spoken. |
| Bracket cues | Deferred to extras. |
| Speaker labels | Deferred to extras. Audio is still captured dual-channel. |
| Ownership | A call belongs to the **owner of the recorded line**. A call between two employees belongs to both. |
| Conferences | Processed **once** per conference; every participating line owner gets it. |
| MVP access | Output folder readable company-wide. **Accepted risk** until the SSO web app. |
| Extras access | SSO web app; a user sees only calls on their own line. |
| Volume | < 20 call-hours/day. |
| Latency | Outputs ready ≤ ~15 min after hang-up. |
| Retention | Audio 90 days, text 365 days. |
| Code | New spec; port proven parts of v1 (§9). |

## 4. Architecture

```
CUCM ──SIPREC──► recorder ──► inbox/ ──► watcher ──► jobs (SQLite)
 (BiB forking)   (dual WAV            (wav + json)        │
                  + metadata)                              ▼
                       audio ──► stt ──► summarize ──► write outputs
                      ffmpeg   LiteLLM    LiteLLM       out/<call>/
                               Whisper    Gemma
```

One on-prem service plus the recorder. The recorder and the pipeline meet at the drop
folder (§5), so the pipeline is testable with fixture files before capture works.

| Module | Responsibility |
|---|---|
| `recorder` (external) | SIPREC endpoint; writes dual-channel WAV + sidecar per recorded line |
| `watcher` | Readiness and mtime guards, sidecar validation, quarantine, enqueue |
| `group` | Collapse a conference's per-line copies into one job (§6) |
| `jobs` | SQLite store, per-stage checkpoints, resume after crash |
| `audio` | ffmpeg normalize to 16 kHz mono for STT; keep original for output |
| `stt` | LiteLLM `/v1/audio/transcriptions`, segment timestamps |
| `summarize` | LiteLLM chat completion → summary + action items as JSON |
| `output` | Write `out/<call>/` files |
| `retention` | Purge past-retention audio and text; audit each deletion |
| `audit` | Append-only trail |

## 5. Drop contract

Unchanged from v1: the recorder writes `<name>.wav.part`, renames it to `<name>.wav`,
then writes `<name>.json` last. The sidecar's appearance means the audio is complete.

Sidecar, v2:

```json
{
  "schema_version": 2,
  "call_id": "cucm-gcid-…",
  "conference_id": null,
  "line_owner": { "extension": "1042", "user": "mhadad", "display_name": "מאיר חדד" },
  "parties": [ { "extension": "2210", "display_name": "…" } ],
  "kind": "call",
  "started_at": "2026-10-07T14:03:11+03:00",
  "ended_at": "2026-10-07T14:16:43+03:00",
  "duration_sec": 812,
  "audio": { "tracks": "dual", "sample_rate": 8000, "channels": 2 }
}
```

Required: `call_id`, `line_owner`, `started_at`, `duration_sec`, `audio.tracks`.
`line_owner.user` is the identity the SSO web app will match against; a sidecar without it is
accepted with a warning. `schema_version` must be `2`. `started_at` must carry a UTC offset, and a
sidecar whose `started_at` is more than one day in the future is quarantined (a recorder clock fault
would otherwise create a call retention never purges). `line_owner.extension` is trimmed.
The dedup key is `(call_id, line_owner.extension)`.

`conference_id` SHOULD be unique per conference instance, not per bridge: a recurring or
back-to-back booking of the same Meet-Me number should get a new `conference_id` each time.
Grouping only separates reuses whose time spans do not overlap (§6); a recorder that reuses
the id for overlapping or adjacent meetings merges them.

## 6. Conference grouping

CUCM forks each participating line separately, so a 10-person conference yields up to
10 recordings. Recordings that share a `conference_id` **and whose time spans overlap**
(within `group.overlap_slack_seconds`, default 5s — deliberately not `settle_seconds`) form one
group. A reused `conference_id`, such as a recurring Meet-Me number or the next booking of the
same bridge, therefore starts a new group (the recorder SHOULD still supply a `conference_id`
unique per conference instance, §5):

- Wait until the group is quiet (no new copy for `group.settle_seconds`, default 60s)
  or until its first copy has waited `group.max_wait_seconds` (default 300s).
- Pick one copy to transcribe — the longest, ties broken by earliest start.
- Write one output and record every member's `line_owner` as an owner of it.
- A copy arriving after the group is processed is attached as an owner without
  reprocessing — **unless** it is longer and ends more than `group.settle_seconds`
  after the processed copy. Then it replaces the processed copy: it is transcribed
  from scratch, the earlier copy's text outputs are deleted (audited as `superseded`),
  and the group still ends with one output.
- A copy that bridges several groups merges them; the longest copy wins.
- A copy that failed on its own recording (in the `audio` or `stt` stage) does not keep its
  group: the failed primary is merged into the winner and the next-longest copy is processed
  instead. A failure in `summarize` or `output` is not the copy's fault — every copy would
  fail the same way — so the primary stays `failed` with its members, for
  `jabberscribe retry` once the cause is fixed. Retrying a group whose failed copies were
  handed over restarts it from the longest failed copy. `status` lists handed-over failed
  copies too, and exits 1 while any failed job is not superseded by a `done` primary.
- Superseded outputs are deleted before re-election; if a file is locked, re-election waits for the next poll.

**Deviation pending owner sign-off.** The replacement rule means a meeting can be
transcribed more than once (extra GPU time). Without it, the first participant to hang
up decides the transcript, and everyone gets a truncated copy of a longer meeting.

1:1 calls between two employees are **not** grouped: each line owner gets their own
copy. This keeps ownership simple; the duplicate cost is small at this volume.

## 7. Outputs

`out/<YYYY>/<MM>/<call_key>/`:

| File | Content |
|---|---|
| `recording.wav` | Original dual-channel audio |
| `transcript.md` | Strict verbatim, one line per segment: `[00:03:12] text` |
| `summary.md` | Hebrew summary |
| `actions.md` | Action items table |
| `result.json` | Everything above, structured, plus owners, models used, and timings. The web app extra reads only this. |

**Transcript rules.** Words as spoken, including fillers (אה, אממ), false starts, and
repetitions — no cleanup. Whisper tends to drop fillers; the STT request carries a
prompt that shows fillers to bias it toward keeping them. This is best effort, not a
guarantee. Segments Whisper most likely invented are dropped: repetition loops
(`compression_ratio` > 2.4), silence (`no_speech_prob` > 0.6 with `avg_logprob` < -1),
and echoes of the prompt.

**Action item rules.** Each item has `task`, `owner`, `due`, and `source_ts` (the
transcript timestamp it came from). `owner` and `due` are filled **only** when stated
in the call; otherwise they are empty, never guessed. The LLM returns JSON validated
against a schema; invalid output is retried once, then the job completes with
"summary unavailable" rather than failing — the transcript must never be lost to a
summary failure. The same holds when the chat route rejects the input itself (HTTP 400,
413 or 422, e.g. the context window exceeded): no retry, "summary unavailable". The
reason is recorded in `result.json` as `summary_error` (null when a summary exists).
Transcripts longer than `summary.max_chunk_chars` are summarized per chunk; the chunk
summaries are merged in batches that fit `max_chunk_chars`, repeatedly, until one is left,
so the merge prompt is bounded too.

## 8. Failure handling

| Stage | On failure |
|---|---|
| `audio` | Retry with backoff; after 3 attempts (e.g. corrupt input) → `failed` for a human to inspect and `jabberscribe retry` |
| `stt` | LiteLLM down or overloaded (transport error, 429, 5xx): retry with backoff (30 s doubling, at most 30 min) for as long as it lasts, counted separately from attempts; the job stays queued. Other errors (4xx, bad response): `failed` after 3 attempts |
| `summarize` | Invalid model output: retry once, then degrade to "summary unavailable" and continue. HTTP 400, 413 or 422 (input rejected, e.g. context length): degrade at once, reason in `summary_error`. LiteLLM down or overloaded: retry with backoff, as for `stt`. Other 4xx (401/403 bad key, 404 wrong model name): `failed` after 3 attempts, loudly — `doctor` catches these at install |
| `output` | Retry with backoff; failure is a bug and fails loudly (`failed` after 3 attempts) |

Every stage is checkpointed; a crash resumes at the first incomplete stage.

## 9. Reuse from v1

Port: `jobs` (checkpoint/resume), `watcher` (readiness, quarantine), `sidecar`
(validation pattern, new schema), `audio` (ffmpeg), `retention`, `audit`, `config`,
and the `doctor` command.
Drop: local faster-whisper `stt`, `diarize`, `render`, `publish`/`confluence`, `notify`.

## 10. Risks and open questions

1. **CUCM is assumed, not confirmed.** If Jabber registers to Webex Calling, CUCM
   forking is unavailable and capture must be redesigned. Confirm first.
2. **CUCM recording protocol.** Verify that the installed CUCM version forks media in a
   form the chosen open-source recorder accepts (standard SIPREC vs. Cisco's own
   recording SIP with `x-cisco` metadata), and which metadata it carries.
3. **Conference ID.** Grouping (§6) needs a shared conference identifier in the
   recording metadata. If CUCM does not supply one, fall back to processing each copy.
4. **Company-wide read access in the MVP.** Real calls — including HR and management —
   readable by every employee until the SSO web app ships. Accepted by the project
   owner on 2026-10-07; the approved recording policy should be checked against it.
5. **Hebrew summary quality** from the deployed Gemma model depends on its size;
   measure on real transcripts early.
6. **LiteLLM Whisper backend.** Confirm the transcription route returns segment
   timestamps (`verbose_json`) and accepts a prompt.
7. **Undecided, not blocking:** web app stack and SSO provider; host OS.

## 11. Testing

- **Unit:** sidecar validation table, conference grouping (out-of-order and late
  copies), stage checkpoints with simulated crashes, retention arithmetic, action-item
  JSON validation.
- **Integration:** fixture WAV + sidecar through the pipeline against a fake LiteLLM
  (canned transcription and chat responses); assert all output files.
- **Idempotency:** same `(call_id, line_owner)` twice → one job, one output.
- **Live (marked `slow`):** a short real Hebrew clip against the actual LiteLLM server.
- **Capture:** a SIPp scenario sending a SIPREC call to the recorder in a lab, before
  touching production CUCM.

**MVP done when:** a real company call, forked by CUCM, produces transcript, summary,
and action items in `out/` within 15 minutes, and a conference produces exactly one
output owned by every participating line.

## 12. Implementation order

1. Port the v1 core (jobs, watcher, audio, config, doctor) with the v2 sidecar.
2. `stt` via LiteLLM.
3. `summarize` via LiteLLM.
4. `output` writer.
5. Conference grouping.
6. Retention and audit.
7. SIPREC recorder in a lab with SIPp, then against CUCM.
