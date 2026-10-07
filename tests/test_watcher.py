import shutil
import sqlite3
from pathlib import Path

from jabberscribe.audit import DISCARDED_DUPLICATE, QUARANTINED
from jabberscribe.jobs import QUEUED, WAITING
from jabberscribe.watcher import find_ready_pairs, scan_once


def test_pair_without_sidecar_is_not_ready(cfg, make_wav) -> None:
    make_wav(cfg.paths.inbox / "a.wav")

    assert find_ready_pairs(cfg.paths.inbox, 0) == []


def test_partial_audio_is_ignored(cfg, make_sidecar) -> None:
    (cfg.paths.inbox / "a.wav.part").write_bytes(b"RIFF")
    make_sidecar(cfg.paths.inbox / "a.json")

    assert find_ready_pairs(cfg.paths.inbox, 0) == []


def test_complete_pair_is_ready(cfg, make_wav, make_sidecar) -> None:
    audio = make_wav(cfg.paths.inbox / "a.wav")
    sidecar = make_sidecar(cfg.paths.inbox / "a.json")

    assert find_ready_pairs(cfg.paths.inbox, 0) == [(audio, sidecar)]


def test_min_age_guard_defers_fresh_files(cfg, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json")

    assert find_ready_pairs(cfg.paths.inbox, min_age_seconds=3600) == []


def test_future_mtime_does_not_defer_a_zero_min_age_pair(cfg, make_wav, make_sidecar) -> None:
    """A just-written file can report an mtime ahead of the clock on Windows."""
    audio = make_wav(cfg.paths.inbox / "a.wav")
    sidecar = make_sidecar(cfg.paths.inbox / "a.json")

    pairs = find_ready_pairs(cfg.paths.inbox, 0, now=audio.stat().st_mtime - 5.0)

    assert pairs == [(audio, sidecar)]


def test_scan_enqueues_into_dated_out_dir(cfg, store, audit, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="abc")

    result = scan_once(cfg, store, audit)

    assert result.enqueued == ("abc_1042",)
    job = store.get("abc_1042")
    assert job.status == QUEUED
    assert job.out_dir == cfg.paths.out_root / "2026" / "10" / "abc_1042"
    assert job.audio_path == job.out_dir / "recording.wav"
    assert job.audio_path.is_file()
    assert not (cfg.paths.inbox / "a.wav").exists()
    assert not (cfg.paths.inbox / "a.json").exists()


def test_conference_copy_is_enqueued_waiting(cfg, store, audit, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="leg", conference_id="conf-1")

    scan_once(cfg, store, audit)

    assert store.get("leg_1042").status == WAITING


def test_bom_encoded_sidecar_is_accepted(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A recorder written in PowerShell or .NET emits BOM'd JSON by default."""
    make_wav(cfg.paths.inbox / "a.wav")
    sidecar = make_sidecar(cfg.paths.inbox / "a.json", call_id="bom")
    sidecar.write_bytes(b"\xef\xbb\xbf" + sidecar.read_bytes())

    result = scan_once(cfg, store, audit)

    assert result.enqueued == ("bom_1042",)
    assert result.quarantined == ()


def test_scan_dedups_repeated_line_copy_and_audits_the_discard(cfg, store, audit, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="dup")
    scan_once(cfg, store, audit)

    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="dup")
    result = scan_once(cfg, store, audit)

    assert result.enqueued == ()
    assert result.skipped == ("dup_1042",)
    assert not (cfg.paths.inbox / "b.wav").exists()
    assert len(store.list_all()) == 1
    assert [e.action for e in audit.entries("dup_1042")] == [DISCARDED_DUPLICATE]
    assert "b.wav" in audit.entries("dup_1042")[0].detail


def test_same_call_on_two_lines_is_two_jobs(cfg, store, audit, make_wav, make_sidecar) -> None:
    """Both ends of an internal call are recorded; each line owner gets a copy."""
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="gc", extension="1042")
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="gc", extension="2210")

    result = scan_once(cfg, store, audit)

    assert sorted(result.enqueued) == ["gc_1042", "gc_2210"]


