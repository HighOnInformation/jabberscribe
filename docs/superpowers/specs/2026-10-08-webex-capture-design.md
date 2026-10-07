# JabberScribe capture: Webex Calling exporter

Date: 2026-10-08. Status: first implementation built against documented API shapes; **not yet
run against a real tenant**. Derived from the research draft
`.superpowers/webex/webex-capture-design.md`.

Legend: **[V]** verified on a fetched primary page; **[P]** partial (search snippet or SDK
source); **[U]** unverified, must be tested against a real tenant.

> **Jabber does not register to multitenant Webex Calling.** In the cloud case the calling
> client is the Webex App (or a desk phone). Recording happens in the Webex Calling platform,
> so this exporter covers Webex Calling recordings **regardless of client**. Jabber or Webex
> App registered to an on-prem or hosted UCM (including Webex Calling Dedicated Instance) is
> the CUCM case and is served by the SIPREC/BiB capture, not by this exporter.
> https://www.cisco.com/c/en/us/td/docs/voice_ip_comm/cloudCollaboration/wbxt/ucmcalling/unified-cm-wbx-teams-deployment-guide/unified-cm-wbx-teams-deployment-guide_chapter_010.html

## 1. What it does

A polling exporter, `python -m jabberscribe.capture.webex --config config/webex.yaml [--once]`,
that turns Webex Calling native recordings into drop pairs the existing watcher consumes
unchanged (spec v2 §5):

```
Webex cloud --list/details/metadata--> exporter (outbound HTTPS only) --> inbox/<job_key>.wav + .json
                                         |-- ledger (own SQLite file, not the jobs DB)
                                         `-- optional delete after export (default off)
```

Each poll:

1. List recordings created in the last `lookback_hours` (default 48, max 720) with
   `status=available`, splitting the range into windows of at most 30 days and following the
   `Link: rel="next"` header through every page.
2. Skip recordings the ledger marks done, or failed `max_attempts` times (parked).
3. For each remaining recording: details (fresh temporary audio link) → metadata →
   stream the MP3 into `work_dir` → ffprobe → ffmpeg to `<job_key>.wav.part` in the inbox
   (channel layout and sample rate kept, PCM s16le) → rename to `.wav` → write the sidecar
   `<job_key>.json.part` → rename to `.json` **last** → ledger done.
4. Optionally delete the Webex copy (`delete_after_export: true`).

Polling, not webhooks: webhook targets must be public HTTPS, and the service sits on-prem
behind NAT. A rolling lookback window plus the ledger replaces a cursor: a recording that
fails is retried on the next poll until it succeeds, is parked, or ages out of the window.

## 2. API surface

| Use | Request | Status |
|---|---|---|
| List (admin/compliance) | `GET /v1/admin/convergedRecordings?from=&to=&status=available&serviceType=calling&max=` | [P] path and params from reference-page snippet + SDK |
| Window limit | `to - from` ≤ 30 days | [P] |
| Paging | RFC 5988 `Link: <…>; rel="next"` | [P] standard Webex paging |
| Details | `GET /v1/convergedRecordings/{id}` → `temporaryDirectDownloadLinks.audioDownloadLink` (expires 3 h) | [V] existence; [U] admin path and field name |
| Metadata | `GET /v1/convergedRecordings/{id}/metadata?showAllTypes=true` → `ownerName`, participants, and (assumed) `serviceData` calling/called party, `personality`, session start | [V] existence; [U] admin path and fields |
| Hard delete | `DELETE /v1/convergedRecordings/{id}` with `reasonForDeletion`, `comment` | [V] compliance-only; [U] exact path/body |
| Audio | MP3 | [V] Cisco TAC |

Sources:
- https://developer.webex.com/docs/api/v1/converged-recordings/list-recordings-for-admin-or-compliance-officer
- https://developer.webex.com/blog/getting-started-with-the-converged-recordings-apis-for-webex-calling
- https://wxc-sdk.readthedocs.io/en/1.25.0/apidoc/wxc_sdk.converged_recordings.html
- https://www.cisco.com/c/en/us/support/docs/unified-communications/webex-calling/222147-download-call-recordings-through-api-wit.html
- https://github.com/CiscoSE/WebexCallingRecordingsDownloader
- https://help.webex.com/en-us/article/6xorz3/Enable-call-recording-for-an-organization

The HTTP client's own INFO logging is silenced (`httpx`, `httpcore` at WARNING) because it
prints full temporary download URLs.

Every unverified path and field name is isolated in one small function or constant in
`jabberscribe/capture/webex.py`, each with an `ASSUMPTION` comment.

The Converged Recordings API covers only the **Webex native** recording provider. Dubber,
Imagicle, CallCabinet etc. keep audio in their own cloud and would need their own exporter.

## 3. Auth and scopes

- Token from env var `JABBERSCRIBE_WEBEX_TOKEN`, never config. Never logged; download URLs
  are never logged either.
- Scopes: `spark-compliance:recordings_read` (compliance officer) or `spark-admin:recordings_read`.
  Only a Compliance Officer may download audio and hard-delete [V from guide];
  `spark-compliance:recordings_write` only if `delete_after_export` is on.
- This build uses a static bearer token. A Webex personal token lasts 12 hours, so production
  needs a Service App or Integration with a refresh-token grant
  (`POST https://webexapis.com/v1/access_token`). Not built yet (open question 2).
