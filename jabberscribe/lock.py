"""Single-instance lock.

claim_next reclaims RUNNING rows as crash leftovers, which is only safe while
one process works the database. `run`, `process` and `purge` therefore hold an
exclusive OS lock on <db_path parent>/jabberscribe.lock; a second instance
fails fast instead of transcribing the same call twice. The OS releases the
lock when the process dies, so a crash never leaves a stale lock behind.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

LOCK_NAME = "jabberscribe.lock"


class LockError(RuntimeError):
    """Another JabberScribe instance holds the lock."""


def lock_path(db_path: Path) -> Path:
    return db_path.parent / LOCK_NAME


def _acquire(fh: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fh: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def instance_lock(db_path: Path) -> Iterator[None]:
    """Hold the instance lock for the duration of the block. Raises LockError if it is taken."""
    path = lock_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+b")
    try:
        _acquire(fh)
    except OSError as exc:
        fh.close()
        raise LockError(f"another JabberScribe instance is running (lock held: {path})") from exc
    try:
        yield
    finally:
        _release(fh)
        fh.close()
