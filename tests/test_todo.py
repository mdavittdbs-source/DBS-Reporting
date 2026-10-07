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

    def fake_make(cw_client, client, model, user, dismissed=None):
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
    ids = [item["id"] for item in made_list["list"]["items"]]
    assert len(set(ids)) == 3
    assert bob.post("/api/todo/done", json={"item": ids[1], "done": True}).status_code == 200
    assert bob.post("/api/todo/done", json={"item": "nope", "done": True}).status_code == 404
    assert bob.get("/api/todo").json()["done"] == [ids[1]]
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


def _list(module, monkeypatch, user="bob", password="password-b"):
    monkeypatch.setattr(module.todo, "make", lambda *a: {"member": "Bob B", "username": "bbee", **REPLY,
                                                          "counts": {"service": 2, "project": 1}, "usage": None})
    client = login(module, user, password)
    return client, client.post("/api/todo").json()["list"]["items"]


def test_arranging_the_list(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, items = _list(module, monkeypatch)
    blue_fin, taco, dock = items  # REPLY order: this_week, now, today
    bob.post("/api/todo/done", json={"item": dock["id"], "done": True})
    bob.post("/api/todo/done", json={"item": taco["id"], "done": True})
    mine = {"id": "m-call-joe", "priority": "now", "mine": True, "title": "  Call Joe back  ", "ticket": 4821}
    arranged = [mine, {**blue_fin, "priority": "today", "title": "Renamed by hand", "why": "edited"}, dock]
    saved = bob.put("/api/todo/items", json={"items": arranged, "remove": [taco["id"]]})
    assert saved.status_code == 200
    got = bob.get("/api/todo").json()
    titles = [(i["title"], i["priority"]) for i in got["list"]["items"]]
    # Order as arranged, Taco Town removed, David's item moved and edited (his other fields kept), own item trimmed
    assert titles == [("Call Joe back", "now"), ("Renamed by hand", "today"), ("Follow up with Dock Bar", "today")]
    edited = got["list"]["items"][1]
    assert edited["why"] == "edited" and edited["client"] == blue_fin["client"] and edited["ticket"] == blue_fin["ticket"]
    assert got["list"]["items"][0] == {"id": "m-call-joe", "title": "Call Joe back", "why": "", "priority": "now",
                                       "ticket": 4821, "client": None, "when": None, "mine": True}
    assert got["done"] == [dock["id"]]  # the removed item's tick went with it
    # Undo after the removal saved: Taco Town comes back with David's wording, even if the client sends less
    restored = bob.put("/api/todo/items", json={"items": [{"id": taco["id"], "priority": "now"}, mine, blue_fin, dock]})
    assert restored.json()["items"][0]["title"] == "Fix Taco Town's kitchen printer"
    # Edit your own item
    bob.put("/api/todo/items", json={"items": [{**mine, "title": "Call Joe at 3"}, blue_fin, dock]})
    assert bob.get("/api/todo").json()["list"]["items"][0]["title"] == "Call Joe at 3"


def test_arranging_rejects_bad_input(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, items = _list(module, monkeypatch)
    put = lambda items: bob.put("/api/todo/items", json={"items": items}).status_code  # noqa: E731
    assert put([{"id": "m1", "priority": "now", "mine": True, "title": "   "}]) == 422  # needs a title
    assert put([{"id": "m1", "priority": "soon", "mine": True, "title": "x"}]) == 422
    assert put([{"id": "bad id!", "priority": "now", "mine": True, "title": "x"}]) == 422
    assert put([{"id": "m1", "priority": "now", "mine": True, "title": "x" * 201}]) == 422
    assert put([{"id": f"m{n}", "priority": "now", "mine": True, "title": "x"} for n in range(101)]) == 422
    # An item that's neither David's nor marked as yours is dropped, not invented; ones not sent stay
    assert put([{"id": "made-up", "priority": "now", "title": "Sneaky"}, items[0]]) == 200
    assert [i["id"] for i in bob.get("/api/todo").json()["list"]["items"]] == [i["id"] for i in items]
    assert bob.put("/api/todo/items", json={"items": [], "remove": ["y" * 41]}).status_code == 200
    alice = login(module, "alice", "password-a")
    assert alice.put("/api/todo/items", json={"items": []}).status_code == 404  # no list yet


def test_your_own_items_carry_over_to_a_new_list(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, items = _list(module, monkeypatch)
    keep = {"id": "m-keep", "priority": "later", "mine": True, "title": "Order more paper"}
    finished = {"id": "m-done", "priority": "now", "mine": True, "title": "Already did it"}
    bob.put("/api/todo/items", json={"items": [*items, keep, finished]})
    bob.post("/api/todo/done", json={"item": "m-done", "done": True})
    fresh = bob.post("/api/todo").json()
    titles = [i["title"] for i in fresh["list"]["items"]]
    assert titles[-1] == "Order more paper" and "Already did it" not in titles
    assert len(titles) == 4 and fresh["done"] == []
    assert not {i["id"] for i in items} & {i["id"] for i in fresh["list"]["items"]}  # David's new items, new ids


def test_lists_saved_before_ids_still_work(tmp_path):
    from dbs_reporting.store import Store
    store = Store(tmp_path / "old.db")
    user_id = store.add_user("sam", "a-long-password", "Sam O")
    with store._db() as db:
        db.execute("INSERT INTO todo_lists (user_id, data, done, created_at) VALUES (?, ?, ?, ?)",
                   (user_id, json.dumps(REPLY), json.dumps([2]), "2026-10-05T12:00:00+00:00"))
    saved = store.get_todo(user_id)
    assert [i["id"] for i in saved["data"]["items"]] == ["d0", "d1", "d2"] and saved["done"] == ["d2"]
    assert store.set_todo_done(user_id, "d0", True)
    assert store.get_todo(user_id)["done"] == ["d2", "d0"]


def test_leaving_out_what_you_removed():
    # Ticket 2 was removed before it last changed: it comes back. Ticket 1 and project 900 were removed after:
    # they stay off, and so does 900's calendar entry. The meeting has no ticket and isn't affected.
    dismissed = [{"ticket": 1, "title": "Fix the printer", "at": stamp(NOW)},
                 {"ticket": 2, "title": "Dock Bar", "at": stamp(NOW - timedelta(days=12))},
                 {"ticket": 900, "title": "Blue Fin install", "at": stamp(NOW)}]
    work = todo.gather_work(cw(), STAFF[0], dismissed=dismissed)
    assert [t["id"] for t in rows(work["tickets"])] == [2]
    assert [c["title"] for c in rows(work["calendar_next_7_days"])] == ["Team meeting"]
    assert work["left_out"] == 2 and work["open_service_tickets"] == 2 and work["open_project_tickets"] == 1


def test_a_removed_item_without_a_ticket_stays_off():
    reply = {"summary": "Busy week.", "items": [
        {"title": "Team meeting", "why": "Today 2 PM.", "priority": "today", "ticket": None, "client": None, "when": "2 PM"},
        {"title": "Fix Taco Town's kitchen printer", "why": "P1.", "priority": "now", "ticket": 1, "client": "Taco Town",
         "when": None}]}
    data = todo.make(cw(), fake_claude([], reply), "claude-opus-5-5", {"display_name": "Sam Ortiz"},
                     [{"ticket": None, "title": "team meeting ", "at": stamp(NOW)}])
    assert [i["title"] for i in data["items"]] == ["Fix Taco Town's kitchen printer"]


def _honest_make(seen):
    """Like todo.make: leaves out the tickets it's told were removed."""
    def make(cw_, client, model, user, dismissed=None):
        seen.append(dismissed)
        gone = {d["ticket"] for d in dismissed or []}
        items = [dict(i) for i in REPLY["items"] if i["ticket"] not in gone]
        return {"member": "Bob B", "username": "bbee", "summary": REPLY["summary"], "items": items, "counts": {},
                "left_out": len(gone), "usage": None}
    return make


def test_removals_follow_you_to_the_next_list(web, monkeypatch):  # noqa: F811
    module, _ = web
    seen = []
    monkeypatch.setattr(module.todo, "make", _honest_make(seen))
    bob = login(module, "bob", "password-b")
    items = bob.post("/api/todo").json()["list"]["items"]
    assert seen == [[]]
    bob.put("/api/todo/items", json={"items": [i for i in items if i["ticket"] != 1],
                                     "remove": [i["id"] for i in items if i["ticket"] == 1]})  # remove Taco Town (#1)
    assert "removed" not in bob.get("/api/todo").json()["list"]  # bookkeeping isn't mixed into the list
    bob.post("/api/todo")
    assert [(d["ticket"], d["title"]) for d in seen[1]] == [(1, "Fix Taco Town's kitchen printer")]
    bob.post("/api/todo")  # still remembered a list later
    assert [d["ticket"] for d in seen[2]] == [1]
    # Older than 30 days: forgotten
    with module.store._db() as db:
        data = json.loads(db.execute("SELECT data FROM todo_lists").fetchone()[0])
        data["removed"][0]["removed_at"] = (NOW - timedelta(days=31)).isoformat(timespec="seconds")
        db.execute("UPDATE todo_lists SET data = ?", (json.dumps(data),))
    bob.post("/api/todo")
    assert seen[3] == []


def test_seeing_and_putting_back_removed_items(web, monkeypatch):  # noqa: F811
    module, _ = web
    seen = []
    monkeypatch.setattr(module.todo, "make", _honest_make(seen))
    bob = login(module, "bob", "password-b")
    blue_fin, taco, dock = bob.post("/api/todo").json()["list"]["items"]
    mine = {"id": "m-paper", "priority": "later", "mine": True, "title": "Order paper", "ticket": 77}
    bob.put("/api/todo/items", json={"items": [blue_fin, taco, dock, mine]})
    bob.put("/api/todo/items", json={"items": [blue_fin, dock], "remove": [taco["id"], mine["id"]]})  # remove Taco, mine
    got = bob.get("/api/todo").json()
    removed = got["removed"]
    assert [r["title"] for r in removed] == ["Fix Taco Town's kitchen printer", "Order paper"]
    assert all(r["removed_at"] for r in removed) and removed[1]["mine"] and removed[1]["ticket"] == 77
    # Put my own item back (into Now this time); it leaves Removed, and its removal time doesn't stick to it
    back = bob.put("/api/todo/items", json={"items": [{**mine, "priority": "now"}, blue_fin, dock]}).json()["items"]
    assert back[0]["title"] == "Order paper" and back[0]["priority"] == "now" and "removed_at" not in back[0]
    assert [r["title"] for r in bob.get("/api/todo").json()["removed"]] == ["Fix Taco Town's kitchen printer"]
    # A new list: Taco Town is left out and still listed under Removed, ready to put back
    fresh = bob.post("/api/todo").json()
    assert "Fix Taco Town's kitchen printer" not in [i["title"] for i in fresh["list"]["items"]]
    assert [r["ticket"] for r in fresh["removed"]] == [1]
    put_back = bob.put("/api/todo/items", json={"items": [*fresh["list"]["items"], {"id": fresh["removed"][0]["id"],
                                                                                    "priority": "now"}]})
    assert put_back.json()["items"][-1]["title"] == "Fix Taco Town's kitchen printer"  # David's wording
    assert bob.get("/api/todo").json()["removed"] == []
    bob.post("/api/todo")
    assert seen[-1] == []  # put back, so no longer left out


def test_a_ticket_back_on_a_new_list_leaves_removed(web, monkeypatch):  # noqa: F811
    module, _ = web
    monkeypatch.setattr(module.todo, "make", lambda *a: {"member": "Bob B", **REPLY, "usage": None})  # ignores removals,
    bob = login(module, "bob", "password-b")                                                        # as if #1 changed
    items = bob.post("/api/todo").json()["list"]["items"]
    bob.put("/api/todo/items", json={"items": [i for i in items if i["ticket"] != 1],
                                     "remove": [i["id"] for i in items if i["ticket"] == 1]})
    assert len(bob.get("/api/todo").json()["removed"]) == 1
    assert bob.post("/api/todo").json()["removed"] == []  # #1 is on the new list again, so it isn't "removed"


def test_removals_saved_the_older_way_still_show(tmp_path):
    from dbs_reporting.store import Store
    store = Store(tmp_path / "old.db")
    user_id = store.add_user("sam", "a-long-password", "Sam O")
    data = {**REPLY, "dismissed": [{"ticket": 5, "title": "Swap the card reader", "at": NOW.isoformat(timespec="seconds")}]}
    with store._db() as db:
        db.execute("INSERT INTO todo_lists (user_id, data, done, created_at) VALUES (?, ?, '[]', ?)",
                   (user_id, json.dumps(data), NOW.isoformat(timespec="seconds")))
    removed = store.get_todo(user_id)["removed"]
    assert [(r["ticket"], r["title"]) for r in removed] == [(5, "Swap the card reader")]
    assert [d["ticket"] for d in store.todo_dismissed(user_id)] == [5]


def test_deleting_from_removed(web, monkeypatch):  # noqa: F811
    module, _ = web
    seen = []
    monkeypatch.setattr(module.todo, "make", _honest_make(seen))
    bob = login(module, "bob", "password-b")
    blue_fin, taco, dock = bob.post("/api/todo").json()["list"]["items"]
    mine = {"id": "m-paper", "priority": "later", "mine": True, "title": "Order paper"}
    bob.put("/api/todo/items", json={"items": [blue_fin, taco, dock, mine]})
    bob.put("/api/todo/items", json={"items": [blue_fin], "remove": [taco["id"], dock["id"], mine["id"]]})
    assert len(bob.get("/api/todo").json()["removed"]) == 3
    left = bob.post("/api/todo/removed/delete", json={"ids": [taco["id"]]}).json()["removed"]
    assert sorted(r["title"] for r in left) == ["Follow up with Dock Bar", "Order paper"]
    assert len(bob.get("/api/todo").json()["removed"]) == 2
    # Undo brings it back to Removed
    back = bob.post("/api/todo/removed/delete", json={"ids": [taco["id"]], "undo": True}).json()["removed"]
    assert taco["title"] in [r["title"] for r in back]
    # Delete all
    assert bob.post("/api/todo/removed/delete", json={}).json()["removed"] == []
    assert bob.get("/api/todo").json()["removed"] == []
    # Deleted tickets still stay off the next list, and still aren't listed under Removed
    fresh = bob.post("/api/todo").json()
    assert sorted(d["ticket"] for d in seen[-1]) == [1, 2]
    assert [i["title"] for i in fresh["list"]["items"]] == ["Prep for Blue Fin install"]
    assert fresh["removed"] == []
    assert bob.post("/api/todo/removed/delete", json={"ids": ["x" * 41]}).status_code == 200  # unknown id: no-op
    assert login(module, "alice", "password-a").post("/api/todo/removed/delete", json={}).status_code == 404


def test_a_page_that_missed_an_item_doesnt_wipe_it(web, monkeypatch):  # noqa: F811
    # One tab (or a page reloaded mid-save) adds an item; another, still showing the older list, then saves a
    # reorder. The item it never saw must survive; only an explicit "remove" takes an item off.
    module, _ = web
    bob, items = _list(module, monkeypatch)
    new = {"id": "m-new", "priority": "today", "mine": True, "title": "Added in the other tab"}
    bob.put("/api/todo/items", json={"items": [*items, new]})
    stale = list(reversed(items))  # the other page's view: no "m-new"
    saved = bob.put("/api/todo/items", json={"items": stale}).json()["items"]
    assert [i["id"] for i in saved] == [i["id"] for i in stale] + ["m-new"]
    assert bob.get("/api/todo").json()["removed"] == []
    bob.put("/api/todo/items", json={"items": stale, "remove": ["m-new"]})
    assert [r["id"] for r in bob.get("/api/todo").json()["removed"]] == ["m-new"]


def test_a_tick_and_a_save_at_the_same_moment_both_stick(tmp_path):
    # Both read the list, then both write it. Without the write lock taken before the read, the second
    # write undid the first and ticks were lost (every run of this lost some).
    import threading
    from dbs_reporting.store import Store
    store = Store(tmp_path / "race.db")
    user_id = store.add_user("sam", "a-long-password", "Sam O")
    store.save_todo(user_id, {"items": [{"title": f"Item {n}", "priority": "today", "ticket": n + 1}
                                        for n in range(10)]}, "m", None)
    items = store.get_todo(user_id)["data"]["items"]
    arranged = [{"id": i["id"], "priority": i["priority"]} for i in items]
    threads = []
    for item in items:
        threads.append(threading.Thread(target=store.set_todo_done, args=(user_id, item["id"], True)))
        threads.append(threading.Thread(target=store.set_todo_items, args=(user_id, arranged)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(store.get_todo(user_id)["done"]) == sorted(i["id"] for i in items)


def test_older_removed_entries_keep_the_same_id(tmp_path):
    # Entries saved in the earlier format got a new random id every time they were read, so the first
    # Put back or Delete after upgrading couldn't find them.
    from dbs_reporting.store import Store
    store = Store(tmp_path / "old.db")
    user_id = store.add_user("sam", "a-long-password", "Sam O")
    data = {**REPLY, "dismissed": [{"ticket": 5, "title": "Swap the card reader", "at": NOW.isoformat(timespec="seconds")}]}
    with store._db() as db:
        db.execute("INSERT INTO todo_lists (user_id, data, done, created_at) VALUES (?, ?, '[]', ?)",
                   (user_id, json.dumps(data), NOW.isoformat(timespec="seconds")))
    first = store.get_todo(user_id)["removed"][0]["id"]
    assert store.get_todo(user_id)["removed"][0]["id"] == first
    assert store.delete_removed(user_id, [first]) == []


def test_a_nine_digit_ticket_number_is_accepted(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, items = _list(module, monkeypatch)
    mine = {"id": "m-big", "priority": "now", "mine": True, "title": "Big ticket", "ticket": 123456789}
    assert bob.put("/api/todo/items", json={"items": [*items, mine]}).status_code == 200


def test_reminders(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, items = _list(module, monkeypatch)
    blue_fin, taco, dock = items
    mine = {"id": "m-call-joe", "priority": "now", "mine": True, "title": "Call Joe"}
    bob.put("/api/todo/items", json={"items": [*items, mine]})
    soon = (NOW + timedelta(hours=2)).isoformat()
    past = (NOW - timedelta(minutes=1)).isoformat()
    remind = lambda item, at: bob.put("/api/todo/reminder", json={"item": item, "at": at})  # noqa: E731
    assert remind("m-call-joe", past).status_code == 200
    assert remind(taco["id"], soon).json()["item"]["remind_at"] == (NOW + timedelta(hours=2)).isoformat(timespec="seconds")
    assert remind(dock["id"], past).status_code == 200
    bob.post("/api/todo/done", json={"item": dock["id"], "done": True})  # done: no reminder
    # Saving the arranged list (which doesn't send reminders) keeps them
    bob.put("/api/todo/items", json={"items": [{**mine, "title": "Call Joe at 3"}, blue_fin, taco, dock]})
    got = bob.post("/api/todo/reminders").json()
    assert [i["title"] for i in got["due"]] == ["Call Joe at 3"]
    assert got["next"] == (NOW + timedelta(hours=2)).isoformat(timespec="seconds")
    assert bob.post("/api/todo/reminders").json()["due"] == []  # each goes off once
    assert bob.get("/api/todo").json()["list"]["items"][0]["reminded"] is True
    # Snoozing sets it again; clearing takes it off
    remind("m-call-joe", past)
    assert len(bob.post("/api/todo/reminders").json()["due"]) == 1
    remind(taco["id"], None)
    assert "remind_at" not in next(i for i in bob.get("/api/todo").json()["list"]["items"] if i["id"] == taco["id"])
    # A reminder on David's item follows its ticket to the next list
    remind(blue_fin["id"], soon)
    fresh = {i["ticket"]: i for i in bob.post("/api/todo").json()["list"]["items"] if i.get("ticket")}
    assert fresh[blue_fin["ticket"]]["remind_at"] and fresh[blue_fin["ticket"]]["id"] != blue_fin["id"]
    assert "remind_at" not in fresh[taco["ticket"]]
    # Bad input
    assert remind(taco["id"], "2026-10-06T09:00:00").status_code == 422  # needs a time zone
    assert remind(taco["id"], (NOW + timedelta(days=400)).isoformat()).status_code == 422
    assert remind("nope", soon).status_code == 404
    alice = login(module, "alice", "password-a")
    got = alice.post("/api/todo/reminders").json()
    assert got["due"] == [] and got["next"] is None and got["now"]


def test_own_sections_can_be_added_renamed_and_taken_out(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, items = _list(module, monkeypatch)
    assert [s["name"] for s in bob.get("/api/todo").json()["list"]["sections"]] == ["Now", "Today", "This week", "Later"]
    sections = [{"key": "now", "name": "Urgent"}, {"key": "today", "name": "Today"},
                {"key": "s-calls1", "name": "  Calls   to make "}, {"key": "later", "name": "Someday"}]  # This week left out
    saved = bob.put("/api/todo/sections", json={"sections": sections}).json()["sections"]
    # David's four can't be renamed or taken out ("This week" comes back); your own name is tidied
    assert [(s["key"], s["name"]) for s in saved] == [("now", "Now"), ("today", "Today"), ("this_week", "This week"),
                                                       ("s-calls1", "Calls to make"), ("later", "Later")]
    # Items go into your own section, and stay there
    mine = {"id": "m-call", "priority": "s-calls1", "mine": True, "title": "Call Joe"}
    moved = {**items[1], "priority": "s-calls1"}
    bob.put("/api/todo/items", json={"items": [mine, moved, items[0], items[2]]})
    got = bob.get("/api/todo").json()["list"]
    assert [(i["title"], i["priority"]) for i in got["items"][:2]] == [("Call Joe", "s-calls1"), (items[1]["title"], "s-calls1")]
    # A section that doesn't exist puts the item in Later; a bad key is refused
    bob.put("/api/todo/items", json={"items": [{**mine, "priority": "s-nowhere"}]})
    assert bob.get("/api/todo").json()["list"]["items"][0]["priority"] == "later"
    assert bob.put("/api/todo/items", json={"items": [{**mine, "priority": "bogus"}]}).status_code == 422
    # Your sections and names last across a new list
    fresh = bob.post("/api/todo").json()["list"]
    assert [s["name"] for s in fresh["sections"]] == ["Now", "Today", "This week", "Calls to make", "Later"]
    # Taking your own section out moves what's in it to Later
    bob.put("/api/todo/items", json={"items": [{**mine, "priority": "s-calls1"}]})
    bob.put("/api/todo/sections", json={"sections": [s for s in saved if s["key"] != "s-calls1"]})
    got = bob.get("/api/todo").json()["list"]
    assert "s-calls1" not in [s["key"] for s in got["sections"]]
    assert next(i for i in got["items"] if i["id"] == "m-call")["priority"] == "later"
    assert login(module, "alice", "password-a").put("/api/todo/sections", json={"sections": sections}).status_code == 404


def test_sections_can_be_put_in_any_order(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, _ = _list(module, monkeypatch)
    order = [{"key": "s-calls1", "name": "Calls"}, {"key": "later", "name": "Later"}, {"key": "now", "name": "Now"},
             {"key": "this_week", "name": "This week"}, {"key": "today", "name": "Today"}]
    saved = bob.put("/api/todo/sections", json={"sections": order}).json()["sections"]
    assert [s["key"] for s in saved] == ["s-calls1", "later", "now", "this_week", "today"]  # David's and yours, any order
    fresh = bob.post("/api/todo").json()["list"]  # and it lasts across a new list
    assert [s["key"] for s in fresh["sections"]] == ["s-calls1", "later", "now", "this_week", "today"]