def test_scan_quarantines_invalid_sidecar_and_audits_it(cfg, store, audit, make_wav) -> None:
    make_wav(cfg.paths.inbox / "bad.wav")
    (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")

    result = scan_once(cfg, store, audit)

    assert result.quarantined == ("bad",)
    assert (cfg.paths.quarantine / "bad.wav").is_file()
    assert (cfg.paths.quarantine / "bad.json").is_file()
    assert "JSON" in (cfg.paths.quarantine / "bad.reason.txt").read_text(encoding="utf-8")
    assert store.list_all() == []
    entry = audit.entries("bad")[0]
    assert entry.action == QUARANTINED
    assert "bad.wav" in entry.detail


def test_non_utf8_sidecar_is_quarantined(cfg, store, audit, make_wav) -> None:
    make_wav(cfg.paths.inbox / "enc.wav")
    (cfg.paths.inbox / "enc.json").write_bytes(b'{"call_id": "\xff"}')

    assert scan_once(cfg, store, audit).quarantined == ("enc",)


def test_future_started_at_is_quarantined(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A recorder without NTP would otherwise create a call retention never purges."""
    make_wav(cfg.paths.inbox / "f.wav")
    make_sidecar(cfg.paths.inbox / "f.json", call_id="future", started_at="2099-01-01T00:00:00+00:00")

    result = scan_once(cfg, store, audit)

    assert result.quarantined == ("f",)
    assert "future" in (cfg.paths.quarantine / "f.reason.txt").read_text(encoding="utf-8")


def test_quarantine_does_not_collide_on_repeat(cfg, store, audit, make_wav) -> None:
    for _ in range(2):
        make_wav(cfg.paths.inbox / "bad.wav")
        (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")
        scan_once(cfg, store, audit)

    assert len(list(cfg.paths.quarantine.glob("bad*.wav"))) == 2


def test_scan_of_empty_inbox_is_harmless(cfg, store, audit) -> None:
    result = scan_once(cfg, store, audit)

    assert (result.enqueued, result.quarantined, result.skipped, result.deferred) == ((), (), (), ())


def test_scan_honours_min_age_override(cfg, store, audit, make_wav, make_sidecar) -> None:
    """`process` needs to bypass the settling delay for a one-shot run."""
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="ovr")

    assert scan_once(cfg, store, audit, min_age_seconds=3600).enqueued == ()
    assert scan_once(cfg, store, audit, min_age_seconds=0).enqueued == ("ovr_1042",)


def test_failed_copy_keeps_the_recording_for_retry(cfg, store, audit, make_wav, make_sidecar, monkeypatch) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="abc")
    monkeypatch.setattr("jabberscribe.watcher.shutil.copyfile", _raise_disk_full)

    result = scan_once(cfg, store, audit)

    assert result.deferred == ("a",)
    assert store.list_all() == []
    assert (cfg.paths.inbox / "a.wav").exists()
    assert (cfg.paths.inbox / "a.json").exists()

    monkeypatch.undo()
    result = scan_once(cfg, store, audit)

    assert result.enqueued == ("abc_1042",)
    assert store.get("abc_1042").audio_path.is_file()
    assert not (cfg.paths.inbox / "a.wav").exists()
    assert not (cfg.paths.inbox / "a.json").exists()


def test_one_unreadable_pair_does_not_block_the_rest(cfg, store, audit, make_wav, make_sidecar, monkeypatch) -> None:
    """A locked file sorts first; everything after it must still be ingested."""
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="locked")
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="fine")
    real_copy = shutil.copyfile

    def copy_unless_locked(src, dst, *args, **kwargs):
        if str(src).endswith("a.wav"):
            raise PermissionError("held by antivirus")
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr("jabberscribe.watcher.shutil.copyfile", copy_unless_locked)

    result = scan_once(cfg, store, audit)

    assert result.deferred == ("a",)
    assert result.enqueued == ("fine_1042",)


def test_no_partial_recording_is_left(cfg, store, audit, make_wav, make_sidecar, monkeypatch) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json")
    scan_once(cfg, store, audit)
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="c2")
    monkeypatch.setattr("jabberscribe.watcher.shutil.copyfile", _write_half_then_fail)

    scan_once(cfg, store, audit)

    assert list(cfg.paths.out_root.rglob("*.part")) == []


def test_quarantine_audits_before_moving_and_moves_sidecar_first(cfg, store, audit, make_wav, monkeypatch) -> None:
    make_wav(cfg.paths.inbox / "bad.wav")
    (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")
    real_move = shutil.move
    calls = []

    def move_once_then_fail(src, dst, *args, **kwargs):
        calls.append(src)
        if len(calls) == 2:
            raise PermissionError("held by antivirus")
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr("jabberscribe.watcher.shutil.move", move_once_then_fail)

    result = scan_once(cfg, store, audit)

    assert result.deferred == ("bad",)
    assert result.quarantined == ()
    # The evidence trail exists even though the pair is split.
    assert "JSON" in (cfg.paths.quarantine / "bad.reason.txt").read_text(encoding="utf-8")
    assert [e.action for e in audit.entries("bad")] == [QUARANTINED]
    # Sidecar went first; the lone audio stays in the inbox, invisible to the next scan.
    assert (cfg.paths.quarantine / "bad.json").is_file()
    assert (cfg.paths.inbox / "bad.wav").is_file()
    assert not (cfg.paths.quarantine / "bad.wav").exists()
    assert not (cfg.paths.inbox / "bad.json").exists()
    assert find_ready_pairs(cfg.paths.inbox, 0) == []


def test_duplicate_discard_is_audited_before_the_unlinks(
    cfg, store, audit, make_wav, make_sidecar, monkeypatch
) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="dup")
    scan_once(cfg, store, audit)
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="dup")
    real_unlink = Path.unlink

    def unlink_unless_sidecar(self, *args, **kwargs):
        if self.name == "b.json":
            raise PermissionError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_unless_sidecar)

    result = scan_once(cfg, store, audit)

    assert result.deferred == ("b",)
    assert [e.action for e in audit.entries("dup_1042")] == [DISCARDED_DUPLICATE]


def test_failed_create_leaves_the_inbox_pair_in_place(cfg, store, audit, make_wav, make_sidecar, monkeypatch) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="abc")

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "create", locked)

    assert scan_once(cfg, store, audit).deferred == ("a",)

    assert (cfg.paths.inbox / "a.wav").is_file()
    assert (cfg.paths.inbox / "a.json").is_file()
    assert store.list_all() == []


def test_inbox_unlink_failure_after_create_becomes_an_audited_duplicate(
    cfg, store, audit, make_wav, make_sidecar, monkeypatch
) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="abc")
    real_unlink = Path.unlink

    def unlink_unless_inbox(self, *args, **kwargs):
        if self.parent == cfg.paths.inbox:
            raise PermissionError("locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink_unless_inbox)

    assert scan_once(cfg, store, audit).deferred == ("a",)
    job = store.get("abc_1042")
    assert job is not None
    assert (cfg.paths.inbox / "a.wav").is_file()

    monkeypatch.undo()
    result = scan_once(cfg, store, audit)

    assert result.skipped == ("abc_1042",)
    assert [e.action for e in audit.entries("abc_1042")] == [DISCARDED_DUPLICATE]
    assert job.audio_path.is_file()
    assert not (cfg.paths.inbox / "a.wav").exists()


def _raise_disk_full(*args, **kwargs):
    raise OSError("disk full")


def _write_half_then_fail(src, dst, *args, **kwargs):
    with open(dst, "wb") as fh:
        fh.write(b"RIFF")
    raise OSError("disk full")


def test_deeply_nested_sidecar_is_quarantined_and_the_scan_goes_on(cfg, store, audit, make_wav, make_sidecar) -> None:
    """Reviewer probe: json.loads raises RecursionError, not JSONDecodeError, on deep nesting."""
    make_wav(cfg.paths.inbox / "a.wav")
    (cfg.paths.inbox / "a.json").write_text("[" * 100000, encoding="utf-8")
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="fine")

    result = scan_once(cfg, store, audit)

    assert result.quarantined == ("a",)
    assert result.enqueued == ("fine_1042",)
    assert (cfg.paths.quarantine / "a.reason.txt").read_text(encoding="utf-8")


def test_any_sidecar_parse_error_quarantines_with_its_reason(cfg, store, audit, make_wav, make_sidecar, monkeypatch):
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json")

    def broken(text):
        raise ValueError("parser bug")

    monkeypatch.setattr("jabberscribe.watcher.parse_sidecar", broken)

    assert scan_once(cfg, store, audit).quarantined == ("a",)
    assert "parser bug" in (cfg.paths.quarantine / "a.reason.txt").read_text(encoding="utf-8")


def test_unexpected_error_on_one_pair_defers_it_and_the_scan_goes_on(
    cfg, store, audit, make_wav, make_sidecar, monkeypatch
) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="stuck")
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="fine")
    real_create = store.create

    def create(**kwargs):
        if kwargs["call_id"] == "stuck":
            raise sqlite3.OperationalError("database is locked")
        return real_create(**kwargs)

    monkeypatch.setattr(store, "create", create)

    result = scan_once(cfg, store, audit)

    assert result.deferred == ("a",)
    assert result.enqueued == ("fine_1042",)
    assert (cfg.paths.inbox / "a.json").is_file()
