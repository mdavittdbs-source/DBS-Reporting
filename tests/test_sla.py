import json

import httpx
from test_tools import SETTINGS, tools_by_name

from dbs_reporting.connectwise import ConnectWiseClient

SLA_TICKETS = [
    {"id": 1, "company": {"name": "Jimmy's Grille"}, "board": {"name": "Help Desk"}, "isInSla": True,
     "closedFlag": True, "_info": {"dateEntered": "2026-09-01T08:00:00Z"},
     "dateResponded": "2026-09-01T09:00:00Z", "dateResolved": "2026-09-01T12:00:00Z"},
    {"id": 2, "company": {"name": "Jimmy's Grille"}, "board": {"name": "Help Desk"}, "isInSla": False,
     "closedFlag": False, "_info": {"dateEntered": "2026-09-02T08:00:00Z"},
     "dateResponded": "2026-09-02T11:00:00Z"},
    {"id": 3, "company": {"name": "Jimmy's Grille"}, "board": {"name": "Projects"}, "isInSla": False,
     "closedFlag": True, "_info": {"dateEntered": "2026-09-03T08:00:00Z"},
     "dateResponded": "2026-09-03T13:00:00Z", "closedDate": "2026-09-04T08:00:00Z"},
    {"id": 4, "company": {"name": "Burger Barn"}, "board": {"name": "Help Desk"},
     "closedFlag": True, "_info": {"dateEntered": "2026-09-05T08:00:00Z"}},
]


def sla_client(requests: list, reject_fields: bool = False) -> ConnectWiseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if reject_fields and "fields" in request.url.params:
            return httpx.Response(400, json={"message": "bad fields"})
        return httpx.Response(200, json=SLA_TICKETS)

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_sla_by_company():
    requests = []
    tools = tools_by_name(sla_client(requests))
    result = json.loads(tools["get_sla_performance"].call({"days": 30, "group_by": "company"}))
    jimmy = result["groups"][0]
    assert jimmy["name"] == "Jimmy's Grille"
    assert (jimmy["tickets"], jimmy["in_sla"], jimmy["breached"], jimmy["open"]) == (3, 1, 2, 1)
    assert jimmy["percent_in_sla"] == 33.3
    assert jimmy["first_response"] == {"count": 3, "median_hours": 3.0, "average_hours": 3.0}   # 1h, 3h, 5h
    assert jimmy["resolution"] == {"count": 2, "median_hours": 14.0, "average_hours": 14.0}      # 4h, 24h
    burger = result["groups"][1]
    assert burger["sla_unknown"] == 1 and burger["percent_in_sla"] is None
    assert result["overall"]["tickets"] == 4 and "business hours" in result["note"]
    params = requests[0].url.params
    assert "isInSla" in params["fields"] and "company/id" not in params["conditions"]


def test_sla_one_client_by_board_with_fallback():
    requests = []
    tools = tools_by_name(sla_client(requests, reject_fields=True))
    result = json.loads(tools["get_sla_performance"].call({"group_by": "board", "company_id": 42}))
    assert [g["name"] for g in result["groups"]] == ["Help Desk", "Projects"]
    assert "company/id=42" in requests[-1].url.params["conditions"] and "fields" not in requests[-1].url.params
    bad = json.loads(tools["get_sla_performance"].call({"group_by": "weather"}))
    assert "group_by must be one of" in bad["error"]

