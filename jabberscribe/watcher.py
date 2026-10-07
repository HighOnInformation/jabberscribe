"""Inbox watcher: the service's only entry point for new work.

The readiness rule comes from the drop contract -- the recorder writes audio
first (as .part, then renames) and the sidecar last, so a sidecar's presence
proves the audio is complete. A min-age guard catches a recorder that died
mid-write, where the sidecar exists but nothing is finished.

One bad pair must never block the rest of the inbox: a filesystem error on a
pair is logged and the pair is left for the next scan.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.audit import DISCARDED_DUPLICATE, QUARANTINED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import JobStore
from jabberscribe.sidecar import Sidecar, SidecarError, parse_sidecar

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".ogg")

#: A recorder clock this far ahead is wrong, and retention would never purge the call.
MAX_FUTURE = timedelta(days=1)


@dataclass(frozen=True)
class ScanResult:
    enqueued: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    #: Stems of pairs that hit a filesystem error and stay in the inbox for the next scan.
    deferred: tuple[str, ...] = ()


def find_ready_pairs(inbox: Path, min_age_seconds: int, now: float | None = None) -> list[tuple[Path, Path]]:
    """Return (audio, sidecar) pairs that are complete and settled."""
    current = time.time() if now is None else now
    pairs: list[tuple[Path, Path]] = []
    for sidecar in sorted(inbox.glob("*.json")):
        audio = next((c for suffix in AUDIO_SUFFIXES if (c := sidecar.with_suffix(suffix)).is_file()), None)
        if audio is None:
            log.debug("sidecar without audio, skipping: %s", sidecar)
            continue
        try:
            youngest = max(audio.stat().st_mtime, sidecar.stat().st_mtime)
        except OSError as exc:
            log.warning("cannot stat %s, skipping this scan: %s", sidecar.stem, exc)
            continue
        # Clamp at zero: a just-written file can report an mtime slightly ahead
        # of time.time() (filesystem and clock resolution differ on Windows),
        # which would make a negative age look younger than any positive
        # threshold and defer a pair that min_age_seconds=0 means to take now.
        age = max(0.0, current - youngest)
        if age < min_age_seconds:
            log.debug("pair still settling, deferring: %s", sidecar.stem)
            continue
        pairs.append((audio, sidecar))
    return pairs


def _unique_target(directory: Path, name: str) -> Path:
    """Never overwrite. A repeated bad file must not erase the earlier evidence."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free name for {name} in {directory}")


def quarantine_pair(paths: list[Path], quarantine_dir: Path, reason: str, audit: AuditLog) -> None:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    stem = paths[0].stem
    present = [path for path in paths if path.exists()]
    # Evidence first: if a move below fails, the reason and audit row still exist.
    _unique_target(quarantine_dir, f"{stem}.reason.txt").write_text(reason, encoding="utf-8")
    audit.record(stem, QUARANTINED, f"{', '.join(path.name for path in present)}: {reason}")
    # Sidecar (last in `paths`) moves first: a lone audio file in the inbox is
    # invisible to find_ready_pairs, whereas a lone sidecar would be skipped forever.
    for path in reversed(present):
        shutil.move(str(path), str(_unique_target(quarantine_dir, path.name)))
    log.warning("quarantined %s: %s", stem, reason)


def out_dir_for(out_root: Path, sidecar: Sidecar) -> Path:
    """out_root/<YYYY>/<MM>/<job_key>, dated by when the call started."""
    started = datetime.fromisoformat(sidecar.started_at)
    return out_root / f"{started:%Y}" / f"{started:%m}" / sidecar.job_key


def _read_sidecar(path: Path, now: datetime) -> Sidecar:
    # utf-8-sig tolerates a BOM and is identical to utf-8 without one.
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SidecarError(f"sidecar is not UTF-8: {exc}") from exc
    sidecar = parse_sidecar(text)
    if datetime.fromisoformat(sidecar.started_at) > now + MAX_FUTURE:
        raise SidecarError(f"started_at {sidecar.started_at} is in the future; check the recorder clock")
    return sidecar


def _ingest(cfg: Config, store: JobStore, audit: AuditLog, audio: Path, sidecar: Sidecar, sidecar_path: Path) -> bool:
    """Move one valid pair into the store. Returns False when it was a duplicate."""
    if store.get(sidecar.job_key) is not None:
        # Already known. Drop the duplicate rather than reprocess it.
        log.info("duplicate %s, discarding inbox copy", sidecar.job_key)
        audit.record(sidecar.job_key, DISCARDED_DUPLICATE, f"{audio.name}, {sidecar_path.name}")
        audio.unlink(missing_ok=True)
        sidecar_path.unlink(missing_ok=True)
        return False

    out_dir = out_dir_for(cfg.paths.out_root, sidecar)
    stored_audio = out_dir / f"recording{audio.suffix}"
    # Every step is safe to repeat: a crash before create() leaves the inbox
    # pair in place and the next scan redoes the copy. The recording is never
    # deleted from the inbox before a job row references a complete copy.
    out_dir.mkdir(parents=True, exist_ok=True)
    part = stored_audio.with_suffix(stored_audio.suffix + ".part")
    try:
        shutil.copyfile(audio, part)
        part.replace(stored_audio)
    except OSError:
        part.unlink(missing_ok=True)
        raise
    store.create(
        job_key=sidecar.job_key,
        call_id=sidecar.call_id,
        conference_id=sidecar.conference_id,
        audio_path=stored_audio,
        out_dir=out_dir,
        sidecar_json=sidecar.raw,
        started_at=sidecar.started_at,
        duration_sec=sidecar.duration_sec,
    )
    audio.unlink(missing_ok=True)
    sidecar_path.unlink(missing_ok=True)
    return True


def scan_once(cfg: Config, store: JobStore, audit: AuditLog, min_age_seconds: int | None = None) -> ScanResult:
    """Process every ready pair in the inbox exactly once.

    `min_age_seconds` overrides the configured settling delay. The `process`
    command passes 0: a human handing us one file is not a race with a recorder.
    """
    min_age = cfg.watcher.min_age_seconds if min_age_seconds is None else min_age_seconds
    now = datetime.now(UTC)
    enqueued: list[str] = []
    quarantined: list[str] = []
    skipped: list[str] = []
    deferred: list[str] = []

    for audio, sidecar_path in find_ready_pairs(cfg.paths.inbox, min_age):
        try:
            try:
                sidecar = _read_sidecar(sidecar_path, now)
            except SidecarError as exc:
                quarantine_pair([audio, sidecar_path], cfg.paths.quarantine, str(exc), audit)
                quarantined.append(sidecar_path.stem)
                continue
            if _ingest(cfg, store, audit, audio, sidecar, sidecar_path):
                enqueued.append(sidecar.job_key)
                log.info("enqueued %s", sidecar.job_key)
            else:
                skipped.append(sidecar.job_key)
        except OSError as exc:
            # A locked file (AV scanner, indexer) or a bad ACL: leave the pair and move on.
            log.error("cannot ingest %s, will retry next scan: %s", sidecar_path.stem, exc, exc_info=True)
            deferred.append(sidecar_path.stem)

    return ScanResult(tuple(enqueued), tuple(quarantined), tuple(skipped), tuple(deferred))
