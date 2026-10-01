"""Go-lives: a project ticket whose phase is Deployment, and the day someone is scheduled on it."""

import json
from datetime import datetime, timedelta, timezone

import httpx
from test_tools import SETTINGS, tools_by_name

from dbs_reporting import eastern
from dbs_reporting.connectwise import ConnectWiseClient

NOW = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
SOON, LATER, PAST = NOW + timedelta(days=3), NOW + timedelta(days=60), NOW - timedelta(days=10)


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def day(dt):
    return eastern.day(eastern.to_eastern(dt).date())


TICKETS = [
    {"id": 501, "summary": "Go live", "closedFlag": False, "company": {"id": 1, "name": "Big Owl's"},
     "project": {"id": 70, "name": "Big Owl's SkyTab install"}, "phase": {"name": "Deployment"},
     "status": {"name": "Scheduled"}},
    {"id": 502, "summary": "Go live", "closedFlag": True, "company": {"id": 2, "name": "Dock Bar"},
     "project": {"id": 71, "name": "Dock Bar install"}, "phase": {"name": "Deployment"}},
    {"id": 503, "summary": "Go live", "closedFlag": False, "company": {"id": 3, "name": "Taco Town"},
     "project": {"id": 72, "name": "Taco Town install"}, "phase": {"name": "Deployment"}},
    {"id": 505, "summary": "Management Training", "closedFlag": False, "company": {"id": 1, "name": "Big Owl's"},
     "project": {"id": 70, "name": "Big Owl's SkyTab install"}, "phase": {"name": "Deployment"}},
    {"id": 504, "summary": "Go live", "closedFlag": False, "company": {"id": 4, "name": "Burger Barn"},
     "project": {"id": 73, "name": "Burger Barn install"}, "phase": {"name": "Deployment"}},
]
ENTRIES = [
    {"objectId": 505, "type": {"identifier": "S"}, "member": {"name": "Kim"}, "dateStart": stamp(NOW + timedelta(days=1))},
    {"objectId": 501, "type": {"identifier": "S"}, "member": {"name": "Sam"}, "dateStart": stamp(SOON)},
    {"objectId": 501, "type": {"identifier": "S"}, "member": {"name": "Ana"}, "dateStart": stamp(SOON)},
    {"objectId": 502, "type": {"identifier": "S"}, "member": {"name": "Sam"}, "dateStart": stamp(PAST)},
    {"objectId": 503, "type": {"identifier": "S"}, "member": {"name": "Ana"}, "dateStart": stamp(LATER)},
    {"objectId": 504, "type": {"identifier": "C", "name": "Activity"}, "member": {"name": "Joe"},
     "dateStart": stamp(SOON)},  # a CRM activity that shares the id: not a schedule on the ticket
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


def client(requests, reject_phase=False):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path, conditions = request.url.path, request.url.params.get("conditions", "")
        if path.endswith("/project/tickets"):
            if reject_phase and "phase/name" in conditions:
                return httpx.Response(400, json={"message": "bad condition"})
            found = [t for t in TICKETS if "closedFlag=false" not in conditions or "or closedDate" in conditions
                     or not t["closedFlag"]]
            if reject_phase:  # unfiltered: other phases come back too
                found = found + [{"id": 600, "summary": "Menu build", "closedFlag": False, "phase": {"name": "Build"},
                                  "company": {"id": 1, "name": "Big Owl's"}, "project": {"id": 70}}]
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
    g = result["go_lives"][0]
    assert g["company"] == "Big Owl's" and g["date"] == day(SOON) and g["installers"] == ["Ana", "Sam"]
    assert g["software"] == "SkyTab" and g["time"].endswith(" ET")
    assert result["by_installer"] == [["Ana", 1], ["Sam", 1]]  # Kim's management training isn't a go-live
    # Burger Barn's only "schedule" is a CRM activity, so its deployment isn't scheduled yet.
    assert [u["company"] for u in result["deployment_not_scheduled"]] == ["Burger Barn"]
    project_query = next(r for r in requests if r.url.path.endswith("/project/tickets"))
    assert 'phase/name like "%Deployment%"' in project_query.url.params["conditions"]
    assert "closedFlag=false" in project_query.url.params["conditions"]


def test_past_go_lives_with_tickets_after():
    tools = tools_by_name(client([]))
    result = json.loads(tools["get_go_lives"].call({"days_ahead": 0, "days_back": 30, "followup_days": 30}))
    assert result["go_live_count"] == 1 and result["past"] == 1
    g = result["go_lives"][0]
    assert g["company"] == "Dock Bar" and g["tickets_after"] == 2  # not the one before go-live or other clients
    assert g["examples_after"] == ["#900 Printer not printing", "#901 Menu change"]
    assert result["followup"]["by_installer"] == [["Sam", 1, 2.0]]
    assert result["followup"]["by_software"] == [["SpotOn", 1, 2.0]]
    assert "deployment_not_scheduled" not in result  # only shown when looking ahead


def test_software_filter_and_phase_filtered_here_when_server_refuses():
    tools = tools_by_name(client([], reject_phase=True))
    result = json.loads(tools["get_go_lives"].call({"days_ahead": 90, "software": "sky tab"}))
    assert [g["company"] for g in result["go_lives"]] == ["Big Owl's", "Taco Town"]
    assert all(u.get("ticket_id") != 600 for u in result.get("deployment_not_scheduled", []))
