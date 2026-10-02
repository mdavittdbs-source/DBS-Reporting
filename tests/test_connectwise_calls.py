"""ConnectWise calls: busy answers are retried, and independent lookups run at the same time."""

import json
import threading
import time

import httpx
import pytest
from test_tools import SETTINGS, TICKETS, tools_by_name

from dbs_reporting import connectwise
from dbs_reporting.connectwise import ConnectWiseClient


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(connectwise, "RETRY_WAIT", 0)


def client(handler) -> ConnectWiseClient:
    return ConnectWiseClient(SETTINGS, transport=httpx.MockTransport(handler))


def test_busy_and_gateway_errors_are_retried():
    answers = iter([httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(503),
                    httpx.Response(200, json=[{"id": 1}])])
    calls = []

    def handler(request):
        calls.append(request)
        return next(answers)

    assert client(handler).get("/service/tickets") == [{"id": 1}]
    assert len(calls) == 3


def test_retries_give_up_and_other_errors_are_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if "busy" in request.url.path else 400)

    with pytest.raises(httpx.HTTPStatusError):
        client(handler).get("/busy")
    assert len(calls) == 1 + connectwise.RETRIES
    calls.clear()
    with pytest.raises(httpx.HTTPStatusError):
        client(handler).get("/bad")
    assert len(calls) == 1  # a bad request won't get better by asking again


def test_dropped_connection_is_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json={"ok": True})

    assert client(handler).get("/system/info") == {"ok": True}


def test_ticket_and_its_notes_are_fetched_together():
    in_flight, most = [0], [0]
    lock = threading.Lock()

    def handler(request):
        with lock:
            in_flight[0] += 1
            most[0] = max(most[0], in_flight[0])
        time.sleep(0.2)
        with lock:
            in_flight[0] -= 1
        if request.url.path.endswith("/notes"):
            return httpx.Response(200, json=[{"text": "Cleared jam"}])
        return httpx.Response(200, json=TICKETS[1])

    result = json.loads(tools_by_name(client(handler))["get_ticket_details"].call({"ticket_id": 2}))
    assert result["summary"] == "Printer jammed" and result["notes"][0]["text"] == "Cleared jam"
    assert most[0] == 2
