import json
import pathlib
import sqlite3
from datetime import UTC, datetime, timedelta

from jabberscribe import group
from jabberscribe.audit import SUPERSEDED
from jabberscribe.group import SettleResult, owners_for, settle
from jabberscribe.jobs import DONE, FAILED, GROUPED, QUEUED, RUNNING, WAITING
from jabberscribe.output import RESULT_FILE, TRANSCRIPT_FILE
from jabberscribe.watcher import scan_once


def _drop(cfg, store, audit, make_wav, make_sidecar, leg: str, ext: str, *, conference_id="conf-1", **extra) -> str:
    make_wav(cfg.paths.inbox / f"{leg}.wav")
    make_sidecar(cfg.paths.inbox / f"{leg}.json", call_id=leg, extension=ext, conference_id=conference_id, **extra)
    scan_once(cfg, store, audit, min_age_seconds=0)
    return f"{leg}_{ext}"


def _age(cfg, key: str, seconds: int) -> None:
    """Backdate a job's arrival; the store stamps created_at itself."""
    created = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    conn = sqlite3.connect(cfg.paths.db_path)
    conn.execute("UPDATE jobs SET created_at = ? WHERE job_key = ?", (created, key))
    conn.commit()
    conn.close()


def _now() -> datetime:
    return datetime.now(UTC)


def _settle(cfg, store, audit, **kwargs) -> SettleResult:
    return settle(cfg, store, audit, _now(), **kwargs)


def _release(cfg, store, audit, *keys: str) -> SettleResult:
    for key in keys:
        _age(cfg, key, 61)
    return _settle(cfg, store, audit)


def _finish(store, key: str, owners: list | None = None) -> None:
    """Pretend the pipeline wrote outputs for `key` and marked it DONE."""
    out_dir = store.get(key).out_dir
    (out_dir / RESULT_FILE).write_text(json.dumps({"owners": owners or []}), encoding="utf-8")
    (out_dir / TRANSCRIPT_FILE).write_text("x", encoding="utf-8")
    store.set_status(key, DONE)


def _owners(store, key: str) -> list[str]:
    data = json.loads((store.get(key).out_dir / RESULT_FILE).read_text(encoding="utf-8"))
    return [o["extension"] for o in data["owners"]]


def test_fresh_copy_keeps_waiting(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")

    assert _settle(cfg, store, audit) == SettleResult()
    assert store.get(key).status == WAITING


def test_quiet_group_releases_longest_copy(cfg, store, audit, make_wav, make_sidecar) -> None:
    short = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=100)
    longest = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=300)
    middle = _drop(cfg, store, audit, make_wav, make_sidecar, "c", "3000", duration_sec=200)

    result = _release(cfg, store, audit, short, longest, middle)

    assert result.released == (longest,)
    assert sorted(result.attached) == sorted([short, middle])
    assert store.get(longest).status == QUEUED
    assert {m.job_key for m in store.members(longest)} == {short, middle}
    assert store.get(short).status == GROUPED


def test_duration_tie_goes_to_earliest_start(cfg, store, audit, make_wav, make_sidecar) -> None:
    late = _drop(
        cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at="2026-10-07T14:05:00+03:00", duration_sec=600
    )
    early = _drop(
        cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at="2026-10-07T14:03:00+03:00", duration_sec=600
    )

    assert _release(cfg, store, audit, late, early).released == (early,)


def test_overdue_group_is_released_despite_new_arrivals(cfg, store, audit, make_wav, make_sidecar) -> None:
    first = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")
    _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210")
    _age(cfg, first, 301)

    assert _settle(cfg, store, audit).released == (first,)


def test_late_shorter_copy_joins_released_primary(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=600)
    _release(cfg, store, audit, primary)

    late = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=300)
    result = _settle(cfg, store, audit)

    assert result.attached == (late,)
    assert store.get(late).grouped_into == primary
    assert store.get(primary).status == QUEUED


