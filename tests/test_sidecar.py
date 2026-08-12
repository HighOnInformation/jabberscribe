import json

import pytest

from jabberscribe.sidecar import SidecarError, parse_sidecar

VALID = {
    "schema_version": 1,
    "call_id": "8f2a1c4e",
    "source": "cucm-bib",
    "kind": "call",
    "started_at": "2026-08-12T14:03:11+03:00",
    "ended_at": "2026-08-12T14:16:43+03:00",
    "duration_sec": 812,
    "subject": None,
    "participants": [
        {
            "display_name": "מאיר חדד",
            "uri": "mhadad@corp.local",
            "extension": "1042",
            "email": "mhadad@corp.local",
            "role": "caller",
        },
        {"display_name": "Support", "extension": "1099", "role": "callee"},
    ],
    "audio": {"tracks": "dual", "codec": "pcm_s16le", "sample_rate": 8000, "channels": 2},
}


def test_parse_valid_sidecar() -> None:
    sc = parse_sidecar(json.dumps(VALID))

    assert sc.call_id == "8f2a1c4e"
    assert sc.kind == "call"
    assert sc.tracks == "dual"
    assert sc.duration_sec == 812
    assert sc.sample_rate == 8000
    assert len(sc.participants) == 2
    assert sc.participants[0].display_name == "מאיר חדד"
    assert sc.participants[1].email is None


def test_raw_is_preserved_verbatim() -> None:
    text = json.dumps(VALID)

    assert parse_sidecar(text).raw == text


def test_emails_property_skips_missing_and_blank() -> None:
    payload = dict(VALID)
    payload["participants"] = [
        {"email": "a@corp.local"},
        {"email": ""},
        {"extension": "1099"},
        {"email": "b@corp.local"},
    ]

    assert parse_sidecar(json.dumps(payload)).emails == ("a@corp.local", "b@corp.local")


def test_defaults_applied_for_optional_fields() -> None:
    minimal = {
        "call_id": "c9",
        "started_at": "2026-08-12T14:03:11+03:00",
        "duration_sec": 10,
        "audio": {"tracks": "mixed"},
    }

    sc = parse_sidecar(json.dumps(minimal))

    assert sc.kind == "call"
    assert sc.source == "unknown"
    assert sc.participants == ()
    assert sc.ended_at is None
    assert sc.sample_rate is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"call_id": None}, "call_id"),
        ({"call_id": ""}, "call_id"),
        ({"started_at": None}, "started_at"),
        ({"duration_sec": None}, "duration_sec"),
        ({"duration_sec": -5}, "duration_sec"),
        ({"duration_sec": "long"}, "duration_sec"),
        ({"audio": {}}, "tracks"),
        ({"audio": {"tracks": "quad"}}, "tracks"),
        ({"audio": None}, "audio"),
        ({"kind": "webinar"}, "kind"),
    ],
)
def test_invalid_sidecars_are_rejected(mutation: dict, message: str) -> None:
    payload = {**VALID, **mutation}

    with pytest.raises(SidecarError, match=message):
        parse_sidecar(json.dumps(payload))


def test_utf8_bom_is_tolerated() -> None:
    """PowerShell, .NET, and Notepad all emit UTF-8 with a BOM by default."""
    sc = parse_sidecar("﻿" + json.dumps(VALID))

    assert sc.call_id == "8f2a1c4e"
    assert not sc.raw.startswith("﻿")


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(SidecarError, match="JSON"):
        parse_sidecar("{not json")


def test_non_object_json_is_rejected() -> None:
    with pytest.raises(SidecarError, match="object"):
        parse_sidecar("[1, 2, 3]")


def test_participants_must_be_a_list() -> None:
    payload = {**VALID, "participants": "מאיר"}

    with pytest.raises(SidecarError, match="participants"):
        parse_sidecar(json.dumps(payload))
