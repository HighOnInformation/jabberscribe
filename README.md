# JabberScribe

On-premises call- and conference-transcription service for Jabber telephony.
Recordings land in a drop folder; JabberScribe transcribes them, optionally summarizes
them, publishes a page per call to Confluence Data Center, and mails the participants.

**Nothing leaves the corporate network.** Speech-to-text runs locally
(faster-whisper + the Hebrew-specialised ivrit.ai model), summarization runs against a
local Ollama model, and both delivery targets — Confluence DC and the SMTP relay — are
internal.

## Status

**End to end works.** A recording dropped into `inbox/` is validated,
normalized, transcribed locally, published as a restricted Confluence page, and
mailed to the participants — with every publish, mail, and deletion audited.

```bash
jabberscribe doctor                          # verify ffmpeg, paths, enabled stages
jabberscribe process call.wav call.json      # one recording, end to end
jabberscribe run                             # watch the inbox
jabberscribe purge                           # delete past-retention audio and pages
```

Secrets come from the environment, never from config:
`JABBERSCRIBE_CONFLUENCE_PAT`, `JABBERSCRIBE_SMTP_USER`,
`JABBERSCRIBE_SMTP_PASSWORD`.

Not built: `enrich`, the local-Ollama Hebrew summary. Everything renders a
"summary unavailable" notice in its place, so it drops in without touching the
rest.

See [the tech spec](docs/superpowers/specs/2026-08-12-jabberscribe-design.md),
[Plan 1](docs/superpowers/plans/2026-08-12-core-transcription.md), and
[Plan 2](docs/superpowers/plans/2026-08-13-delivery-and-lifecycle.md).

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
| `enrich` | Hebrew summary via local Ollama | not built |
| `render` | Confluence XHTML + mail bodies | available |
| `publish` | Confluence page per call | available |
| `notify` | email to participants | available |

A stage that needs configuration will not start without it: enabling `publish`
with no `confluence:` section, or `notify` with no `mail:` section, is rejected
when the config loads.

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
