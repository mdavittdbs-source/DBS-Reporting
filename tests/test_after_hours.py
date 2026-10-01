import json
from datetime import datetime, timedelta, timezone

import httpx
from test_tools import SETTINGS, tools_by_name

from dbs_reporting.connectwise import ConnectWiseClient
from dbs_reporting.eastern import period


def et(month, day, hour, minute=0):
    """A UTC timestamp for an Eastern (daylight time, UTC-4) wall-clock time in 2026."""
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc) + timedelta(hours=4)


def test_office_hours_boundaries():
    # 09/28/2026 is a Monday, 09/29 a Tuesday.
    assert period(et(9, 28, 8, 29)) == "evening"
    assert period(et(9, 28, 8, 30)) == "business hours"
    assert period(et(9, 29, 8, 45)) == "evening"          # Tuesday opens at 9:00
    assert period(et(9, 29, 9, 0)) == "business hours"
    assert period(et(10, 2, 16, 59)) == "business hours"  # Friday
    assert period(et(10, 2, 17, 0)) == "evening"
    assert period(et(10, 3, 12, 0)) == "weekend"
    assert period(et(10, 4, 23, 0)) == "weekend"
    # Winter (EST, UTC-5): 1:45 PM UTC on Monday 01/12/2026 is 8:45 AM Eastern.
    assert period(datetime(2026, 1, 12, 13, 45, tzinfo=timezone.utc)) == "business hours"
    assert period(datetime(2026, 1, 12, 13, 15, tzinfo=timezone.utc)) == "evening"


def stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


TICKETS = [
    {"id": 1, "summary": "Printer down", "company": {"id": 1, "name": "Big Owl's"}, "source": {"name": "Phone"},
     "_info": {"dateEntered": stamp(et(9, 26, 19, 30)), "enteredBy": "sam"}},          # Saturday night
    {"id": 2, "summary": "Card reader declining", "company": {"id": 1, "name": "Big Owl's"},
     "source": {"name": "Phone"}, "_info": {"dateEntered": stamp(et(9, 29, 21, 0)), "enteredBy": "sam"}},  # Tue eve
    {"id": 3, "summary": "Menu change", "company": {"id": 2, "name": "Dock Bar"}, "source": {"name": "Email"},
     "_info": {"dateEntered": stamp(et(9, 30, 10, 0)), "enteredBy": "ana"}},           # Wed business hours
    {"id": 4, "summary": "Handheld offline", "company": {"id": 2, "name": "Dock Bar"}, "source": {"name": "Email"},
     "_info": {"dateEntered": stamp(et(9, 29, 8, 40)), "enteredBy": "ana"}},           # Tue, before 9
]


def client(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=TICKETS)

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_after_hours_report():
    requests = []
    result = json.loads(tools_by_name(client(requests))["get_after_hours"].call({"days": 30}))
    assert result["by_period"] == {"business hours": 1, "evening": 2, "weekend": 1, "after_hours": 3,
                                   "after_hours_pct": 75.0}
    assert result["by_weekday"]["Tue"] == {"business hours": 0, "evening": 2, "weekend": 0}
    assert result["evening_by_hour"] == [["8 AM", 1], ["9 PM", 1]] and result["weekend_by_hour"] == [["7 PM", 1]]
    assert [w["week"] for w in result["by_week_starting"]] == ["09/21/2026", "09/28/2026"]
    assert result["groups"][0] == {"name": "Big Owl's", "business hours": 0, "evening": 1, "weekend": 1,
                                   "after_hours": 2, "after_hours_pct": 100.0}
    assert [t["id"] for t in result["recent_after_hours"]] == [2, 4, 1]
    assert result["recent_after_hours"][0]["entered"] == "09/29/2026 9:00 PM ET"
    assert "_info/enteredBy" in requests[0].url.params["fields"]


def test_phone_calls_only_by_who_took_them():
    tools = tools_by_name(client([]))
    result = json.loads(tools["get_after_hours"].call({"source": "phone", "group_by": "entered_by"}))
    assert result["ticket_count"] == 2 and result["groups"][0]["name"] == "sam"
    assert "error" in json.loads(tools["get_after_hours"].call({"group_by": "moon"}))
