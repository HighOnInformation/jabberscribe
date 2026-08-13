"""Stage orchestration.

Stages are idempotent and checkpointed, so a restart resumes at the first
incomplete stage.

Which stages run is configuration (`pipeline.stages`), not code. That is the
activation switch for the system: the core stages are implemented and enabled by
default, and each outer stage becomes live by adding its name to the config once
it exists. Enabling one that does not exist yet raises StageNotImplementedError,
and `doctor` reports it up front rather than letting it surface mid-call.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from jabberscribe.audio import prepare
from jabberscribe.audit import AuditLog
from jabberscribe.config import Config
from jabberscribe.diarize import merge_tracks
from jabberscribe.jobs import DONE, FAILED, NEEDS_REVIEW, QUEUED, Job, JobStore, next_stage
from jabberscribe.notify import notify_call
from jabberscribe.publish import publish_call
from jabberscribe.render import load_transcript, render_call
from jabberscribe.stt import Segment, Transcriber

log = logging.getLogger(__name__)

#: Stages with a working implementation. Outer stages are added here as they land.
IMPLEMENTED_STAGES: tuple[str, ...] = ("audio", "stt", "render", "publish", "notify")

MAX_ATTEMPTS = 3


class StageNotImplementedError(RuntimeError):
    """A stage is enabled in config but has no implementation yet."""


def unimplemented(stages: tuple[str, ...]) -> tuple[str, ...]:
    """Return the configured stages that cannot run yet. Used by `doctor`."""
    return tuple(s for s in stages if s not in IMPLEMENTED_STAGES)


def write_transcript(path: Path, segments: list[Segment], call_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "call_id": call_id,
        "segments": [{"start": s.start, "end": s.end, "text": s.text, "speaker": s.speaker} for s in segments],
    }
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _work_dir(cfg: Config, call_id: str) -> Path:
    return cfg.paths.work_dir / call_id


def _audit_or_raise(audit: AuditLog | None) -> AuditLog:
    """Delivery stages must not run without an audit trail.

    Publishing or mailing a call without recording it would defeat the entire
    point of the trail, so this is a hard error rather than a warning.
    """
    if audit is None:
        raise ValueError("delivery stages require an AuditLog")
    return audit


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
                page_url = None
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

    # An ambiguous send raises out of notify_call, so this line is unreachable in
    # that case and needs no needs_review guard.
    store.set_status(job.call_id, DONE)
    log.info("%s: done", job.call_id)
    return transcript_path


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
                # A human owns this one now; claim_next will not re-claim it.
                log.error("%s: left for human review", job.call_id)
            elif refreshed is not None and refreshed.attempts >= MAX_ATTEMPTS:
                store.set_status(job.call_id, FAILED)
                log.error("%s: giving up after %d attempts", job.call_id, refreshed.attempts)
            else:
                # Leave it queued so the next pass retries it.
                store.set_status(job.call_id, QUEUED)
                break
    return processed
