import json
from pathlib import Path

import pytest

from jabberscribe.output import (
    ACTIONS_FILE,
    REPLACE_ATTEMPTS,
    RESULT_FILE,
    SUMMARY_FILE,
    SUMMARY_UNAVAILABLE,
    TEXT_FILES,
    TRANSCRIPT_FILE,
    update_owners,
    write_atomic,
    write_outputs,
)
from jabberscribe.sidecar import Party, parse_sidecar
from jabberscribe.stt import Segment
from jabberscribe.summarize import ActionItem, Summary

SEGMENTS = [Segment(0.0, 1.0, "אה, שלום"), Segment(61.0, 62.5, "נדבר מחר")]
SUMMARY = Summary("סיכום קצר.", (ActionItem("לשלוח | לבדוק", None, "מחר", "00:01:01"),))
MODELS = {"stt": "whisper-he", "summary": "gemma-3"}


def _write(tmp_path: Path, make_sidecar, summary: Summary | None = SUMMARY) -> Path:
    sidecar = parse_sidecar(make_sidecar(tmp_path / "s.json", call_id="gc1").read_text(encoding="utf-8"))
    out = tmp_path / "out"
    write_outputs(
        out,
        sidecar=sidecar,
        segments=SEGMENTS,
        summary=summary,
        owners=[sidecar.line_owner],
        models=MODELS,
        recording=out / "recording.wav",
    )
    return out


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_writes_all_files_without_partials(tmp_path: Path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar)

    assert sorted(p.name for p in out.iterdir()) == sorted(TEXT_FILES)


def test_transcript_has_timestamped_lines(tmp_path: Path, make_sidecar) -> None:
    text = _read(_write(tmp_path, make_sidecar) / TRANSCRIPT_FILE)

    assert "[00:00:00] אה, שלום" in text
    assert "[00:01:01] נדבר מחר" in text


def test_summary_markdown(tmp_path: Path, make_sidecar) -> None:
    assert "סיכום קצר." in _read(_write(tmp_path, make_sidecar) / SUMMARY_FILE)


def test_actions_table_escapes_pipes_and_marks_missing_owner(tmp_path: Path, make_sidecar) -> None:
    text = _read(_write(tmp_path, make_sidecar) / ACTIONS_FILE)

    assert "| לשלוח \\| לבדוק | — | מחר | 00:01:01 |" in text


def test_no_action_items_says_so(tmp_path: Path, make_sidecar) -> None:
    text = _read(_write(tmp_path, make_sidecar, Summary("ס", ())) / ACTIONS_FILE)

    assert "לא עלו משימות" in text


def test_unavailable_summary_is_visible(tmp_path: Path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar, summary=None)

    assert SUMMARY_UNAVAILABLE in _read(out / SUMMARY_FILE)
    assert SUMMARY_UNAVAILABLE in _read(out / ACTIONS_FILE)
    result = json.loads(_read(out / RESULT_FILE))
    assert result["summary_available"] is False
    assert result["summary"] is None
    assert result["action_items"] == []


def test_result_json_contents(tmp_path: Path, make_sidecar) -> None:
    result = json.loads(_read(_write(tmp_path, make_sidecar) / RESULT_FILE))

    assert result["schema_version"] == 1
    assert result["job_key"] == "gc1_1042"
    assert result["call_id"] == "gc1"
    assert result["kind"] == "call"
    assert result["owners"] == [{"extension": "1042", "user": "meir", "display_name": "מאיר"}]
    assert result["parties"] == [{"extension": "2210", "user": None, "display_name": "דנה"}]
    assert result["recording"] == "recording.wav"
    assert result["transcript"][1] == {"start": 61.0, "end": 62.5, "text": "נדבר מחר"}
    assert result["summary_available"] is True
    assert result["action_items"] == [{"task": "לשלוח | לבדוק", "owner": None, "due": "מחר", "source_ts": "00:01:01"}]
    assert result["models"] == MODELS
    assert result["generated_at"]


def test_update_owners_rewrites_only_owners(tmp_path: Path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar)
    before = json.loads(_read(out / RESULT_FILE))

    update_owners(out, [Party("1042", "meir", "מאיר"), Party("3000", "yossi", "יוסי")])

    after = json.loads(_read(out / RESULT_FILE))
    assert [o["extension"] for o in after["owners"]] == ["1042", "3000"]
    assert {k: v for k, v in after.items() if k != "owners"} == {k: v for k, v in before.items() if k != "owners"}


def test_markdown_bodies_are_right_to_left(tmp_path: Path, make_sidecar) -> None:
    out = _write(tmp_path, make_sidecar)

    for name in (TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE):
        text = _read(out / name)
        assert text.startswith('<div dir="rtl">\n\n#')
        assert text.endswith("</div>\n")


def test_timings_are_recorded(tmp_path: Path, make_sidecar) -> None:
    sidecar = parse_sidecar(make_sidecar(tmp_path / "s.json", call_id="gc1").read_text(encoding="utf-8"))
    timings = {"audio_sec": 1.5, "stt_sec": 20.0, "summarize_sec": 4.0, "hangup_to_output_sec": 95.0}

    path = write_outputs(
        tmp_path / "out",
        sidecar=sidecar,
        segments=SEGMENTS,
        summary=SUMMARY,
        owners=[sidecar.line_owner],
        models=MODELS,
        recording=tmp_path / "out" / "recording.wav",
        timings=timings,
    )

    assert json.loads(_read(path))["timings"] == timings


def test_write_atomic_retries_while_a_reader_holds_the_file(tmp_path: Path, monkeypatch) -> None:
    real_replace = Path.replace
    failures = iter([PermissionError("in use"), PermissionError("in use")])

    def flaky_replace(self: Path, target: Path) -> Path:
        error = next(failures, None)
        if error is not None:
            raise error
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr("jabberscribe.output.time.sleep", lambda seconds: None)

    write_atomic(tmp_path / "result.json", "{}")

    assert _read(tmp_path / "result.json") == "{}"


def test_write_atomic_gives_up_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    calls: list[Path] = []

    def locked(self: Path, target: Path) -> Path:
        calls.append(target)
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr("jabberscribe.output.time.sleep", lambda seconds: None)

    with pytest.raises(PermissionError):
        write_atomic(tmp_path / "result.json", "{}")

    assert len(calls) == REPLACE_ATTEMPTS
    assert list(tmp_path.iterdir()) == []
