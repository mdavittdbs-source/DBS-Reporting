import json
from datetime import datetime, timedelta, timezone

import httpx
from test_tools import SETTINGS, tools_by_name

from dbs_reporting.connectwise import ConnectWiseClient, summary_matches
from tables import rows


def ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


SERVICE = [
    {"id": 104900, "summary": "Handhelds won't sync", "closedFlag": False, "company": {"name": "Dock Bar"},
     "_info": {"dateEntered": ago(3)}},
    {"id": 104780, "summary": "Handheld not syncing orders", "closedFlag": True, "closedDate": ago(100),
     "company": {"name": "Big Owl's"}, "_info": {"dateEntered": ago(110)}},
]
PROJECT = [
    {"id": 5001, "summary": "Replace hand held scanners", "company": {"name": "Taco Town"},
     "project": {"id": 7, "name": "POS refresh"}, "_info": {"dateEntered": ago(20)}},
]


def client(requests, project_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/project/tickets"):
            return httpx.Response(project_status, json=PROJECT if project_status == 200 else {"message": "no"})
        return httpx.Response(200, json=SERVICE)

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_summary_condition():
    assert summary_matches([["handheld"], ["hand", "held"]]) == (
        '(summary like "%handheld%" or (summary like "%hand%" and summary like "%held%"))')


def test_search_across_all_clients_with_project_tickets():
    requests = []
    result = json.loads(tools_by_name(client(requests))["search_tickets"].call({"text": "handheld, hand held"}))
    assert result["match_count"] == 3 and result["project_ticket_matches"] == 1
    assert [t["id"] for t in rows(result["tickets"])] == [104900, 5001, 104780]  # newest first
    assert {c for c, _ in result["by_company"]} == {"Dock Bar", "Big Owl's", "Taco Town"}
    service = requests[0].url.params["conditions"]
    assert "company/id" not in service and "dateEntered" not in service  # every client, any time


def test_search_one_client_recent_and_project_failure_is_reported():
    requests = []
    tools = tools_by_name(client(requests, project_status=403))
    result = json.loads(tools["search_tickets"].call({"text": "handheld", "days": 30, "company_id": 9}))
    assert "dateEntered>=" in requests[0].url.params["conditions"]
    assert requests[0].url.params["conditions"].endswith("company/id=9")
    assert result["match_count"] == 2 and "403" in result["project_ticket_error"]


def test_search_needs_words():
    assert "error" in json.loads(tools_by_name(client([]))["search_tickets"].call({"text": " , "}))
