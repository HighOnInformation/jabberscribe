from jabberscribe.jobs import QUEUED, JobStore
from jabberscribe.watcher import find_ready_pairs, scan_once


def _store(cfg) -> JobStore:
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    return store


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
    """A just-written file can report an mtime ahead of the clock on Windows.

    Clamping the age at zero keeps that from deferring a pair the caller asked
    for immediately -- the bug that made `process` intermittently find nothing.
    """
    audio = make_wav(cfg.paths.inbox / "a.wav")
    sidecar = make_sidecar(cfg.paths.inbox / "a.json")
    ahead = 5.0

    pairs = find_ready_pairs(cfg.paths.inbox, 0, now=audio.stat().st_mtime - ahead)

    assert pairs == [(audio, sidecar)]


def test_scan_enqueues_and_moves_audio_out_of_inbox(cfg, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="abc")
    store = _store(cfg)

    result = scan_once(cfg, store)

    assert result.enqueued == ("abc",)
    job = store.get("abc")
    assert job is not None
    assert job.status == QUEUED
    assert job.audio_path == cfg.paths.audio_store / "abc.wav"
    assert job.audio_path.is_file()
    assert not (cfg.paths.inbox / "a.wav").exists()
    assert not (cfg.paths.inbox / "a.json").exists()


def test_scan_dedups_repeated_call_id(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="dup")
    scan_once(cfg, store)

    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="dup")
    result = scan_once(cfg, store)

    assert result.enqueued == ()
    assert result.skipped == ("dup",)
    assert len(store.list_by_status(QUEUED)) == 1


def test_scan_quarantines_invalid_sidecar(cfg, make_wav) -> None:
    make_wav(cfg.paths.inbox / "bad.wav")
    (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")
    store = _store(cfg)

    result = scan_once(cfg, store)

    assert result.quarantined == ("bad",)
    assert (cfg.paths.quarantine / "bad.wav").is_file()
    assert (cfg.paths.quarantine / "bad.json").is_file()
    reason = (cfg.paths.quarantine / "bad.reason.txt").read_text(encoding="utf-8")
    assert "JSON" in reason
    assert store.list_by_status(QUEUED) == []


def test_quarantine_does_not_collide_on_repeat(cfg, make_wav) -> None:
    store = _store(cfg)
    for _ in range(2):
        make_wav(cfg.paths.inbox / "bad.wav")
        (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")
        scan_once(cfg, store)

    quarantined = sorted(p.name for p in cfg.paths.quarantine.glob("bad*.wav"))
    assert len(quarantined) == 2


def test_scan_of_empty_inbox_is_harmless(cfg) -> None:
    result = scan_once(cfg, _store(cfg))

    assert (result.enqueued, result.quarantined, result.skipped) == ((), (), ())


def test_scan_honours_min_age_override(cfg, make_wav, make_sidecar) -> None:
    """`process` needs to bypass the settling delay for a one-shot run."""
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="ovr")
    store = _store(cfg)

    deferred = scan_once(cfg, store, min_age_seconds=3600)
    assert deferred.enqueued == ()

    immediate = scan_once(cfg, store, min_age_seconds=0)
    assert immediate.enqueued == ("ovr",)
