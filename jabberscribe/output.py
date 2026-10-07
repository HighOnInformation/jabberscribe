"""Per-call output folder: Markdown for people, JSON for machines.

result.json is the contract with the future web app; the Markdown files are
renderings of the same data. Every file is written via .part + rename so a
reader never sees half a file.

The Markdown bodies are wrapped in <div dir="rtl">: mixed Hebrew and English
lines and tables render scrambled in most viewers without it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from jabberscribe.jobs import utcnow
from jabberscribe.sidecar import Party, Sidecar
from jabberscribe.stt import Segment, format_ts
from jabberscribe.summarize import Summary

TRANSCRIPT_FILE = "transcript.md"
SUMMARY_FILE = "summary.md"
ACTIONS_FILE = "actions.md"
RESULT_FILE = "result.json"
TEXT_FILES = (TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE, RESULT_FILE)

SUMMARY_UNAVAILABLE = "הסיכום אינו זמין עבור שיחה זו."

#: A reader holding the destination open (Explorer preview, an editor, AV) blocks os.replace on Windows.
REPLACE_ATTEMPTS = 5
REPLACE_DELAY_SECONDS = 0.2


def write_atomic(path: Path, text: str) -> None:
    """Write via .part + rename, retrying the rename while a reader holds the file.

    Raises PermissionError when the file stays locked; the .part is removed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(REPLACE_DELAY_SECONDS)


def _rtl(markdown: str) -> str:
    # The blank lines matter: without them CommonMark treats the body as raw HTML, not Markdown.
    return '<div dir="rtl">\n\n' + markdown + "\n</div>\n"


def _cell(value: str | None) -> str:
    return (value or "—").replace("|", "\\|").replace("\n", " ")


def _render_transcript(segments: list[Segment]) -> str:
    # Blank lines between segments: consecutive Markdown lines would merge into one paragraph.
    lines = [f"[{format_ts(s.start)}] {s.text}" for s in segments]
    return _rtl("# תמליל\n\n" + "\n\n".join(lines) + "\n")


def _render_summary(summary: Summary | None) -> str:
    return _rtl("# סיכום\n\n" + (summary.text if summary else SUMMARY_UNAVAILABLE) + "\n")


def _render_actions(summary: Summary | None) -> str:
    if summary is None:
        return _rtl("# משימות\n\n" + SUMMARY_UNAVAILABLE + "\n")
    if not summary.action_items:
        return _rtl("# משימות\n\nלא עלו משימות בשיחה.\n")
    rows = ["| משימה | אחראי | מועד | זמן בהקלטה |", "|---|---|---|---|"]
    rows += [f"| {_cell(i.task)} | {_cell(i.owner)} | {_cell(i.due)} | {i.source_ts} |" for i in summary.action_items]
    return _rtl("# משימות\n\n" + "\n".join(rows) + "\n")


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def write_outputs(
    out_dir: Path,
    *,
    sidecar: Sidecar,
    segments: list[Segment],
    summary: Summary | None,
    owners: list[Party],
    summary_error: str | None = None,
    models: dict[str, str],
    recording: Path,
    timings: dict[str, float] | None = None,
) -> Path:
    """Write every output file for one call. Returns the result.json path.

    `timings` holds per-stage seconds and the hang-up-to-output latency (see pipeline.py).
    `summary_error` says why the summary is unavailable, when the reason is known.
    """
    write_atomic(out_dir / TRANSCRIPT_FILE, _render_transcript(segments))
    write_atomic(out_dir / SUMMARY_FILE, _render_summary(summary))
    write_atomic(out_dir / ACTIONS_FILE, _render_actions(summary))
    result = {
        "schema_version": 1,
        "job_key": sidecar.job_key,
        "call_id": sidecar.call_id,
        "conference_id": sidecar.conference_id,
        "kind": sidecar.kind,
        "started_at": sidecar.started_at,
        "ended_at": sidecar.ended_at,
        "duration_sec": sidecar.duration_sec,
        "owners": [o.as_dict() for o in owners],
        "parties": [p.as_dict() for p in sidecar.parties],
        "recording": recording.name,
        "transcript": [asdict(s) for s in segments],
        "summary_available": summary is not None,
        "summary_error": None if summary is not None else summary_error,
        "summary": summary.text if summary else None,
        "action_items": [asdict(i) for i in summary.action_items] if summary else [],
        "models": models,
        "timings": timings or {},
        "generated_at": utcnow(),
    }
    path = out_dir / RESULT_FILE
    write_atomic(path, _dump(result))
    return path


def update_owners(out_dir: Path, owners: list[Party]) -> None:
    """Replace the owner list of an already-written call (a late conference copy joined).

    Raises OSError (e.g. PermissionError while a reader holds the file); the caller retries later.
    """
    path = out_dir / RESULT_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    data["owners"] = [o.as_dict() for o in owners]
    write_atomic(path, _dump(data))
