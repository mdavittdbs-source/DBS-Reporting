import json
from datetime import datetime, timedelta, timezone

import httpx
from test_tools import SETTINGS, tools_by_name

from dbs_reporting.connectwise import ConnectWiseClient


def ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


OPEN = [
    {"id": 30, "summary": "New laptop", "closedFlag": False, "company": {"name": "Jimmy's Grille"},
     "board": {"name": "Help Desk"}, "status": {"name": "New"}, "_info": {"dateEntered": ago(2), "lastUpdated": ago(1)}},
    {"id": 10, "summary": "Printer keeps jamming", "closedFlag": False, "company": {"name": "Jimmy's Grille"},
     "board": {"name": "Help Desk"}, "status": {"name": "Waiting on client"}, "owner": {"identifier": "sam"},
     "_info": {"dateEntered": ago(730), "lastUpdated": ago(200)}},
    {"id": 20, "summary": "VPN drops", "closedFlag": False, "company": {"name": "Burger Barn"},
     "board": {"name": "Network"}, "status": {"name": "In Progress"}, "_info": {"dateEntered": ago(45)}},
]


def open_client(requests: list, reject_fields: bool = False) -> ConnectWiseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if reject_fields and "fields" in request.url.params:
            return httpx.Response(400, json={"message": "bad fields"})
        return httpx.Response(200, json=OPEN)

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_oldest_open_tickets_regardless_of_age():
    requests = []
    tools = tools_by_name(open_client(requests))
    result = json.loads(tools["get_open_tickets"].call({}))
    assert result["open_count"] == 3
    # The two-year-old ticket comes first: a date-range lookup would have missed it.
    assert [t["id"] for t in result["oldest"]] == [10, 20, 30]
    oldest = result["oldest"][0]
    assert oldest["age_days"] == 730 and oldest["days_since_update"] == 200 and oldest["owner"] == "sam"
    assert result["age_buckets"] == {"0-7 days": 1, "8-30 days": 0, "31-90 days": 1, "91-365 days": 0,
                                     "over 1 year": 1}
    assert result["median_age_days"] == 45
    assert result["by_company"][0] == ["Jimmy's Grille", 2]
    params = requests[0].url.params
    assert params["conditions"] == "closedFlag=false" and "dateEntered" not in params["conditions"]
    assert "_info/dateEntered" in params["fields"]


def test_open_tickets_for_one_client_and_board_with_field_fallback():
    requests = []
    tools = tools_by_name(open_client(requests, reject_fields=True))
    result = json.loads(tools["get_open_tickets"].call({"company_id": 42, "board_name": "Help Desk", "oldest": 1}))
    assert len(result["oldest"]) == 1 and "by_company" not in result
    assert requests[-1].url.params["conditions"] == 'closedFlag=false and company/id=42 and board/name="Help Desk"'
    assert "fields" not in requests[-1].url.params
