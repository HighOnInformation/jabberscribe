import httpx
import pytest

from jabberscribe.config import LiteLLMConfig
from jabberscribe.llm import KEY_ENV, TransientError, make_client, post


def _capture() -> tuple[dict, httpx.MockTransport]:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={})

    return seen, httpx.MockTransport(handler)


def test_sends_bearer_key_from_environment(monkeypatch) -> None:
    monkeypatch.setenv(KEY_ENV, "sk-test")
    seen, transport = _capture()

    make_client(LiteLLMConfig(base_url="http://litellm.test"), transport=transport).get("/v1/models")

    assert seen["auth"] == "Bearer sk-test"
    assert seen["url"] == "http://litellm.test/v1/models"


def test_no_auth_header_without_key(monkeypatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    seen, transport = _capture()

    make_client(LiteLLMConfig(base_url="http://litellm.test"), transport=transport).get("/v1/models")

    assert seen["auth"] is None


def test_trailing_v1_in_base_url_is_not_doubled(monkeypatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    seen, transport = _capture()

    make_client(LiteLLMConfig(base_url="http://litellm.test/v1/"), transport=transport).get("/v1/models")

    assert seen["url"] == "http://litellm.test/v1/models"


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_post_turns_overload_into_transient_error(status: int) -> None:
    with pytest.raises(TransientError, match=str(status)):
        post(_client(lambda r: httpx.Response(status)), "/v1/chat/completions", json={})


def test_post_turns_transport_failure_into_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(TransientError, match="timed out"):
        post(_client(handler), "/v1/chat/completions", json={})


def test_post_leaves_client_errors_permanent() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        post(_client(lambda r: httpx.Response(404)), "/v1/chat/completions", json={})


def test_post_returns_successful_responses() -> None:
    assert post(_client(lambda r: httpx.Response(200, json={"ok": True})), "/x", json={}).json() == {"ok": True}
