"""The client's POS software, from the "Software" custom field on its company record."""

import json

import httpx
from test_tools import SETTINGS, tools_by_name

from dbs_reporting.connectwise import ConnectWiseClient


def field(value, caption="Software"):
    return {"id": 9, "caption": caption, "type": "Text", "value": value}


COMPANIES = [
    {"id": 1, "name": "Big Owl's", "customFields": [field("SkyTab")]},
    {"id": 2, "name": "Dock Bar", "customFields": [field("Shift4 Dine"), field("x", "Region")]},
    {"id": 3, "name": "Taco Town", "customFields": [field("Sky Tab")]},
    {"id": 4, "name": "New Client", "customFields": [field(None)]},
]
TICKETS = [
    {"id": 10, "summary": "Handheld won't sync", "closedFlag": False, "company": {"id": 1, "name": "Big Owl's"}},
    {"id": 11, "summary": "Printer offline", "closedFlag": False, "company": {"id": 2, "name": "Dock Bar"}},
    {"id": 12, "summary": "Menu change", "closedFlag": True, "company": {"id": 3, "name": "Taco Town"}},
    {"id": 13, "summary": "New terminal", "closedFlag": False, "company": {"id": 4, "name": "New Client"}},
]


def client(requests, companies=COMPANIES, fail_companies=False):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/company/companies"):
            return httpx.Response(403, json={}) if fail_companies else httpx.Response(200, json=companies)
        if request.url.path.endswith("/project/tickets"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=TICKETS)

    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def company_calls(requests):
    return [r for r in requests if r.url.path.endswith("/company/companies")]


def test_clients_by_software_and_spelling_variants():
    requests = []
    tools = tools_by_name(client(requests))
    result = json.loads(tools["get_clients_by_software"].call({"software": "skytab"}))
    assert result["clients"] == 4 and result["with_software_set"] == 3
    assert [m["name"] for m in result["matches"]] == ["Big Owl's", "Taco Town"]  # "Sky Tab" counts too
    assert company_calls(requests)[0].url.params["fields"] == "id,name,customFields"
    # The company list is reused, not fetched again for each question.
    tools["get_clients_by_software"].call({})
    assert len(company_calls(requests)) == 1


def test_ticket_totals_by_and_for_software():
    tools = tools_by_name(client([]))
    by = json.loads(tools["get_ticket_totals"].call({"group_by": "software"}))
    # "SkyTab" and "Sky Tab" are one group.
    assert {g["name"]: g["tickets"] for g in by["groups"]} == {"SkyTab": 2, "Shift4 Dine": 1, "(software not set)": 1}
    only = json.loads(tools["get_ticket_totals"].call({"software": "SkyTab", "group_by": "company"}))
    assert only["ticket_count"] == 2 and only["software"] == "SkyTab"


def test_open_tickets_and_search_filter_by_software():
    tools = tools_by_name(client([]))
    opened = json.loads(tools["get_open_tickets"].call({"software": "shift4"}))
    assert [t["id"] for t in opened["oldest"]] == [11]
    everyone = json.loads(tools["get_open_tickets"].call({}))
    assert dict(map(tuple, everyone["by_software"]))["(software not set)"] == 1
    found = json.loads(tools["search_tickets"].call({"text": "handheld", "software": "Sky Tab"}))
    assert [t["id"] for t in found["tickets"]] == [10, 12]  # the fake returns every ticket; filter keeps SkyTab


def test_missing_field_is_explained_and_optional_breakdown_skipped():
    companies = [{"id": 1, "name": "A", "customFields": [field("North", "Region")]}]
    tools = tools_by_name(client([], companies=companies))
    error = json.loads(tools["get_clients_by_software"].call({}))["error"]
    assert '"Software"' in error and '"Region"' in error
    # Open tickets still work when the software can't be read; only the breakdown is left out.
    tools = tools_by_name(client([], fail_companies=True))
    result = json.loads(tools["get_open_tickets"].call({}))
    assert result["open_count"] == 4 and "by_software" not in result
