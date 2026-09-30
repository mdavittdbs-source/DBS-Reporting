import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from test_tools import SETTINGS, tools_by_name

from dbs_reporting.connectwise import ConnectWiseClient
from dbs_reporting.tools import period_start

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("cycle, start, expected", [
    ("CalendarMonth", None, "2026-09-01"),
    ("CalendarQuarter", None, "2026-07-01"),
    ("CalendarYear", None, "2026-01-01"),
    ("CalendarWeek", None, "2026-09-28"),                 # Monday
    ("ContractYear", datetime(2024, 11, 15, tzinfo=timezone.utc), "2025-11-15"),
    ("ContractQuarter", datetime(2026, 1, 31, tzinfo=timezone.utc), "2026-07-31"),
    ("Contract4Weeks", datetime(2026, 9, 1, tzinfo=timezone.utc), "2026-09-29"),
    ("SomethingNew", None, "2026-09-01"),
])
def test_period_start(cycle, start, expected):
    begin, basis = period_start(cycle, start, NOW)
    assert begin.date().isoformat() == expected and basis


def iso(days_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")


AGREEMENTS = [
    {"id": 1, "name": "Managed Services", "company": {"name": "Jimmy's Grille"}, "type": {"name": "MSP"},
     "agreementStatus": "Active", "startDate": "2025-01-01T00:00:00Z", "endDate": iso(20), "billAmount": 1500,
     "billingCycle": {"name": "Monthly"}, "applicationUnits": "Hours", "applicationLimit": 10,
     "applicationCycle": "CalendarMonth", "applicationUnlimitedFlag": False},
    {"id": 2, "name": "Block Hours", "company": {"name": "Burger Barn"}, "type": {"name": "Block"},
     "agreementStatus": "Active", "startDate": "2025-01-01T00:00:00Z", "endDate": iso(200),
     "applicationUnits": "Amount", "applicationLimit": 5000, "applicationCycle": "ContractYear"},
    {"id": 3, "name": "Old", "company": {"name": "Burger Barn"}, "agreementStatus": "Active",
     "noEndingDateFlag": True, "endDate": iso(5)},
]


def agreement_client(requests: list) -> ConnectWiseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/finance/agreements"):
            return httpx.Response(200, json=AGREEMENTS)
        if path.endswith("/finance/agreements/1"):
            return httpx.Response(200, json=AGREEMENTS[0])
        if path.endswith("/finance/agreements/2"):
            return httpx.Response(200, json=AGREEMENTS[1])
        if path.endswith("/time/entries"):
            return httpx.Response(200, json=[
                {"hoursDeduct": 4.0, "actualHours": 5.0, "member": {"name": "Sam"},
                 "chargeToType": "ServiceTicket", "chargeToId": 101},
                {"actualHours": 3.5, "member": {"name": "Ana"}, "chargeToType": "ServiceTicket", "chargeToId": 102},
            ])
        return httpx.Response(404)

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_get_agreements_all_clients_and_renewals():
    requests = []
    tools = tools_by_name(agreement_client(requests))
    everything = json.loads(tools["get_agreements"].call({}))
    assert everything["count"] == 3
    first = everything["agreements"][0]
    assert first["company"] == "Jimmy's Grille" and first["allowance"] == {
        "units": "Hours", "limit": 10, "cycle": "CalendarMonth", "unlimited": False}
    assert requests[0].url.params["conditions"] == 'agreementStatus="Active"'
    assert everything["agreements"][2]["end"] is None  # no ending date

    renewals = json.loads(tools["get_agreements"].call({"company_id": 42, "ending_within_days": 30}))
    assert [a["id"] for a in renewals["agreements"]] == [1]
    assert requests[-1].url.params["conditions"] == 'company/id=42 and agreementStatus="Active"'


def test_agreement_usage_hours():
    requests = []
    tools = tools_by_name(agreement_client(requests))
    result = json.loads(tools["get_agreement_usage"].call({"agreement_id": 1}))
    usage = result["usage"]
    assert usage["hours_used"] == 7.5            # 4.0 deducted + 3.5 actual
    assert usage["hours_allowed"] == 10 and usage["hours_remaining"] == 2.5 and usage["percent_used"] == 75.0
    assert usage["by_member"] == [["Sam", 4.0], ["Ana", 3.5]]
    assert usage["period"] == "this calendar month"
    conditions = requests[-1].url.params["conditions"]
    assert conditions.startswith("agreement/id=1 and timeStart>=[")
    assert f"{datetime.now(timezone.utc):%Y-%m}-01T00:00:00Z" in conditions


def test_agreement_usage_amount_units():
    tools = tools_by_name(agreement_client([]))
    usage = json.loads(tools["get_agreement_usage"].call({"agreement_id": 2}))["usage"]
    assert "hours_allowed" not in usage and "Amount" in usage["note_units"]


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


def test_period_start_leap_day_contract_year():
    begin, _ = period_start("ContractYear", datetime(2024, 2, 29, tzinfo=timezone.utc), NOW)
    assert begin.date().isoformat() == "2026-02-28"
