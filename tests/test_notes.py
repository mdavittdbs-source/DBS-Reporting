"""Notes: saving, privacy, David's clean-up (one request, held to a schema), Undo, and action items to To Do."""

import pytest
from test_todo import fake_claude
from test_web import login, web  # noqa: F401  (fixture)

from dbs_reporting import notes

CLEANED = {"title": "Taco Town KDS call", "body": "## Summary\nKDS rollout moves to Tuesday.\n\n## Next steps\n- Send Joe the menu PDF",
           "action_items": [{"title": "Send Joe the menu PDF", "priority": "today"}, {"title": "  ", "priority": "later"}]}


def test_clean_up_is_one_request_and_keeps_to_the_schema():
    sent = []
    data, used = notes.clean_up(fake_claude(sent, CLEANED), "claude-opus-5-5", "", "9/13 meeting",
                                "kds moved to tues. send joe menu pdf")
    assert data["title"] == "Taco Town KDS call" and data["body"].startswith("## Summary")
    assert data["action_items"] == [{"title": "Send Joe the menu PDF", "priority": "today"}]  # blank one dropped
    assert len(sent) == 1 and sent[0]["output_config"]["format"]["type"] == "json_schema"
    assert "kds moved to tues" in sent[0]["messages"][0]["content"] and used["requests"] == 1
    with pytest.raises(notes.NoteError, match="Write something"):
        notes.clean_up(fake_claude([], CLEANED), "claude-opus-5-5", "", "", "   ")


def test_notes_are_private_and_clean_up_can_be_undone(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, alice = login(module, "bob", "password-b"), login(module, "alice", "password-a")
    note = {"title": "", "label": "9/13 meeting", "body": "kds moved to tues. send joe menu pdf"}
    saved = bob.put("/api/notes/n-abc123", json=note).json()
    assert saved["label"] == "9/13 meeting" and saved["cleaned_at"] is None
    assert [n["id"] for n in bob.get("/api/notes").json()] == ["n-abc123"]
    # Someone else's note: not listed, not readable, not writable
    assert alice.get("/api/notes").json() == []
    assert alice.get("/api/notes/n-abc123").status_code == 404
    assert alice.put("/api/notes/n-abc123", json=note).status_code == 404
    assert bob.put("/api/notes/bad id", json=note).status_code == 404

    real = notes.clean_up
    monkeypatch.setattr(module.notes, "clean_up", lambda _client, *a: real(fake_claude([], CLEANED), *a))
    cleaned = bob.post("/api/notes/n-abc123/cleanup").json()
    assert cleaned["title"] == "Taco Town KDS call" and cleaned["cleaned_at"]
    assert cleaned["actions"] == [{"title": "Send Joe the menu PDF", "priority": "today"}]
    # Action items to To Do, each only once
    assert bob.post("/api/notes/n-abc123/todo", json={"picks": [0]}).json()["actions"][0]["added"] is True
    bob.post("/api/notes/n-abc123/todo", json={})
    todo = bob.get("/api/todo").json()["list"]["items"]
    assert [(i["title"], i["priority"], i["why"]) for i in todo] == [("Send Joe the menu PDF", "today", "From your note: 9/13 meeting")]
    assert alice.post("/api/notes/n-abc123/todo", json={}).status_code == 404
    # Cleaning up again keeps the very first version for Undo
    bob.post("/api/notes/n-abc123/cleanup")
    undone = bob.post("/api/notes/n-abc123/undo").json()
    assert undone["body"] == note["body"] and undone["title"] == "" and undone["actions"] == []
    assert bob.post("/api/notes/n-abc123/undo").status_code == 404  # nothing left to undo
    empty = bob.put("/api/notes/n-empty1", json={"body": " "}).json()
    assert bob.post(f"/api/notes/{empty['id']}/cleanup").status_code == 400
    assert bob.delete("/api/notes/n-abc123").status_code == 200
    assert alice.delete("/api/notes/n-empty1").status_code == 404


def test_action_items_go_to_to_do(web):  # noqa: F811
    module, _ = web
    bob = login(module, "bob", "password-b")
    assert bob.get("/api/todo").json()["list"] is None
    added = bob.post("/api/todo/add", json={"items": [{"title": "Send Joe the menu PDF", "priority": "today"}]}).json()
    assert added["items"][0]["mine"] is True
    got = bob.get("/api/todo").json()  # no list yet: one is started with just this item
    assert [(i["title"], i["priority"]) for i in got["list"]["items"]] == [("Send Joe the menu PDF", "today")]
    bob.post("/api/todo/add", json={"items": [{"title": "Book the install", "priority": "later"}]})
    assert len(bob.get("/api/todo").json()["list"]["items"]) == 2
    assert bob.post("/api/todo/add", json={"items": [{"title": "  "}]}).status_code == 422


def test_two_clicks_at_once_dont_add_or_pay_twice(web, monkeypatch):  # noqa: F811
    import threading
    import time

    module, _ = web
    bob = login(module, "bob", "password-b")
    bob.put("/api/notes/n-race01", json={"body": "kds moved to tues. send joe menu pdf"})
    calls = []
    real = notes.clean_up

    def slow(_client, *args):
        calls.append(1)
        time.sleep(0.3)
        return real(fake_claude([], CLEANED), *args)

    monkeypatch.setattr(module.notes, "clean_up", slow)
    answers = []
    clicks = [threading.Thread(target=lambda: answers.append(bob.post("/api/notes/n-race01/cleanup").json()))
              for _ in range(2)]
    for t in clicks:
        t.start()
    for t in clicks:
        t.join()
    assert len(calls) == 1 and [a["title"] for a in answers] == ["Taco Town KDS call"] * 2  # the second waits for the first

    adds = [threading.Thread(target=lambda: bob.post("/api/notes/n-race01/todo", json={"picks": [0, 0]}))
            for _ in range(4)]
    for t in adds:
        t.start()
    for t in adds:
        t.join()
    assert [i["title"] for i in bob.get("/api/todo").json()["list"]["items"]] == ["Send Joe the menu PDF"]
