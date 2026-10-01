import json
from datetime import datetime, timedelta, timezone

import httpx
from test_tools import SETTINGS, tools_by_name

from dbs_reporting.connectwise import ConnectWiseClient

PROJECTS = [
    {"id": 7, "name": "Office move", "company": {"id": 42, "name": "Jimmy's Grille"}, "status": {"name": "In Progress"},
     "closedFlag": False, "manager": {"identifier": "sam", "name": "Sam Smith"}, "percentComplete": 60,
     "budgetHours": 40, "actualHours": 52.5, "estimatedEnd": "2026-10-15T00:00:00Z"},
    {"id": 8, "name": "Firewall upgrade", "company": {"id": 42, "name": "Jimmy's Grille"}, "status": {"name": "New"},
     "closedFlag": False, "manager": {"name": "Ann Lee"}, "budgetHours": 10, "actualHours": 2},
]
RECENT = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
PROJECT_TICKETS = [
    {"id": 501, "summary": "Run cabling", "project": {"id": 7, "name": "Office move"}, "phase": {"name": "Build"},
     "status": {"name": "Open"}, "closedFlag": False, "budgetHours": 8, "actualHours": 10,
     "_info": {"dateEntered": RECENT}, "resources": "sam"},
    {"id": 500, "summary": "Site survey", "project": {"id": 7, "name": "Office move"}, "phase": {"name": "Plan"},
     "status": {"name": "Closed"}, "closedFlag": True, "closedDate": "2025-01-05T10:00:00Z", "actualHours": 3,
     "_info": {"dateEntered": "2025-01-02T10:00:00Z"}},
]


def project_client(requests: list) -> ConnectWiseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/project/projects"):
            return httpx.Response(200, json=PROJECTS)
        if path.endswith("/project/tickets"):
            return httpx.Response(200, json=PROJECT_TICKETS)
        if path.endswith("/project/tickets/501"):
            return httpx.Response(200, json=PROJECT_TICKETS[0])
        if path.endswith("/project/tickets/501/notes"):
            return httpx.Response(200, json=[{"text": "Pulled 12 drops", "resolutionFlag": False}])
        return httpx.Response(404, text="not found")

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_projects_for_a_client():
    requests = []
    tools = tools_by_name(project_client(requests))
    result = json.loads(tools["get_projects"].call({"company_id": 42}))
    assert result["project_count"] == 2 and result["open_count"] == 2 and result["over_budget_count"] == 1
    move = result["projects"][0]
    assert move["name"] == "Office move" and move["manager"] == "Sam Smith" and move["over_budget_hours"] == 12.5
    assert result["budget_hours"] == 50 and result["actual_hours"] == 54.5
    conditions = requests[0].url.params["conditions"]
    assert requests[0].url.path.endswith("/project/projects")
    assert "company/id=42" in conditions and "closedFlag=false" in conditions


def test_project_tickets():
    requests = []
    tools = tools_by_name(project_client(requests))
    result = json.loads(tools["get_project_tickets"].call({"project_id": 7}))
    assert result["ticket_count"] == 2 and result["open_count"] == 1
    assert result["by_phase"] == [["Build", 1], ["Plan", 1]]
    assert result["tickets"][0] == {"id": 501, "summary": "Run cabling", "project": "Office move", "project_id": 7,
                                    "phase": "Build", "status": "Open", "entered": RECENT,
                                    "budget_hours": 8.0, "actual_hours": 10.0, "resources": "sam"}
    assert requests[0].url.params["conditions"] == "project/id=7"
    recent = json.loads(tools["get_project_tickets"].call({"company_id": 42, "days": 60}))
    assert [t["id"] for t in recent["tickets"]] == [501]
    assert "error" in json.loads(tools["get_project_tickets"].call({}))


def test_ticket_details_falls_back_to_project_tickets():
    tools = tools_by_name(project_client([]))
    details = json.loads(tools["get_ticket_details"].call({"ticket_id": 501}))
    assert details["kind"] == "project ticket" and details["project"] == "Office move"
    assert details["notes"][0]["text"] == "Pulled 12 drops"
