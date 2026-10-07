from pathlib import Path

import pytest

from jabberscribe.lock import LockError, instance_lock, lock_path


def test_lock_file_sits_next_to_the_database(tmp_path: Path) -> None:
    assert lock_path(tmp_path / "data" / "js.db") == tmp_path / "data" / "jabberscribe.lock"


def test_second_holder_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    with instance_lock(db):
        with pytest.raises(LockError, match="another JabberScribe instance"):
            with instance_lock(db):
                pass


def test_lock_is_released_after_the_block(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    with instance_lock(db):
        pass

    with instance_lock(db):
        pass


def test_lock_is_released_when_the_block_raises(tmp_path: Path) -> None:
    db = tmp_path / "js.db"
    with pytest.raises(RuntimeError):
        with instance_lock(db):
            raise RuntimeError("boom")

    with instance_lock(db):
        pass
