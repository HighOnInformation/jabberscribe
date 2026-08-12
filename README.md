# JabberScribe

On-premises call- and conference-transcription service for Jabber telephony.
Recordings land in a drop folder; JabberScribe transcribes them, optionally summarizes
them, publishes a page per call to Confluence Data Center, and mails the participants.

**Nothing leaves the corporate network.** Speech-to-text runs locally
(faster-whisper + the Hebrew-specialised ivrit.ai model), summarization runs against a
local Ollama model, and both delivery targets — Confluence DC and the SMTP relay — are
internal.

## Status

**Core transcription works.** A recording dropped into `inbox/` is validated,
normalized, transcribed locally, and written out as a timestamped transcript.
Delivery to Confluence and email is Plan 2.

```bash
jabberscribe doctor                          # verify ffmpeg, paths, enabled stages
jabberscribe process call.wav call.json      # one recording, end to end
jabberscribe run                             # watch the inbox
```

See [the tech spec](docs/superpowers/specs/2026-08-12-jabberscribe-design.md) and
[Plan 1](docs/superpowers/plans/2026-08-12-core-transcription.md).

## Turning capabilities on

Stages are configuration, not code. `pipeline.stages` in
`config/jabberscribe.yaml` lists what this deployment runs, in order:

```yaml
pipeline:
  stages: [audio, stt]        # core: recording -> transcript (available now)
```

Each outer capability switches on by adding its stage name once it exists:

| Stage | Adds | Status |
|---|---|---|
| `audio` | ffmpeg normalize, channel split | available |
| `stt` | local Hebrew transcription | available |
| `enrich` | Hebrew summary via local Ollama | Plan 2 |
| `render` | Confluence XHTML + mail bodies | Plan 2 |
| `publish` | Confluence page per call | Plan 2 |
| `notify` | email to participants | Plan 2 |

Enabling a stage that does not exist yet is reported by `jabberscribe doctor`
at startup rather than failing partway through someone's call. Stages are
checkpointed per job, so adding one only does the new work — already-transcribed
calls are not re-transcribed.

## Scope boundary

JabberScribe does **not** record calls. Capture is deliberately out of scope: any
recorder that writes an audio file plus a JSON metadata sidecar into the drop folder
satisfies the contract. The spec documents three capture options (CUCM Built-in-Bridge
media forking, a Windows endpoint agent, an XMPP/Jitsi conference bot) without
committing to one.

## Language

Hebrew-primary audio with mixed-in English technical terms. User-facing output is
Hebrew (RTL); code, comments, commits, and logs are English.