- The bearer header is sent to the download link only if it is on the API host; a
  temporary link on another host is assumed self-authenticating [U].

## 4. Sidecar mapping

| Sidecar field | Source | Rule |
|---|---|---|
| `schema_version` | const | `2` |
| `call_id` | `serviceData.callSessionId`, else `serviceData.callId`, else recording `id` | Prefixed `wxc-`. Both ends of an internal call share it; the dedup key `(call_id, extension)` separates them, so each line owner gets their own copy (spec v2 §6). |
| `conference_id` | `callSessionId` | Set (as `wxc-<session>`) only on positive evidence: metadata lists more than 2 participants, or the listing shows more than 2 distinct recording owners for the session [U]. Two legs sharing a session are the two ends of a 1:1 call and are **not** a conference. Null otherwise. |
| `line_owner.extension` | `calledParty.number` if `personality` is terminating, else `callingParty.number`. Party, personality and session start are read from the metadata response first, then details/list [U] | Fallback: `ownerEmail` local part, so the pair is not quarantined. |
| `line_owner.user` | `ownerEmail` local part | Must equal the SSO identity (open question 7). |
| `line_owner.display_name` | metadata `ownerName`, else the owner party's `name` | |
| `parties[]` | the other party (`number` → extension, `name` → display_name) | Conference participant lists not mapped yet [U]. |
| `kind` | | `conference` if `conference_id`, else `call`. |
| `started_at` | `serviceData.session.startTime`, else `timeRecorded`, else `createTime` | Rendered ISO 8601 in the host's local offset. `timeRecorded` is recording start, not call start. |
| `ended_at` | `started_at + duration_sec` | |
| `duration_sec` | `durationSeconds` | int; ffprobe duration if absent. |
| `audio.tracks` | ffprobe channel count | 2 → `dual`, otherwise `mixed`. |
| `audio.sample_rate`, `audio.channels` | ffprobe | |

File name base: `jabberscribe.sidecar.job_key(call_id, extension)`.

## 5. Failure handling

