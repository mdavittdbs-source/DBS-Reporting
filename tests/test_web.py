"""End-to-end tests of logins and per-user chats, with ConnectWise and Claude mocked out."""

import importlib
import logging

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web(monkeypatch, tmp_path):
    for name in ("CW_SITE", "CW_COMPANY_ID", "CW_PUBLIC_KEY", "CW_PRIVATE_KEY", "CW_CLIENT_ID"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("CLAUDE_MODELS", "claude-opus-5-5,claude-haiku-4-5")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "users.txt"))
    (tmp_path / "users.txt").write_text(
        "alice | Alice A | password-a | admin\nbob | Bob B | password-b |\n", encoding="utf-8"
    )
    from dbs_reporting import web as module

    module = importlib.reload(module)
    from dbs_reporting import activity

    monkeypatch.setattr(activity, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(activity, "_logger", None)
    for handler in list(logging.getLogger("dbs_reporting.activity").handlers):
        logging.getLogger("dbs_reporting.activity").removeHandler(handler)
    calls = []

    def fake_respond_stream(history, question, model=None):
        calls.append((list(history), question, model))
        yield {"type": "status", "text": "Looking up the company…"}
        yield {"type": "reset"}
        answer = f"answer to {question}"
        yield {"type": "text", "text": answer[:6]}
        yield {"type": "text", "text": answer[6:]}
        yield {"type": "done", "answer": answer, "history": history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
        ], "usage": {"requests": 2, "input_tokens": 12000, "output_tokens": 800, "cache_read_tokens": 3000,
                     "cache_write_tokens": 0, "cost_usd": 0.0326, "priced": True}}

    monkeypatch.setattr(module.agent, "respond_stream", fake_respond_stream)
    return module, calls


def login(module, username, password):
    client = TestClient(module.app)
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def test_requires_login(web):
    module, _ = web
    client = TestClient(module.app)
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/chat", json={"question": "hi"}).status_code == 401
    bad = client.post("/api/login", json={"username": "alice", "password": "wrong"})
    assert bad.status_code == 401


