"""Conference grouping: one meeting, one transcript.

CUCM forks every participating line separately, so a conference arrives as
several recordings sharing a conference_id. A group is the copies of one
conference_id whose time spans overlap (within overlap_slack_seconds, a few
seconds): a reused conference_id -- the weekly Meet-Me number, or the next
booking of the same bridge -- therefore starts a new group instead of joining
the earlier meeting. The recorder should still supply a conference_id unique
per conference instance; the overlap test is the safety net.

New copies wait (status WAITING) until their group goes quiet or has waited
max_wait_seconds. Then the longest copy is processed and the rest are attached
to it, so every participating line owner gets the same single result.

A group can be released before the meeting is over: the first participant to
hang up produces the first copy. When a later copy is longer and ends more
than settle_seconds after the primary, it replaces the primary: it is
processed from scratch and the old primary's text outputs are deleted
(audited as superseded). This trades extra GPU time for a complete transcript
and is a deviation from "processed once" that needs owner sign-off.

A copy that overlaps several primaries -- a late joiner was released on its own --
merges them into one group under the longest; the others are superseded the
same way. Outputs are discarded before the database changes, so a file held
open only delays the change to the next poll.

A primary that failed on its own recording (in the audio or STT stage) does not
keep its conference: the next-longest copy is elected, or it is handed to the
winner of a merge that bridges it. A failure in summarize or output is the
system's, not the copy's (a rejected key, a wrong model name): every copy would
fail the same way, so the primary stays FAILED with its members, for
`jabberscribe retry` once the cause is fixed. Retrying a conference whose
failed chain was handed over restarts it from its longest failed copy.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from jabberscribe.audit import SUPERSEDED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import COPY_STAGES, FAILED, GROUPED, QUEUED, WAITING, Job, JobStore
from jabberscribe.output import RESULT_FILE, TEXT_FILES, update_owners
from jabberscribe.sidecar import Party, parse_sidecar

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettleResult:
    #: Job keys that became the processed copy of their conference (new, replacing, or re-elected).
    released: tuple[str, ...] = ()
    #: Job keys attached to a primary instead of being processed.
    attached: tuple[str, ...] = ()
    #: Former primaries replaced by a longer copy or by a re-election after failure.
    superseded: tuple[str, ...] = ()


@dataclass
class _Changes:
    released: list[str] = field(default_factory=list)
    attached: list[str] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)


def owners_for(job: Job, store: JobStore) -> list[Party]:
    """The job's own line owner, then each attached copy's, one per extension."""
    owners: list[Party] = []
    for member in [job, *store.members(job.job_key)]:
        _add_owner(owners, parse_sidecar(member.sidecar_json).line_owner)
    return owners


def _add_owner(owners: list[Party], owner: Party) -> None:
    if all(o.extension != owner.extension for o in owners):
        owners.append(owner)


def _start(job: Job) -> datetime:
    return datetime.fromisoformat(job.started_at)


def _end(job: Job) -> datetime:
    return _start(job) + timedelta(seconds=job.duration_sec)


def _rank(job: Job) -> tuple[int, datetime]:
    # Longest recording first; a tie goes to whoever joined first.
    return (-job.duration_sec, _start(job))


def _copy_failed(job: Job) -> bool:
    """FAILED on its own recording: the failure struck in the audio or STT stage, so another copy may do better."""
    return job.status == FAILED and job.stage in COPY_STAGES


def _span(primary: Job, store: JobStore) -> tuple[datetime, datetime]:
    jobs = [primary, *store.members(primary.job_key)]
    return min(_start(j) for j in jobs), max(_end(j) for j in jobs)


def _overlaps(job: Job, span: tuple[datetime, datetime], slack: timedelta) -> bool:
    start, end = span
    return _start(job) <= end + slack and _end(job) >= start - slack


def _clusters(jobs: list[Job], slack: timedelta) -> list[list[Job]]:
    """Group copies whose time spans overlap, allowing `slack` between them."""
    clusters: list[list[Job]] = []
    cluster_end: datetime | None = None
    for job in sorted(jobs, key=_start):
        if cluster_end is not None and _start(job) <= cluster_end + slack:
            clusters[-1].append(job)
            cluster_end = max(cluster_end, _end(job))
        else:
            clusters.append([job])
            cluster_end = _end(job)
    return clusters


def _discard_outputs(cfg: Config, audit: AuditLog, old: Job, new_key: str) -> bool:
    """Delete a superseded primary's text outputs and work folder, each best-effort. True when all are gone.

    What was removed is audited. A file a reader holds open stays; the caller
    then leaves the database alone so the next poll tries again.
    """
    targets = [(name, (old.out_dir / name).unlink) for name in TEXT_FILES if (old.out_dir / name).is_file()]
    work = cfg.paths.work_dir / old.job_key
    if work.is_dir():
        targets.append(("work", lambda: shutil.rmtree(work)))
    removed: list[str] = []
    errors: list[OSError] = []
    for name, remove in targets:
        try:
            remove()
            removed.append(name)
        except OSError as exc:
            errors.append(exc)
    if removed:
        audit.record(old.job_key, SUPERSEDED, f"replaced by {new_key}: {', '.join(removed)}")
    if errors:
        log.warning("cannot discard outputs of %s yet, retrying next poll: %s", old.job_key, errors[0])
    return not errors


def _discard_all(cfg: Config, store: JobStore, audit: AuditLog, olds: list[Job], new_key: str) -> bool:
    """Discard the outputs of every superseded primary, except one on legal hold: its outputs are evidence.

    The new primary writes its own folder, so a held loser's files stay where they are. Its audit row
    is written once, not again on each poll that retries a locked file of another loser.
    """
    done: list[bool] = []
    # Try every job, even after one fails, so the retry has less left to do.
    for old in olds:
        if store.is_held(old.job_key):
            detail = f"replaced by {new_key}; outputs kept under legal hold"
            if not any(e.action == SUPERSEDED and e.detail == detail for e in audit.entries(old.job_key)):
                audit.record(old.job_key, SUPERSEDED, detail)
            done.append(True)
        else:
            done.append(_discard_outputs(cfg, audit, old, new_key))
    return all(done)


def _promote(store: JobStore, new: Job, olds: list[Job], changes: _Changes) -> None:
    """Make `new` the conference's primary, processed from scratch; each of `olds` becomes a member."""
    for old in olds:
        store.hand_over(old.job_key, new.job_key)
        changes.superseded.append(old.job_key)
        log.info("conference %s: %s replaces %s as primary", new.conference_id, new.job_key, old.job_key)
    store.reset_job(new.job_key)
    changes.released.append(new.job_key)


