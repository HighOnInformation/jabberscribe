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
from jabberscribe.config import Config
from jabberscribe.diarize import merge_tracks
from jabberscribe.jobs import DONE, FAILED, QUEUED, Job, JobStore, next_stage
from jabberscribe.stt import Segment, Transcriber

log = logging.getLogger(__name__)

#: Stages with a working implementation. Outer stages are added here as they land.
IMPLEMENTED_STAGES: tuple[str, ...] = ("audio", "stt")

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


def process_job(job: Job, cfg: Config, store: JobStore, transcriber: Transcriber) -> Path:
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


def run_once(cfg: Config, store: JobStore, transcriber: Transcriber) -> int:
    """Process every runnable job once. Returns how many were attempted."""
    processed = 0
    while (job := store.claim_next()) is not None:
        processed += 1
        try:
            process_job(job, cfg, store, transcriber)
        except Exception:
            refreshed = store.get(job.call_id)
            if refreshed is not None and refreshed.attempts >= MAX_ATTEMPTS:
                store.set_status(job.call_id, FAILED)
                log.error("%s: giving up after %d attempts", job.call_id, refreshed.attempts)
            else:
                # Leave it queued so the next pass retries it.
                store.set_status(job.call_id, QUEUED)
                break
    return processed
