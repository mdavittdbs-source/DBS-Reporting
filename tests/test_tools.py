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


def test_ollama_agent_runs_tools(monkeypatch):
    from dbs_reporting import ollama_agent
    from dbs_reporting.agent import SYSTEM_PROMPT

    replies = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "find_company", "arguments": {"name": "Joe's Pizza"}}}]},
        {"role": "assistant", "content": "<think>hmm</think>Top issue: printers."},
    ]
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"message": replies[len(sent) - 1]})

    monkeypatch.setenv("OLLAMA_MODEL", "test-model")
    agent = ollama_agent.OllamaAgent(make_client([]), SYSTEM_PROMPT)
    agent._http = httpx.Client(transport=httpx.MockTransport(handler))

    answer, history = agent.respond([], "most common issues at Joe's Pizza?")
    assert answer == "Top issue: printers."
    assert sent[0]["model"] == "test-model"
    assert sent[0]["tools"][0]["function"]["name"] == "find_company"
    tool_message = sent[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert json.loads(tool_message["content"]) == [{"id": 42, "name": "Joe's Pizza"}]
    assert json.loads(json.dumps(history)) == history


ALL_TICKETS = [
    {"id": 10, "closedFlag": False, "company": {"name": "Jimmy's Grille"}, "site": {"name": "Downtown"},
     "type": {"name": "POS"}, "board": {"name": "Help Desk"}},
    {"id": 11, "closedFlag": True, "company": {"name": "Jimmy's Grille"}, "site": {"name": "Downtown"},
     "type": {"name": "POS"}, "board": {"name": "Help Desk"}},
    {"id": 12, "closedFlag": False, "company": {"name": "Jimmy's Grille"}, "site": {"name": "Airport"},
     "type": {"name": "Printer"}, "board": {"name": "Help Desk"}},
    {"id": 13, "closedFlag": True, "company": {"name": "Burger Barn"}, "site": None,
     "type": {"name": "Email"}, "board": {"name": "Projects"}},
]


def totals_client(requests: list, reject_fields: bool = False) -> ConnectWiseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if reject_fields and "fields" in request.url.params:
            return httpx.Response(400, json={"message": "Invalid field"})
        return httpx.Response(200, json=ALL_TICKETS)

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_ticket_totals_by_company():
    requests = []
    tools = tools_by_name(totals_client(requests))
    result = json.loads(tools["get_ticket_totals"].call({"days": 30, "group_by": "company", "top": 1}))
    assert result["ticket_count"] == 4 and result["open_count"] == 2 and result["group_count"] == 2
    top = result["groups"][0]
    assert top == {"name": "Jimmy's Grille", "tickets": 3, "open": 2, "share_pct": 75.0,
                   "top_types": [["POS", 2], ["Printer", 1]]}
    assert result["other_groups"] == {"groups": 1, "tickets": 1}
    params = requests[0].url.params
    assert params["conditions"].startswith("dateEntered>=[") and "company/id" not in params["conditions"]
    assert "company/name" in params["fields"] and "site/name" in params["fields"]


def test_ticket_totals_by_site_and_board_filter():
    requests = []
    tools = tools_by_name(totals_client(requests))
    result = json.loads(tools["get_ticket_totals"].call({"group_by": "site", "board_name": "Help Desk"}))
    names = [(g["name"], g["tickets"]) for g in result["groups"]]
    assert names[0] == ("Jimmy's Grille – Downtown", 2)
    assert ("Burger Barn – (no site)", 1) in names
    assert 'board/name="Help Desk"' in requests[0].url.params["conditions"]


def test_ticket_totals_falls_back_without_fields_and_rejects_bad_group():
    requests = []
    tools = tools_by_name(totals_client(requests, reject_fields=True))
    result = json.loads(tools["get_ticket_totals"].call({"group_by": "board"}))
    assert result["groups"][0] == {"name": "Help Desk", "tickets": 3, "open": 2, "share_pct": 75.0,
                                   "top_types": [["POS", 2], ["Printer", 1]]}
    assert "fields" in requests[0].url.params and "fields" not in requests[1].url.params
    bad = json.loads(tools["get_ticket_totals"].call({"group_by": "planet"}))
    assert "group_by must be one of" in bad["error"]
