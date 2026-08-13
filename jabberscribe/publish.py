"""Publish a call to Confluence, idempotently and default-closed.

Idempotency rests on one stored fact: confluence_page_id. If it is set, the call
already has a page and we update it; a retry can never create a duplicate. The id
is stored the instant the page exists, before restrictions are applied, because a
page we failed to restrict is still a page we must not create twice.
"""

from __future__ import annotations

import logging

from jabberscribe.audit import PUBLISHED, UPDATED, AuditLog
from jabberscribe.config import Config
from jabberscribe.jobs import Job, JobStore
from jabberscribe.render import RenderedCall
from jabberscribe.sidecar import Sidecar, parse_sidecar

log = logging.getLogger(__name__)


def _restriction_users(sidecar: Sidecar) -> list[str]:
    """Confluence usernames for the call's participants.

    The sidecar's `uri` is the closest thing to a directory identity a recorder
    can supply, with email as a fallback. Anyone we cannot resolve simply is not
    granted -- the compliance group still has read access, so a page is never
    orphaned, and over-granting would be the worse failure.
    """
    users: list[str] = []
    for participant in sidecar.participants:
        identity = participant.uri or participant.email
        if identity:
            users.append(identity)
    return users


def publish_call(
    job: Job,
    cfg: Config,
    store: JobStore,
    audit: AuditLog,
    client,
    rendered: RenderedCall,
) -> str:
    if cfg.confluence is None:  # pragma: no cover - config validation prevents this
        raise ValueError("publish stage requires a confluence: config section")

    sidecar = parse_sidecar(job.sidecar_json)
    page_id = job.confluence_page_id

    if page_id:
        client.update_page(page_id, rendered.title, rendered.body_xhtml)
        audit.record(job.call_id, UPDATED, f"page {page_id}")
    else:
        page_id = client.create_page(
            cfg.confluence.space_key,
            cfg.confluence.parent_page_id,
            rendered.title,
            rendered.body_xhtml,
        )
        # Store before restricting: an unrestricted page is a problem, but a
        # duplicated page is a worse one.
        store.set_page_id(job.call_id, page_id)
        audit.record(job.call_id, PUBLISHED, f"page {page_id}")

    client.set_read_restrictions(page_id, _restriction_users(sidecar), cfg.confluence.compliance_group)
    return page_id
