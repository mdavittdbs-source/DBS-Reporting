"""End-to-end tests of logins and per-user chats, with ConnectWise and Claude mocked out."""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web(monkeypatch, tmp_path):
    for name in ("CW_SITE", "CW_COMPANY_ID", "CW_PUBLIC_KEY", "CW_PRIVATE_KEY", "CW_CLIENT_ID"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("CLAUDE_MODELS", "claude-opus-5-5,claude-haiku-4-5")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("USERS_FILE", str(tmp_path / "users.txt"))
    (tmp_path / "users.txt").write_text(
        "alice | Alice A | password-a |\nbob | Bob B | password-b |\n", encoding="utf-8"
    )
    from dbs_reporting import web as module

    module = importlib.reload(module)
    calls = []

    def fake_respond(history, question, model=None):
        calls.append((list(history), question, model))
        return f"answer to {question}", history + [
            {"role": "user", "content": question},
            {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
        ]

    monkeypatch.setattr(module.agent, "respond", fake_respond)
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


def test_switch_model_mid_chat(web):
    module, calls = web
    alice = login(module, "alice", "password-a")
    first = alice.post("/api/chat", json={"question": "hi", "model": "claude-opus-5-5"}).json()
    assert first["model"] == "claude-opus-5-5"
    chat_id = first["conversation_id"]

    # Switching model continues the same chat with its full history.
    second = alice.post("/api/chat", json={"question": "again", "conversation_id": chat_id,
                                           "model": "claude-haiku-4-5"}).json()
    assert second["conversation_id"] == chat_id and second["model"] == "claude-haiku-4-5"
    history, _, model = calls[-1]
    assert model == "claude-haiku-4-5" and len(history) == 2

    # Without a model, the chat continues on the last one used.
    alice.post("/api/chat", json={"question": "more", "conversation_id": chat_id})
    assert calls[-1][2] == "claude-haiku-4-5"

    chat = alice.get(f"/api/conversations/{chat_id}").json()
    assert chat["model"] == "claude-haiku-4-5"
    assert [t["model"] for t in chat["turns"] if t["role"] == "assistant"] == [
        "claude-opus-5-5", "claude-haiku-4-5", "claude-haiku-4-5"]


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