def _attach(store: JobStore, copy: Job, primary: Job, absorbed: list[Job], changes: _Changes) -> bool:
    """Attach a late copy, and the `absorbed` primaries it bridged, to `primary`.

    If the primary already wrote result.json, the new owners are added there
    first. When it cannot be updated (a reader holds it open), nothing changes,
    the copy stays WAITING and the next poll tries again.
    """
    if (primary.out_dir / RESULT_FILE).is_file():
        owners = owners_for(primary, store)
        for job in absorbed:
            for owner in owners_for(job, store):
                _add_owner(owners, owner)
        _add_owner(owners, parse_sidecar(copy.sidecar_json).line_owner)
        try:
            update_owners(primary.out_dir, owners)
        except (OSError, ValueError) as exc:
            log.warning("cannot add owner %s to %s yet, retrying next poll: %s", copy.job_key, primary.job_key, exc)
            return False
    for job in absorbed:
        store.hand_over(job.job_key, primary.job_key)
        changes.superseded.append(job.job_key)
        log.info("conference %s: %s absorbs %s", primary.conference_id, primary.job_key, job.job_key)
    store.group_into(copy.job_key, primary.job_key)
    changes.attached.append(copy.job_key)
    return True


def _replaces(copy: Job, primary: Job, slack: timedelta) -> bool:
    return copy.duration_sec > primary.duration_sec and _end(copy) > _end(primary) + slack


def _winner(store: JobStore, copy: Job | None, live: list[Job], failed: list[Job], slack: timedelta) -> Job | None:
    """The job that keeps the merged group: a live primary, the copy, or a failed primary's member.

    A live primary wins without reprocessing unless a longer candidate `_replaces`
    it; a failed primary never wins. None when there is no candidate yet.
    """
    members = [m for f in failed for m in store.members(f.job_key) if m.status == GROUPED]
    candidates = live + members + ([copy] if copy is not None else [])
    if not candidates:
        return None
    top = min(candidates, key=_rank)
    if not live:
        return top
    best = min(live, key=_rank)
    return top if _replaces(top, best, slack) else best


