"""What's on a staff member's schedule: meetings and 1-on-1s as well as tickets."""

import json
import sys
from datetime import datetime, timedelta, timezone

import httpx
from tables import rows
from test_tools import SETTINGS, tools_by_name

from dbs_reporting import eastern
from dbs_reporting.connectwise import ConnectWiseClient

TODAY = eastern.now().date()


def at(days, hour, minute=0):
    """UTC timestamp for an Eastern wall-clock time `days` from today."""
    d = TODAY + timedelta(days=days)
    zone = eastern.to_eastern(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)).tzinfo
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=zone).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


VANESSA = {"id": 5, "identifier": "vduprey", "firstName": "Vanessa", "lastName": "Duprey", "title": "Installation Manager"}
ENTRIES = [
    {"id": 1, "name": "1-on-1: Chris", "type": {"identifier": "C", "name": "1-On-1"}, "dateStart": at(0, 9),
     "dateEnd": at(0, 9, 30), "hoursScheduled": 0.5, "where": {"name": "Remote"}},
    {"id": 2, "name": "Top Callers Meeting", "type": {"identifier": "M", "name": "Meeting"}, "dateStart": at(0, 14),
     "dateEnd": at(0, 15), "hoursScheduled": 1, "where": {"name": "In Office"}},
    {"id": 3, "objectId": 105102, "name": "Blue Fin Sushi / Go live", "type": {"identifier": "S", "name": "Service"},
     "dateStart": at(1, 8), "dateEnd": at(1, 12), "hoursScheduled": 4},
]


def client(requests, members=(VANESSA,)):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/system/members"):
            return httpx.Response(200, json=list(members))
        if path.endswith("/schedule/entries"):
            return httpx.Response(200, json=ENTRIES)
        if path.endswith("/service/tickets"):
            return httpx.Response(200, json=[])
        if path.endswith("/project/tickets"):
            return httpx.Response(200, json=[{"id": 105102, "summary": "Go live", "company": {"name": "Blue Fin Sushi"},
                                              "project": {"name": "Blue Fin SkyTab install"}}])
        return httpx.Response(404, json={})

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_whole_schedule_not_just_tickets():
    requests = []
    result = json.loads(tools_by_name(client(requests))["get_schedule"].call({"person": "Vanessa", "days": 2}))
    assert result["person"] == "Vanessa Duprey" and result["entry_count"] == 3 and result["hours_scheduled"] == 5.5
    today, tomorrow = result["days"]
    assert [e["title"] for e in today["entries"]] == ["1-on-1: Chris", "Top Callers Meeting"]
    assert today["entries"][0]["time"] == "9:00 AM–9:30 AM" and today["entries"][0]["where"] == "Remote"
    go_live = tomorrow["entries"][0]
    assert go_live["ticket"] == "#105102" and go_live["client"] == "Blue Fin Sushi"
    assert go_live["project"] == "Blue Fin SkyTab install"  # not a service ticket, so found among project tickets
    query = next(r for r in requests if r.url.path.endswith("/schedule/entries")).url.params["conditions"]
    assert 'member/identifier="vduprey"' in query and "dateStart<" in query and "dateEnd>" in query


def test_unknown_or_ambiguous_person():
    assert "error" in json.loads(tools_by_name(client([], members=()))["get_schedule"].call({"person": "Nobody"}))
    two = (VANESSA, {**VANESSA, "identifier": "vsmith", "lastName": "Smith"})
    result = json.loads(tools_by_name(client([], members=two))["get_schedule"].call({"person": "Van"}))
    assert len(result["matches"]) == 2
    one = json.loads(tools_by_name(client([], members=two))["get_schedule"].call({"person": "Vanessa Duprey"}))
    assert one["person"] == "Vanessa Duprey"


def test_blank_first_or_last_name():
    two = ({**VANESSA, "lastName": None}, {**VANESSA, "identifier": "vsmith", "firstName": None, "lastName": "Smith"})
    result = json.loads(tools_by_name(client([], members=two))["get_schedule"].call({"person": "V"}))
    assert [m["name"] for m in result["matches"]] == ["Vanessa", "Smith"]  # never "Vanessa None"


