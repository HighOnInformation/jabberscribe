import json
from pathlib import Path

import pytest

from jabberscribe.audit import MAILED, PUBLISHED, AuditLog
from jabberscribe.jobs import DONE, NEEDS_REVIEW, JobStore
from jabberscribe.notify import AmbiguousSendError
from jabberscribe.pipeline import process_job
from jabberscribe.stt import Segment
from jabberscribe.watcher import scan_once


class FakeTranscriber:
    def transcribe(self, wav: Path) -> list[Segment]:
        return [Segment(0.0, 1.5, "שלום")]


class FakeConfluence:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.updated: list[str] = []
        self.restrictions: list[str] = []

    def create_page(self, space_key, parent_id, title, body) -> str:
        self.created.append(title)
        return "777"

    def update_page(self, page_id, title, body) -> None:
        self.updated.append(page_id)

    def set_read_restrictions(self, page_id, usernames, group) -> None:
        self.restrictions.append(page_id)

    def page_url(self, page_id) -> str:
        return f"https://wiki/x/{page_id}"


def _delivery_cfg(cfg, stages=("audio", "stt", "render", "publish", "notify")):
    """Build a delivery-enabled config.

    Note the explicit model instances: `model_copy(update=...)` does NOT validate,
    so handing it plain dicts would leave dicts in place and every later
    `cfg.confluence.space_key` would fail on a dict.
    """
    from jabberscribe.config import ConfluenceConfig, MailConfig, PipelineConfig

    return cfg.model_copy(
        update={
            "pipeline": PipelineConfig(stages=stages),
            "confluence": ConfluenceConfig(
                base_url="https://wiki.corp.local",
                space_key="CALLS",
                parent_page_id="1",
                compliance_group="grp",
            ),
            "mail": MailConfig(
                smtp_host="smtp.corp.local",
                from_address="js@corp.local",
                fallback_to="ops@corp.local",
            ),
        }
    )


def _setup(cfg, make_wav, make_sidecar):
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path, actor="svc")
    audit.init_schema()
    make_wav(cfg.paths.inbox / "in.wav")
    make_sidecar(cfg.paths.inbox / "in.json", call_id="d1")
    scan_once(cfg, store, min_age_seconds=0)
    return store, audit


def test_full_pipeline_publishes_and_mails(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg)
    store, audit = _setup(staged, make_wav, make_sidecar)
    client, sent = FakeConfluence(), []

    process_job(
        store.claim_next(),
        staged,
        store,
        FakeTranscriber(),
        audit=audit,
        confluence=client,
        mail_sender=lambda c, m, r: sent.append(r),
    )

    job = store.get("d1")
    assert job.status == DONE
    assert job.confluence_page_id == "777"
    assert job.notified_at
    assert client.restrictions == ["777"]
    assert len(sent) == 1
    assert {e.action for e in audit.entries("d1")} == {PUBLISHED, MAILED}


def test_mail_carries_the_page_link(cfg, make_wav, make_sidecar) -> None:
    """The page must exist before the mail is composed, or the link is missing."""
    staged = _delivery_cfg(cfg)
    store, audit = _setup(staged, make_wav, make_sidecar)
    messages = []

    process_job(
        store.claim_next(),
        staged,
        store,
        FakeTranscriber(),
        audit=audit,
        confluence=FakeConfluence(),
        mail_sender=lambda c, m, r: messages.append(m),
    )

    body = messages[0].get_body(preferencelist=("plain",)).get_content()
    assert "https://wiki/x/777" in body


def test_rerun_neither_duplicates_page_nor_remails(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg)
    store, audit = _setup(staged, make_wav, make_sidecar)
    client, sent = FakeConfluence(), []
    args = {"audit": audit, "confluence": client, "mail_sender": lambda c, m, r: sent.append(r)}
    process_job(store.claim_next(), staged, store, FakeTranscriber(), **args)

    store.set_status("d1", "running")
    store.complete_stage("d1", "render")
    process_job(store.get("d1"), staged, store, FakeTranscriber(), **args)

    assert len(client.created) == 1
    assert client.updated == ["777"]
    assert len(sent) == 1


def test_ambiguous_mail_leaves_job_needing_review(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg)
    store, audit = _setup(staged, make_wav, make_sidecar)

    def disconnect(c, m, r):
        raise AmbiguousSendError("mid-DATA")

    with pytest.raises(AmbiguousSendError):
        process_job(
            store.claim_next(),
            staged,
            store,
            FakeTranscriber(),
            audit=audit,
            confluence=FakeConfluence(),
            mail_sender=disconnect,
        )

    assert store.get("d1").status == NEEDS_REVIEW


def test_publish_body_contains_the_transcript(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg, stages=("audio", "stt", "render", "publish"))
    store, audit = _setup(staged, make_wav, make_sidecar)

    class Capturing(FakeConfluence):
        body = ""

        def create_page(self, space_key, parent_id, title, body) -> str:
            Capturing.body = body
            return "777"

    process_job(store.claim_next(), staged, store, FakeTranscriber(), audit=audit, confluence=Capturing())

    assert "שלום" in Capturing.body
    assert 'dir="rtl"' in Capturing.body


def test_delivery_stages_require_an_audit_log(cfg, make_wav, make_sidecar) -> None:
    """Publishing without an audit trail would defeat the point of the trail."""
    staged = _delivery_cfg(cfg, stages=("audio", "stt", "render", "publish"))
    store, _audit = _setup(staged, make_wav, make_sidecar)

    with pytest.raises(ValueError, match="AuditLog"):
        process_job(store.claim_next(), staged, store, FakeTranscriber(), confluence=FakeConfluence())


def test_transcript_json_is_still_written(cfg, make_wav, make_sidecar) -> None:
    staged = _delivery_cfg(cfg)
    store, audit = _setup(staged, make_wav, make_sidecar)

    path = process_job(
        store.claim_next(),
        staged,
        store,
        FakeTranscriber(),
        audit=audit,
        confluence=FakeConfluence(),
        mail_sender=lambda c, m, r: None,
    )

    assert json.loads(path.read_text(encoding="utf-8"))["call_id"] == "d1"
