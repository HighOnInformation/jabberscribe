"""Command-line entry point.

`doctor` exists so a bad deployment fails loudly at install time rather than
silently at 2 a.m.: it sends a real chat completion and a real transcription
through LiteLLM, not just a model listing.

`run`, `process` and `purge` hold the single-instance lock. `run` never dies on
a bad poll: each phase (scan, settle, process, purge) logs its own error and
the loop carries on. Operator
output is English, like the logs; user-facing files are Hebrew.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from jabberscribe.alerts import LITELLM_DOWN, PURGE_ERRORS, Alerter, check_jobs, make_alerter, probe_litellm
from jabberscribe.audit import LEGAL_HOLD_RELEASED, LEGAL_HOLD_SET, UNHOLD_FAILED, AuditLog
from jabberscribe.config import Config, ConfigError, load_config
from jabberscribe.cues import Tagger, load_tagger, unavailable_reason
from jabberscribe.group import requeue_failed, settle
from jabberscribe.health import heartbeat_path, latency_summary, read_heartbeat, write_heartbeat
from jabberscribe.jobs import DONE, FAILED, GROUPED, QUEUED, RUNNING, WAITING, JobStore, SchemaError
from jabberscribe.llm import TransientError, make_client, post
from jabberscribe.lock import LockError, instance_lock
from jabberscribe.pipeline import run_job, run_once
from jabberscribe.retention import purge
from jabberscribe.sidecar import SidecarError, parse_sidecar
from jabberscribe.stt import LiteLLMTranscriber, SttError, build_prompt, load_vocabulary
from jabberscribe.summarize import LiteLLMSummarizer, strip_fences
from jabberscribe.watcher import scan_once

log = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("config/jabberscribe.yaml")

CHAT_PROBE = 'Reply with exactly this JSON object and nothing else: {"ok": true}'
STATUS_ORDER = (WAITING, QUEUED, RUNNING, DONE, GROUPED, FAILED)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _describe(exc: Exception) -> str:
    """An HTTP error with the server's own reason, which is what an operator needs to see."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{exc} {exc.response.text[:200]}"
    return str(exc)