def test_entry_running_over_several_days_shows_on_each(monkeypatch):
    # Anthony's training: one entry from Tuesday 8:30 AM to Wednesday 5:00 PM, which ConnectWise draws on both
    # days. It used to show on Tuesday only, so Wednesday looked open.
    monday = TODAY - timedelta(days=TODAY.weekday()) + timedelta(days=7)
    day = (monday - TODAY).days
    training = {"id": 9, "name": "Internal Training / Oversee: Server image setup", "type": {"identifier": "C",
                "name": "Internal Training"}, "dateStart": at(day + 1, 8, 30), "dateEnd": at(day + 2, 17),
                "hoursScheduled": 17}
    over_weekend = {"id": 10, "name": "On call", "type": {"name": "On Call"}, "dateStart": at(day - 3, 9),
                    "dateEnd": at(day, 17), "hoursScheduled": 16}  # Friday to Monday, started before the week
    monkeypatch.setattr(sys.modules[__name__], "ENTRIES", [over_weekend, training])
    result = json.loads(tools_by_name(client([]))["get_schedule"].call({"person": "Vanessa", "start_day": day,
                                                                          "days": 7}))
    days = {d["day"][:3]: [e["title"] for e in d["entries"]] for d in result["days"]}
    assert days == {"Mon": ["On call"], "Tue": ["Internal Training / Oversee: Server image setup"],
                    "Wed": ["Internal Training / Oversee: Server image setup"]}
    tuesday = result["days"][1]["entries"][0]
    assert tuesday["time"] == "8:30 AM–5:00 PM" and tuesday["spans"].startswith("Tue") and "to Wed" in tuesday["spans"]
    assert result["entry_count"] == 2


STAFF = [VANESSA, {"id": 6, "identifier": "sortiz", "firstName": "Sam", "lastName": "Ortiz", "title": "Technician"},
         {"id": 7, "identifier": "kchen", "firstName": "Kim", "lastName": "Chen", "title": "Technician"},
         {"id": 8, "identifier": "kmoss", "firstName": "Kim", "lastName": "Moss", "title": "Technician"}]
TEAM_ENTRIES = [
    {"id": 20, "member": {"identifier": "sortiz", "name": "Sam Ortiz"}, "name": "Taco Town / printer",
     "type": {"identifier": "S", "name": "Service"}, "objectId": 105102, "dateStart": at(1, 9), "dateEnd": at(1, 11)},
    {"id": 21, "member": {"identifier": "vduprey", "name": "Vanessa Duprey"}, "name": "Top Callers Meeting",
     "type": {"identifier": "M", "name": "Meeting"}, "dateStart": at(0, 14), "dateEnd": at(0, 15), "hoursScheduled": 1},
    {"id": 22, "member": {"identifier": "aruiz", "name": "Ana Ruiz"}, "name": "Vacation",
     "type": {"name": "Vacation"}, "dateStart": at(0, 8, 30), "dateEnd": at(1, 17), "hoursScheduled": 16.5},
]


def team_client(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path, conditions = request.url.path, request.url.params.get("conditions", "")
        if path.endswith("/system/members"):
            words = [w.split("%")[1] for w in conditions.split("like ")[1:]]
            return httpx.Response(200, json=[m for m in STAFF if all(
                any(w.lower() in (m[f] or "").lower() for f in ("firstName", "lastName", "identifier")) for w in words)])
        if path.endswith("/schedule/entries"):
            wanted = [i.strip('"') for i in conditions.split("in (")[1].rstrip(")").split(",")] if " in (" in conditions else None
            return httpx.Response(200, json=[e for e in TEAM_ENTRIES if wanted is None or e["member"]["identifier"] in wanted])
        if path.endswith("/service/tickets"):
            return httpx.Response(200, json=[{"id": 105102, "summary": "Printer down", "company": {"name": "Taco Town"}}])
        if path.endswith("/project/tickets"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_several_people_at_once():
    requests = []
    tool = tools_by_name(team_client(requests))["get_schedule"]
    result = json.loads(tool.call({"person": "Sam, Vanessa, Kim Chen, Kim, Nobody", "days": 2}))
    entries = rows(result["schedule"])
    assert [(e["person"], e["title"]) for e in entries] == [("Sam Ortiz", "Taco Town / printer"),
                                                           ("Vanessa Duprey", "Top Callers Meeting")]
    assert entries[0]["ticket"] == "#105102" and entries[0]["client"] == "Taco Town"
    assert result["nothing_scheduled"] == ["Kim Chen"]
    assert result["not_found"] == ["Nobody"] and len(result["unclear"]["Kim"]) == 2  # Kim Chen or Kim Moss?
    query = next(r for r in requests if r.url.path.endswith("/schedule/entries")).url.params["conditions"]
    assert 'member/identifier in ("sortiz","vduprey","kchen")' in query  # one query for all of them


def test_everyones_calendar():
    requests = []
    result = json.loads(tools_by_name(team_client(requests))["get_schedule"].call({"person": "everyone", "days": 2}))
    entries = rows(result["schedule"])
    assert [e["person"] for e in entries] == ["Ana Ruiz", "Ana Ruiz", "Sam Ortiz", "Vanessa Duprey"]
    assert entries[0]["spans"].startswith(f"{TODAY:%a}")  # Ana's two-day vacation shows on both days
    assert result["people_with_entries"] == 3 and ["Ana Ruiz", 16.5] in result["hours_by_person"]
    assert not any(r.url.path.endswith("/system/members") for r in requests)  # no name lookups needed
    blank = json.loads(tools_by_name(team_client([]))["get_schedule"].call({"days": 1}))
    assert blank["who"] == "everyone with something scheduled"