def test_late_copy_after_done_updates_result_owners(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")
    _release(cfg, store, audit, primary)
    _finish(store, primary)

    _drop(cfg, store, audit, make_wav, make_sidecar, "b", "3000")
    _settle(cfg, store, audit)

    assert _owners(store, primary) == ["1042", "3000"]


def test_late_copy_updates_owners_even_if_done_was_never_recorded(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A crash between the output stage and DONE leaves the primary RUNNING with a result.json."""
    primary = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")
    _release(cfg, store, audit, primary)
    _finish(store, primary)
    store.set_status(primary, RUNNING)

    _drop(cfg, store, audit, make_wav, make_sidecar, "b", "3000")
    _settle(cfg, store, audit)

    assert _owners(store, primary) == ["1042", "3000"]


def test_locked_result_keeps_the_copy_waiting_until_next_poll(cfg, store, audit, make_wav, make_sidecar, monkeypatch):
    primary = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042")
    _release(cfg, store, audit, primary)
    _finish(store, primary)
    late = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "3000")

    def locked(out_dir, owners):
        raise PermissionError("result.json is open in Explorer")

    monkeypatch.setattr("jabberscribe.group.update_owners", locked)
    assert _settle(cfg, store, audit).attached == ()
    assert store.get(late).status == WAITING

    monkeypatch.undo()
    assert _settle(cfg, store, audit).attached == (late,)
    assert _owners(store, primary) == ["1042", "3000"]


def test_plain_calls_are_not_touched(cfg, store, audit, make_wav, make_sidecar) -> None:
    key = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", conference_id=None)

    assert _settle(cfg, store, audit) == SettleResult()
    assert store.get(key).status == QUEUED


def test_owners_dedupe_by_extension(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A participant who dropped and rejoined yields two copies from one line."""
    first = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=300)
    _drop(cfg, store, audit, make_wav, make_sidecar, "b", "1042")
    _drop(cfg, store, audit, make_wav, make_sidecar, "c", "2210")
    _release(cfg, store, audit, "a_1042", "b_1042", "c_2210")

    assert [o.extension for o in owners_for(store.get(first), store)] == ["1042", "2210"]


def test_longer_later_copy_replaces_an_early_leavers_primary(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A leaves at minute 8 and is processed; the host's 60-minute copy must not be thrown away."""
    start = "2026-10-07T14:00:00+03:00"
    leaver = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at=start, duration_sec=480)
    _release(cfg, store, audit, leaver)
    _finish(store, leaver)

    host = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at=start, duration_sec=3600)
    other = _drop(cfg, store, audit, make_wav, make_sidecar, "c", "3000", started_at=start, duration_sec=3500)
    result = _settle(cfg, store, audit)

    assert result.released == (host,)
    assert result.superseded == (leaver,)
    assert result.attached == (other,)
    assert (store.get(host).status, store.get(host).stage) == (QUEUED, QUEUED)
    assert (store.get(leaver).status, store.get(leaver).grouped_into) == (GROUPED, host)
    assert {m.job_key for m in store.members(host)} == {leaver, other}
    assert not (store.get(leaver).out_dir / RESULT_FILE).exists()
    assert not (store.get(leaver).out_dir / TRANSCRIPT_FILE).exists()
    assert store.get(leaver).audio_path.is_file()
    entry = audit.entries(leaver)[-1]
    assert entry.action == SUPERSEDED
    assert host in entry.detail
    assert RESULT_FILE in entry.detail