| Failure | Behaviour |
|---|---|
| 401/403 on list | Poll aborts with an error log; nothing is written. |
| 429 / network error | Transient: the poll stops, nothing counts against the recording, next poll retries. `Retry-After` is not honoured beyond the poll interval. |
| 4xx or 5xx on one recording, empty link, ffprobe/ffmpeg failure, malformed recording (bad time, missing field) | Recording's attempt count increments, the poll continues with the next recording; retried next poll; parked after `max_attempts` (default 5). |
| 401/403 on details, metadata or download | Token or scope problem, not a recording problem: the poll stops, no attempt is counted, an error naming the status (no URL) is logged. (Metadata 403/404 still degrades to no metadata.) |
| Malformed or non-http temporary download link (`httpx.InvalidURL`, `UnsupportedProtocol`) | Counted as a per-recording failure like any other; the poll continues. |
| Sustained Webex 5xx outage longer than `max_attempts` polls | Every affected recording is parked (each poll counts an attempt). Recover by deleting their rows from the exporter's ledger (SQLite file at `state_path`): `DELETE FROM exported WHERE state = 'failed';` (or `... WHERE recording_id = '<id>'`). The next poll retries them if still inside `lookback_hours`. |
| Unexpected error in a poll | Logged by class name only (no URLs); the daemon keeps polling. |
| Crash mid-export | Only `.part` files or the work-dir MP3 remain; they are overwritten on retry. The watcher ignores `.part` files and a `.wav` without a `.json`. |
| Any error before the sidecar rename | No `.json` appears, so the watcher never sees a partial pair; leftover `.wav.part` is removed. |
| Metadata 403/404 | Degrades to no metadata (display name from the party, no participant count); export continues. |
| Delete fails after export | Logged; the export stands and is not retried. |
| Late conference leg (arrives in a later poll) | Gets `conference_id` only if metadata reports >2 participants or the listing shows >2 owners; otherwise exported as a call (open question 5). |
| Download redirect | Followed; httpx drops the bearer header on a cross-origin redirect. |
| Crash between pair write and ledger mark | The recording is exported again under the same `job_key`; the watcher discards it as a duplicate. |

## 6. Configuration

Separate file `config/webex.yaml` (example: `config/webex.yaml.example`), its own pydantic
model with `extra="forbid"`. The main `jabberscribe/config.py` is untouched.

## 7. Open questions for the owner

1. Is telephony multitenant Webex Calling (Webex App client) or Dedicated Instance / hosted UCM
   (then SIPREC applies)? Which recording provider is selected? This API covers only native.
2. Can we get a Compliance Officer-authorised Service App or Integration with a refresh token?
   Needed to download, to hard-delete, and to run longer than a 12 h personal token. [U]
3. Is the native MP3 stereo (one party per channel) or mixed mono? Test one recording; it
   decides `tracks` and whether diarization is needed. [U]
4. Exact admin/compliance paths and field names for details, metadata and delete
   (`temporaryDirectDownloadLinks.audioDownloadLink`, `ownerName`, participants). [U]
5. Conference recordings: does each recorded user's leg yield its own recording sharing
   `callSessionId`, and where are participants listed? Webex Meetings recordings need a
   second exporter (`/v1/admin/recordings`) and more scopes. [U]
6. Is "Always" recording enforced for all users and locations, including transfers, forwards,
   PSTN and multi-party legs?
7. `line_owner.user`: email local part or full email, to match the SSO web app?
8. Webex-side retention vs company policy: delete after export? Hard delete (compliance) or
   recycle bin (admin)?
9. Does the temporary download link need the bearer token when it is on a different host? [U]
10. Rate limits and the delay between hang-up and recording availability. [U]
11. Data residency of the tenant's `storageRegion` and acceptance of the egress.
12. Where do `callingParty`, `calledParty`, `personality` and the session start live: the list
    item, the details response, or `GET .../metadata`? The exporter reads metadata first and
    falls back to details/list (`_with_metadata_call_fields`). [U]
13. A failed delete-after-export is logged and **not retried**, so the Webex copy stays. Needs a
    decision: retry on later polls, or accept and sweep by retention policy.
14. `line_owner.extension` when Webex only reports E.164 numbers: is that acceptable as the
    "extension", or must we enrich from the People / Calling user API (more scopes)?
15. Parked recordings are never retried automatically. Should the exporter unpark or back off
   (e.g. do not count 5xx outage polls, or retry parked rows after a cool-down) instead of
   requiring a manual ledger delete?
