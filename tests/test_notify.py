import json
import smtplib
from pathlib import Path

import pytest

from jabberscribe.audit import MAILED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import NEEDS_REVIEW, JobStore
from jabberscribe.notify import AmbiguousSendError, MailError, build_message, notify_call
from jabberscribe.render import render_call
from jabberscribe.sidecar import parse_sidecar

SIDECAR = json.dumps(
    {
        "call_id": "n1",
        "kind": "call",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 60,
        "participants": [
            {"display_name": "מאיר", "email": "meir@corp.local", "role": "caller"},
            {"display_name": "Support", "email": "support@corp.local", "role": "callee"},
        ],
        "audio": {"tracks": "mixed"},
    }
)

NO_EMAIL_SIDECAR = json.dumps(
    {
        "call_id": "n2",
        "kind": "call",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 60,
        "participants": [{"display_name": "Unknown", "extension": "1099"}],
        "audio": {"tracks": "mixed"},
    }
)


def _cfg(tmp_path: Path) -> Config:
    return Config(
        paths={
            "drop_root": tmp_path / "drop",
            "work_dir": tmp_path / "work",
            "audio_store": tmp_path / "audio",
            "db_path": tmp_path / "js.db",
        },
        pipeline={"stages": ("audio", "stt", "render", "notify")},
        mail={
            "smtp_host": "smtp.corp.local",
            "from_address": "jabberscribe@corp.local",
            "compliance_bcc": "archive@corp.local",
            "fallback_to": "it-ops@corp.local",
        },
    )


def _fixture(tmp_path: Path, sidecar_json: str = SIDECAR):
    cfg = _cfg(tmp_path)
    store = JobStore(cfg.paths.db_path)
    store.init_schema()
    audit = AuditLog(cfg.paths.db_path, actor="svc")
    audit.init_schema()
    sidecar = parse_sidecar(sidecar_json)
    store.create(
        call_id=sidecar.call_id,
        audio_path=cfg.paths.audio_store / f"{sidecar.call_id}.wav",
        sidecar_json=sidecar_json,
        kind="call",
        started_at=sidecar.started_at,
        duration_sec=60,
    )
    rendered = render_call(sidecar, [], page_url="https://wiki/x/1")
    return cfg, store, audit, store.get(sidecar.call_id), rendered


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def __call__(self, cfg, message, recipients) -> None:
        self.sent.append((message, recipients))


def test_notify_sends_to_participants_and_bcc(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    sender = Recorder()

    assert notify_call(job, cfg, store, audit, rendered, sender=sender) is True

    message, recipients = sender.sent[0]
    assert set(recipients) == {"meir@corp.local", "support@corp.local", "archive@corp.local"}
    assert message["To"] == "meir@corp.local, support@corp.local"
    assert message["From"] == "jabberscribe@corp.local"
    assert store.get("n1").notified_at
    assert [e.action for e in audit.entries("n1")] == [MAILED]


def test_bcc_is_an_envelope_recipient_not_a_header(tmp_path: Path) -> None:
    """Participants must never see the compliance archive address."""
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    sender = Recorder()

    notify_call(job, cfg, store, audit, rendered, sender=sender)

    message, recipients = sender.sent[0]
    assert message["Bcc"] is None
    assert "archive@corp.local" not in str(message)
    assert "archive@corp.local" in recipients


def test_notify_is_at_most_once(tmp_path: Path) -> None:
    """The whole point: never mail participants a second copy."""
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    sender = Recorder()
    notify_call(job, cfg, store, audit, rendered, sender=sender)

    again = notify_call(store.get("n1"), cfg, store, audit, rendered, sender=sender)

    assert again is False
    assert len(sender.sent) == 1


def test_transcript_is_attached(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)
    sender = Recorder()

    notify_call(job, cfg, store, audit, rendered, sender=sender)

    message, _ = sender.sent[0]
    names = [part.get_filename() for part in message.iter_attachments()]
    assert "n1.transcript.txt" in names


def test_missing_participant_emails_fall_back(tmp_path: Path) -> None:
    """An unresolved recipient must not strand a transcript."""
    cfg, store, audit, job, rendered = _fixture(tmp_path, NO_EMAIL_SIDECAR)
    sender = Recorder()

    assert notify_call(job, cfg, store, audit, rendered, sender=sender) is True

    message, recipients = sender.sent[0]
    assert "it-ops@corp.local" in recipients
    assert "1099" in message.get_body(preferencelist=("plain",)).get_content()


def test_connection_failure_is_retryable_and_does_not_mark_notified(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)

    def refuse(cfg_, message, recipients):
        raise MailError("connection refused")

    with pytest.raises(MailError):
        notify_call(job, cfg, store, audit, rendered, sender=refuse)

    assert store.get("n1").notified_at is None
    assert store.get("n1").status != NEEDS_REVIEW
    assert audit.entries("n1") == []


def test_ambiguous_send_marks_needs_review_and_never_retries(tmp_path: Path) -> None:
    cfg, store, audit, job, rendered = _fixture(tmp_path)

    def disconnect(cfg_, message, recipients):
        raise AmbiguousSendError("server disconnected mid-DATA")

    with pytest.raises(AmbiguousSendError):
        notify_call(job, cfg, store, audit, rendered, sender=disconnect)

    refreshed = store.get("n1")
    assert refreshed.status == NEEDS_REVIEW
    assert refreshed.notified_at is None
    assert "disconnected" in refreshed.last_error


def test_build_message_carries_subject_and_body(tmp_path: Path) -> None:
    cfg, _store, _audit, _job, rendered = _fixture(tmp_path)

    message = build_message(cfg, rendered, ["a@corp.local"], "n1.transcript.txt")

    assert message["Subject"] == rendered.mail_subject
    assert "https://wiki/x/1" in message.get_body(preferencelist=("plain",)).get_content()


def test_smtp_exceptions_map_to_the_right_error_types(monkeypatch, tmp_path: Path) -> None:
    """Connect failures are retryable; failures during send are not."""
    from jabberscribe import notify

    cfg, _store, _audit, _job, rendered = _fixture(tmp_path)
    message = build_message(cfg, rendered, ["a@corp.local"], "n1.transcript.txt")

    class ConnectFails:
        def __init__(self, *a, **k):
            raise smtplib.SMTPConnectError(421, "no")

    monkeypatch.setattr(notify.smtplib, "SMTP", ConnectFails)
    with pytest.raises(MailError):
        notify.send_via_smtp(cfg, message, ["a@corp.local"])

    class SendFails:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_message(self, *a, **k):
            raise smtplib.SMTPServerDisconnected("mid-DATA")

    monkeypatch.setattr(notify.smtplib, "SMTP", SendFails)
    with pytest.raises(AmbiguousSendError):
        notify.send_via_smtp(cfg, message, ["a@corp.local"])
