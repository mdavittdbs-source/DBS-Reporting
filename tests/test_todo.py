"""To Do tab: matching a login to ConnectWise, gathering the person's work, and one ranked list."""

import json
from datetime import datetime, timedelta, timezone

import anthropic
import httpx
import httpx2
import pytest
from tables import rows
from test_tools import SETTINGS
from test_web import login, web  # noqa: F401  (fixture)

from dbs_reporting import eastern, todo
from dbs_reporting.connectwise import ConnectWiseClient

NOW = datetime.now(timezone.utc)
TODAY = eastern.now().date()
stamp = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
STAFF = [{"identifier": "sortiz", "firstName": "Sam", "lastName": "Ortiz"},
         {"identifier": "sowens", "firstName": "Sam", "lastName": "Owens"},
         {"identifier": "mdavitt", "firstName": "Michael", "lastName": "Davitt"}]
SERVICE = [
    {"id": 1, "summary": "Kitchen printer offline", "company": {"name": "Taco Town"}, "status": {"name": "New"},
     "priority": {"name": "Priority 1 - Critical"}, "owner": {"identifier": "sortiz"},
     "_info": {"dateEntered": stamp(NOW - timedelta(days=2)), "lastUpdated": stamp(NOW - timedelta(days=1))}},
    {"id": 2, "summary": "Handheld won't sync", "company": {"name": "Dock Bar"}, "status": {"name": "Waiting"},
     "owner": {"identifier": "kchen"}, "resources": "kchen, sortiz",
     "_info": {"dateEntered": stamp(NOW - timedelta(days=20)), "lastUpdated": stamp(NOW - timedelta(days=9))}},
    {"id": 3, "summary": "Someone else's", "owner": {"identifier": "kchen"}, "resources": "sortizjr",
     "_info": {"dateEntered": stamp(NOW)}},  # "like %sortiz%" matches it, but it isn't Sam's
]
PROJECT = [{"id": 900, "summary": "Installation", "company": {"name": "Blue Fin"}, "project": {"name": "Blue Fin install"},
            "status": {"name": "Scheduled"}, "resources": "sortiz", "_info": {"dateEntered": stamp(NOW)}}]


def at(day, hour, minute=0):
    d = TODAY + timedelta(days=day)
    zone = eastern.to_eastern(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)).tzinfo
    return stamp(datetime(d.year, d.month, d.day, hour, minute, tzinfo=zone).astimezone(timezone.utc))


SCHEDULE = [{"objectId": 900, "type": {"identifier": "P", "name": "Project"}, "name": "Blue Fin / Installation",
             "dateStart": at(1, 8, 30), "dateEnd": at(2, 17)},
            {"name": "Team meeting", "type": {"name": "Meeting"}, "dateStart": at(0, 14), "dateEnd": at(0, 15)}]


def cw(requests=None):
    def handler(request: httpx.Request) -> httpx.Response:
        (requests if requests is not None else []).append(request)
        path, conditions = request.url.path, request.url.params.get("conditions", "")
        if path.endswith("/system/members"):
            words = [w.split("%")[1].lower() for w in conditions.split("like ")[1:]]
            return httpx.Response(200, json=[m for m in STAFF if all(
                any(w in m[f].lower() for f in ("firstName", "lastName", "identifier")) for w in words)])
        if path.endswith("/service/tickets"):
            return httpx.Response(200, json=SERVICE)
        if path.endswith("/project/tickets"):
            return httpx.Response(200, json=PROJECT)
        if path.endswith("/schedule/entries"):
            return httpx.Response(200, json=SCHEDULE)
        return httpx.Response(404, json={})

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_matching_a_login_to_connectwise():
    client = cw()
    assert todo.find_member(client, {"display_name": "Sam Ortiz"})["identifier"] == "sortiz"
    assert todo.find_member(client, {"display_name": "Sam", "cw_member": "sowens"})["identifier"] == "sowens"
    assert todo.find_member(client, {"display_name": "Mikey Davitt"})["identifier"] == "mdavitt"  # by last name
    with pytest.raises(todo.TodoError, match="users.txt"):
        todo.find_member(client, {"display_name": "Sam"})  # two Sams
    with pytest.raises(todo.TodoError, match="spelling"):
        todo.find_member(client, {"display_name": "Sam", "cw_member": "nobody"})


