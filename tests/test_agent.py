import json

import anthropic
import httpx2
import pytest
from test_tools import make_client

from dbs_reporting.agent import ReportingAgent, request_options


def sse(model: str, blocks: list[dict], stop_reason: str) -> str:
    """A Messages API streaming response (server-sent events) for the given content blocks."""
    events = [("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}})]
    for i, block in enumerate(blocks):
        if block["type"] == "text":
            events.append(("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {"type": "text", "text": ""}}))
            for piece in (block["text"][:2], block["text"][2:]):
                events.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                        "delta": {"type": "text_delta", "text": piece}}))
        else:
            events.append(("content_block_start", {"type": "content_block_start", "index": i,
                                                    "content_block": {**block, "input": {}}}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(block["input"])}}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    events.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason,
                                     "stop_sequence": None}, "usage": {"output_tokens": 1}}))
    events.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def fake_claude(sent: list, turns: list | None = None) -> anthropic.Anthropic:
    """Answers each request with the next (blocks, stop_reason) from `turns`, else a plain "ok"."""
    turns = list(turns or [])

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append((dict(request.headers), body))
        blocks, stop = turns.pop(0) if turns else ([{"type": "text", "text": "ok"}], "end_turn")
        return httpx2.Response(200, text=sse(body["model"], blocks, stop),
                               headers={"content-type": "text/event-stream"})

    return anthropic.Anthropic(api_key="x", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def test_request_options_per_model():
    opus = request_options("claude-opus-5-5", "medium")
    assert opus["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert opus["output_config"] == {"effort": "medium"}
    assert opus["fallbacks"] == "default"
    assert opus["betas"] == ["thinking-binding-controls-2026-08-01", "server-side-fallback-2026-07-01"]
    assert request_options("claude-haiku-4-5", "medium") == {}
    sonnet5 = request_options("claude-sonnet-5", "medium")
    assert "fallbacks" not in sonnet5 and sonnet5["betas"] == ["thinking-binding-controls-2026-08-01"]
    future = request_options("claude-some-future-model", "medium")
    assert future["thinking"]["type"] == "adaptive" and "block_binding" in future["thinking"]


def test_default_and_choices(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("CLAUDE_MODELS", "claude-opus-5-5, claude-haiku-4-5")
    agent = ReportingAgent(make_client([]), fake_claude([]))
    assert agent.default_model == "claude-sonnet-5-5"
    assert agent.models == ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5"]


def test_respond_saves_replayable_history(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("CLAUDE_MODELS", "claude-opus-5-5,claude-haiku-4-5")
    sent = []
    agent = ReportingAgent(make_client([]), fake_claude(sent))

    answer, history = agent.respond([], "hi")
    assert answer == "ok"
    assert sent[-1][1]["model"] == "claude-opus-5-5"
    assert sent[-1][0]["anthropic-beta"] == "thinking-binding-controls-2026-08-01,server-side-fallback-2026-07-01"
    # History must survive a JSON round trip (it's saved to the database).
    history = json.loads(json.dumps(history))

    _, history = agent.respond(history, "follow up")
    assert len(sent[-1][1]["messages"]) == 3

    agent.respond([], "again", "claude-haiku-4-5")
    body = sent[-1][1]
    assert body["model"] == "claude-haiku-4-5"
    assert "thinking" not in body and "fallbacks" not in body

    with pytest.raises(ValueError):
        agent.respond([], "nope", "claude-not-enabled")


def test_respond_stream_events(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    monkeypatch.delenv("CLAUDE_MODELS", raising=False)
    sent = []
    turns = [
        ([{"type": "tool_use", "id": "tu_1", "name": "find_company", "input": {"name": "Joe's Pizza"}}], "tool_use"),
        ([{"type": "text", "text": "Top issue: printers."}], "end_turn"),
    ]
    agent = ReportingAgent(make_client([]), fake_claude(sent, turns))
    events = list(agent.respond_stream([], "issues at Joe's Pizza?"))

    assert sent[0][1]["stream"] is True
    assert {"type": "status", "text": "Looking up the company…"} in events
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert text == "Top issue: printers."
    done = events[-1]
    assert done["type"] == "done" and done["answer"] == "Top issue: printers."
    # Usage covers both API calls (the tool call and the answer); the fake reports 1 in / 1 out each.
    assert done["usage"]["requests"] == 2
    assert done["usage"]["input_tokens"] == 2 and done["usage"]["output_tokens"] == 2
    assert done["usage"]["cost_usd"] > 0 and done["usage"]["priced"]
    # question, tool call, tool result, answer; tool result carries the real ConnectWise lookup
    roles = [m["role"] for m in done["history"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert "Joe's Pizza" in json.dumps(done["history"][2])
    assert json.loads(json.dumps(done["history"])) == done["history"]


def test_system_prompt_is_frozen_and_date_goes_with_question(monkeypatch):
    from datetime import date

    from dbs_reporting.agent import SYSTEM_PROMPT

    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    monkeypatch.delenv("CLAUDE_MODELS", raising=False)
    sent = []
    agent = ReportingAgent(make_client([]), fake_claude(sent))
    _, history = agent.respond([], "hi")
    agent.respond(json.loads(json.dumps(history)), "again")
    first, second = sent[0][1], sent[1][1]
    # Same instructions every turn (no date in them), so saved reasoning stays valid.
    assert first["system"] == second["system"] and "{today}" not in SYSTEM_PROMPT
    assert first["system"] == [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    # Prompt caching: instructions marked, plus automatic caching of the conversation.
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["messages"][0]["content"].startswith(f"(Today's date: {date.today().isoformat()})")
    assert first["messages"][0]["content"].endswith("hi")
    # The second request replays the first turn exactly as it was sent.
    assert second["messages"][:2] == first["messages"][:1] + [second["messages"][1]]
    assert second["messages"][0] == first["messages"][0]
    assert second["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}


def test_cut_off_tool_call_keeps_the_chat_usable(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    monkeypatch.delenv("CLAUDE_MODELS", raising=False)
    sent = []
    turns = [([{"type": "text", "text": "Let me check."},
               {"type": "tool_use", "id": "tu_1", "name": "find_company", "input": {"name": "Joe"}}], "max_tokens")]
    agent = ReportingAgent(make_client([]), fake_claude(sent, turns))
    answer, history = agent.respond([], "issues at Joe's?")
    assert "cut off" in answer
    # The unanswered tool call gets a result, so the next question isn't rejected by the API.
    assert history[-1]["role"] == "user"
    assert history[-1]["content"][0]["tool_use_id"] == "tu_1" and history[-1]["content"][0]["is_error"]
    agent.respond(json.loads(json.dumps(history)), "try again")
    assert len(sent) == 2
