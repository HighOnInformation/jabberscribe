from jabberscribe.jobs import QUEUED, WAITING, JobStore
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
    """A just-written file can report an mtime ahead of the clock on Windows."""
    audio = make_wav(cfg.paths.inbox / "a.wav")
    sidecar = make_sidecar(cfg.paths.inbox / "a.json")

    pairs = find_ready_pairs(cfg.paths.inbox, 0, now=audio.stat().st_mtime - 5.0)

    assert pairs == [(audio, sidecar)]


def test_scan_enqueues_into_dated_out_dir(cfg, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="abc")
    store = _store(cfg)

    result = scan_once(cfg, store)

    assert result.enqueued == ("abc_1042",)
    job = store.get("abc_1042")
    assert job.status == QUEUED
    assert job.out_dir == cfg.paths.out_root / "2026" / "10" / "abc_1042"
    assert job.audio_path == job.out_dir / "recording.wav"
    assert job.audio_path.is_file()
    assert not (cfg.paths.inbox / "a.wav").exists()
    assert not (cfg.paths.inbox / "a.json").exists()


def test_conference_copy_is_enqueued_waiting(cfg, make_wav, make_sidecar) -> None:
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="leg", conference_id="conf-1")
    store = _store(cfg)

    scan_once(cfg, store)

    assert store.get("leg_1042").status == WAITING


def test_bom_encoded_sidecar_is_accepted(cfg, make_wav, make_sidecar) -> None:
    """A recorder written in PowerShell or .NET emits BOM'd JSON by default."""
    make_wav(cfg.paths.inbox / "a.wav")
    sidecar = make_sidecar(cfg.paths.inbox / "a.json", call_id="bom")
    sidecar.write_bytes(b"\xef\xbb\xbf" + sidecar.read_bytes())

    result = scan_once(cfg, _store(cfg))

    assert result.enqueued == ("bom_1042",)
    assert result.quarantined == ()


def test_scan_dedups_repeated_line_copy(cfg, make_wav, make_sidecar) -> None:
    store = _store(cfg)
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="dup")
    scan_once(cfg, store)

    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="dup")
    result = scan_once(cfg, store)

    assert result.enqueued == ()
    assert result.skipped == ("dup_1042",)
    assert not (cfg.paths.inbox / "b.wav").exists()
    assert len(store.list_all()) == 1


def test_same_call_on_two_lines_is_two_jobs(cfg, make_wav, make_sidecar) -> None:
    """Both ends of an internal call are recorded; each line owner gets a copy."""
    store = _store(cfg)
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="gc", extension="1042")
    make_wav(cfg.paths.inbox / "b.wav")
    make_sidecar(cfg.paths.inbox / "b.json", call_id="gc", extension="2210")

    result = scan_once(cfg, store)

    assert sorted(result.enqueued) == ["gc_1042", "gc_2210"]


def test_scan_quarantines_invalid_sidecar(cfg, make_wav) -> None:
    make_wav(cfg.paths.inbox / "bad.wav")
    (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")
    store = _store(cfg)

    result = scan_once(cfg, store)

    assert result.quarantined == ("bad",)
    assert (cfg.paths.quarantine / "bad.wav").is_file()
    assert (cfg.paths.quarantine / "bad.json").is_file()
    assert "JSON" in (cfg.paths.quarantine / "bad.reason.txt").read_text(encoding="utf-8")
    assert store.list_all() == []


def test_quarantine_does_not_collide_on_repeat(cfg, make_wav) -> None:
    store = _store(cfg)
    for _ in range(2):
        make_wav(cfg.paths.inbox / "bad.wav")
        (cfg.paths.inbox / "bad.json").write_text("{not json", encoding="utf-8")
        scan_once(cfg, store)

    assert len(list(cfg.paths.quarantine.glob("bad*.wav"))) == 2


def test_scan_of_empty_inbox_is_harmless(cfg) -> None:
    result = scan_once(cfg, _store(cfg))

    assert (result.enqueued, result.quarantined, result.skipped) == ((), (), ())


def test_scan_honours_min_age_override(cfg, make_wav, make_sidecar) -> None:
    """`process` needs to bypass the settling delay for a one-shot run."""
    make_wav(cfg.paths.inbox / "a.wav")
    make_sidecar(cfg.paths.inbox / "a.json", call_id="ovr")
    store = _store(cfg)

    assert scan_once(cfg, store, min_age_seconds=3600).enqueued == ()
    assert scan_once(cfg, store, min_age_seconds=0).enqueued == ("ovr_1042",)
