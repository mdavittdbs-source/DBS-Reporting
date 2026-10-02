"""Go-lives: Installation and Live Support project tickets in the Scheduled status, and the day someone is
scheduled on them."""

import json
from datetime import datetime, timedelta, timezone

import httpx
from test_tools import SETTINGS, tools_by_name

from dbs_reporting import eastern
from dbs_reporting.connectwise import ConnectWiseClient
from tables import rows

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
SOON, LATER, PAST = NOW + timedelta(days=3), NOW + timedelta(days=60), NOW - timedelta(days=10)


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def day(dt):
    return eastern.day(eastern.to_eastern(dt).date())


TICKETS = [
    {"id": 501, "summary": "Installation", "closedFlag": False, "company": {"id": 1, "name": "Big Owl's"},
     "project": {"id": 70, "name": "Big Owl's SkyTab install"}, "status": {"name": "Scheduled"}},
    {"id": 502, "summary": "Live Support", "closedFlag": True, "company": {"id": 2, "name": "Dock Bar"},
     "project": {"id": 71, "name": "Dock Bar install"}, "status": {"name": "Completed"}},
    {"id": 503, "summary": "Live Support - go live day", "closedFlag": False, "company": {"id": 3, "name": "Taco Town"},
     "project": {"id": 72, "name": "Taco Town install"}, "status": {"name": "Scheduled"}},
    {"id": 505, "summary": "Live Support / Management Training", "closedFlag": False,
     "company": {"id": 1, "name": "Big Owl's"}, "project": {"id": 70, "name": "Big Owl's SkyTab install"},
     "status": {"name": "Scheduled"}},
    {"id": 504, "summary": "Installation", "closedFlag": False, "company": {"id": 4, "name": "Burger Barn"},
     "project": {"id": 73, "name": "Burger Barn install"}, "status": {"name": "Scheduled"}},
    # Open means nobody has picked it up yet: not a go-live, even with something on the calendar.
    {"id": 506, "summary": "Installation", "closedFlag": False, "company": {"id": 5, "name": "Pasta Place"},
     "project": {"id": 74, "name": "Pasta Place install"}, "status": {"name": "Open"}},
    # Scheduled, but not an installation or live support ticket.
    {"id": 507, "summary": "Menu build", "closedFlag": False, "company": {"id": 1, "name": "Big Owl's"},
     "project": {"id": 70, "name": "Big Owl's SkyTab install"}, "status": {"name": "Scheduled"},
     "phase": {"name": "Deployment"}},
]
ENTRIES = [
    {"objectId": 505, "type": {"identifier": "S"}, "member": {"name": "Kim"}, "dateStart": stamp(NOW + timedelta(days=1))},
    {"objectId": 501, "type": {"identifier": "S"}, "member": {"name": "Sam"}, "dateStart": stamp(SOON)},
    {"objectId": 501, "type": {"identifier": "S"}, "member": {"name": "Ana"}, "dateStart": stamp(SOON)},
    {"objectId": 502, "type": {"identifier": "S"}, "member": {"name": "Sam"}, "dateStart": stamp(PAST)},
    {"objectId": 503, "type": {"identifier": "S"}, "member": {"name": "Ana"}, "dateStart": stamp(LATER)},
    {"objectId": 504, "type": {"identifier": "C", "name": "Activity"}, "member": {"name": "Joe"},
     "dateStart": stamp(SOON)},  # a CRM activity that shares the id: not a schedule on the ticket
    {"objectId": 506, "type": {"identifier": "S"}, "member": {"name": "Joe"}, "dateStart": stamp(SOON)},
    {"objectId": 507, "type": {"identifier": "S"}, "member": {"name": "Joe"}, "dateStart": stamp(SOON)},
]
SUPPORT = [
    {"id": 900, "summary": "Printer not printing", "company": {"id": 2},
     "_info": {"dateEntered": stamp(PAST + timedelta(days=2))}},
    {"id": 901, "summary": "Menu change", "company": {"id": 2}, "_info": {"dateEntered": stamp(PAST + timedelta(days=5))}},
    {"id": 902, "summary": "Before go-live", "company": {"id": 2}, "_info": {"dateEntered": stamp(PAST - timedelta(days=1))}},
    {"id": 903, "summary": "Other client", "company": {"id": 1}, "_info": {"dateEntered": stamp(PAST + timedelta(days=1))}},
]
COMPANIES = [{"id": i, "name": n, "customFields": [{"caption": "Software", "value": v}]}
             for i, n, v in ((1, "Big Owl's", "SkyTab"), (2, "Dock Bar", "SpotOn"), (3, "Taco Town", "SkyTab"))]


