# JabberScribe

On-premises call- and conference-transcription service for Jabber telephony.
Recordings land in a drop folder; JabberScribe transcribes them, optionally summarizes
them, publishes a page per call to Confluence Data Center, and mails the participants.

**Nothing leaves the corporate network.** Speech-to-text runs locally
(faster-whisper + the Hebrew-specialised ivrit.ai model), summarization runs against a
local Ollama model, and both delivery targets — Confluence DC and the SMTP relay — are
internal.

## Status

Design phase. See [the tech spec](docs/superpowers/specs/2026-08-12-jabberscribe-design.md).

## Scope boundary

JabberScribe does **not** record calls. Capture is deliberately out of scope: any
recorder that writes an audio file plus a JSON metadata sidecar into the drop folder
satisfies the contract. The spec documents three capture options (CUCM Built-in-Bridge
media forking, a Windows endpoint agent, an XMPP/Jitsi conference bot) without
committing to one.

## Language

Hebrew-primary audio with mixed-in English technical terms. User-facing output is
Hebrew (RTL); code, comments, commits, and logs are English.
