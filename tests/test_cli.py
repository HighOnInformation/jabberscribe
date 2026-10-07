import json
import shutil
from pathlib import Path

import httpx
import pytest

from jabberscribe import cli
from jabberscribe.cli import CHAT_PROBE, doctor, main
from jabberscribe.jobs import DONE, FAILED, QUEUED, JobStore
from jabberscribe.lock import instance_lock
from jabberscribe.output import ACTIONS_FILE, RESULT_FILE, SUMMARY_FILE, TRANSCRIPT_FILE

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")

SUMMARY_ANSWER = json.dumps(
    {
        "summary": "סיכום.",
        "action_items": [{"task": "לשלוח את הדוח", "owner": "דנה", "due": "מחר", "source_ts": "00:00:00"}],
    },
    ensure_ascii=False,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(handler))


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


def _chat(content: object) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


class FakeLiteLLM:
    """Routes the three LiteLLM endpoints JabberScribe uses."""

    def __init__(self, models: tuple[str, ...] = ("whisper-he", "gemma-3"), probe_answer: object = '{"ok": true}'):
        self.models = models
        self.probe_answer = probe_answer
        self.transcriptions = 0
        self.chats: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if path == "/v1/audio/transcriptions":
            request.read()
            self.transcriptions += 1
            return httpx.Response(200, json={"segments": [{"start": 0.0, "end": 2.0, "text": "דנה תשלח את הדוח מחר"}]})
        if path == "/v1/chat/completions":
            body = json.loads(request.content)
            self.chats.append(body)
            if CHAT_PROBE in body["messages"][0]["content"]:
                return _chat(self.probe_answer)
            return _chat(SUMMARY_ANSWER)
        return httpx.Response(404)


@pytest.fixture
def cfg_file(tmp_path: Path) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "paths:\n"
        f"  drop_root: {(tmp_path / 'drop').as_posix()}\n"
        f"  work_dir: {(tmp_path / 'work').as_posix()}\n"
        f"  out_root: {(tmp_path / 'out').as_posix()}\n"
        f"  db_path: {(tmp_path / 'js.db').as_posix()}\n"
        "watcher:\n"
        "  min_age_seconds: 0\n"
        "litellm:\n"
        "  base_url: http://litellm.test\n"
        "stt:\n"
        "  model: whisper-he\n"
        "summary:\n"
        "  model: gemma-3\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fake_litellm(monkeypatch) -> FakeLiteLLM:
    server = FakeLiteLLM()
    monkeypatch.setattr(cli, "make_client", lambda litellm_cfg: _client(server))
    return server


def _checks(cfg, server) -> dict:
    return {c.name: c for c in doctor(cfg, _client(server))}


@needs_ffmpeg
def test_doctor_passes_against_a_working_server(cfg) -> None:
    server = FakeLiteLLM()

    checks = _checks(cfg, server)

    assert all(c.ok for c in checks.values()), [c for c in checks.values() if not c.ok]
    assert server.transcriptions == 1
    probe = server.chats[0]
    assert [m["role"] for m in probe["messages"]] == ["user"]
    assert probe["response_format"] == {"type": "json_object"}
    assert cfg.paths.out_root.is_dir()


def test_doctor_names_a_missing_model(cfg) -> None:
    checks = _checks(cfg, FakeLiteLLM(models=("whisper-he",)))

    assert not checks["litellm"].ok
    assert "gemma-3" in checks["litellm"].detail


@pytest.mark.parametrize("answer", [None, "not json", '{"ok": false}'])
def test_doctor_fails_when_the_chat_answer_is_unusable(cfg, answer) -> None:
    assert not _checks(cfg, FakeLiteLLM(probe_answer=answer))["litellm.chat"].ok