def test_later_copy_that_is_not_longer_just_joins(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _drop(
        cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at="2026-10-07T14:00:00+03:00", duration_sec=600
    )
    _release(cfg, store, audit, primary)
    late = _drop(
        cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at="2026-10-07T14:09:00+03:00", duration_sec=300
    )

    result = _settle(cfg, store, audit)

    assert (result.attached, result.superseded) == ((late,), ())


def test_reused_conference_id_starts_a_new_group(cfg, store, audit, make_wav, make_sidecar) -> None:
    """The weekly Meet-Me call reuses its id; week 2 must get its own transcript and owners."""
    week1 = _drop(cfg, store, audit, make_wav, make_sidecar, "w1", "1042", started_at="2026-09-30T10:00:00+03:00")
    _release(cfg, store, audit, week1)
    _finish(store, week1, owners=[{"extension": "1042"}])

    week2 = _drop(cfg, store, audit, make_wav, make_sidecar, "w2", "3000", started_at="2026-10-07T10:00:00+03:00")
    assert _settle(cfg, store, audit) == SettleResult()
    assert store.get(week2).status == WAITING

    assert _release(cfg, store, audit, week2).released == (week2,)
    assert store.get(week2).grouped_into is None
    assert _owners(store, week1) == ["1042"]


def test_failed_primary_hands_its_conference_to_the_next_longest_copy(
    cfg, store, audit, make_wav, make_sidecar
) -> None:
    longest = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=300)
    second = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=200)
    third = _drop(cfg, store, audit, make_wav, make_sidecar, "c", "3000", duration_sec=100)
    _release(cfg, store, audit, longest, second, third)
    store.set_status(longest, FAILED)

    result = _settle(cfg, store, audit)

    assert (result.released, result.superseded) == ((second,), (longest,))
    assert store.get(second).status == QUEUED
    assert (store.get(longest).status, store.get(longest).grouped_into) == (FAILED, second)
    assert {m.job_key for m in store.members(second)} == {longest, third}
    assert [o.extension for o in owners_for(store.get(second), store)] == ["2210", "1042", "3000"]


def test_late_copy_takes_over_a_failed_primary_without_members(cfg, store, audit, make_wav, make_sidecar) -> None:
    failed = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=300)
    _release(cfg, store, audit, failed)
    store.set_status(failed, FAILED)

    late = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=100)
    result = _settle(cfg, store, audit)

    assert result.released == (late,)
    assert store.get(late).status == QUEUED
    assert store.get(failed).grouped_into == late


def test_settle_can_be_limited_to_one_conference(cfg, store, audit, make_wav, make_sidecar) -> None:
    mine = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", conference_id="conf-1")
    other = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", conference_id="conf-2")
    _age(cfg, mine, 61)
    _age(cfg, other, 61)

    assert _settle(cfg, store, audit, conference_id="conf-1").released == (mine,)
    assert store.get(other).status == WAITING


def _lock(monkeypatch, name: str) -> None:
    """Make `name` undeletable, as when Word holds the file open on Windows."""
    unlink = pathlib.Path.unlink

    def locked(self, *args, **kwargs):
        if self.name == name:
            raise PermissionError(f"{name} is open in Word")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", locked)


def test_locked_output_delays_the_replacement_until_it_can_be_discarded(
    cfg, store, audit, make_wav, make_sidecar, monkeypatch
) -> None:
    start = "2026-10-07T14:00:00+03:00"
    leaver = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at=start, duration_sec=480)
    _release(cfg, store, audit, leaver)
    _finish(store, leaver)
    host = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at=start, duration_sec=3600)
    elsewhere = _drop(cfg, store, audit, make_wav, make_sidecar, "c", "3000", conference_id="conf-2")
    _age(cfg, elsewhere, 61)
    _lock(monkeypatch, TRANSCRIPT_FILE)

    result = _settle(cfg, store, audit)

    assert result.released == (elsewhere,)
    assert store.get(host).status == WAITING
    assert (store.get(leaver).status, store.get(leaver).grouped_into) == (DONE, None)
    out_dir = store.get(leaver).out_dir
    assert (out_dir / TRANSCRIPT_FILE).is_file()
    assert not (out_dir / RESULT_FILE).exists()
    entry = audit.entries(leaver)[-1]
    assert entry.action == SUPERSEDED
    assert RESULT_FILE in entry.detail
    assert TRANSCRIPT_FILE not in entry.detail

    monkeypatch.undo()
    result = _settle(cfg, store, audit)

    assert (result.released, result.superseded) == ((host,), (leaver,))
    assert (store.get(leaver).status, store.get(leaver).grouped_into) == (GROUPED, host)
    assert not (out_dir / TRANSCRIPT_FILE).exists()
    assert TRANSCRIPT_FILE in audit.entries(leaver)[-1].detail