def _merge(
    cfg: Config,
    store: JobStore,
    audit: AuditLog,
    copy: Job | None,
    live: list[Job],
    failed: list[Job],
    slack: timedelta,
    changes: _Changes,
) -> Job | None:
    """Merge the primaries a copy overlaps (or one failed primary) into one group. Returns the winner.

    Every primary but the winner, failed ones included, is superseded. Losers'
    outputs go first, so a locked file leaves the database untouched (None)
    and the next poll retries.
    """
    winner = _winner(store, copy, live, failed, slack)
    if winner is None:
        return None
    losers = [p for p in live if p.job_key != winner.job_key] + failed
    if not _discard_all(cfg, store, audit, losers, winner.job_key):
        return None
    if winner in live:
        assert copy is not None  # without a copy there is no live primary to merge
        if not _attach(store, copy, winner, losers, changes):
            return None
    else:
        _promote(store, winner, losers, changes)
        if copy is not None and copy.job_key != winner.job_key:
            store.group_into(copy.job_key, winner.job_key)
            changes.attached.append(copy.job_key)
    return store.get(winner.job_key)


def _settle_conference(
    cfg: Config, store: JobStore, audit: AuditLog, now: datetime, conference_id: str, changes: _Changes
) -> None:
    overlap = timedelta(seconds=cfg.group.overlap_slack_seconds)
    slack = timedelta(seconds=cfg.group.settle_seconds)
    jobs = store.conference_jobs(conference_id)
    primaries = [j for j in jobs if j.status not in (WAITING, GROUPED) and j.grouped_into is None]
    unmatched: list[Job] = []

    for copy in sorted((j for j in jobs if j.status == WAITING), key=_rank):
        overlapping = [p for p in primaries if _overlaps(copy, _span(p, store), overlap)]
        # A primary that failed for a system reason keeps its group like a live one.
        live = [p for p in overlapping if not _copy_failed(p)]
        failed = [p for p in overlapping if _copy_failed(p)]
        if not overlapping:
            unmatched.append(copy)
            continue
        winner = _merge(cfg, store, audit, copy, live, failed, slack, changes)
        if winner is not None:
            primaries = [p for p in primaries if p not in overlapping] + [winner]

    for primary in primaries:
        if _copy_failed(primary):
            # A copy that failed on its own recording does not keep its conference: elect the longest of its copies.
            _merge(cfg, store, audit, None, [], [primary], slack, changes)

    for cluster in _clusters(unmatched, overlap):
        arrivals = [datetime.fromisoformat(j.created_at) for j in cluster]
        quiet = (now - max(arrivals)).total_seconds() >= cfg.group.settle_seconds
        overdue = (now - min(arrivals)).total_seconds() >= cfg.group.max_wait_seconds
        if not (quiet or overdue):
            continue
        chosen = min(cluster, key=_rank)
        store.set_status(chosen.job_key, QUEUED)
        changes.released.append(chosen.job_key)
        for job in cluster:
            if job.job_key != chosen.job_key:
                store.group_into(job.job_key, chosen.job_key)
                changes.attached.append(job.job_key)
        log.info("conference %s: processing %s for %d copies", conference_id, chosen.job_key, len(cluster))


def settle(
    cfg: Config, store: JobStore, audit: AuditLog, now: datetime, *, conference_id: str | None = None
) -> SettleResult:
    """Release, attach, replace and re-elect conference copies. `now` must be timezone-aware.

    `conference_id` limits the pass to one conference (the `process` command).
    """
    changes = _Changes()
    for cid in [conference_id] if conference_id else store.conference_ids_to_settle():
        try:
            _settle_conference(cfg, store, audit, now, cid, changes)
        except Exception:
            # One unreachable folder (or a bug) must not hold up every other conference; the next poll retries.
            log.warning("cannot settle conference %s yet, retrying next poll", cid, exc_info=True)
    return SettleResult(tuple(changes.released), tuple(changes.attached), tuple(changes.superseded))


def requeue_failed(store: JobStore, job_key: str) -> str | None:
    """Give a FAILED job, or its conference, a fresh set of attempts (`retry`). Returns the key requeued.

    `job_key` may be the failed primary or a failed copy handed over to it.
    When copy after copy failed and handed the conference on, the longest
    failed copy becomes the primary again, from scratch, rather than the last
    one elected. None when the job, or its conference's primary, is not FAILED.
    """
    root = store.get(job_key)
    seen: set[str] = set()
    while root is not None and root.grouped_into is not None and root.job_key not in seen:
        seen.add(root.job_key)
        root = store.get(root.grouped_into)
    if root is None or root.status != FAILED or root.grouped_into is not None:
        return None
    best = min([j for j in [root, *store.members(root.job_key)] if j.status == FAILED], key=_rank)
    if best.job_key == root.job_key:
        return root.job_key if store.requeue(root.job_key) else None
    store.hand_over(root.job_key, best.job_key)
    store.reset_job(best.job_key)
    log.info("conference %s: retry restarts from %s, its longest failed copy", root.conference_id, best.job_key)
    return best.job_key
