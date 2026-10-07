import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jabberscribe.cli import main
from jabberscribe.health import (
    HEARTBEAT_FILE,
    heartbeat_path,
    latency_summary,
    percentile,
    read_heartbeat,
    write_heartbeat,
)
from jabberscribe.jobs import DONE, QUEUED


def _job(store, key: str) -> None:
    store.create(
        job_key=key,
        call_id=key.split("_")[0],
        conference_id=None,
        audio_path=Path(f"/out/{key}/recording.wav"),
        out_dir=Path(f"/out/{key}"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )


def _finished(store, latencies: tuple[float, ...]) -> None:
    for index, latency in enumerate(latencies):
        key = f"k{index}_1"
        _job(store, key)
        store.record_output(key, latency)
        store.set_status(key, DONE)


def test_percentile_is_nearest_rank() -> None:
    assert percentile([5.0], 95) == 5.0
    assert [percentile([900.0, 60.0, 120.0], p) for p in (50, 95)] == [120.0, 900.0]
    assert percentile([float(n) for n in range(1, 101)], 95) == 95.0


def test_latencies_are_read_back_for_a_time_window(store) -> None:
    _job(store, "a_1")
    _job(store, "b_2")

    store.record_output("a_1", 300.0)

    assert store.latencies_since(datetime.now(UTC) - timedelta(hours=24)) == [300.0]
    assert store.latencies_since(datetime.now(UTC) + timedelta(minutes=1)) == []
    job = store.get("a_1")
    assert job.latency_sec == 300.0
    assert job.output_at is not None


def test_latency_summary(store) -> None:
    assert latency_summary(store, datetime.now(UTC)) == "latency 24h (hang-up to output): no calls"

    _finished(store, (60.0, 120.0, 900.0))

    assert latency_summary(store, datetime.now(UTC)).endswith("3 call(s), p50 2.0 min, p95 15.0 min")


def test_status_counts_and_oldest_pending(store) -> None:
    _job(store, "a_1")
    _job(store, "b_2")
    store.set_status("b_2", DONE)

    assert store.status_counts() == {QUEUED: 1, DONE: 1}
    assert store.oldest_pending_created_at() == store.get("a_1").created_at

    store.set_status("a_1", DONE)
    assert store.oldest_pending_created_at() is None


def test_heartbeat_round_trip(store, tmp_path: Path) -> None:
    _job(store, "a_1")
    path = heartbeat_path(tmp_path / "js.db")

    write_heartbeat(path, store, datetime.now(UTC), polls=7, last_poll_ok=False)

    beat = read_heartbeat(path)
    assert path == tmp_path / HEARTBEAT_FILE
    assert (beat["polls"], beat["last_poll_ok"], beat["counts"]) == (7, False, {QUEUED: 1})
    assert beat["oldest_pending_age_sec"] >= 0
    assert list(tmp_path.glob("*.part")) == []


def test_an_unreadable_heartbeat_reads_as_none(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")

    assert read_heartbeat(tmp_path / "missing.json") is None
    assert read_heartbeat(bad) is None


def test_run_once_writes_a_heartbeat(cfg_file, tmp_path: Path) -> None:
    assert main(["--config", str(cfg_file), "run", "--once"]) == 0

    beat = json.loads((tmp_path / HEARTBEAT_FILE).read_text(encoding="utf-8"))
    assert (beat["polls"], beat["last_poll_ok"], beat["counts"], beat["oldest_pending_age_sec"]) == (1, True, {}, None)


def test_status_shows_latency_and_heartbeat(cfg_file, store, capsys) -> None:
    _finished(store, (60.0, 120.0, 900.0))

    assert main(["--config", str(cfg_file), "status"]) == 0
    out = capsys.readouterr().out
    assert "3 call(s), p50 2.0 min, p95 15.0 min" in out
    assert "heartbeat: none" in out

    main(["--config", str(cfg_file), "run", "--once"])
    capsys.readouterr()
    main(["--config", str(cfg_file), "status"])
    assert "last poll ok" in capsys.readouterr().out


def test_a_heartbeat_with_a_naive_timestamp_reads_as_none(tmp_path: Path) -> None:
    naive = tmp_path / "naive.json"
    naive.write_text(json.dumps({"last_poll_at": "2026-10-07T11:03:11"}), encoding="utf-8")

    assert read_heartbeat(naive) is None
