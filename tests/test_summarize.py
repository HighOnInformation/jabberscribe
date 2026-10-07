import json

import httpx
import pytest

from jabberscribe.stt import Segment
from jabberscribe.summarize import ActionItem, LiteLLMSummarizer, Summary, parse_summary, transcript_text

SEGMENTS = [Segment(0.0, 2.0, "שלום, מה שלומך"), Segment(65.0, 70.0, "דנה תשלח את הדוח עד יום חמישי")]

GOOD = {
    "summary": "שיחה קצרה על הדוח.",
    "action_items": [
        {"task": "לשלוח את הדוח", "owner": "דנה", "due": "יום חמישי", "source_ts": "00:01:05"},
        {"task": "לבדוק את ה-API", "owner": None, "due": None, "source_ts": "00:00:00"},
    ],
}
GOOD_JSON = json.dumps(GOOD, ensure_ascii=False)


class Server:
    """Fake LiteLLM chat route: answers each request with the next response."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        response = self.responses.pop(0)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": response}}]})


def _summarizer(server: Server) -> LiteLLMSummarizer:
    client = httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(server))
    return LiteLLMSummarizer(client, model="gemma-3")


def test_transcript_text_is_timestamped_lines() -> None:
    assert transcript_text(SEGMENTS) == "[00:00:00] שלום, מה שלומך\n[00:01:05] דנה תשלח את הדוח עד יום חמישי"


def test_returns_summary_and_action_items() -> None:
    summary = _summarizer(Server(GOOD_JSON)).summarize(SEGMENTS)

    assert summary == Summary(
        "שיחה קצרה על הדוח.",
        (
            ActionItem("לשלוח את הדוח", "דנה", "יום חמישי", "00:01:05"),
            ActionItem("לבדוק את ה-API", None, None, "00:00:00"),
        ),
    )


def test_request_carries_model_json_mode_and_transcript() -> None:
    server = Server(GOOD_JSON)

    _summarizer(server).summarize(SEGMENTS)

    request = server.requests[0]
    assert request["model"] == "gemma-3"
    assert request["response_format"] == {"type": "json_object"}
    assert request["messages"][0]["role"] == "system"
    assert "[00:01:05] דנה תשלח" in request["messages"][1]["content"]


def test_retries_once_after_unusable_output() -> None:
    server = Server("not json at all", GOOD_JSON)

    assert _summarizer(server).summarize(SEGMENTS) is not None
    assert len(server.requests) == 2


def test_gives_up_after_two_unusable_answers() -> None:
    server = Server("nope", "still nope")

    assert _summarizer(server).summarize(SEGMENTS) is None
    assert len(server.requests) == 2


def test_server_errors_degrade_to_none() -> None:
    server = Server(httpx.Response(503), httpx.Response(503))

    assert _summarizer(server).summarize(SEGMENTS) is None


def test_empty_transcript_skips_the_request() -> None:
    server = Server()

    assert _summarizer(server).summarize([]) is None
    assert server.requests == []


def test_parse_strips_code_fences() -> None:
    assert parse_summary(f"```json\n{GOOD_JSON}\n```").text == "שיחה קצרה על הדוח."


def test_parse_turns_blank_owner_and_due_into_none() -> None:
    doc = {"summary": "ס", "action_items": [{"task": "t", "owner": " ", "due": "", "source_ts": "00:00:01"}]}

    item = parse_summary(json.dumps(doc)).action_items[0]

    assert (item.owner, item.due) == (None, None)


@pytest.mark.parametrize(
    "doc",
    [
        {"summary": "", "action_items": []},
        {"summary": "ס"},
        {"summary": "ס", "action_items": [{"task": "t", "source_ts": "1:05"}]},
        {"summary": "ס", "action_items": [{"task": "", "source_ts": "00:00:01"}]},
    ],
)
def test_parse_rejects_invalid_documents(doc: dict) -> None:
    with pytest.raises(ValueError):
        parse_summary(json.dumps(doc))