def client(requests, reject_status=False):
    """A fake ConnectWise that applies the name, status and closed conditions the way the server would."""
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path, conditions = request.url.path, request.url.params.get("conditions", "")
        if path.endswith("/project/tickets"):
            if reject_status and "status/name" in conditions:
                return httpx.Response(400, json={"message": "bad condition"})
            named = [t for t in TICKETS if "installation" in t["summary"].lower() or "live support" in t["summary"].lower()]
            if "status/name" in conditions:
                found = [t for t in named if (not t["closedFlag"] and t["status"]["name"] == "Scheduled")
                         or (t["closedFlag"] and "closedDate" in conditions)]
            else:  # the status is left for the client to check
                found = [t for t in named if not t["closedFlag"] or "closedDate" in conditions]
            return httpx.Response(200, json=found)
        if path.endswith("/schedule/entries"):
            ids = {int(i) for i in conditions.split("(")[1].rstrip(")").split(",")}
            return httpx.Response(200, json=[e for e in ENTRIES if e["objectId"] in ids])
        if path.endswith("/company/companies"):
            return httpx.Response(200, json=COMPANIES)
        if path.endswith("/service/tickets"):
            return httpx.Response(200, json=SUPPORT)
        return httpx.Response(404, json={})

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_upcoming_go_lives():
    requests = []
    result = json.loads(tools_by_name(client(requests))["get_go_lives"].call({"days_ahead": 14}))
    assert result["go_live_count"] == 1 and result["upcoming"] == 1
    g = rows(result["go_lives"])[0]
    assert g["company"] == "Big Owl's" and g["date"] == day(SOON) and g["installers"] == ["Ana", "Sam"]
    assert g["software"] == "SkyTab" and g["time"].endswith(" ET")
    # Kim's management training, Pasta Place's untouched (Open) installation and the menu build aren't go-lives.
    assert result["by_installer"] == [["Ana", 1], ["Sam", 1]] and result["site_count"] == 1
    # Burger Barn's installation says Scheduled, but its only calendar item is a CRM activity.
    assert [u["company"] for u in rows(result["scheduled_but_not_on_calendar"])] == ["Burger Barn"]
    conditions = next(r for r in requests if r.url.path.endswith("/project/tickets")).url.params["conditions"]
    assert 'summary like "%installation%"' in conditions and 'summary like "%live%"' in conditions
    assert 'closedFlag=false and status/name like "%Scheduled%"' in conditions


def test_past_go_lives_with_tickets_after():
    tools = tools_by_name(client([]))
    result = json.loads(tools["get_go_lives"].call({"days_ahead": 0, "days_back": 30, "followup_days": 30}))
    assert result["go_live_count"] == 1 and result["past"] == 1
    g = rows(result["go_lives"])[0]
    assert g["company"] == "Dock Bar" and g["tickets_after"] == 2  # not the one before go-live or other clients
    assert g["examples_after"] == ["#900 Printer not printing", "#901 Menu change"]
    assert result["followup"]["by_installer"] == [["Sam", 1, 2.0]]
    assert result["followup"]["by_software"] == [["SpotOn", 1, 2.0]]
    assert "scheduled_but_not_on_calendar" not in result  # only shown when looking ahead


def test_software_filter_and_status_checked_here_when_server_refuses():
    tools = tools_by_name(client([], reject_status=True))
    result = json.loads(tools["get_go_lives"].call({"days_ahead": 90, "software": "sky tab"}))
    assert [g["company"] for g in rows(result["go_lives"])] == ["Big Owl's", "Taco Town"]  # not Pasta Place (Open)
    assert result["site_count"] == 2