def test_os_error_in_one_conference_does_not_stop_the_others(
    cfg, store, audit, make_wav, make_sidecar, monkeypatch
) -> None:
    broken = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", conference_id="conf-1")
    fine = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", conference_id="conf-2")
    _age(cfg, broken, 61)
    _age(cfg, fine, 61)
    real = group._settle_conference

    def flaky(cfg, store, audit, now, conference_id, changes):
        if conference_id == "conf-1":
            raise PermissionError("share is offline")
        real(cfg, store, audit, now, conference_id, changes)

    monkeypatch.setattr(group, "_settle_conference", flaky)

    assert _settle(cfg, store, audit).released == (fine,)
    assert store.get(broken).status == WAITING


def test_copy_bridging_two_groups_merges_them(cfg, store, audit, make_wav, make_sidecar) -> None:
    """The leaver and a late joiner were released separately; the host's copy spans both."""
    leaver = _drop(
        cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at="2026-10-07T14:00:00+03:00", duration_sec=480
    )
    _release(cfg, store, audit, leaver)
    _finish(store, leaver)
    joiner = _drop(
        cfg, store, audit, make_wav, make_sidecar, "j", "4000", started_at="2026-10-07T14:30:00+03:00", duration_sec=600
    )
    assert _release(cfg, store, audit, joiner).released == (joiner,)
    _finish(store, joiner)

    host = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "b",
        "2210",
        started_at="2026-10-07T14:00:00+03:00",
        duration_sec=3600,
    )
    result = _settle(cfg, store, audit)

    assert result.released == (host,)
    assert sorted(result.superseded) == sorted([leaver, joiner])
    assert (store.get(host).status, store.get(host).stage) == (QUEUED, QUEUED)
    assert {m.job_key for m in store.members(host)} == {leaver, joiner}
    assert not (store.get(joiner).out_dir / TRANSCRIPT_FILE).exists()
    entry = audit.entries(joiner)[-1]
    assert (entry.action, host in entry.detail) == (SUPERSEDED, True)
    assert [o.extension for o in owners_for(store.get(host), store)] == ["2210", "1042", "4000"]


def test_done_primary_absorbs_a_bridged_group_without_reprocessing(cfg, store, audit, make_wav, make_sidecar) -> None:
    first = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "a",
        "1042",
        started_at="2026-10-07T14:00:00+03:00",
        duration_sec=1800,
    )
    _release(cfg, store, audit, first)
    _finish(store, first, owners=[{"extension": "1042"}])
    second = _drop(
        cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at="2026-10-07T14:40:00+03:00", duration_sec=600
    )
    _release(cfg, store, audit, second)
    _finish(store, second)

    bridge = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "c",
        "3000",
        started_at="2026-10-07T14:20:00+03:00",
        duration_sec=1500,
    )
    result = _settle(cfg, store, audit)

    assert result == SettleResult(attached=(bridge,), superseded=(second,))
    assert store.get(first).status == DONE
    assert (store.get(second).status, store.get(second).grouped_into) == (GROUPED, first)
    assert store.get(bridge).grouped_into == first
    assert not (store.get(second).out_dir / RESULT_FILE).exists()
    assert audit.entries(second)[-1].action == SUPERSEDED
    assert _owners(store, first) == ["1042", "2210", "3000"]