def test_doctor_reports_a_rejected_chat_request(cfg) -> None:
    def reject_chat(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(400, text="System role not supported")
        return FakeLiteLLM()(request)

    check = _checks(cfg, reject_chat)["litellm.chat"]

    assert not check.ok
    assert "400" in check.detail


@needs_ffmpeg
def test_doctor_reports_a_transcription_route_without_segments(cfg) -> None:
    def no_segments(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions":
            request.read()
            return httpx.Response(200, json={"text": "שלום"})
        return FakeLiteLLM()(request)

    check = _checks(cfg, no_segments)["litellm.transcription"]

    assert not check.ok
    assert "verbose_json" in check.detail


def test_doctor_reports_an_unreachable_server(cfg) -> None:
    checks = _checks(cfg, _refuse)

    assert not checks["litellm"].ok
    assert "refused" in checks["litellm"].detail
    assert not checks["litellm.chat"].ok


def test_doctor_reports_a_missing_vocabulary_file(cfg, tmp_path) -> None:
    cfg.stt.vocabulary_file = tmp_path / "missing.txt"

    check = _checks(cfg, FakeLiteLLM())["stt.vocabulary_file"]

    assert not check.ok
    assert "missing.txt" in check.detail


def test_main_doctor_fails_when_litellm_is_down(cfg_file, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "make_client", lambda litellm_cfg: _client(_refuse))

    code = main(["--config", str(cfg_file), "doctor"])

    assert code == 1
    assert "[FAIL] litellm" in capsys.readouterr().out


def test_main_reports_bad_config_without_traceback(tmp_path: Path, capsys) -> None:
    code = main(["--config", str(tmp_path / "missing.yaml"), "doctor"])

    assert code == 2
    assert "not found" in capsys.readouterr().err


@needs_ffmpeg
def test_process_produces_outputs_end_to_end(tmp_path, cfg_file, fake_litellm, make_wav, make_sidecar, capsys) -> None:
    audio = make_wav(tmp_path / "src" / "call.wav", channels=2, seconds=2.0)
    sidecar = make_sidecar(tmp_path / "src" / "call.json", call_id="e2e", tracks="dual")

    code = main(["--config", str(cfg_file), "process", str(audio), str(sidecar)])

    assert code == 0
    out = tmp_path / "out" / "2026" / "10" / "e2e_1042"
    result = json.loads((out / RESULT_FILE).read_text(encoding="utf-8"))
    assert result["transcript"][0]["text"] == "דנה תשלח את הדוח מחר"
    assert result["action_items"][0]["owner"] == "דנה"
    for name in ("recording.wav", TRANSCRIPT_FILE, SUMMARY_FILE, ACTIONS_FILE):
        assert (out / name).is_file()
    assert "e2e_1042: done" in capsys.readouterr().out


@needs_ffmpeg
def test_reprocessing_the_same_call_is_a_no_op(tmp_path, cfg_file, fake_litellm, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "src" / "call.wav", channels=2)
    sidecar = make_sidecar(tmp_path / "src" / "call.json", call_id="twice", tracks="dual")

    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 0
    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 0

    assert fake_litellm.transcriptions == 2  # one per channel of the dual-track call, and only once
    assert len(JobStore(tmp_path / "js.db").list_all()) == 1


@needs_ffmpeg
def test_process_does_not_wait_on_a_conference_copy(tmp_path, cfg_file, fake_litellm, make_wav, make_sidecar) -> None:
    audio = make_wav(tmp_path / "src" / "conf.wav", channels=2)
    sidecar = make_sidecar(tmp_path / "src" / "conf.json", call_id="m1", conference_id="conf-1", tracks="dual")

    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 0

    assert (tmp_path / "out" / "2026" / "10" / "m1_1042" / RESULT_FILE).is_file()


@needs_ffmpeg
def test_process_leaves_other_queued_jobs_alone(tmp_path, cfg_file, fake_litellm, make_wav, make_sidecar) -> None:
    """`process` is a manual tool; it must not drain the service's queue."""
    make_wav(tmp_path / "drop" / "inbox" / "other.wav")
    make_sidecar(tmp_path / "drop" / "inbox" / "other.json", call_id="other")
    audio = make_wav(tmp_path / "src" / "mine.wav")
    sidecar = make_sidecar(tmp_path / "src" / "mine.json", call_id="mine")

    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 0

    store = JobStore(tmp_path / "js.db")
    assert store.get("mine_1042").status == DONE
    assert store.get("other_1042").status == QUEUED
    assert fake_litellm.transcriptions == 1


@needs_ffmpeg
def test_process_exits_non_zero_when_the_job_did_not_finish(tmp_path, cfg_file, monkeypatch, make_wav, make_sidecar):
    def stt_down(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions":
            return httpx.Response(503)
        return FakeLiteLLM()(request)

    monkeypatch.setattr(cli, "make_client", lambda litellm_cfg: _client(stt_down))
    audio = make_wav(tmp_path / "src" / "call.wav")
    sidecar = make_sidecar(tmp_path / "src" / "call.json", call_id="down")

    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 1
    assert JobStore(tmp_path / "js.db").get("down_1042").status == QUEUED


def test_process_refuses_to_run_beside_another_instance(tmp_path, cfg_file, make_wav, make_sidecar, capsys) -> None:
    audio = make_wav(tmp_path / "src" / "call.wav")
    sidecar = make_sidecar(tmp_path / "src" / "call.json")

    with instance_lock(tmp_path / "js.db"):
        code = main(["--config", str(cfg_file), "process", str(audio), str(sidecar)])

    assert code == 1
    assert "another JabberScribe instance" in capsys.readouterr().err


class _Stop(Exception):
    pass


def _stop_after(polls: int):
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= polls:
            raise _Stop

    return sleep


@needs_ffmpeg
def test_run_survives_a_failing_poll(tmp_path, cfg_file, fake_litellm, monkeypatch, make_wav, make_sidecar) -> None:
    make_wav(tmp_path / "drop" / "inbox" / "a.wav")
    make_sidecar(tmp_path / "drop" / "inbox" / "a.json", call_id="svc")
    real_scan = cli.scan_once
    calls = {"n": 0}

    def flaky_scan(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("share unavailable")
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(cli, "scan_once", flaky_scan)
    monkeypatch.setattr(cli.time, "sleep", _stop_after(2))

    with pytest.raises(_Stop):
        main(["--config", str(cfg_file), "run"])

    assert JobStore(tmp_path / "js.db").get("svc_1042").status == DONE


def test_run_purges_once_a_day(cfg_file, fake_litellm, monkeypatch) -> None:
    purges: list[object] = []
    real_purge = cli.purge

    def counting_purge(*args, **kwargs):
        purges.append(kwargs["now"])
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(cli, "purge", counting_purge)
    monkeypatch.setattr(cli.time, "sleep", _stop_after(3))

    with pytest.raises(_Stop):
        main(["--config", str(cfg_file), "run"])

    assert len(purges) == 1


def test_run_once_reports_a_failing_poll(cfg_file, fake_litellm, monkeypatch) -> None:
    def broken_scan(*args, **kwargs):
        raise OSError("share unavailable")

    monkeypatch.setattr(cli, "scan_once", broken_scan)

    assert main(["--config", str(cfg_file), "run", "--once"]) == 1


def _failed_job(tmp_path: Path, key: str = "f_1042") -> JobStore:
    store = JobStore(tmp_path / "js.db")
    store.init_schema()
    store.create(
        job_key=key,
        call_id="f",
        conference_id=None,
        audio_path=tmp_path / "out" / key / "recording.wav",
        out_dir=tmp_path / "out" / key,
        sidecar_json="{}",
        started_at="2026-10-07T14:03:11+03:00",
        duration_sec=5,
    )
    store.record_attempt(key, "stt: HTTP 413")
    store.set_status(key, FAILED)
    return store


def test_retry_requeues_a_failed_job(tmp_path, cfg_file, capsys) -> None:
    _failed_job(tmp_path)

    assert main(["--config", str(cfg_file), "retry", "f_1042"]) == 0

    job = JobStore(tmp_path / "js.db").get("f_1042")
    assert (job.status, job.attempts) == (QUEUED, 0)
    assert "f_1042: requeued" in capsys.readouterr().out


def test_retry_failed_requeues_every_failed_job(tmp_path, cfg_file) -> None:
    _failed_job(tmp_path, "a_1")
    _failed_job(tmp_path, "b_2")

    assert main(["--config", str(cfg_file), "retry", "--failed"]) == 0

    store = JobStore(tmp_path / "js.db")
    assert {store.get("a_1").status, store.get("b_2").status} == {QUEUED}


def test_retry_refuses_a_job_that_did_not_fail(tmp_path, cfg_file, capsys) -> None:
    store = _failed_job(tmp_path)
    store.set_status("f_1042", DONE)

    assert main(["--config", str(cfg_file), "retry", "f_1042"]) == 1
    assert "nothing requeued" in capsys.readouterr().err


def test_status_lists_counts_and_failures(tmp_path, cfg_file, capsys) -> None:
    _failed_job(tmp_path)

    code = main(["--config", str(cfg_file), "status"])

    out = capsys.readouterr().out
    assert code == 1
    assert "failed: 1" in out
    assert "! f_1042: stt: HTTP 413" in out


def test_status_is_zero_when_nothing_failed(cfg_file, capsys) -> None:
    assert main(["--config", str(cfg_file), "status"]) == 0
    assert "queued: 0" in capsys.readouterr().out


@needs_ffmpeg
def test_process_exit_follows_the_primary_of_a_conference_copy(
    tmp_path, cfg_file, monkeypatch, make_wav, make_sidecar, capsys
) -> None:
    def stt_down(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/audio/transcriptions":
            return httpx.Response(503)
        return FakeLiteLLM()(request)

    monkeypatch.setattr(cli, "make_client", lambda litellm_cfg: _client(stt_down))
    first = make_wav(tmp_path / "src" / "a.wav", channels=2)
    first_side = make_sidecar(tmp_path / "src" / "a.json", call_id="m1", conference_id="conf-1", tracks="dual")
    second = make_wav(tmp_path / "src" / "b.wav", channels=2)
    second_side = make_sidecar(
        tmp_path / "src" / "b.json", call_id="m1", extension="1043", conference_id="conf-1", tracks="dual"
    )

    assert main(["--config", str(cfg_file), "process", str(first), str(first_side)]) == 1
    code = main(["--config", str(cfg_file), "process", str(second), str(second_side)])

    store = JobStore(tmp_path / "js.db")
    copy = store.get("m1_1043")
    assert copy.grouped_into == "m1_1042"
    assert store.get("m1_1042").status == QUEUED
    assert code == 1
    assert "m1_1043: queued" in capsys.readouterr().out


def test_process_refuses_to_overwrite_an_inbox_file(tmp_path, cfg_file, make_wav, make_sidecar, capsys) -> None:
    make_wav(tmp_path / "drop" / "inbox" / "call.wav")
    audio = make_wav(tmp_path / "src" / "call.wav")
    sidecar = make_sidecar(tmp_path / "src" / "call.json")

    assert main(["--config", str(cfg_file), "process", str(audio), str(sidecar)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_run_purges_even_when_the_pipeline_raises(cfg_file, fake_litellm, monkeypatch) -> None:
    purges: list[object] = []
    monkeypatch.setattr(cli, "run_once", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(cli, "purge", lambda *a, **k: purges.append(k["now"]))
    monkeypatch.setattr(cli.time, "sleep", _stop_after(3))

    with pytest.raises(_Stop):
        main(["--config", str(cfg_file), "run"])

    assert len(purges) == 1


def test_run_does_not_retry_a_failing_purge_the_same_day(cfg_file, fake_litellm, monkeypatch) -> None:
    attempts: list[int] = []

    def broken_purge(*args, **kwargs):
        attempts.append(1)
        raise OSError("disk gone")

    monkeypatch.setattr(cli, "purge", broken_purge)
    monkeypatch.setattr(cli.time, "sleep", _stop_after(3))

    with pytest.raises(_Stop):
        main(["--config", str(cfg_file), "run"])

    assert len(attempts) == 1


def test_run_purges_again_on_the_next_utc_day(cfg_file, fake_litellm, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    clock = {"now": datetime(2026, 10, 7, 23, 59, tzinfo=UTC)}
    purges: list[object] = []
    monkeypatch.setattr(cli, "_utcnow", lambda: clock["now"])
    monkeypatch.setattr(cli, "purge", lambda *a, **k: purges.append(k["now"]))
    calls = {"n": 0}

    def sleep(seconds: float) -> None:
        calls["n"] += 1
        clock["now"] += timedelta(minutes=2)
        if calls["n"] >= 3:
            raise _Stop

    monkeypatch.setattr(cli.time, "sleep", sleep)

    with pytest.raises(_Stop):
        main(["--config", str(cfg_file), "run"])

    assert len(purges) == 2


def test_doctor_includes_the_servers_reason_for_a_4xx(cfg) -> None:
    def reject_chat(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(400, text="System role not supported")
        return FakeLiteLLM()(request)

    assert "System role not supported" in _checks(cfg, reject_chat)["litellm.chat"].detail


def test_doctor_reports_an_unreadable_vocabulary_file(cfg, tmp_path) -> None:
    vocab = tmp_path / "vocab.txt"
    vocab.write_bytes(b"\xff\xfe\xfa bad")
    cfg.stt.vocabulary_file = vocab

    checks = _checks(cfg, FakeLiteLLM())

    assert not checks["stt.vocabulary_file"].ok
    assert not checks["litellm.transcription"].ok


def test_run_exits_2_for_an_unreadable_vocabulary_file(tmp_path, cfg_file, capsys) -> None:
    vocab = tmp_path / "vocab.txt"
    vocab.write_bytes(b"\xff\xfe\xfa bad")
    text = cfg_file.read_text(encoding="utf-8")
    extra = f"  vocabulary_file: {vocab.as_posix()}"
    cfg_file.write_text(text.replace("stt:\n", "stt:\n" + extra + "\n"), encoding="utf-8")

    assert main(["--config", str(cfg_file), "run", "--once"]) == 2
    assert "vocabulary" in capsys.readouterr().err


def _failed_chain(tmp_path: Path) -> JobStore:
    """Copies a (300 s), b (200 s), c (100 s) of one conference; each failed and handed over, c last."""
    store = JobStore(tmp_path / "js.db")
    store.init_schema()
    for key, seconds in (("a_1", 300), ("b_2", 200), ("c_3", 100)):
        store.create(
            job_key=key,
            call_id=key[0],
            conference_id="conf",
            audio_path=tmp_path / "out" / key / "recording.wav",
            out_dir=tmp_path / "out" / key,
            sidecar_json="{}",
            started_at="2026-10-07T14:00:00+03:00",
            duration_sec=seconds,
        )
        store.record_attempt(key, "audio: share offline")
        store.set_status(key, FAILED)
    store.hand_over("a_1", "b_2")
    store.hand_over("b_2", "c_3")
    return store


def test_retry_failed_restarts_a_handed_over_chain_from_the_longest_copy(tmp_path, cfg_file, capsys) -> None:
    _failed_chain(tmp_path)

    assert main(["--config", str(cfg_file), "retry", "--failed"]) == 0

    store = JobStore(tmp_path / "js.db")
    assert (store.get("a_1").status, store.get("a_1").grouped_into) == (QUEUED, None)
    assert {m.job_key for m in store.members("a_1")} == {"b_2", "c_3"}
    assert "a_1: requeued" in capsys.readouterr().out


def test_retry_by_key_of_a_handed_over_copy_requeues_the_longest(tmp_path, cfg_file) -> None:
    _failed_chain(tmp_path)

    assert main(["--config", str(cfg_file), "retry", "c_3"]) == 0

    assert JobStore(tmp_path / "js.db").get("a_1").status == QUEUED


def test_status_lists_handed_over_failed_copies(tmp_path, cfg_file, capsys) -> None:
    _failed_chain(tmp_path)

    assert main(["--config", str(cfg_file), "status"]) == 1

    out = capsys.readouterr().out
    assert "! a_1: audio: share offline (handed over to c_3)" in out
    assert "! c_3: audio: share offline" in out


def test_status_is_zero_when_failed_copies_were_superseded_by_a_done_primary(tmp_path, cfg_file, capsys) -> None:
    store = _failed_chain(tmp_path)
    store.set_status("c_3", DONE)

    assert main(["--config", str(cfg_file), "status"]) == 0
    assert "(handed over to c_3)" in capsys.readouterr().out


def test_run_gives_each_phase_its_own_chance(cfg_file, fake_litellm, monkeypatch) -> None:
    """A scan that raises must not skip settle, processing, or purge in the same poll."""
    ran: list[str] = []

    def phase(name: str, fail: bool = False):
        def call(*args, **kwargs):
            ran.append(name)
            if fail:
                raise RuntimeError(f"{name} bug")

        return call

    monkeypatch.setattr(cli, "scan_once", phase("scan", fail=True))
    monkeypatch.setattr(cli, "settle", phase("settle", fail=True))
    monkeypatch.setattr(cli, "run_once", phase("process"))

    assert main(["--config", str(cfg_file), "run", "--once"]) == 1
    assert ran == ["scan", "settle", "process"]
