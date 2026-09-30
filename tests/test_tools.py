import json

import httpx

from dbs_reporting.config import ConnectWiseSettings
from dbs_reporting.connectwise import ConnectWiseClient
from dbs_reporting.tools import build_tools

SETTINGS = ConnectWiseSettings(
    site="cw.example.com", company_id="acme", public_key="pub", private_key="priv", client_id="cid"
)

TICKETS = [
    {"id": 3, "summary": "Printer offline", "closedFlag": False, "board": {"name": "Help Desk"},
     "type": {"name": "Hardware"}, "_info": {"dateEntered": "2026-09-20T10:00:00Z"}},
    {"id": 2, "summary": "Printer jammed", "closedFlag": True, "closedDate": "2026-09-11T10:00:00Z",
     "board": {"name": "Help Desk"}, "type": {"name": "Hardware"},
     "_info": {"dateEntered": "2026-09-10T10:00:00Z"}},
    {"id": 1, "summary": "Password reset", "closedFlag": True, "closedDate": "2026-09-02T10:00:00Z",
     "board": {"name": "Help Desk"}, "_info": {"dateEntered": "2026-09-02T09:00:00Z"}},
]


def make_client(requests: list) -> ConnectWiseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/company/companies"):
            return httpx.Response(200, json=[{"id": 42, "name": "Joe's Pizza"}])
        if path.endswith("/service/tickets"):
            return httpx.Response(200, json=TICKETS)
        if path.endswith("/service/tickets/2"):
            return httpx.Response(200, json=TICKETS[1])
        if path.endswith("/service/tickets/2/notes"):
            return httpx.Response(200, json=[{"text": "Cleared jam", "resolutionFlag": True}])
        if path.endswith("/time/entries"):
            return httpx.Response(200, json=[
                {"actualHours": 1.5, "member": {"name": "Sam"}, "workType": {"name": "Remote"},
                 "chargeToType": "ServiceTicket", "chargeToId": 2},
                {"actualHours": 0.5, "member": {"name": "Sam"}, "workType": {"name": "Remote"},
                 "chargeToType": "ServiceTicket", "chargeToId": 3},
            ])
        return httpx.Response(404, text="not found")

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def tools_by_name(cw):
    return {t.name: t for t in build_tools(cw)}


def test_auth_headers_and_company_search():
    requests = []
    tools = tools_by_name(make_client(requests))
    result = json.loads(tools["find_company"].call({"name": "Joe's Pizza"}))
    assert result == [{"id": 42, "name": "Joe's Pizza"}]
    req = requests[0]
    assert req.url.host == "cw.example.com"
    assert req.url.path == "/v4_6_release/apis/3.0/company/companies"
    assert req.headers["clientId"] == "cid"
    assert req.headers["Authorization"].startswith("Basic ")
    assert 'name like "%Joe\'s Pizza%"' in req.url.params["conditions"]


def test_ticket_breakdown():
    requests = []
    tools = tools_by_name(make_client(requests))
    result = json.loads(tools["get_company_tickets"].call({"company_id": 42, "days": 30}))
    assert result["ticket_count"] == 3
    assert result["open_count"] == 1
    assert result["by_type"][0] == ["Hardware", 2]
    assert result["tickets"][0]["entered"] == "2026-09-20T10:00:00Z"
    conditions = requests[0].url.params["conditions"]
    assert conditions.startswith("company/id=42 and dateEntered>=[")


def test_ticket_details_and_time():
    tools = tools_by_name(make_client([]))
    details = json.loads(tools["get_ticket_details"].call({"ticket_id": 2}))
    assert details["notes"][0]["kind"] == "resolution"
    time = json.loads(tools["get_company_time"].call({"company_id": 42}))
    assert time["total_hours"] == 2.0
    assert time["by_member"] == [["Sam", 2.0]]


def test_errors_are_returned_to_claude():
    tools = tools_by_name(make_client([]))
    result = json.loads(tools["get_ticket_details"].call({"ticket_id": 999}))
    assert "HTTP 404" in result["error"]


def test_tool_schemas():
    for tool in build_tools(make_client([])):
        schema = tool.to_dict()
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"