def _failed_joiner(cfg, store, audit, make_wav, make_sidecar, *, with_member: bool) -> tuple[str, str, str | None]:
    """A leaver processed alone, then a late joiner released as its own primary that failed."""
    leaver = _drop(
        cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at="2026-10-07T14:00:00+03:00", duration_sec=480
    )
    _release(cfg, store, audit, leaver)
    _finish(store, leaver)
    joiner = _drop(
        cfg, store, audit, make_wav, make_sidecar, "j", "4000", started_at="2026-10-07T14:30:00+03:00", duration_sec=600
    )
    member = None
    if with_member:
        member = _drop(
            cfg,
            store,
            audit,
            make_wav,
            make_sidecar,
            "k",
            "5000",
            started_at="2026-10-07T14:31:00+03:00",
            duration_sec=500,
        )
    _release(cfg, store, audit, *[k for k in (joiner, member) if k])
    store.set_status(joiner, FAILED)
    return leaver, joiner, member


def test_bridging_copy_takes_in_a_failed_primary_without_members(cfg, store, audit, make_wav, make_sidecar) -> None:
    leaver, joiner, _ = _failed_joiner(cfg, store, audit, make_wav, make_sidecar, with_member=False)
    host = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "b",
        "2210",
        started_at="2026-10-07T14:00:00+03:00",
        duration_sec=3600,
    )

    result = _settle(cfg, store, audit)

    assert result.released == (host,)
    assert sorted(result.superseded) == sorted([leaver, joiner])
    assert (store.get(joiner).status, store.get(joiner).grouped_into) == (FAILED, host)
    assert [o.extension for o in owners_for(store.get(host), store)] == ["2210", "1042", "4000"]


def test_bridging_copy_takes_in_a_failed_primary_and_its_members(cfg, store, audit, make_wav, make_sidecar) -> None:
    leaver, joiner, member = _failed_joiner(cfg, store, audit, make_wav, make_sidecar, with_member=True)
    host = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "b",
        "2210",
        started_at="2026-10-07T14:00:00+03:00",
        duration_sec=3600,
    )

    result = _settle(cfg, store, audit)

    assert result.released == (host,)
    assert (store.get(joiner).status, store.get(joiner).grouped_into) == (FAILED, host)
    assert (store.get(member).status, store.get(member).grouped_into) == (GROUPED, host)
    assert {o.extension for o in owners_for(store.get(host), store)} == {"2210", "1042", "4000", "5000"}


def test_copy_bridging_only_failed_primaries_merges_them(cfg, store, audit, make_wav, make_sidecar) -> None:
    leaver, joiner, member = _failed_joiner(cfg, store, audit, make_wav, make_sidecar, with_member=True)
    store.set_status(leaver, FAILED)
    bridge = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "b",
        "2210",
        started_at="2026-10-07T14:05:00+03:00",
        duration_sec=1800,
    )

    result = _settle(cfg, store, audit)

    assert result.released == (bridge,)
    assert sorted(result.superseded) == sorted([leaver, joiner])
    assert store.get(bridge).status == QUEUED
    assert (store.get(leaver).status, store.get(leaver).grouped_into) == (FAILED, bridge)
    assert (store.get(joiner).status, store.get(joiner).grouped_into) == (FAILED, bridge)
    assert store.get(member).grouped_into == bridge
    assert {o.extension for o in owners_for(store.get(bridge), store)} == {"2210", "1042", "4000", "5000"}


def test_failed_reelection_waits_while_an_output_is_locked(cfg, store, audit, make_wav, make_sidecar, monkeypatch):
    longest = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=300)
    second = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=200)
    _release(cfg, store, audit, longest, second)
    (store.get(longest).out_dir / TRANSCRIPT_FILE).write_text("partial", encoding="utf-8")
    store.set_status(longest, FAILED)
    _lock(monkeypatch, TRANSCRIPT_FILE)

    assert _settle(cfg, store, audit) == SettleResult()
    assert (store.get(longest).status, store.get(longest).grouped_into) == (FAILED, None)
    assert store.get(second).grouped_into == longest

    monkeypatch.undo()
    assert _settle(cfg, store, audit).released == (second,)
    assert not (store.get(longest).out_dir / TRANSCRIPT_FILE).exists()


