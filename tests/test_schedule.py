"""What's on a staff member's schedule: meetings and 1-on-1s as well as tickets."""

import json
from datetime import datetime, timedelta, timezone

import httpx
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
