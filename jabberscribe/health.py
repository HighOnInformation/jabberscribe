"""Liveness and latency for operators.

`run` rewrites heartbeat.json next to the database after every poll, so an
external monitor (a scheduled task, a file-age sensor) can alarm when it goes
stale -- a hung service writes nothing, so it cannot report itself. `status`
reports the hang-up-to-output latency percentiles that the spec's 15-minute
budget is measured against.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

from jabberscribe.jobs import JobStore, iso
from jabberscribe.output import write_atomic

HEARTBEAT_FILE = "heartbeat.json"
LATENCY_WINDOW = timedelta(hours=24)


def heartbeat_path(db_path: Path) -> Path:
    return db_path.parent / HEARTBEAT_FILE


def pending_age_sec(store: JobStore, now: datetime) -> float | None:
    """Seconds since the oldest unfinished job arrived, or None when nothing is pending."""
    oldest = store.oldest_pending_created_at()
    if oldest is None:
        return None
    return round((now - datetime.fromisoformat(oldest)).total_seconds(), 1)


def write_heartbeat(path: Path, store: JobStore, now: datetime, *, polls: int, last_poll_ok: bool) -> None:
    beat = {
        "last_poll_at": iso(now),
        "pid": os.getpid(),
        "polls": polls,
        "last_poll_ok": last_poll_ok,
        "counts": store.status_counts(),
        "oldest_pending_age_sec": pending_age_sec(store, now),
    }
    write_atomic(path, json.dumps(beat, indent=2))


def read_heartbeat(path: Path) -> dict | None:
    """The last heartbeat, or None when there is none or it cannot be read.

    A last_poll_at without a timezone is unreadable too: its age cannot be compared with an aware clock.
    """
    try:
        beat = json.loads(path.read_text(encoding="utf-8"))
        last_poll = datetime.fromisoformat(beat["last_poll_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if last_poll.tzinfo is None:
        return None
    return beat if isinstance(beat, dict) else None


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty list: an observed value, never an interpolation."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def latency_summary(store: JobStore, now: datetime) -> str:
    latencies = store.latencies_since(now - LATENCY_WINDOW)
    if not latencies:
        return "latency 24h (hang-up to output): no calls"
    p50, p95 = percentile(latencies, 50) / 60, percentile(latencies, 95) / 60
    return f"latency 24h (hang-up to output): {len(latencies)} call(s), p50 {p50:.1f} min, p95 {p95:.1f} min"