def _read_vocabulary(cfg: Config) -> str | None:
    try:
        return load_vocabulary(cfg.stt.vocabulary_file)
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read vocabulary file {cfg.stt.vocabulary_file}: {exc}") from exc


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _check_ffmpeg() -> Check:
    exe = shutil.which("ffmpeg")
    if exe is None:
        return Check("ffmpeg", False, "not found on PATH")
    try:
        proc = subprocess.run(
            [exe, "-version"], capture_output=True, encoding="utf-8", errors="replace", timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("ffmpeg", False, f"{exe}: {exc}")
    if proc.returncode != 0:
        return Check("ffmpeg", False, f"{exe} exited {proc.returncode}")
    first_line = proc.stdout.splitlines()[0] if proc.stdout else exe
    return Check("ffmpeg", True, first_line)


def _check_models(cfg: Config, client: httpx.Client) -> Check:
    """The server must be reachable and serve both configured models."""
    try:
        response = client.get("/v1/models")
        response.raise_for_status()
        served = {m["id"] for m in response.json()["data"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return Check("litellm", False, f"{cfg.litellm.base_url}: {_describe(exc)}")
    missing = [m for m in (cfg.stt.model, cfg.summary.model) if m not in served]
    if missing:
        return Check("litellm", False, f"{cfg.litellm.base_url} does not serve: {', '.join(missing)}")
    return Check("litellm", True, f"{cfg.litellm.base_url}: {cfg.stt.model}, {cfg.summary.model}")


def _check_chat(cfg: Config, client: httpx.Client) -> Check:
    """The summary request shape (user role, JSON mode, max_tokens) must be accepted and answered."""
    try:
        response = post(
            client,
            "/v1/chat/completions",
            json={
                "model": cfg.summary.model,
                "temperature": 0,
                "max_tokens": 20,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": CHAT_PROBE}],
            },
        )
        answer = json.loads(strip_fences(response.json()["choices"][0]["message"]["content"]))
    except (httpx.HTTPError, TransientError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        return Check("litellm.chat", False, f"{cfg.summary.model}: {_describe(exc)}")
    if answer != {"ok": True}:
        return Check("litellm.chat", False, f"{cfg.summary.model}: unexpected answer {answer!r}")
    return Check("litellm.chat", True, f"{cfg.summary.model}: JSON answer received")


def _check_transcription(cfg: Config, client: httpx.Client) -> Check:
    """A one-second tone must go through the transcription route with our prompt and verbose_json."""
    try:
        prompt = build_prompt(_read_vocabulary(cfg))
    except ConfigError as exc:
        return Check("litellm.transcription", False, str(exc))
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.ogg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", str(probe),
                ],
                capture_output=True,
                timeout=60,
                check=True,
            )
            segments = LiteLLMTranscriber(client, cfg.stt.model, prompt).transcribe(probe)
        except (OSError, subprocess.SubprocessError) as exc:
            return Check("litellm.transcription", False, f"cannot make the probe tone: {exc}")
        except (TransientError, SttError) as exc:
            return Check("litellm.transcription", False, f"{cfg.stt.model}: {_describe(exc)}")
    return Check("litellm.transcription", True, f"{cfg.stt.model}: {len(segments)} segment(s), prompt accepted")


def _check_vocabulary(cfg: Config) -> Check:
    path = cfg.stt.vocabulary_file
    if path is None:
        return Check("stt.vocabulary_file", True, "not configured")
    try:
        vocabulary = _read_vocabulary(cfg)
    except ConfigError as exc:
        return Check("stt.vocabulary_file", False, str(exc))
    if vocabulary is None:
        return Check("stt.vocabulary_file", False, f"missing or empty: {path}")
    return Check("stt.vocabulary_file", True, f"{path}: {len(vocabulary.split(', '))} terms")


def _check_cues(cfg: Config) -> Check:
    """Information only: the cue layer is optional, so a missing tagger never fails doctor."""
    reason = unavailable_reason(cfg.cues)
    return Check("cues", True, "tagger available" if reason is None else f"skipped: {reason}")


def _check_dir(name: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check(name, False, f"cannot create {path}: {exc}")
    probe = path / ".jabberscribe-write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(name, False, f"not writable: {path}: {exc}")
    return Check(name, True, str(path))


def doctor(cfg: Config, client: httpx.Client) -> list[Check]:
    """Verify the environment. Creates missing directories as a side effect."""
    return [
        _check_ffmpeg(),
        _check_models(cfg, client),
        _check_chat(cfg, client),
        _check_transcription(cfg, client),
        _check_vocabulary(cfg),
        _check_cues(cfg),
        _check_dir("paths.drop_root/inbox", cfg.paths.inbox),
        _check_dir("paths.drop_root/quarantine", cfg.paths.quarantine),
        _check_dir("paths.work_dir", cfg.paths.work_dir),
        _check_dir("paths.out_root", cfg.paths.out_root),
        _check_dir("paths.db_path parent", cfg.paths.db_path.parent),
    ]


def _ensure_dirs(cfg: Config) -> None:
    paths = cfg.paths
    for path in (paths.inbox, paths.quarantine, paths.work_dir, paths.out_root, paths.db_path.parent):
        path.mkdir(parents=True, exist_ok=True)


def _open(cfg: Config) -> tuple[JobStore, AuditLog]:
    # The job store first: it refuses a database from another version before anything writes to it.
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path)
    audit.init_schema()
    return store, audit


def _workers(cfg: Config, client: httpx.Client) -> tuple[LiteLLMTranscriber, LiteLLMSummarizer, Tagger | None]:
    prompt = build_prompt(_read_vocabulary(cfg))
    summarizer = LiteLLMSummarizer(client, cfg.summary.model, cfg.summary.max_chunk_chars)
    return LiteLLMTranscriber(client, cfg.stt.model, prompt), summarizer, load_tagger(cfg.cues)


def _process(cfg: Config, store: JobStore, audit: AuditLog, audio: Path, sidecar_path: Path) -> int:
    """Run one recording end to end: only its own job, and only its own conference is released."""
    try:
        key = parse_sidecar(sidecar_path.read_text(encoding="utf-8-sig")).job_key
    except (SidecarError, OSError, UnicodeDecodeError) as exc:
        print(f"invalid sidecar {sidecar_path}: {exc}", file=sys.stderr)
        return 1
    if store.get(key) is None:
        for src in (audio, sidecar_path):
            if (cfg.paths.inbox / src.name).exists():
                print(f"inbox already holds {src.name}; refusing to overwrite it", file=sys.stderr)
                return 2
        shutil.copy2(audio, cfg.paths.inbox / audio.name)
        shutil.copy2(sidecar_path, cfg.paths.inbox / sidecar_path.name)
        # min_age 0: a human handing us one file is not racing a recorder.
        result = scan_once(cfg, store, audit, min_age_seconds=0)
        if store.get(key) is None:
            reason = f"quarantined {result.quarantined}, deferred {result.deferred}"
            print(f"{key}: not ingested ({reason})", file=sys.stderr)
            return 1
    job = store.get(key)
    if job.status == WAITING:
        # Do not wait for other copies: release this conference now.
        immediate = cfg.model_copy(update={"group": cfg.group.model_copy(update={"max_wait_seconds": 0})})
        settle(immediate, store, audit, datetime.now(UTC), conference_id=job.conference_id)
        job = store.get(key)
    target_key = job.grouped_into or key
    transcriber, summarizer, tagger = _workers(cfg, make_client(cfg.litellm))
    run_job(target_key, cfg, store, transcriber, summarizer, tagger=tagger)
    target = store.get(target_key)
    print(f"{key}: {target.status} -> {target.out_dir}")
    return 0 if target.status == DONE else 1


def _alert_purge(alerter: Alerter | None, errors: tuple[str, ...]) -> None:
    if alerter is not None and errors:
        alerter.send(PURGE_ERRORS, f"{len(errors)} purge problem(s); see service log", _utcnow())


def _report_purge(cfg: Config, store: JobStore, audit: AuditLog) -> int:
    result = purge(cfg, store, audit, now=datetime.now(UTC))
    _alert_purge(make_alerter(cfg), result.errors)
    print(f"audio deleted: {len(result.audio_deleted)}")
    print(f"text deleted: {len(result.text_deleted)}")
    print(f"leftovers swept: {len(result.swept)}")
    print(f"on legal hold (kept): {len(result.held)}")
    for problem in result.errors:
        print(f"  ! {problem}", file=sys.stderr)
    return 1 if result.errors else 0


def _watch(cfg: Config, store: JobStore, alerter: Alerter, client: httpx.Client) -> None:
    """After a poll: alert on new failures, an old backlog, and an unreachable LiteLLM."""
    now = _utcnow()
    check_jobs(store, alerter, now, cfg.alerts.backlog_minutes)
    problem = probe_litellm(client)
    if problem is not None:
        alerter.send(LITELLM_DOWN, f"{cfg.litellm.base_url}: {problem}", now)


def _serve(cfg: Config, store: JobStore, audit: AuditLog, once: bool) -> int:
    client = make_client(cfg.litellm)
    transcriber, summarizer, tagger = _workers(cfg, client)
    alerter = make_alerter(cfg)
    last_purge: date | None = None
    polls = 0
    while True:
        ok = True
        phases = (
            ("scan", lambda: scan_once(cfg, store, audit)),
            ("settle", lambda: settle(cfg, store, audit, _utcnow())),
            ("process", lambda: run_once(cfg, store, transcriber, summarizer, tagger=tagger)),
        )
        for name, phase in phases:
            try:
                phase()
            except Exception:
                # One bad phase (a locked file, a full disk, a bug) must not stop the service, nor the other phases.
                log.exception("%s failed; continuing", name)
                ok = False
        today = _utcnow().date()
        if last_purge != today:
            # Marked first: a purge that fails is retried tomorrow, not on every poll.
            last_purge = today
            try:
                result = purge(cfg, store, audit, now=_utcnow())
                for problem in result.errors:
                    log.error("purge: %s", problem)
                _alert_purge(alerter, result.errors)
            except Exception:
                log.exception("purge failed; next attempt tomorrow")
                ok = False
        if alerter is not None:
            try:
                _watch(cfg, store, alerter, client)
            except Exception:
                log.exception("alert checks failed; continuing")
                ok = False
        polls += 1
        _beat(cfg, store, polls, ok)
        if once:
            return 0 if ok else 1
        time.sleep(cfg.watcher.poll_seconds)


def _beat(cfg: Config, store: JobStore, polls: int, ok: bool) -> None:
    try:
        write_heartbeat(heartbeat_path(cfg.paths.db_path), store, _utcnow(), polls=polls, last_poll_ok=ok)
    except Exception:
        # A monitor will notice the stale heartbeat; the service itself carries on.
        log.exception("cannot write the heartbeat")


def _retry(store: JobStore, job_key: str | None, all_failed: bool) -> int:
    if all_failed:
        keys = [j.job_key for j in store.list_by_status(FAILED) if j.grouped_into is None]
        if not keys:
            print("no failed jobs")
    else:
        keys = [job_key]
    refused = 0
    for key in keys:
        requeued = requeue_failed(store, key)
        if requeued == key:
            print(f"{key}: requeued")
        elif requeued is not None:
            print(f"{requeued}: requeued (longest failed copy of {key}'s conference)")
        else:
            print(f"{key}: not a failed job of a failed conference; nothing requeued", file=sys.stderr)
            refused += 1
    return 1 if refused else 0


def _hold(store: JobStore, audit: AuditLog, job_key: str, reason: str) -> int:
    """Put a call on legal hold. The audit row's actor is the OS account running the command."""
    reason = reason.strip()
    if not reason:
        print("a reason is required for a legal hold", file=sys.stderr)
        return 1
    if not store.hold(job_key, reason):
        print(f"{job_key}: unknown job", file=sys.stderr)
        return 1
    audit.record(job_key, LEGAL_HOLD_SET, reason)
    print(f"{job_key}: on legal hold; purge skips it and every copy of its conference")
    return 0


def _unhold(store: JobStore, audit: AuditLog, job_key: str, reason: str) -> int:
    """Release a legal hold. The audit row is written first: a hold is never released unaudited."""
    job = store.get(job_key)
    if job is None or not job.legal_hold:
        holders = store.holders(job_key) if job is not None else []
        if holders:
            print(
                f"{job_key}: not on legal hold itself; the call is held by {', '.join(holders)}; unhold that copy",
                file=sys.stderr,
            )
        else:
            print(f"{job_key}: not on legal hold; nothing released", file=sys.stderr)
        return 1
    try:
        audit.record(job_key, LEGAL_HOLD_RELEASED, reason.strip())
    except sqlite3.Error as exc:
        print(f"{job_key}: cannot write the audit row; hold kept: {exc}", file=sys.stderr)
        return 1
    try:
        released = store.release_hold(job_key)
        error = "" if released else "the hold was already released"
    except sqlite3.Error as exc:
        released, error = False, str(exc)
    if not released:
        print(f"{job_key}: legal hold not released: {error}", file=sys.stderr)
        try:
            audit.record(job_key, UNHOLD_FAILED, error)
        except sqlite3.Error as exc:
            print(f"{job_key}: cannot audit the failed release: {exc}", file=sys.stderr)
        return 1
    others = store.holders(job_key)
    if others:
        print(f"{job_key}: legal hold released; the call is still held by {', '.join(others)}; purge still skips it")
    else:
        print(f"{job_key}: legal hold released; retention applies again")
    return 0


def _heartbeat_line(cfg: Config, now: datetime) -> str:
    beat = read_heartbeat(heartbeat_path(cfg.paths.db_path))
    if beat is None:
        return "heartbeat: none (is `jabberscribe run` running?)"
    age = (now - datetime.fromisoformat(beat["last_poll_at"])).total_seconds()
    state = "ok" if beat.get("last_poll_ok") else "FAILED"
    return f"heartbeat: {age:.0f} s ago, poll {beat.get('polls')}, last poll {state}"


def _status(cfg: Config, store: JobStore) -> int:
    """Counts by status, the oldest waiting and queued job, latency, heartbeat, and every retrying or failed job.

    Exits 1 while any FAILED job is not superseded by a DONE primary, so a scheduled task can alert on it.
    """
    jobs = store.list_all()
    by_key = {j.job_key: j for j in jobs}
    counts = Counter(j.status for j in jobs)
    for status in STATUS_ORDER:
        print(f"{status}: {counts[status]}")
    now = datetime.now(UTC)
    for status in (WAITING, QUEUED):
        pending = [j for j in jobs if j.status == status]
        if pending:
            oldest = min(pending, key=lambda j: j.created_at)
            minutes = (now - datetime.fromisoformat(oldest.created_at)).total_seconds() / 60
            print(f"oldest {status}: {oldest.job_key} for {minutes:.0f} min")
    print(latency_summary(store, now))
    print(_heartbeat_line(cfg, now))
    unresolved = False
    for job in jobs:
        if job.status == QUEUED and job.next_attempt_at:
            print(
                f"  ~ {job.job_key}: attempt {job.attempts} (+{job.transient_failures} transient), "
                f"next at {job.next_attempt_at}: {job.last_error}"
            )
        if job.status == FAILED:
            primary = by_key.get(job.grouped_into) if job.grouped_into else None
            note = f" (handed over to {job.grouped_into})" if job.grouped_into else ""
            print(f"  ! {job.job_key}: {job.last_error}{note}")
            unresolved = unresolved or primary is None or primary.status != DONE
    for job in store.held_jobs():
        # Information only: a hold never changes the exit code.
        print(f"  h {job.job_key}: on legal hold: {job.hold_reason}")
    return 1 if unresolved else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jabberscribe")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to jabberscribe.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="verify environment and LiteLLM routes; create missing directories")

    process_cmd = sub.add_parser("process", help="process one recording from a path pair")
    process_cmd.add_argument("audio", type=Path)
    process_cmd.add_argument("sidecar", type=Path)

    run_cmd = sub.add_parser("run", help="watch the inbox and process jobs; purge once a day")
    run_cmd.add_argument("--once", action="store_true", help="single pass, then exit")

    sub.add_parser("purge", help="delete audio and text past their retention window")

    retry_cmd = sub.add_parser("retry", help="give a failed job a fresh set of attempts")
    target = retry_cmd.add_mutually_exclusive_group(required=True)
    target.add_argument("job_key", nargs="?")
    target.add_argument("--failed", action="store_true", help="every failed job")

    sub.add_parser("status", help="job counts, backlog age, retrying, failed and held jobs")

    hold_cmd = sub.add_parser("hold", help="put a call on legal hold: purge keeps it until unhold")
    hold_cmd.add_argument("job_key")
    hold_cmd.add_argument("--reason", required=True, help="why, e.g. the case number (audited)")

    unhold_cmd = sub.add_parser("unhold", help="release a legal hold; retention applies again")
    unhold_cmd.add_argument("job_key")
    unhold_cmd.add_argument("--reason", default="", help="why (audited)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config.resolve())
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "doctor":
        checks = doctor(cfg, make_client(cfg.litellm))
        for check in checks:
            print(f"[{'OK ' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        down = [c for c in checks if c.name.startswith("litellm") and not c.ok]
        alerter = make_alerter(cfg)
        if alerter is not None and down:
            details = "; ".join(f"{c.name}: {c.detail}" for c in down)
            alerter.send(LITELLM_DOWN, f"doctor: {details}", _utcnow())
        return 0 if all(c.ok for c in checks) else 1

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _ensure_dirs(cfg)
    try:
        _read_vocabulary(cfg)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        store, audit = _open(cfg)
    except SchemaError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "retry":
        return _retry(store, args.job_key, args.failed)
    if args.command == "status":
        return _status(cfg, store)
    if args.command == "hold":
        return _hold(store, audit, args.job_key, args.reason)
    if args.command == "unhold":
        return _unhold(store, audit, args.job_key, args.reason)

    try:
        with instance_lock(cfg.paths.db_path):
            if args.command == "process":
                return _process(cfg, store, audit, args.audio, args.sidecar)
            if args.command == "run":
                return _serve(cfg, store, audit, args.once)
            if args.command == "purge":
                return _report_purge(cfg, store, audit)
    except LockError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
