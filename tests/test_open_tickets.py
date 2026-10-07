import json
from datetime import datetime, timedelta, timezone

import httpx
from test_tools import SETTINGS, tools_by_name

from dbs_reporting.connectwise import ConnectWiseClient
from tables import rows


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
    assert [t["id"] for t in rows(result["oldest"])] == [10, 20, 30]
    oldest = rows(result["oldest"])[0]
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
    assert len(rows(result["oldest"])) == 1 and "by_company" not in result
    assert requests[-1].url.params["conditions"] == 'closedFlag=false and company/id=42 and board/name="Help Desk"'
    assert "fields" not in requests[-1].url.params


def test_one_persons_tickets_are_the_ones_they_own_or_are_a_resource_on():
    tickets = OPEN + [{"id": 40, "summary": "Handheld not taking cards", "closedFlag": False,
                       "company": {"name": "Harbor Shack"}, "owner": {"identifier": "mdavitt", "name": "Mikey Davitt"},
                       "resources": "mdavitt, sam", "_info": {"dateEntered": ago(3)}}]
    staff = [{"identifier": "sam", "firstName": "Sam", "lastName": "Ortiz"}]
    projects = [{"id": 900, "summary": "Installation", "company": {"name": "Blue Fin"}, "project": {"name": "Blue Fin install"},
                 "status": {"name": "Scheduled"}, "resources": "sam", "_info": {"dateEntered": ago(4)}},
                {"id": 901, "summary": "Training", "resources": "samantha", "_info": {"dateEntered": ago(1)}}]
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/project/tickets"):
            queries.append(request.url.params["conditions"])
            return httpx.Response(200, json=projects)
        return httpx.Response(200, json=staff if path.endswith("/system/members") else tickets)

    tools = tools_by_name(ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler)))
    mikey = json.loads(tools["get_open_tickets"].call({"person": "Mikey"}))
    assert [t["id"] for t in rows(mikey["oldest"])] == [40] and mikey["open_count"] == 1
    # Sam owns #10 and is a resource on #40; by his full name too (resources are listed by username only)
    for asked in ("sam", "Sam Ortiz"):
        sam = json.loads(tools["get_open_tickets"].call({"person": asked}))
        assert [t["id"] for t in rows(sam["oldest"])] == [10, 40], asked
    assert rows(sam["oldest"])[1]["resources"] == "mdavitt, sam"
    # ...and the project tickets he's a resource on (not #901: "samantha" isn't "sam")
    assert [(t["id"], t["company"], t["status"]) for t in rows(sam["project_tickets"])] == [(900, "Blue Fin", "Scheduled")]
    assert sam["open_project_tickets"] == 1 and queries[-1] == 'closedFlag=false and resources like "%sam%"'
    nobody = json.loads(tools["get_open_tickets"].call({"person": "Zed"}))
    assert nobody["open_count"] == 0 and "Zed" in nobody["note"]
