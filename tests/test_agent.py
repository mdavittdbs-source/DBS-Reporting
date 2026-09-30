import json

import anthropic
import httpx2
import pytest
from test_tools import make_client

from dbs_reporting.agent import ReportingAgent, request_options


def fake_claude(sent: list) -> anthropic.Anthropic:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append((dict(request.headers), body))
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": [{"type": "text", "text": "ok"}],
        })

    return anthropic.Anthropic(api_key="x", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def test_request_options_per_model():
    opus = request_options("claude-opus-5-5", "medium")
    assert opus["thinking"] == {"type": "adaptive"}
    assert opus["output_config"] == {"effort": "medium"}
    assert opus["fallbacks"] == "default"
    assert request_options("claude-haiku-4-5", "medium") == {}
    assert "fallbacks" not in request_options("claude-sonnet-5", "medium")
    assert request_options("claude-some-future-model", "medium") == {"thinking": {"type": "adaptive"}}


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
    assert sent[-1][0]["anthropic-beta"] == "server-side-fallback-2026-07-01"
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
