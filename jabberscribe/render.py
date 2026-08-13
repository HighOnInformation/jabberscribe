"""Rendering: transcript data to human-readable artifacts.

Pure functions, no I/O beyond reading the transcript file. Everything here is
snapshot-testable, which matters because these are the only outputs a human ever
reads.

All interpolated content is HTML-escaped. Transcribed speech and directory
display names are untrusted text: a caller who says "less-than script" must not
be able to inject markup into a wiki page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path

from jabberscribe.sidecar import Sidecar
from jabberscribe.stt import Segment

_NO_SUMMARY_HE = "סיכום אינו זמין"
_TRANSCRIPT_HEADING_HE = "תמלול"
_SUMMARY_HEADING_HE = "סיכום"
_DETAILS_HEADING_HE = "פרטי השיחה"
_ATTACHED_HE = "התמלול המלא מצורף."


@dataclass(frozen=True)
class RenderedCall:
    title: str
    body_xhtml: str
    mail_subject: str
    mail_body: str
    transcript_text: str


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def load_transcript(path: Path) -> list[Segment]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        Segment(
            start=float(s["start"]),
            end=float(s["end"]),
            text=s["text"],
            speaker=s.get("speaker"),
        )
        for s in payload.get("segments", [])
    ]


def _participant_names(sidecar: Sidecar) -> str:
    names = [p.display_name or p.extension or p.uri or "?" for p in sidecar.participants]
    return ", ".join(names) if names else "unknown"


def _local_time(sidecar: Sidecar) -> datetime | None:
    try:
        return datetime.fromisoformat(sidecar.started_at)
    except ValueError:
        return None


def _date_and_time(sidecar: Sidecar) -> tuple[str, str]:
    stamp = _local_time(sidecar)
    if stamp is None:
        return sidecar.started_at, ""
    return stamp.strftime("%Y-%m-%d"), stamp.strftime("%H:%M")


def render_page_title(sidecar: Sidecar) -> str:
    date, time = _date_and_time(sidecar)
    duration = format_timestamp(sidecar.duration_sec)
    return f"{date} {time} — {_participant_names(sidecar)} ({duration})"


def render_transcript_text(segments: list[Segment]) -> str:
    lines = []
    for seg in segments:
        stamp = format_timestamp(seg.start)
        prefix = f"[{stamp}] {seg.speaker}: " if seg.speaker else f"[{stamp}] "
        lines.append(f"{prefix}{seg.text}")
    return "\n".join(lines) + "\n"


def _metadata_rows(sidecar: Sidecar, audio_note: str | None) -> list[tuple[str, str]]:
    date, time = _date_and_time(sidecar)
    rows = [
        ("מזהה שיחה", sidecar.call_id),
        ("תאריך", f"{date} {time}"),
        ("משך", format_timestamp(sidecar.duration_sec)),
        ("סוג", sidecar.kind),
        ("מקור ההקלטה", sidecar.source),
        ("משתתפים", _participant_names(sidecar)),
    ]
    if sidecar.subject:
        rows.append(("נושא", sidecar.subject))
    if audio_note:
        rows.append(("קובץ אודיו", audio_note))
    return rows


def render_confluence_body(
    sidecar: Sidecar,
    segments: list[Segment],
    summary: str | None,
    audio_note: str | None,
) -> str:
    rows = "".join(
        f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>"
        for label, value in _metadata_rows(sidecar, audio_note)
    )
    summary_html = f"<p>{escape(summary)}</p>" if summary else f"<p><em>{escape(_NO_SUMMARY_HE)}</em></p>"
    transcript_html = "".join(
        "<p><strong>[{stamp}]{speaker}</strong> {text}</p>".format(
            stamp=format_timestamp(seg.start),
            speaker=f" {escape(seg.speaker)}:" if seg.speaker else "",
            text=escape(seg.text),
        )
        for seg in segments
    )
    return (
        f'<div dir="rtl">'
        f"<h2>{escape(_DETAILS_HEADING_HE)}</h2>"
        f"<table><tbody>{rows}</tbody></table>"
        f"<h2>{escape(_SUMMARY_HEADING_HE)}</h2>{summary_html}"
        f"<h2>{escape(_TRANSCRIPT_HEADING_HE)}</h2>{transcript_html}"
        f"</div>"
    )


def render_mail(sidecar: Sidecar, summary: str | None, page_url: str | None) -> tuple[str, str]:
    date, time = _date_and_time(sidecar)
    subject = f"[Call] {date} {time} — {_participant_names(sidecar)}"
    lines = [
        f"{_DETAILS_HEADING_HE}: {_participant_names(sidecar)}",
        f"{date} {time}, {format_timestamp(sidecar.duration_sec)}",
        "",
        f"{_SUMMARY_HEADING_HE}:",
        summary or _NO_SUMMARY_HE,
        "",
    ]
    if page_url:
        lines.append(page_url)
    lines.append("")
    lines.append(_ATTACHED_HE)
    return subject, "\n".join(lines)


def render_call(
    sidecar: Sidecar,
    segments: list[Segment],
    summary: str | None = None,
    page_url: str | None = None,
    audio_note: str | None = None,
) -> RenderedCall:
    subject, body = render_mail(sidecar, summary, page_url)
    return RenderedCall(
        title=render_page_title(sidecar),
        body_xhtml=render_confluence_body(sidecar, segments, summary, audio_note),
        mail_subject=subject,
        mail_body=body,
        transcript_text=render_transcript_text(segments),
    )
