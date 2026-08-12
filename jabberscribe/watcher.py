"""Inbox watcher: the service's only entry point for new work.

The readiness rule comes from the drop contract -- the recorder writes audio
first (as .part, then renames) and the sidecar last, so a sidecar's presence
proves the audio is complete. A min-age guard catches a recorder that died
mid-write, where the sidecar exists but nothing is finished.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from jabberscribe.config import Config
from jabberscribe.jobs import JobStore
from jabberscribe.sidecar import SidecarError, parse_sidecar

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".ogg")


@dataclass(frozen=True)
class ScanResult:
    enqueued: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


def find_ready_pairs(inbox: Path, min_age_seconds: int, now: float | None = None) -> list[tuple[Path, Path]]:
    """Return (audio, sidecar) pairs that are complete and settled."""
    current = time.time() if now is None else now
    pairs: list[tuple[Path, Path]] = []
    for sidecar in sorted(inbox.glob("*.json")):
        audio = next((c for suffix in AUDIO_SUFFIXES if (c := sidecar.with_suffix(suffix)).is_file()), None)
        if audio is None:
            log.debug("sidecar without audio, skipping: %s", sidecar)
            continue
        youngest = max(audio.stat().st_mtime, sidecar.stat().st_mtime)
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


def quarantine_pair(paths: list[Path], quarantine_dir: Path, reason: str) -> None:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    stem = paths[0].stem
    for path in paths:
        if path.exists():
            shutil.move(str(path), str(_unique_target(quarantine_dir, path.name)))
    _unique_target(quarantine_dir, f"{stem}.reason.txt").write_text(reason, encoding="utf-8")
    log.warning("quarantined %s: %s", stem, reason)


def scan_once(cfg: Config, store: JobStore, min_age_seconds: int | None = None) -> ScanResult:
    """Process every ready pair in the inbox exactly once.

    `min_age_seconds` overrides the configured settling delay. The `process`
    command passes 0: a human handing us one file is not a race with a recorder.
    """
    cfg.paths.audio_store.mkdir(parents=True, exist_ok=True)
    min_age = cfg.watcher.min_age_seconds if min_age_seconds is None else min_age_seconds
    enqueued: list[str] = []
    quarantined: list[str] = []
    skipped: list[str] = []

    for audio, sidecar_path in find_ready_pairs(cfg.paths.inbox, min_age):
        try:
            # utf-8-sig tolerates a BOM and is identical to utf-8 without one.
            sidecar = parse_sidecar(sidecar_path.read_text(encoding="utf-8-sig"))
        except (SidecarError, OSError, UnicodeDecodeError) as exc:
            quarantine_pair([audio, sidecar_path], cfg.paths.quarantine, str(exc))
            quarantined.append(sidecar_path.stem)
            continue

        stored_audio = cfg.paths.audio_store / f"{sidecar.call_id}{audio.suffix}"
        created = store.create(
            call_id=sidecar.call_id,
            audio_path=stored_audio,
            sidecar_json=sidecar.raw,
            kind=sidecar.kind,
            started_at=sidecar.started_at,
            duration_sec=sidecar.duration_sec,
        )
        if not created:
            # Already known. Drop the duplicate rather than reprocess it.
            log.info("duplicate call_id %s, discarding inbox copy", sidecar.call_id)
            audio.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            skipped.append(sidecar.call_id)
            continue

        shutil.move(str(audio), str(stored_audio))
        sidecar_path.unlink(missing_ok=True)
        enqueued.append(sidecar.call_id)
        log.info("enqueued %s", sidecar.call_id)

    return ScanResult(tuple(enqueued), tuple(quarantined), tuple(skipped))
