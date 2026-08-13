"""Command-line entry point.

`doctor` exists so a bad deployment fails loudly at install time rather than
silently at 2 a.m. Subcommands are added by later tasks.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jabberscribe.audit import AuditLog
from jabberscribe.config import Config, ConfigError, load_config
from jabberscribe.confluence import ConfluenceClient
from jabberscribe.jobs import JobStore
from jabberscribe.pipeline import run_once, unimplemented
from jabberscribe.stt import WhisperLocal, load_vocabulary
from jabberscribe.watcher import scan_once

DEFAULT_CONFIG = Path("config/jabberscribe.yaml")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _check_ffmpeg() -> Check:
    exe = shutil.which("ffmpeg")
    if exe is None:
        return Check("ffmpeg", False, "not found on PATH")
    try:
        proc = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("ffmpeg", False, f"{exe}: {exc}")
    if proc.returncode != 0:
        return Check("ffmpeg", False, f"{exe} exited {proc.returncode}")
    first_line = proc.stdout.splitlines()[0] if proc.stdout else exe
    return Check("ffmpeg", True, first_line)


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


def _check_stages(cfg: Config) -> Check:
    """Report stages that are enabled but not implemented yet.

    Activating an outer stage before it exists is a config mistake worth
    catching at startup rather than partway through someone's call.
    """
    enabled = ", ".join(cfg.pipeline.stages)
    missing = unimplemented(cfg.pipeline.stages)
    if missing:
        return Check("pipeline.stages", False, f"enabled but not implemented: {', '.join(missing)} (of {enabled})")
    return Check("pipeline.stages", True, enabled)


def doctor(cfg: Config) -> list[Check]:
    """Verify the environment. Creates missing directories as a side effect."""
    return [
        _check_ffmpeg(),
        _check_stages(cfg),
        _check_dir("paths.drop_root/inbox", cfg.paths.inbox),
        _check_dir("paths.drop_root/quarantine", cfg.paths.quarantine),
        _check_dir("paths.work_dir", cfg.paths.work_dir),
        _check_dir("paths.audio_store", cfg.paths.audio_store),
        _check_dir("paths.db_path parent", cfg.paths.db_path.parent),
    ]


def _transcriber(cfg: Config) -> WhisperLocal:
    device = cfg.stt.device
    if device == "auto":
        device = "cpu"  # CUDA selection is a deployment decision, made explicit in config
    return WhisperLocal(
        model=cfg.stt.model,
        compute_type=cfg.stt.compute_type,
        device=device,
        vocabulary=load_vocabulary(cfg.stt.vocabulary_file),
    )


def _confluence(cfg: Config) -> ConfluenceClient | None:
    """Build a Confluence client when publishing is enabled.

    The PAT comes from the environment only. Refusing to start without it beats
    discovering it missing when the first call reaches the publish stage.
    """
    if cfg.confluence is None or "publish" not in cfg.pipeline.stages:
        return None
    pat = os.environ.get("JABBERSCRIBE_CONFLUENCE_PAT")
    if not pat:
        raise ConfigError("publish is enabled but JABBERSCRIBE_CONFLUENCE_PAT is not set")
    return ConfluenceClient(cfg.confluence.base_url, pat)


def _audit(cfg: Config) -> AuditLog:
    audit = AuditLog(cfg.paths.db_path)
    audit.init_schema()
    return audit


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jabberscribe")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="path to jabberscribe.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="verify environment and create missing directories")

    process_cmd = sub.add_parser("process", help="process one recording from a path pair")
    process_cmd.add_argument("audio", type=Path)
    process_cmd.add_argument("sidecar", type=Path)

    run_cmd = sub.add_parser("run", help="watch the inbox and process jobs")
    run_cmd.add_argument("--once", action="store_true", help="single pass, then exit")

    sub.add_parser("purge", help="delete audio and pages past their retention window")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.command == "doctor":
        checks = doctor(cfg)
        for check in checks:
            print(f"[{'OK ' if check.ok else 'FAIL'}] {check.name}: {check.detail}")
        return 0 if all(c.ok for c in checks) else 1

    if args.command == "process":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        doctor(cfg)  # ensure directories exist
        store = JobStore(cfg.paths.db_path)
        store.init_schema()
        shutil.copy2(args.audio, cfg.paths.inbox / args.audio.name)
        shutil.copy2(args.sidecar, cfg.paths.inbox / args.sidecar.name)
        # min_age 0: a human handing us one file is not racing a recorder.
        result = scan_once(cfg, store, min_age_seconds=0)
        if result.quarantined:
            print(f"quarantined: {', '.join(result.quarantined)}", file=sys.stderr)
            return 1
        try:
            confluence = _confluence(cfg)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        run_once(cfg, store, _transcriber(cfg), audit=_audit(cfg), confluence=confluence)
        for call_id in result.enqueued:
            job = store.get(call_id)
            print(f"{call_id}: {job.status} -> {job.transcript_path}")
            if job.transcript_path and job.transcript_path.is_file():
                payload = json.loads(job.transcript_path.read_text(encoding="utf-8"))
                print(f"  {len(payload['segments'])} segments")
        return 0

    if args.command == "run":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        doctor(cfg)
        store = JobStore(cfg.paths.db_path)
        store.init_schema()
        transcriber = _transcriber(cfg)
        try:
            confluence = _confluence(cfg)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        audit = _audit(cfg)
        while True:
            scan_once(cfg, store)
            run_once(cfg, store, transcriber, audit=audit, confluence=confluence)
            if args.once:
                return 0
            time.sleep(cfg.watcher.poll_seconds)

    if args.command == "purge":
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        from jabberscribe.retention import purge

        store = JobStore(cfg.paths.db_path)
        store.init_schema()
        try:
            confluence = _confluence(cfg)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        result = purge(cfg, store, _audit(cfg), now=datetime.now(UTC), confluence=confluence)
        print(f"audio deleted: {len(result.audio_deleted)}")
        print(f"pages deleted: {len(result.pages_deleted)}")
        for problem in result.errors:
            print(f"  ! {problem}", file=sys.stderr)
        return 1 if result.errors else 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
