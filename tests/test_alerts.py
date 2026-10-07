import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from jabberscribe import cli
from jabberscribe.alerts import (
    BACKLOG,
    JOB_FAILED,
    LITELLM_DOWN,
    PURGE_ERRORS,
    TOKEN_ENV,
    URL_ENV,
    Alerter,
    check_jobs,
    make_alerter,
    probe_litellm,
)
from jabberscribe.jobs import FAILED
from jabberscribe.retention import PurgeResult

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class Hook:
    """Fake webhook: records every POST and answers with `status`."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[dict] = []
        self.urls: list[str] = []
        self.auth: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts.append(json.loads(request.content))
        self.urls.append(str(request.url))
        self.auth = request.headers.get("authorization")
        return httpx.Response(self.status)

    @property
    def kinds(self) -> list[str]:
        return [p["kind"] for p in self.posts]


def _alerter(tmp_path: Path, hook: Hook, token: str | None = None) -> Alerter:
    client = httpx.Client(transport=httpx.MockTransport(hook))
    return Alerter("http://hooks.test/alert", tmp_path / "alerts.json", client, token)


def _job(store, key: str) -> None:
    store.create(
        job_key=key,
        call_id=key.split("_")[0],
        conference_id=None,
        audio_path=Path(f"/out/{key}/recording.wav"),
        out_dir=Path(f"/out/{key}"),
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )


def _fail(store, key: str) -> None:
    _job(store, key)
    store.record_attempt(key, "stt: HTTP 413")
    store.set_status(key, FAILED)


def _age(cfg, key: str, minutes: int) -> None:
    created = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    conn = sqlite3.connect(cfg.paths.db_path)
    conn.execute("UPDATE jobs SET created_at = ? WHERE job_key = ?", (created, key))
    conn.commit()
    conn.close()


def test_send_posts_json_with_a_text_field(tmp_path: Path) -> None:
    hook = Hook()

    assert _alerter(tmp_path, hook).send(BACKLOG, "oldest call waited 20 min", T0) is True

    post = hook.posts[0]
    assert post["text"] == "JabberScribe backlog: oldest call waited 20 min"
    assert (post["source"], post["kind"], post["at"]) == ("jabberscribe", BACKLOG, "2026-10-07T12:00:00+00:00")
    assert post["host"]
    assert hook.auth is None


def test_each_kind_is_sent_at_most_once_an_hour(tmp_path: Path) -> None:
    hook = Hook()
    alerter = _alerter(tmp_path, hook)

    assert alerter.send(BACKLOG, "a", T0) is True
    assert alerter.send(BACKLOG, "b", T0 + timedelta(minutes=30)) is False
    assert alerter.send(JOB_FAILED, "c", T0 + timedelta(minutes=31)) is True
    assert alerter.send(BACKLOG, "d", T0 + timedelta(minutes=61)) is True

    assert hook.kinds == [BACKLOG, JOB_FAILED, BACKLOG]


def test_the_rate_limit_survives_a_restart(tmp_path: Path) -> None:
    hook = Hook()
    _alerter(tmp_path, hook).send(BACKLOG, "a", T0)

    assert _alerter(tmp_path, hook).send(BACKLOG, "b", T0 + timedelta(minutes=5)) is False
    assert list(tmp_path.glob("*.part")) == []


def test_an_undelivered_alert_is_tried_again(tmp_path: Path) -> None:
    hook = Hook(status=500)
    alerter = _alerter(tmp_path, hook)

    assert alerter.send(BACKLOG, "a", T0) is False
    hook.status = 200
    assert alerter.send(BACKLOG, "a", T0 + timedelta(minutes=6)) is True


def test_an_unreachable_webhook_never_raises(tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(refuse))

    assert Alerter("http://hooks.test/alert", tmp_path / "alerts.json", client).send(BACKLOG, "a", T0) is False


def test_url_and_token_come_from_the_environment(cfg, monkeypatch) -> None:
    hook = Hook()
    monkeypatch.setenv(URL_ENV, "http://relay.test/hook")
    monkeypatch.setenv(TOKEN_ENV, "s3cret")

    alerter = make_alerter(cfg, httpx.Client(transport=httpx.MockTransport(hook)))
    alerter.send(BACKLOG, "a", T0)

    assert hook.urls == ["http://relay.test/hook"]
    assert hook.auth == "Bearer s3cret"


def test_alerting_is_off_without_a_url(cfg, monkeypatch) -> None:
    monkeypatch.delenv(URL_ENV, raising=False)

    assert make_alerter(cfg) is None


def test_the_configured_url_is_used_when_the_environment_has_none(cfg, monkeypatch) -> None:
    monkeypatch.delenv(URL_ENV, raising=False)
    cfg.alerts.webhook_url = "http://config.test/hook"
    hook = Hook()

    make_alerter(cfg, httpx.Client(transport=httpx.MockTransport(hook))).send(BACKLOG, "a", T0)

    assert hook.urls == ["http://config.test/hook"]


def test_a_new_failure_is_alerted_once(store, tmp_path: Path) -> None:
    hook = Hook()
    alerter = _alerter(tmp_path, hook)
    _fail(store, "a_1")

    check_jobs(store, alerter, T0, 15)
    check_jobs(store, alerter, T0 + timedelta(hours=2), 15)

    assert hook.kinds == [JOB_FAILED]
    assert "a_1" in hook.posts[0]["message"]

    _fail(store, "b_2")
    check_jobs(store, alerter, T0 + timedelta(hours=3), 15)

    assert hook.kinds == [JOB_FAILED, JOB_FAILED]
    assert "b_2" in hook.posts[1]["message"]
    assert "a_1" not in hook.posts[1]["message"]


def test_a_rate_limited_failure_is_alerted_later(store, tmp_path: Path) -> None:
    hook = Hook()
    alerter = _alerter(tmp_path, hook)
    _fail(store, "a_1")
    check_jobs(store, alerter, T0, 15)
    _fail(store, "b_2")

    check_jobs(store, alerter, T0 + timedelta(minutes=10), 15)
    check_jobs(store, alerter, T0 + timedelta(minutes=70), 15)

    assert hook.kinds == [JOB_FAILED, JOB_FAILED]
    assert "b_2" in hook.posts[1]["message"]


def test_a_backlog_older_than_the_threshold_is_alerted(cfg, store, tmp_path: Path) -> None:
    hook = Hook()
    alerter = _alerter(tmp_path, hook)
    _job(store, "young_1")

    check_jobs(store, alerter, datetime.now(UTC), 15)
    assert hook.kinds == []

    _job(store, "old_2")
    _age(cfg, "old_2", 20)
    check_jobs(store, alerter, datetime.now(UTC), 15)

    assert hook.kinds == [BACKLOG]
    assert "20 min" in hook.posts[0]["message"]


def test_probe_litellm() -> None:
    def client(handler) -> httpx.Client:
        return httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(handler))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert probe_litellm(client(lambda r: httpx.Response(200, json={"data": []}))) is None
    assert probe_litellm(client(lambda r: httpx.Response(503))) == "HTTP 503"
    assert "refused" in probe_litellm(client(refuse))


def test_run_alerts_when_litellm_is_down(cfg_file, tmp_path: Path, monkeypatch) -> None:
    hook = Hook()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli, "make_alerter", lambda cfg: _alerter(tmp_path, hook))
    monkeypatch.setattr(
        cli,
        "make_client",
        lambda litellm_cfg: httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(refuse)),
    )

    assert cli.main(["--config", str(cfg_file), "run", "--once"]) == 0

    assert hook.kinds == [LITELLM_DOWN]
    assert "refused" in hook.posts[0]["message"]


def test_purge_command_alerts_on_errors(cfg_file, tmp_path: Path, monkeypatch) -> None:
    hook = Hook()
    monkeypatch.setattr(cli, "make_alerter", lambda cfg: _alerter(tmp_path, hook))
    monkeypatch.setattr(cli, "purge", lambda *args, **kwargs: PurgeResult(errors=("k_1: cannot delete audio",)))

    assert cli.main(["--config", str(cfg_file), "purge"]) == 1

    assert hook.kinds == [PURGE_ERRORS]
    assert hook.posts[0]["message"] == "1 purge problem(s); see service log"
    assert "cannot delete" not in json.dumps(hook.posts[0])


def test_doctor_alerts_when_litellm_checks_fail(cfg_file, tmp_path: Path, monkeypatch) -> None:
    hook = Hook()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli, "make_alerter", lambda cfg: _alerter(tmp_path, hook))
    monkeypatch.setattr(
        cli,
        "make_client",
        lambda litellm_cfg: httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(refuse)),
    )

    assert cli.main(["--config", str(cfg_file), "doctor"]) == 1

    assert hook.kinds == [LITELLM_DOWN]
    assert hook.posts[0]["message"].startswith("doctor: litellm:")


def test_a_malformed_url_never_raises(tmp_path: Path) -> None:
    client = httpx.Client(transport=httpx.MockTransport(Hook()))

    assert Alerter("http://a:b:c/", tmp_path / "alerts.json", client).send(BACKLOG, "a", T0) is False


def test_corrupt_state_values_are_ignored(tmp_path: Path) -> None:
    for sent in ({"backlog": "garbage"}, {"backlog": "2026-10-07T12:00:00"}, {"backlog": 5}, {"backlog": None}):
        hook = Hook()
        path = tmp_path / "alerts.json"
        path.write_text(json.dumps({"sent": sent, "failed_backoff": {"backlog": "garbage"}}), encoding="utf-8")
        alerter = Alerter("http://hooks.test/alert", path, httpx.Client(transport=httpx.MockTransport(hook)))

        assert alerter.send(BACKLOG, "a", T0) is True


def test_an_unwritable_state_file_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    hook = Hook()
    client = httpx.Client(transport=httpx.MockTransport(hook))

    assert Alerter("http://hooks.test/alert", blocker / "alerts.json", client).send(BACKLOG, "a", T0) is True


def test_the_webhook_url_is_never_logged(tmp_path: Path, caplog) -> None:
    hook = Hook(status=500)
    client = httpx.Client(transport=httpx.MockTransport(hook))
    alerter = Alerter("http://hooks.test/services/T0/SECRETPATH", tmp_path / "alerts.json", client)

    with caplog.at_level(logging.DEBUG):
        alerter.send(BACKLOG, "a", T0)

    assert "500" in caplog.text
    assert "SECRETPATH" not in caplog.text
    assert "hooks.test" not in caplog.text


def test_a_failed_delivery_backs_off_for_five_minutes(tmp_path: Path) -> None:
    hook = Hook(status=500)
    alerter = _alerter(tmp_path, hook)

    assert alerter.send(BACKLOG, "a", T0) is False
    hook.status = 200
    assert alerter.send(BACKLOG, "a", T0 + timedelta(minutes=4)) is False
    assert len(hook.posts) == 1
    assert alerter.send(BACKLOG, "a", T0 + timedelta(minutes=6)) is True
    assert _alerter(tmp_path, hook).send(BACKLOG, "a", T0 + timedelta(minutes=7)) is False


def test_the_backoff_survives_a_restart(tmp_path: Path) -> None:
    hook = Hook(status=500)
    _alerter(tmp_path, hook).send(BACKLOG, "a", T0)

    assert _alerter(tmp_path, hook).send(BACKLOG, "a", T0 + timedelta(minutes=1)) is False
    assert len(hook.posts) == 1


def test_purge_and_doctor_survive_a_dead_webhook(cfg_file, tmp_path: Path, monkeypatch, capsys) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    dead = httpx.Client(transport=httpx.MockTransport(refuse))
    monkeypatch.setattr(cli, "make_alerter", lambda cfg: Alerter("http://a:b:c/", tmp_path / "alerts.json", dead))
    monkeypatch.setattr(cli, "purge", lambda *args, **kwargs: PurgeResult(errors=("k_1: cannot delete audio",)))

    assert cli.main(["--config", str(cfg_file), "purge"]) == 1
    assert capsys.readouterr().out.strip()

    monkeypatch.setattr(
        cli,
        "make_client",
        lambda litellm_cfg: httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(refuse)),
    )
    assert cli.main(["--config", str(cfg_file), "doctor"]) == 1
    assert "[FAIL] litellm" in capsys.readouterr().out