def test_merge_retries_after_owner_update_fails_without_auditing_twice(
    cfg, store, audit, make_wav, make_sidecar, monkeypatch
) -> None:
    first = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "a",
        "1042",
        started_at="2026-10-07T14:00:00+03:00",
        duration_sec=1800,
    )
    _release(cfg, store, audit, first)
    _finish(store, first, owners=[{"extension": "1042"}])
    second = _drop(
        cfg, store, audit, make_wav, make_sidecar, "b", "2210", started_at="2026-10-07T14:40:00+03:00", duration_sec=600
    )
    _release(cfg, store, audit, second)
    _finish(store, second)
    bridge = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "c",
        "3000",
        started_at="2026-10-07T14:20:00+03:00",
        duration_sec=1500,
    )

    def locked(out_dir, owners):
        raise PermissionError("result.json is open in Explorer")

    monkeypatch.setattr("jabberscribe.group.update_owners", locked)
    assert _settle(cfg, store, audit) == SettleResult()
    assert store.get(bridge).status == WAITING
    assert (store.get(second).status, store.get(second).grouped_into) == (DONE, None)

    monkeypatch.undo()
    assert _settle(cfg, store, audit) == SettleResult(attached=(bridge,), superseded=(second,))
    assert _owners(store, first) == ["1042", "2210", "3000"]
    assert [e.action for e in audit.entries(second)].count(SUPERSEDED) == 1


def _failed_with_member(cfg, store, audit, make_wav, make_sidecar, *, member_sec: int) -> tuple[str, str]:
    """A 60-minute copy from 14:30 that failed, with a shorter copy of the same group attached."""
    failed = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "f",
        "4000",
        started_at="2026-10-07T14:30:00+03:00",
        duration_sec=3600,
    )
    member = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "m",
        "5000",
        started_at="2026-10-07T14:31:00+03:00",
        duration_sec=member_sec,
    )
    _release(cfg, store, audit, failed, member)
    store.set_status(failed, FAILED)
    return failed, member


def test_longer_member_of_a_failed_primary_beats_a_short_bridging_copy(cfg, store, audit, make_wav, make_sidecar):
    leaver = _drop(
        cfg, store, audit, make_wav, make_sidecar, "a", "1042", started_at="2026-10-07T14:00:00+03:00", duration_sec=480
    )
    _release(cfg, store, audit, leaver)
    _finish(store, leaver)
    failed, member = _failed_with_member(cfg, store, audit, make_wav, make_sidecar, member_sec=3300)
    bridge = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "c",
        "3000",
        started_at="2026-10-07T14:05:00+03:00",
        duration_sec=1800,
    )

    result = _settle(cfg, store, audit)

    assert result.released == (member,)
    assert sorted(result.superseded) == sorted([leaver, failed])
    assert result.attached == (bridge,)
    assert (store.get(member).status, store.get(member).grouped_into) == (QUEUED, None)
    assert store.get(bridge).grouped_into == member
    assert (store.get(leaver).status, store.get(leaver).grouped_into) == (GROUPED, member)
    assert (store.get(failed).status, store.get(failed).grouped_into) == (FAILED, member)
    assert {o.extension for o in owners_for(store.get(member), store)} == {"5000", "1042", "4000", "3000"}
    assert _settle(cfg, store, audit) == SettleResult()