def test_chats_are_saved_and_private(web):
    module, calls = web
    alice = login(module, "alice", "password-a")
    bob = login(module, "bob", "password-b")

    first = alice.post("/api/chat", json={"question": "Issues at Jimmy's Grille?", "model": "claude-haiku-4-5"}).json()
    assert first["answer"] == "answer to Issues at Jimmy's Grille?"
    assert first["title"] == "Issues at Jimmy's Grille?"
    chat_id = first["conversation_id"]

    # Follow-up continues the saved history with the chat's own model.
    alice.post("/api/chat", json={"question": "and last week?", "conversation_id": chat_id})
    history, question, model = calls[-1]
    assert question == "and last week?" and model == "claude-haiku-4-5" and len(history) == 2

    chats = alice.get("/api/conversations").json()
    assert [c["id"] for c in chats] == [chat_id]
    turns = alice.get(f"/api/conversations/{chat_id}").json()["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]

    # Bob can't see, continue, rename or delete Alice's chat.
    assert bob.get("/api/conversations").json() == []
    assert bob.get(f"/api/conversations/{chat_id}").status_code == 404
    assert bob.post("/api/chat", json={"question": "x", "conversation_id": chat_id}).status_code == 404
    assert bob.patch(f"/api/conversations/{chat_id}", json={"title": "mine"}).status_code == 404
    assert bob.delete(f"/api/conversations/{chat_id}").status_code == 404

    assert alice.patch(f"/api/conversations/{chat_id}", json={"title": "Jimmy's"}).status_code == 200
    assert alice.get("/api/conversations").json()[0]["title"] == "Jimmy's"
    assert alice.delete(f"/api/conversations/{chat_id}").status_code == 200
    assert alice.get("/api/conversations").json() == []


def test_chats_survive_restart(web, monkeypatch):
    module, _ = web
    alice = login(module, "alice", "password-a")
    chat_id = alice.post("/api/chat", json={"question": "hello"}).json()["conversation_id"]

    from dbs_reporting.store import Store

    reopened = Store(module.store.path)
    user = reopened.authenticate("alice", "password-a")
    assert [c["id"] for c in reopened.list_conversations(user["id"])] == [chat_id]
    assert len(reopened.get_conversation(user["id"], chat_id)["history"]) == 2


def test_logout_and_lockout(web):
    module, _ = web
    alice = login(module, "alice", "password-a")
    alice.post("/api/logout")
    assert alice.get("/api/me").status_code == 401

    client = TestClient(module.app)
    for _ in range(5):
        client.post("/api/login", json={"username": "bob", "password": "nope"})
    locked = client.post("/api/login", json={"username": "bob", "password": "password-b"})
    assert locked.status_code == 429


def test_rejects_disabled_model(web):
    module, _ = web
    alice = login(module, "alice", "password-a")
    response = alice.post("/api/chat", json={"question": "hi", "model": "claude-bogus"})
    assert response.status_code == 400


def test_removing_from_users_file_signs_out(web, tmp_path):
    import os

    module, _ = web
    bob = login(module, "bob", "password-b")
    assert bob.get("/api/me").status_code == 200
    path = tmp_path / "users.txt"
    path.write_text("\n".join(l for l in path.read_text().splitlines() if not l.startswith("bob")) + "\n")
    stamp = path.stat().st_mtime + 10
    os.utime(path, (stamp, stamp))
    assert bob.get("/api/me").status_code == 401


def test_model_is_locked_per_chat(web):
    module, calls = web
    alice = login(module, "alice", "password-a")
    first = alice.post("/api/chat", json={"question": "hi", "model": "claude-haiku-4-5"}).json()
    assert first["model"] == "claude-haiku-4-5"
    chat_id = first["conversation_id"]

    # Asking for a different model on an existing chat still uses the chat's own model.
    second = alice.post("/api/chat", json={"question": "again", "conversation_id": chat_id,
                                           "model": "claude-opus-5-5"}).json()
    assert second["conversation_id"] == chat_id and second["model"] == "claude-haiku-4-5"
    assert calls[-1][2] == "claude-haiku-4-5"

    chat = alice.get(f"/api/conversations/{chat_id}").json()
    assert chat["model"] == "claude-haiku-4-5"
    assert [t["model"] for t in chat["turns"] if t["role"] == "assistant"] == ["claude-haiku-4-5"] * 2


def test_disabled_model_falls_back_to_default(web, monkeypatch):
    module, calls = web
    alice = login(module, "alice", "password-a")
    chat_id = alice.post("/api/chat", json={"question": "hi", "model": "claude-haiku-4-5"}).json()["conversation_id"]
    monkeypatch.setattr(module.agent, "models", ["claude-opus-5-5"])
    response = alice.post("/api/chat", json={"question": "again", "conversation_id": chat_id})
    assert response.status_code == 200 and response.json()["model"] == "claude-opus-5-5"


def test_logo(web, tmp_path, monkeypatch):
    module, _ = web
    client = TestClient(module.app)
    monkeypatch.setattr(module, "BRANDING", tmp_path / "branding")
    assert client.get("/logo").status_code == 404
    (tmp_path / "branding").mkdir()
    (tmp_path / "branding" / "logo.png").write_bytes(b"\x89PNG fake")
    response = client.get("/logo")  # public: no sign-in needed
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"


def test_dark_logo(web, tmp_path, monkeypatch):
    module, _ = web
    client = TestClient(module.app)
    branding = tmp_path / "branding"
    branding.mkdir()
    monkeypatch.setattr(module, "BRANDING", branding)
    (branding / "logo.svg").write_text("<svg/>")
    assert client.get("/logo-dark").status_code == 404  # optional; pages fall back to /logo
    (branding / "logo-dark.png").write_bytes(b"\x89PNG fake")
    response = client.get("/logo-dark")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert client.get("/logo").headers["content-type"].startswith("image/svg")


def read_events(response) -> list[dict]:
    import json

    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_stream_endpoint(web):
    module, calls = web
    alice = login(module, "alice", "password-a")
    response = alice.post("/api/chat/stream", json={"question": "Issues at Jimmy's?", "model": "claude-haiku-4-5"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = read_events(response)
    assert [e["type"] for e in events] == ["status", "reset", "text", "text", "done"]
    assert "".join(e["text"] for e in events if e["type"] == "text") == "answer to Issues at Jimmy's?"
    done = events[-1]
    assert done["title"] == "Issues at Jimmy's?" and done["model"] == "claude-haiku-4-5"

    # The streamed answer is saved like any other, and follow-ups continue the chat.
    chat = alice.get(f"/api/conversations/{done['conversation_id']}").json()
    assert [t["text"] for t in chat["turns"]] == ["Issues at Jimmy's?", "answer to Issues at Jimmy's?"]
    follow = read_events(alice.post("/api/chat/stream", json={"question": "and last week?",
                                                             "conversation_id": done["conversation_id"]}))
    assert follow[-1]["conversation_id"] == done["conversation_id"]
    assert len(calls[-1][0]) == 2 and calls[-1][2] == "claude-haiku-4-5"


def test_stream_errors(web, monkeypatch):
    module, _ = web
    alice = login(module, "alice", "password-a")
    assert TestClient(module.app).post("/api/chat/stream", json={"question": "hi"}).status_code == 401
    assert alice.post("/api/chat/stream", json={"question": "  "}).status_code == 400
    assert alice.post("/api/chat/stream", json={"question": "x", "conversation_id": "nope"}).status_code == 404

    def broken(history, question, model=None):
        yield {"type": "status", "text": "Looking up the company…"}
        raise RuntimeError("ConnectWise fell over")

    monkeypatch.setattr(module.agent, "respond_stream", broken)
    events = read_events(alice.post("/api/chat/stream", json={"question": "hi"}))
    assert events[-1] == {"type": "error", "message": "ConnectWise fell over"}
    assert alice.get("/api/conversations").json() == []  # nothing half-saved


def test_api_errors_show_the_apis_message(web, monkeypatch):
    import anthropic
    import httpx2

    module, _ = web
    alice = login(module, "alice", "password-a")

    def rejected(history, question, model=None):
        request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        response = httpx2.Response(400, request=request, json={
            "type": "error", "error": {"type": "invalid_request_error",
                                       "message": "messages.5.content.0: Invalid `signature` in `thinking` block."}})
        raise anthropic.BadRequestError("bad request", response=response, body=response.json())
        yield  # pragma: no cover

    monkeypatch.setattr(module.agent, "respond_stream", rejected)
    events = read_events(alice.post("/api/chat/stream", json={"question": "hi"}))
    assert events[-1]["type"] == "error"
    assert events[-1]["message"].startswith("AI service error (400): messages.5.content.0: Invalid `signature`")


def test_usage_visible_to_admins_only(web):
    module, _ = web
    alice = login(module, "alice", "password-a")  # admin
    bob = login(module, "bob", "password-b")
    assert alice.get("/api/me").json()["is_admin"] is True and bob.get("/api/me").json()["is_admin"] is False

    a = read_events(alice.post("/api/chat/stream", json={"question": "hi"}))[-1]
    b = read_events(bob.post("/api/chat/stream", json={"question": "hi"}))[-1]
    assert a["usage"]["input_tokens"] == 12000 and b["usage"] is None

    a_turns = alice.get(f"/api/conversations/{a['conversation_id']}").json()["turns"]
    b_turns = bob.get(f"/api/conversations/{b['conversation_id']}").json()["turns"]
    assert a_turns[1]["usage"]["cost_usd"] == 0.0326 and b_turns[1]["usage"] is None

    # Both answers are recorded for the usage report, whoever asked.
    from dbs_reporting import usage

    text = usage.report(days=30)
    assert "Answers: 2" in text and "Alice A" in text and "Bob B" in text and "$0.07" in text


def test_activity_log_and_history(web, tmp_path, monkeypatch):
    from dbs_reporting import activity

    module, _ = web
    alice = login(module, "alice", "password-a")
    bob = login(module, "bob", "password-b")
    read_events(alice.post("/api/chat/stream", json={"question": "Printer issues at Jimmy's?"}))
    read_events(bob.post("/api/chat/stream", json={"question": "Hours at Burger Barn?"}))

    def broken(history, question, model=None):
        raise RuntimeError("ConnectWise fell over")
        yield  # pragma: no cover

    monkeypatch.setattr(module.agent, "respond_stream", broken)
    read_events(bob.post("/api/chat/stream", json={"question": "Will this work?"}))

    log = (tmp_path / "logs" / "activity.log").read_text(encoding="utf-8")
    assert "Alice A (alice)" in log and "Printer issues at Jimmy's?" in log
    assert "answer to Printer issues at Jimmy's?" in log
    assert "Usage: 15.0k in (3.0k cached) · 800 out · 2 calls · ≈ $0.033" in log
    assert "Bob B (bob)" in log and "ERROR" in log and "ConnectWise fell over" in log

    everything = activity.history_report(days=7, user=None, search=None, full=False, limit=50)
    assert "Printer issues" in everything and "Burger Barn" in everything and "2 answer(s) shown." in everything
    only_bob = activity.history_report(days=7, user="bob", search=None, full=False, limit=50)
    assert "Burger Barn" in only_bob and "Printer issues" not in only_bob
    searched = activity.history_report(days=7, user=None, search="printer", full=False, limit=50)
    assert "Printer issues" in searched and "Burger Barn" not in searched


def test_tool_calls_are_described():
    from dbs_reporting.activity import tool_calls

    claude = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "x"},
            {"type": "tool_use", "id": "t1", "name": "get_ticket_totals", "input": {"days": 30, "group_by": "site"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    assert tool_calls(claude) == ["get_ticket_totals(days=30, group_by='site')"]


def test_charts_and_excel_export(web, monkeypatch):
    import io

    from openpyxl import load_workbook

    module, _ = web
    chart_input = {"title": "Tickets by site", "chart_type": "hbar", "labels": ["Downtown", "Airport"],
                   "series": [{"name": "Tickets", "values": [31, 18]}]}

    def with_chart(history, question, model=None):
        answer = "| Site | Tickets |\n|---|---|\n| Downtown | 31 |\n| Airport | 18 |"
        yield {"type": "text", "text": answer}
        yield {"type": "done", "answer": answer, "usage": None, "history": history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "create_chart",
                                               "input": chart_input}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                          "content": '{"chart_added": true}'}]},
            {"role": "assistant", "content": [{"type": "text", "text": answer}]},
        ]}

    monkeypatch.setattr(module.agent, "respond_stream", with_chart)
    alice = login(module, "alice", "password-a")
    done = read_events(alice.post("/api/chat/stream", json={"question": "sites?"}))[-1]
    assert [c["title"] for c in done["charts"]] == ["Tickets by site"] and done["answer_id"]

    turns = alice.get(f"/api/conversations/{done['conversation_id']}").json()["turns"]
    assert turns[1]["charts"][0]["labels"] == ["Downtown", "Airport"] and turns[1]["id"] == done["answer_id"]

    response = alice.get(f"/api/answers/{done['answer_id']}/export.xlsx")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(response.content))
    assert wb.sheetnames == ["Summary", "Table 1", "Chart 1"]

    # Only the person who asked can download it.
    bob = login(module, "bob", "password-b")
    assert bob.get(f"/api/answers/{done['answer_id']}/export.xlsx").status_code == 404
    assert module.app.url_path_for("static", path="vendor/chart.umd.min.js")
    assert bob.get("/static/vendor/chart.umd.min.js").status_code == 200