def test_gathering_someones_work():
    requests = []
    work = todo.gather_work(cw(requests), STAFF[0])
    tickets = rows(work["tickets"])
    assert [t["id"] for t in tickets] == [2, 1, 900]  # service first, oldest first; not #3
    assert tickets[1]["role"] == "owner" and tickets[0]["role"] == "resource" and tickets[0]["days_since_update"] == 9
    calendar = rows(work["calendar_next_7_days"])
    assert [c["title"] for c in calendar] == ["Team meeting", "Blue Fin / Installation", "Blue Fin / Installation"]
    assert calendar[1]["ticket"] == 900 and work["open_service_tickets"] == 2
    service_query = next(r for r in requests if r.url.path.endswith("/service/tickets")).url.params["conditions"]
    assert 'owner/identifier="sortiz" or resources like "%sortiz%"' in service_query


def fake_claude(sent, reply: dict, stop="end_turn"):
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": stop,
            "stop_sequence": None, "content": [{"type": "text", "text": json.dumps(reply)}],
            "usage": {"input_tokens": 900, "output_tokens": 300}})
    return anthropic.Anthropic(api_key="x", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


REPLY = {"summary": "2 open tickets and an install Tuesday.", "items": [
    {"title": "Prep for Blue Fin install", "why": "Scheduled Tuesday.", "priority": "this_week", "ticket": 900,
     "client": "Blue Fin", "when": "Tue 8:30 AM"},
    {"title": "Fix Taco Town's kitchen printer", "why": "Priority 1, owned by you.", "priority": "now", "ticket": 1,
     "client": "Taco Town", "when": None},
    {"title": "Follow up with Dock Bar", "why": "No update in 9 days.", "priority": "today", "ticket": 2,
     "client": "Dock Bar", "when": None}]}


def test_one_ranked_list_from_one_request():
    sent = []
    data = todo.make(cw(), fake_claude(sent, REPLY), "claude-opus-5-5", {"display_name": "Sam Ortiz"})
    assert [i["priority"] for i in data["items"]] == ["now", "today", "this_week"]  # grouped in order
    assert data["member"] == "Sam Ortiz" and data["counts"] == {"service": 2, "project": 1}
    assert data["usage"]["input_tokens"] == 900 and len(sent) == 1
    body = sent[0]
    assert body["output_config"]["format"]["type"] == "json_schema" and body["output_config"]["effort"] == "low"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}  # same instructions for everyone: cached
    assert "Kitchen printer offline" in body["messages"][0]["content"]
    with pytest.raises(todo.TodoError):
        todo.make(cw(), fake_claude([], REPLY, stop="refusal"), "claude-opus-5-5", {"display_name": "Sam Ortiz"})


def test_nothing_open_needs_no_request():
    sent = []
    empty = todo.rank(fake_claude(sent, REPLY), "claude-opus-5-5", {"tickets": {"columns": [], "rows": []}})
    assert empty[0]["items"] == [] and not sent


def test_todo_endpoints(web, monkeypatch):  # noqa: F811
    module, _ = web
    made = []

    def fake_make(cw_client, client, model, user):
        made.append(user["username"])
        return {"member": "Bob B", "username": "bbee", **REPLY, "counts": {"service": 2, "project": 1},
                "usage": {"requests": 1, "input_tokens": 900, "output_tokens": 300, "cache_read_tokens": 0,
                          "cache_write_tokens": 0, "cost_usd": 0.01, "priced": True}}

    monkeypatch.setattr(module.todo, "make", fake_make)
    bob, alice = login(module, "bob", "password-b"), login(module, "alice", "password-a")
    assert bob.get("/api/todo").json() == {"list": None}
    made_list = bob.post("/api/todo").json()
    assert made_list["list"]["items"][0]["title"] == "Prep for Blue Fin install" and made_list["done"] == []
    assert "usage" not in made_list  # admins only
    assert bob.post("/api/todo/done", json={"item": 1, "done": True}).status_code == 200
    assert bob.post("/api/todo/done", json={"item": 9, "done": True}).status_code == 404
    assert bob.get("/api/todo").json()["done"] == [1]
    assert alice.get("/api/todo").json() == {"list": None}  # each person has their own
    assert "usage" in alice.post("/api/todo").json()
    assert bob.post("/api/todo").json()["done"] == []  # a fresh list starts unticked
    assert made == ["bob", "alice", "bob"]
    assert bob.get("/todo", follow_redirects=False).headers["location"] == "/#todo"

    def not_linked(*args):
        raise module.todo.TodoError("Ask your admin to add your ConnectWise username")

    monkeypatch.setattr(module.todo, "make", not_linked)
    failed = bob.post("/api/todo")
    assert failed.status_code == 400 and "ConnectWise username" in failed.json()["detail"]
    assert bob.get("/api/todo").json()["list"]["items"]  # the last good list stays