def test_live_primary_beats_a_shorter_member_of_a_failed_primary(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "a",
        "1042",
        started_at="2026-10-07T13:20:00+03:00",
        duration_sec=3000,
    )
    _release(cfg, store, audit, primary)
    _finish(store, primary, owners=[{"extension": "1042"}])
    failed, member = _failed_with_member(cfg, store, audit, make_wav, make_sidecar, member_sec=600)
    bridge = _drop(
        cfg,
        store,
        audit,
        make_wav,
        make_sidecar,
        "c",
        "3000",
        started_at="2026-10-07T14:05:00+03:00",
        duration_sec=1800,
    )

    result = _settle(cfg, store, audit)

    assert result == SettleResult(attached=(bridge,), superseded=(failed,))
    assert store.get(primary).status == DONE
    assert (store.get(failed).status, store.get(failed).grouped_into) == (FAILED, primary)
    assert (store.get(member).status, store.get(member).grouped_into) == (GROUPED, primary)
    assert _owners(store, primary) == ["1042", "4000", "5000", "3000"]


def _three_copies(cfg, store, audit, make_wav, make_sidecar) -> tuple[str, str, str]:
    longest = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=300)
    second = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=200)
    third = _drop(cfg, store, audit, make_wav, make_sidecar, "c", "3000", duration_sec=100)
    _release(cfg, store, audit, longest, second, third)
    return longest, second, third


def test_summary_failure_keeps_the_primary_and_its_members(cfg, store, audit, make_wav, make_sidecar) -> None:
    """A 4xx in summarize would fail every copy alike: no re-election, the group waits for retry."""
    longest, second, third = _three_copies(cfg, store, audit, make_wav, make_sidecar)
    for stage in ("audio", "stt"):
        store.complete_stage(longest, stage)
    store.set_status(longest, FAILED)

    assert _settle(cfg, store, audit) == SettleResult()
    assert (store.get(longest).status, store.get(longest).grouped_into) == (FAILED, None)
    assert {m.job_key for m in store.members(longest)} == {second, third}
    assert store.conference_ids_to_settle() == []


def test_late_copy_joins_a_primary_that_failed_in_summarize(cfg, store, audit, make_wav, make_sidecar) -> None:
    primary = _drop(cfg, store, audit, make_wav, make_sidecar, "a", "1042", duration_sec=300)
    _release(cfg, store, audit, primary)
    for stage in ("audio", "stt", "summarize"):
        store.complete_stage(primary, stage)
    store.set_status(primary, FAILED)

    late = _drop(cfg, store, audit, make_wav, make_sidecar, "b", "2210", duration_sec=100)
    result = _settle(cfg, store, audit)

    assert (result.attached, result.superseded, result.released) == ((late,), (), ())
    assert store.get(late).grouped_into == primary
    assert store.get(primary).status == FAILED


def test_retry_after_a_failed_chain_restarts_from_the_longest_copy(cfg, store, audit, make_wav, make_sidecar) -> None:
    longest, second, third = _three_copies(cfg, store, audit, make_wav, make_sidecar)
    for failing in (longest, second, third):
        # Each copy fails in the audio stage, as on a share outage, and hands the conference on.
        store.set_status(failing, FAILED)
        _settle(cfg, store, audit)
    assert (store.get(third).status, store.get(third).grouped_into) == (FAILED, None)

    assert group.requeue_failed(store, second) == longest

    assert store.get(longest).status == QUEUED
    assert (store.get(longest).stage, store.get(longest).grouped_into) == (QUEUED, None)
    assert {m.job_key for m in store.members(longest)} == {second, third}
    assert {store.get(k).status for k in (second, third)} == {FAILED}
    assert group.requeue_failed(store, longest) is None


def test_requeue_failed_refuses_a_copy_whose_primary_is_done(cfg, store, audit, make_wav, make_sidecar) -> None:
    longest, second, _ = _three_copies(cfg, store, audit, make_wav, make_sidecar)
    store.set_status(longest, FAILED)
    _settle(cfg, store, audit)
    store.set_status(second, DONE)

    assert group.requeue_failed(store, longest) is None
    assert store.get(longest).status == FAILED
