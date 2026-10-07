"""Stage orchestration.

Stages run in the fixed order audio -> stt -> summarize -> output, and each one
checkpoints in the job store. A restart resumes at the first incomplete stage,
so a crash after transcription never transcribes again.

Retry policy. Every failure counts an attempt and schedules the next one with
exponential backoff (30 s, doubling, capped at 30 min); claim_next skips the
job until then, so it never blocks the jobs behind it.

- TransientError (LiteLLM down, overloaded, 5xx): retried forever. An outage
  delays calls; it must never lose them.
- Anything else (corrupt audio, a 4xx, a bug): FAILED after MAX_ATTEMPTS, for a
  human to inspect and `jabberscribe retry`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audio import STT_FILENAME, prepare_for_stt
from jabberscribe.config import Config
from jabberscribe.group import owners_for
from jabberscribe.jobs import DONE, FAILED, Job, JobStore, next_stage
from jabberscribe.llm import TransientError
from jabberscribe.output import RESULT_FILE, write_atomic, write_outputs
from jabberscribe.sidecar import Sidecar, parse_sidecar
from jabberscribe.stt import Segment, Transcriber
from jabberscribe.summarize import ActionItem, Summarizer, Summary

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 1800

SEGMENTS_FILE = "segments.json"
SUMMARY_JSON = "summary.json"
TIMINGS_FILE = "timings.json"


def backoff(attempts: int) -> timedelta:
    """Delay before the next try after `attempts` failures: 30 s, 60 s, 120 s, ... at most 30 min."""
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS))


def _write_segments(path: Path, segments: list[Segment]) -> None:
    write_atomic(path, json.dumps([asdict(s) for s in segments], ensure_ascii=False, indent=2))


def _read_segments(path: Path) -> list[Segment]:
    return [Segment(**s) for s in json.loads(path.read_text(encoding="utf-8"))]


def _write_summary(path: Path, summary: Summary | None) -> None:
    payload = None
    if summary is not None:
        payload = {"text": summary.text, "action_items": [asdict(i) for i in summary.action_items]}
    write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_summary(path: Path) -> Summary | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data is None:
        return None
    return Summary(data["text"], tuple(ActionItem(**i) for i in data["action_items"]))


def _read_timings(path: Path) -> dict[str, float]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _hangup(sidecar: Sidecar) -> datetime:
    return datetime.fromisoformat(sidecar.started_at) + timedelta(seconds=sidecar.duration_sec)


def _delete_stt_audio(work: Path) -> None:
    # The Opus copy is the voice too; it must not outlive the recording's retention.
    (work / STT_FILENAME).unlink(missing_ok=True)
    (work / f"{STT_FILENAME}.part").unlink(missing_ok=True)


def process_job(
    job: Job,
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    summarizer: Summarizer,
) -> Path:
    """Run `job` from its checkpoint to the end. Returns the result.json path.

    Raises on failure after recording the attempt, leaving the retry decision
    to the caller.
    """
    work = cfg.paths.work_dir / job.job_key
    segments_path = work / SEGMENTS_FILE
    summary_path = work / SUMMARY_JSON
    timings_path = work / TIMINGS_FILE
    stage = job.stage

    try:
        sidecar = parse_sidecar(job.sidecar_json)
        timings = _read_timings(timings_path)
        while (stage := next_stage(stage)) is not None:
            log.info("%s: stage %s", job.job_key, stage)
            began = time.monotonic()
            if stage == "audio":
                prepare_for_stt(job.audio_path, work)
            elif stage == "stt":
                _write_segments(segments_path, transcriber.transcribe(prepare_for_stt(job.audio_path, work)))
                _delete_stt_audio(work)
            elif stage == "summarize":
                _write_summary(summary_path, summarizer.summarize(_read_segments(segments_path)))
            elif stage == "output":
                timings["hangup_to_output_sec"] = round((datetime.now(UTC) - _hangup(sidecar)).total_seconds(), 1)
                write_outputs(
                    job.out_dir,
                    sidecar=sidecar,
                    segments=_read_segments(segments_path),
                    summary=_read_summary(summary_path),
                    owners=owners_for(job, store),
                    models={"stt": cfg.stt.model, "summary": cfg.summary.model},
                    recording=job.audio_path,
                    timings=timings,
                )
            if stage != "output":
                timings[f"{stage}_sec"] = round(time.monotonic() - began, 1)
                write_atomic(timings_path, json.dumps(timings))
            store.complete_stage(job.job_key, stage)
    except Exception as exc:
        store.record_attempt(job.job_key, f"{stage}: {exc}")
        log.exception("%s: stage %s failed", job.job_key, stage)
        raise

    store.set_status(job.job_key, DONE)
    log.info("%s: done", job.job_key)
    return job.out_dir / RESULT_FILE


def _run(
    job: Job, cfg: Config, store: JobStore, transcriber: Transcriber, summarizer: Summarizer, now: datetime
) -> None:
    try:
        process_job(job, cfg, store, transcriber, summarizer)
    except Exception as exc:
        attempts = store.get(job.job_key).attempts
        if isinstance(exc, TransientError) or attempts < MAX_ATTEMPTS:
            retry_at = now + backoff(attempts)
            store.schedule_retry(job.job_key, retry_at)
            log.warning("%s: attempt %d failed, retrying at %s", job.job_key, attempts, retry_at.isoformat())
        else:
            store.set_status(job.job_key, FAILED)
            _delete_stt_audio(cfg.paths.work_dir / job.job_key)
            log.error("%s: FAILED after %d attempts", job.job_key, attempts)


def run_once(
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    summarizer: Summarizer,
    now: datetime | None = None,
) -> int:
    """Process every job that is due once. Returns how many were attempted."""
    moment = now or datetime.now(UTC)
    processed = 0
    while (job := store.claim_next(moment)) is not None:
        processed += 1
        _run(job, cfg, store, transcriber, summarizer, moment)
    return processed


def run_job(job_key: str, cfg: Config, store: JobStore, transcriber: Transcriber, summarizer: Summarizer) -> bool:
    """Process one job now, due or not (the `process` command). Returns False if it was not runnable."""
    job = store.claim(job_key)
    if job is None:
        return False
    _run(job, cfg, store, transcriber, summarizer, datetime.now(UTC))
    return True
