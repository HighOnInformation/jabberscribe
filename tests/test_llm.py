import httpx

from jabberscribe.config import LiteLLMConfig
from jabberscribe.llm import KEY_ENV, make_client


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
