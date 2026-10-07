"""Technician workload: scheduled hours against office hours, time off, open tickets and time logged."""

import json
from datetime import datetime, timedelta, timezone

import httpx
from tables import rows
from test_tools import SETTINGS, tools_by_name

from dbs_reporting import eastern
from dbs_reporting.connectwise import ConnectWiseClient

TODAY = eastern.now().date()
MONDAY = TODAY - timedelta(days=TODAY.weekday()) + timedelta(days=7)  # next week, Monday to Friday
START = (MONDAY - TODAY).days


def at(day, hour, minute=0):
    """UTC timestamp for an Eastern time `day` days after next Monday."""
    d = MONDAY + timedelta(days=day)
    zone = eastern.to_eastern(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc)).tzinfo
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=zone).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SAM, ANA = {"identifier": "sortiz", "name": "Sam Ortiz"}, {"identifier": "aruiz", "name": "Ana Ruiz"}
ENTRIES = [
    {"member": SAM, "name": "Jimmy's Grille / printer", "type": {"name": "Service"}, "dateStart": at(0, 9),
     "dateEnd": at(0, 12), "hoursScheduled": 3},
    {"member": SAM, "name": "Internal Training", "type": {"name": "Internal Training"}, "dateStart": at(1, 8, 30),
     "dateEnd": at(2, 17), "hoursScheduled": 16},  # Tuesday to Wednesday
    {"member": SAM, "name": "Out", "type": {"name": "Vacation"}, "dateStart": at(4, 8, 30), "dateEnd": at(4, 17),
     "hoursScheduled": 8.5},
    {"member": ANA, "name": "Top Callers Meeting", "type": {"name": "Meeting"}, "dateStart": at(3, 14),
     "dateEnd": at(3, 16), "hoursScheduled": 2},
    # Friday before to this Monday: half of it falls in the week.
    {"member": ANA, "name": "Blue Fin install", "type": {"name": "Project"}, "dateStart": at(-3, 8),
     "dateEnd": at(0, 13), "hoursScheduled": 10},
]
NOW = datetime.now(timezone.utc)
OPEN = [
    {"id": 1, "owner": {"identifier": "sortiz", "name": "Sam Ortiz"}, "resources": "sortiz, kchen",
     "_info": {"dateEntered": (NOW - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")}},
    {"id": 2, "owner": {"identifier": "sortiz", "name": "Sam Ortiz"},
     "_info": {"dateEntered": (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}},
]
LOGGED = [{"member": SAM, "actualHours": 3.5}, {"member": SAM, "actualHours": 1.5},
          {"member": {"identifier": "kchen", "name": "Kim Chen"}, "actualHours": 2}]


def client(requests, staff=None):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/system/members") and staff is not None:
            return httpx.Response(200, json=staff)
        if path.endswith("/schedule/entries"):
            return httpx.Response(200, json=ENTRIES)
        if path.endswith("/service/tickets"):
            return httpx.Response(200, json=OPEN)
        if path.endswith("/time/entries"):
            return httpx.Response(200, json=LOGGED)
        return httpx.Response(404, json={})

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_workload_for_a_week():
    requests = []
    result = json.loads(tools_by_name(client(requests))["get_workload"].call({"start_day": START, "days": 7}))
    assert result["office_hours_in_period"] == 42.0  # 8.5 h Mon, Wed-Fri; 8 h Tue
    people = {p["username"]: p for p in rows(result["workload"])}
    sam, ana, kim = people["sortiz"], people["aruiz"], people["kchen"]
    # Sam: 3 h Monday + both days of the training; Friday's vacation is time off, not work.
    assert (sam["scheduled_hours"], sam["time_off_hours"], sam["available_hours"]) == (19.0, 8.5, 33.5)
    assert sam["booked_pct"] == 57 and sam["open_owned"] == 2 and sam["oldest_open_days"] == 100
    assert sam["logged_hours"] == 5.0
    assert ana["scheduled_hours"] == 7.0 and ana["booked_pct"] == 17  # 2 h meeting + Monday's half of the install
    assert kim["name"] == "Kim Chen" and kim["open_as_resource"] == 1 and kim.get("open_owned") == 0
    assert [p["username"] for p in rows(result["workload"])] == ["sortiz", "aruiz", "kchen"]  # most booked first
    conditions = next(r for r in requests if r.url.path.endswith("/schedule/entries")).url.params["conditions"]
    assert "dateStart<" in conditions and "dateEnd>" in conditions


def test_workload_for_one_person():
    tools = tools_by_name(client([]))
    result = json.loads(tools["get_workload"].call({"start_day": START, "person": "ana", "logged_days": 0}))
    assert [p["name"] for p in rows(result["workload"])] == ["Ana Ruiz"]
    assert "logged_hours" not in result["workload"]["columns"]
    assert "error" in json.loads(tools["get_workload"].call({"person": "nobody"}))


def test_everyone_on_staff_counts_even_with_nothing_booked():
    staff = [{"identifier": "sortiz", "firstName": "Sam", "lastName": "Ortiz"},
             {"identifier": "lpark", "firstName": "Lee", "lastName": "Park"},  # empty week: the most room
             {"identifier": "apiuser", "firstName": "API", "licenseClass": "A"}]
    result = json.loads(tools_by_name(client([], staff))["get_workload"].call({"start_day": START}))
    people = {p["username"]: p for p in rows(result["workload"])}
    assert people["lpark"]["name"] == "Lee Park" and people["lpark"]["booked_pct"] == 0
    assert people["lpark"]["available_hours"] == 42.0 and "apiuser" not in people
