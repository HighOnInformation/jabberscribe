"""Stage orchestration.

Stages run in the fixed order audio -> stt -> cues -> summarize -> output, and
each one checkpoints in the job store. A restart resumes at the first
incomplete stage, so a crash after transcription never transcribes again.

The cues stage never fails a job: without a tagger, or when tagging fails,
the call goes out with its cue layer marked unavailable.

Retry policy. Every failure schedules the next try with exponential backoff
(30 s, doubling, capped at 30 min, counted over all failures) from the moment
it failed; claim_next skips the job until then, so it never blocks the jobs
behind it.

- TransientError (LiteLLM down, overloaded, 5xx, a locked output file): counted
  in transient_failures and retried forever. An outage delays calls; it must
  never lose them, nor spend their attempts.
- Anything else (corrupt audio, a 4xx, a bug): counts an attempt; FAILED after
  MAX_ATTEMPTS, for a human to inspect and `jabberscribe retry`.

Status writes at the end of a run apply only while the job is still RUNNING:
grouping may hand the job over to another copy while the worker holds it.
complete_stage, record_attempt and record_transient stay unguarded; on a job
taken away they are harmless, because reset_job clears all three.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audio import STT_GLOB
from jabberscribe.config import Config
from jabberscribe.cues import Cue, Tagger
from jabberscribe.group import owners_for
from jabberscribe.jobs import DONE, FAILED, Job, JobStore, next_stage
from jabberscribe.llm import TransientError
from jabberscribe.output import RESULT_FILE, write_atomic, write_outputs
from jabberscribe.sidecar import Sidecar, parse_sidecar
from jabberscribe.speakers import stt_inputs, transcribe_inputs
from jabberscribe.stt import Segment, Transcriber
from jabberscribe.summarize import ActionItem, Summarizer, Summary, SummaryUnavailable

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 1800

SEGMENTS_FILE = "segments.json"
SUMMARY_JSON = "summary.json"
TIMINGS_FILE = "timings.json"
CUES_JSON = "cues.json"


def backoff(attempts: int) -> timedelta:
    """Delay before the next try after `attempts` failures: 30 s, 60 s, 120 s, ... at most 30 min."""
    return timedelta(seconds=min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_CAP_SECONDS))


def _write_segments(path: Path, segments: list[Segment]) -> None:
    write_atomic(path, json.dumps([asdict(s) for s in segments], ensure_ascii=False, indent=2))


def _read_segments(path: Path) -> list[Segment]:
    return [Segment(**s) for s in json.loads(path.read_text(encoding="utf-8"))]


def _read_cues(path: Path) -> list[Cue] | None:
    """None when the layer is unavailable -- including a job that passed the cues stage before it existed."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return None if data is None else [Cue(**c) for c in data]


def _tag(tagger: Tagger | None, audio: Path, job_key: str) -> str:
    """The cues checkpoint as JSON: the recording's cues, or null when there is no tagger or it fails.

    Cues never fail a job, so the tagger's output is serialised inside the guard: malformed output is a failed tagging.
    """
    if tagger is None:
        log.info("%s: no tagger; cues skipped", job_key)
        return json.dumps(None)
    try:
        cues = tagger.tag(audio)
        payload = None if cues is None else [asdict(c) for c in cues]
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception:
        log.exception("%s: tagging failed; the call goes out without cues", job_key)
        return json.dumps(None)


def _summarize(summarizer: Summarizer, segments: list[Segment]) -> tuple[Summary | None, str | None]:
    """The summary, or None and why it is unavailable (None when no reason was given)."""
    try:
        return summarizer.summarize(segments), None
    except SummaryUnavailable as exc:
        return None, str(exc)


