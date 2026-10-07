import json

import httpx
import pytest

from jabberscribe.llm import TransientError
from jabberscribe.stt import Segment
from jabberscribe.summarize import (
    MAX_TOKENS,
    ActionItem,
    LiteLLMSummarizer,
    Summary,
    SummaryError,
    SummaryUnavailable,
    chunk_segments,
    parse_summary,
    transcript_text,
)

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
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": response}}]})


def _summarizer(server: Server, max_chunk_chars: int = 12000) -> LiteLLMSummarizer:
    client = httpx.Client(base_url="http://litellm.test", transport=httpx.MockTransport(server))
    return LiteLLMSummarizer(client, model="gemma-3", max_chunk_chars=max_chunk_chars)


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


def test_request_has_no_system_role_and_caps_tokens() -> None:
    """Gemma 1 and 2 chat templates reject a system message."""
    server = Server(GOOD_JSON)

    _summarizer(server).summarize(SEGMENTS)

    request = server.requests[0]
    assert request["model"] == "gemma-3"
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == MAX_TOKENS
    assert [m["role"] for m in request["messages"]] == ["user"]
    assert "Return only a JSON object" in request["messages"][0]["content"]
    assert "[00:01:05] דנה תשלח" in request["messages"][0]["content"]


def test_retries_once_after_unusable_output() -> None:
    server = Server("not json at all", GOOD_JSON)

    assert _summarizer(server).summarize(SEGMENTS) is not None
    assert len(server.requests) == 2


def test_gives_up_after_two_unusable_answers_and_logs_them(caplog) -> None:
    server = Server("nope", "still nope")

    with pytest.raises(SummaryUnavailable, match="unusable"):
        _summarizer(server).summarize(SEGMENTS)
    assert len(server.requests) == 2
    assert "still nope" in caplog.text


def test_null_content_is_unusable_output_not_a_crash() -> None:
    """A refusal or empty completion comes back as content: null."""
    server = Server(None, "")

    with pytest.raises(SummaryUnavailable):
        _summarizer(server).summarize(SEGMENTS)
    assert len(server.requests) == 2


@pytest.mark.parametrize("status", [429, 500, 503])
def test_server_overload_is_transient(status: int) -> None:
    with pytest.raises(TransientError):
        _summarizer(Server(httpx.Response(status))).summarize(SEGMENTS)


def test_unreachable_server_is_transient() -> None:
    with pytest.raises(TransientError):
        _summarizer(Server(httpx.ConnectError("refused"))).summarize(SEGMENTS)


@pytest.mark.parametrize("status", [400, 413, 422])
def test_unprocessable_request_degrades_to_summary_unavailable(status: int) -> None:
    """A context-length 400 is the transcript's size, not a broken setup: the transcript must still ship."""
    server = Server(httpx.Response(status, json={"error": "maximum context length exceeded"}))

    with pytest.raises(SummaryUnavailable, match=f"HTTP {status}"):
        _summarizer(server).summarize(SEGMENTS)
    assert len(server.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 404])
def test_rejected_request_fails_loudly(status: int) -> None:
    """A wrong model name must not quietly ship "summary unavailable" for every call."""
    with pytest.raises(SummaryError, match=str(status)):
        _summarizer(Server(httpx.Response(status))).summarize(SEGMENTS)


def test_empty_transcript_skips_the_request() -> None:
    server = Server()

    assert _summarizer(server).summarize([]) is None
    assert server.requests == []


def test_short_transcript_is_one_chunk() -> None:
    assert chunk_segments(SEGMENTS, 12000) == [SEGMENTS]


def test_long_transcript_is_split_on_segment_boundaries() -> None:
    segments = [Segment(float(i), float(i + 1), "מילה " * 10) for i in range(10)]

    chunks = chunk_segments(segments, 200)

    assert [s for chunk in chunks for s in chunk] == segments
    assert len(chunks) > 1
    assert all(len(transcript_text(chunk)) <= 200 for chunk in chunks)


def test_long_transcript_is_summarized_per_chunk_then_merged() -> None:
    first = {"summary": "חלק ראשון.", "action_items": [GOOD["action_items"][1]]}
    second = {"summary": "חלק שני.", "action_items": [GOOD["action_items"][0], GOOD["action_items"][1]]}
    merged = {"summary": "סיכום מאוחד."}
    server = Server(*(json.dumps(d, ensure_ascii=False) for d in (first, second, merged)))

    summary = _summarizer(server, max_chunk_chars=40).summarize(SEGMENTS)

    assert summary == Summary(
        "סיכום מאוחד.",
        (
            ActionItem("לבדוק את ה-API", None, None, "00:00:00"),
            ActionItem("לשלוח את הדוח", "דנה", "יום חמישי", "00:01:05"),
        ),
    )
    assert "שלום, מה שלומך" in server.requests[0]["messages"][0]["content"]
    assert "דנה תשלח" in server.requests[1]["messages"][0]["content"]
    assert "חלק ראשון." in server.requests[2]["messages"][0]["content"]


def test_an_unusable_chunk_makes_the_summary_unavailable() -> None:
    server = Server("nope", "still nope")

    with pytest.raises(SummaryUnavailable):
        _summarizer(server, max_chunk_chars=40).summarize(SEGMENTS)


def test_merge_pass_is_bounded_by_max_chunk_chars() -> None:
    """Six chunk summaries that do not fit one merge prompt are merged in batches that do."""
    segments = [Segment(float(i), float(i + 1), "x" * 40) for i in range(6)]
    parts = [json.dumps({"summary": f"חלק {i}", "action_items": []}) for i in range(6)]
    merges = [json.dumps({"summary": text}) for text in ("איחוד 0", "איחוד 1", "סיכום סופי")]
    server = Server(*parts, *merges)

    summary = _summarizer(server, max_chunk_chars=60).summarize(segments)

    assert summary == Summary("סיכום סופי", ())
    bodies = [r["messages"][0]["content"].split("Partial summaries:\n", 1) for r in server.requests[6:]]
    assert len(bodies) == 3
    assert all(len(body) <= 60 for _, body in bodies)
    assert "איחוד 0" in bodies[2][1] and "איחוד 1" in bodies[2][1]


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
