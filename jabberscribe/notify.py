"""Email delivery, at most once.

Confluence is safely retryable because a stored page id makes a retry an update.
Email has no such handle: a crash between the send and the commit of notified_at
is indistinguishable from a crash before the send, and guessing wrong re-mails
the participants their own call.

So the failure modes are separated. Failing to connect delivered nothing and is
retryable (MailError). Failing during the send may have delivered, so the job
goes to needs_review for a human (AmbiguousSendError) and is never retried
automatically. Under-notifying is recoverable; duplicate transcripts are not.
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import replace
from datetime import UTC, datetime
from email.message import EmailMessage

from jabberscribe.audit import MAILED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import NEEDS_REVIEW, Job, JobStore
from jabberscribe.render import RenderedCall
from jabberscribe.sidecar import parse_sidecar

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    """Nothing was delivered. Safe to retry."""


class AmbiguousSendError(RuntimeError):
    """The message may have been delivered. Never retry automatically."""


def build_message(cfg: Config, rendered: RenderedCall, to: list[str], transcript_name: str) -> EmailMessage:
    """Compose the message.

    The compliance Bcc is deliberately absent from the headers: it is passed to
    the SMTP envelope as an extra recipient instead, so participants never see
    the archive address.
    """
    if cfg.mail is None:  # pragma: no cover - config validation prevents this
        raise ValueError("notify stage requires a mail: config section")
    message = EmailMessage()
    message["Subject"] = rendered.mail_subject
    message["From"] = cfg.mail.from_address
    message["To"] = ", ".join(to)
    message.set_content(rendered.mail_body)
    message.add_attachment(
        rendered.transcript_text.encode("utf-8"),
        maintype="text",
        subtype="plain",
        filename=transcript_name,
    )
    return message


def send_via_smtp(cfg: Config, message: EmailMessage, recipients: list[str]) -> None:
    if cfg.mail is None:  # pragma: no cover
        raise ValueError("notify stage requires a mail: config section")
    mail = cfg.mail
    try:
        smtp = smtplib.SMTP(mail.smtp_host, mail.smtp_port, timeout=60)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"cannot connect to {mail.smtp_host}:{mail.smtp_port}: {exc}") from exc

    with smtp:
        try:
            if mail.use_tls:
                smtp.starttls()
            user = os.environ.get("JABBERSCRIBE_SMTP_USER")
            password = os.environ.get("JABBERSCRIBE_SMTP_PASSWORD")
            if user and password:
                smtp.login(user, password)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailError(f"SMTP handshake failed: {exc}") from exc
        try:
            smtp.send_message(message, to_addrs=recipients)
        except (OSError, smtplib.SMTPException) as exc:
            raise AmbiguousSendError(f"send may have partially completed: {exc}") from exc


def notify_call(
    job: Job,
    cfg: Config,
    store: JobStore,
    audit: AuditLog,
    rendered: RenderedCall,
    sender=send_via_smtp,
) -> bool:
    """Mail the transcript. Returns False if this call was already notified."""
    if cfg.mail is None:  # pragma: no cover - config validation prevents this
        raise ValueError("notify stage requires a mail: config section")
    if job.notified_at:
        log.info("%s: already notified at %s, skipping", job.call_id, job.notified_at)
        return False

    sidecar = parse_sidecar(job.sidecar_json)
    to = list(sidecar.emails)
    if not to:
        # No resolvable recipient must not strand a transcript. Name AND
        # extension, because whoever picks this up needs enough to find the
        # participant in the directory and the extension is often the only handle.
        to = [cfg.mail.fallback_to]
        unresolved = ", ".join(f"{p.display_name or '?'} (ext {p.extension or '-'})" for p in sidecar.participants)
        rendered = replace(
            rendered,
            mail_body=f"{rendered.mail_body}\n\nNo participant email could be resolved. Participants: {unresolved}\n",
        )

    recipients = list(to)
    if cfg.mail.compliance_bcc:
        recipients.append(cfg.mail.compliance_bcc)

    message = build_message(cfg, rendered, to, f"{job.call_id}.transcript.txt")

    try:
        sender(cfg, message, recipients)
    except AmbiguousSendError as exc:
        store.set_status(job.call_id, NEEDS_REVIEW, last_error=str(exc))
        log.error("%s: ambiguous send, needs review: %s", job.call_id, exc)
        raise

    store.mark_notified(job.call_id, datetime.now(UTC).isoformat(timespec="seconds"))
    audit.record(job.call_id, MAILED, ", ".join(recipients))
    log.info("%s: mailed %d recipient(s)", job.call_id, len(recipients))
    return True
