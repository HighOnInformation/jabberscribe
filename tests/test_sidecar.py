import json

import pytest

from jabberscribe.sidecar import SidecarError, job_key, parse_sidecar


def _doc(**overrides: object) -> str:
    payload: dict[str, object] = {
        "schema_version": 2,
        "call_id": "gcid-1",
        "conference_id": None,
        "line_owner": {"extension": "1042", "user": "meir", "display_name": "מאיר"},
        "parties": [{"extension": "2210", "display_name": "דנה"}],
        "kind": "call",
        "started_at": "2026-10-07T14:03:11+03:00",
        "ended_at": "2026-10-07T14:16:43+03:00",
        "duration_sec": 812,
        "audio": {"tracks": "dual", "sample_rate": 8000, "channels": 2},
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_parses_valid_sidecar() -> None:
    sidecar = parse_sidecar(_doc())

    assert sidecar.call_id == "gcid-1"
    assert sidecar.conference_id is None
    assert sidecar.line_owner.extension == "1042"
    assert sidecar.line_owner.user == "meir"
    assert sidecar.parties[0].display_name == "דנה"
    assert sidecar.kind == "call"
    assert sidecar.started_at == "2026-10-07T14:03:11+03:00"
    assert sidecar.ended_at == "2026-10-07T14:16:43+03:00"
    assert sidecar.duration_sec == 812
    assert sidecar.tracks == "dual"


def test_job_key_combines_call_and_line() -> None:
    assert parse_sidecar(_doc()).job_key == "gcid-1_1042"


def test_job_key_is_filesystem_safe() -> None:
    assert job_key("a:b/c\\d*e", "10 42") == "a-b-c-d-e_10-42"


def test_party_as_dict() -> None:
    owner = parse_sidecar(_doc()).line_owner

    assert owner.as_dict() == {"extension": "1042", "user": "meir", "display_name": "מאיר"}


def test_conference_id_is_parsed() -> None:
    sidecar = parse_sidecar(_doc(conference_id="conf-9", kind="conference"))

    assert sidecar.conference_id == "conf-9"
    assert sidecar.kind == "conference"


def test_leading_bom_is_tolerated() -> None:
    assert parse_sidecar("﻿" + _doc()).call_id == "gcid-1"


def test_unknown_fields_are_ignored() -> None:
    assert parse_sidecar(_doc(recorder_build="7.1")).call_id == "gcid-1"


def test_missing_parties_defaults_to_empty() -> None:
    payload = json.loads(_doc())
    del payload["parties"]

    assert parse_sidecar(json.dumps(payload)).parties == ()


def test_rejects_non_json() -> None:
    with pytest.raises(SidecarError, match="not valid JSON"):
        parse_sidecar("{nope")


def test_rejects_non_object() -> None:
    with pytest.raises(SidecarError, match="JSON object"):
        parse_sidecar("[]")


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"call_id": ""}, "call_id"),
        ({"call_id": None}, "call_id"),
        ({"line_owner": None}, "line_owner"),
        ({"line_owner": "1042"}, "line_owner"),
        ({"line_owner": {"user": "meir"}}, "line_owner.extension"),
        ({"started_at": "yesterday"}, "started_at"),
        ({"duration_sec": -1}, "duration_sec"),
        ({"duration_sec": True}, "duration_sec"),
        ({"duration_sec": "812"}, "duration_sec"),
        ({"audio": None}, "audio"),
        ({"audio": {"tracks": "quad"}}, "audio.tracks"),
        ({"kind": "webinar"}, "kind"),
        ({"parties": "everyone"}, "parties"),
        ({"parties": ["dana"]}, "parties"),
        ({"conference_id": 7}, "conference_id"),
    ],
)
def test_rejects_malformed(overrides: dict, fragment: str) -> None:
    with pytest.raises(SidecarError, match=fragment):
        parse_sidecar(_doc(**overrides))