def _write_summary(path: Path, summary: Summary | None, error: str | None) -> None:
    payload: dict | None = None
    if summary is not None:
        payload = {"text": summary.text, "action_items": [asdict(i) for i in summary.action_items]}
    elif error is not None:
        payload = {"error": error}
    write_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _read_summary(path: Path) -> tuple[Summary | None, str | None]:
    """The summary checkpoint: a summary, or None and the reason it is unavailable."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data is None:
        return None, None
    if "error" in data:
        return None, data["error"]
    return Summary(data["text"], tuple(ActionItem(**i) for i in data["action_items"])), None


def _read_timings(path: Path) -> dict[str, float]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _hangup(sidecar: Sidecar) -> datetime:
    return datetime.fromisoformat(sidecar.started_at) + timedelta(seconds=sidecar.duration_sec)


def _delete_stt_audio(work: Path) -> None:
    # The Opus copy is the voice too; it must not outlive the recording's retention.
    for leftover in work.glob(STT_GLOB):
        leftover.unlink(missing_ok=True)


def process_job(
    job: Job,
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    summarizer: Summarizer,
    tagger: Tagger | None = None,
) -> Path:
    """Run `job` from its checkpoint to the end. Returns the result.json path.

    Raises on failure after recording the attempt, leaving the retry decision
    to the caller.
    """
    work = cfg.paths.work_dir / job.job_key
    segments_path = work / SEGMENTS_FILE
    summary_path = work / SUMMARY_JSON
    timings_path = work / TIMINGS_FILE
    cues_path = work / CUES_JSON
    stage = job.stage

    try:
        sidecar = parse_sidecar(job.sidecar_json)
        timings = _read_timings(timings_path)
        while (stage := next_stage(stage)) is not None:
            log.info("%s: stage %s", job.job_key, stage)
            began = time.monotonic()
            if stage == "audio":
                stt_inputs(job.audio_path, work, sidecar, cfg.stt)
            elif stage == "stt":
                inputs = stt_inputs(job.audio_path, work, sidecar, cfg.stt)
                _write_segments(segments_path, transcribe_inputs(transcriber, inputs))
                _delete_stt_audio(work)
            elif stage == "cues":
                write_atomic(cues_path, _tag(tagger, job.audio_path, job.job_key))
            elif stage == "summarize":
                _write_summary(summary_path, *_summarize(summarizer, _read_segments(segments_path)))
            elif stage == "output":
                timings["hangup_to_output_sec"] = round((datetime.now(UTC) - _hangup(sidecar)).total_seconds(), 1)
                # Read the checkpoints first: a missing one is a real failure, not an outage.
                segments = _read_segments(segments_path)
                summary, summary_error = _read_summary(summary_path)
                cues = _read_cues(cues_path)
                owners = owners_for(job, store)
                try:
                    write_outputs(
                        job.out_dir,
                        sidecar=sidecar,
                        segments=segments,
                        summary=summary,
                        summary_error=summary_error,
                        owners=owners,
                        models={"stt": cfg.stt.model, "summary": cfg.summary.model},
                        recording=job.audio_path,
                        timings=timings,
                        cues=cues,
                    )
                except OSError as exc:
                    # A file locked or a share unavailable on the output side is
                    # not bad data; it clears on its own.
                    raise TransientError(str(exc)) from exc
                store.record_output(job.job_key, timings["hangup_to_output_sec"])
            if stage != "output":
                timings[f"{stage}_sec"] = round(time.monotonic() - began, 1)
                write_atomic(timings_path, json.dumps(timings))
            store.complete_stage(job.job_key, stage)
    except Exception as exc:
        if isinstance(exc, TransientError):
            store.record_transient(job.job_key, f"{stage}: {exc}")
        else:
            store.record_attempt(job.job_key, f"{stage}: {exc}")
        log.exception("%s: stage %s failed", job.job_key, stage)
        raise

    if store.finish(job.job_key, DONE):
        log.info("%s: done", job.job_key)
    else:
        log.warning("%s: done, but the job changed while it ran; left as it is", job.job_key)
    return job.out_dir / RESULT_FILE


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _run(
    job: Job,
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    summarizer: Summarizer,
    clock: Callable[[], datetime],
    tagger: Tagger | None = None,
) -> None:
    try:
        process_job(job, cfg, store, transcriber, summarizer, tagger)
    except Exception as exc:
        failed = store.get(job.job_key)
        attempts = failed.attempts
        if isinstance(exc, TransientError) or attempts < MAX_ATTEMPTS:
            retry_at = clock() + backoff(attempts + failed.transient_failures)
            if store.retry_later(job.job_key, retry_at):
                log.warning(
                    "%s: failed (attempts %d, transient failures %d), retrying at %s",
                    job.job_key,
                    attempts,
                    failed.transient_failures,
                    retry_at.isoformat(),
                )
            else:
                log.warning("%s: failed, but the job changed while it ran; not rescheduled", job.job_key)
        else:
            _delete_stt_audio(cfg.paths.work_dir / job.job_key)
            if store.finish(job.job_key, FAILED):
                log.error("%s: FAILED after %d attempts", job.job_key, attempts)
            else:
                log.warning("%s: failed for good, but the job changed while it ran; left as it is", job.job_key)


def run_once(
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    summarizer: Summarizer,
    now: datetime | None = None,
    clock: Callable[[], datetime] = _utcnow,
    tagger: Tagger | None = None,
) -> int:
    """Process every job that is due once. Returns how many were attempted.

    `now` decides which jobs are due for the whole pass; `clock` times each
    failure, so a long pass never schedules a retry in the past.
    """
    moment = now or clock()
    processed = 0
    while (job := store.claim_next(moment)) is not None:
        processed += 1
        _run(job, cfg, store, transcriber, summarizer, clock, tagger)
    return processed


def run_job(
    job_key: str,
    cfg: Config,
    store: JobStore,
    transcriber: Transcriber,
    summarizer: Summarizer,
    clock: Callable[[], datetime] = _utcnow,
    tagger: Tagger | None = None,
) -> bool:
    """Process one job now, due or not (the `process` command). Returns False if it was not runnable."""
    job = store.claim(job_key)
    if job is None:
        return False
    _run(job, cfg, store, transcriber, summarizer, clock, tagger)
    return True
