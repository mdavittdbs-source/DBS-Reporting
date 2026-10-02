"""Tool results are kept small: every step of an answer re-reads them, so their size is most of
what a question costs."""

import json

from tables import rows
from test_tools import TICKETS, make_client, tools_by_name

from dbs_reporting.tools import _dumps, _table, clean_note_text


def test_compact_json_keeps_letters_as_they_are():
    text = _dumps({"summary": "Handheld won’t sync – patio", "n": [1, 2]})
    assert text == '{"summary":"Handheld won’t sync – patio","n":[1,2]}'
    assert _dumps({"s": "bad \ud800 char"}) == '{"s":"bad ? char"}'  # a broken character can't stop the request


def test_table_names_fields_once():
    records = [{"id": 1, "board": "Help Desk", "type": "Hardware", "item": None},
               {"id": 2, "board": "Help Desk", "type": None}]
    table = _table(records, ("id", "summary", "board", "type", "item"))
    assert table == {"columns": ["id", "type"], "rows": [[1, "Hardware"], [2, None]],
                     "every_row": {"board": "Help Desk"}}  # blank columns dropped, shared values given once
    assert rows(table) == [{"id": 1, "board": "Help Desk", "type": "Hardware"}, {"id": 2, "board": "Help Desk"}]
    assert _table([{"id": 1, "board": "Help Desk"}], ("id", "board")) == {"columns": ["id", "board"],
                                                                         "rows": [[1, "Help Desk"]]}


def test_quoted_email_threads_are_left_out_of_notes():
    reply = ("Still down after the reboot.\r\n\r\n\r\n\r\nThanks,\r\nJoe   Smith\r\n\r\nFrom: Help Desk <help@dbs.com>\r\n"
             "Sent: Monday, September 28, 2026 9:14 AM\r\nTo: Joe Smith\r\nSubject: RE: Printer\r\n\r\nWe rebooted it.")
    assert clean_note_text(reply) == ("Still down after the reboot.\n\nThanks,\nJoe Smith\n"
                                      "[earlier emails in the thread left out]")
    forwarded = "From: Joe\nSent: Monday\nSubject: Printer\n\nKitchen printer is down.\n\n-----Original Message-----\nold"
    assert clean_note_text(forwarded).startswith("From: Joe\nSent: Monday")  # its own header stays
    assert "Kitchen printer is down." in clean_note_text(forwarded) and "old" not in clean_note_text(forwarded)
    plain = "Called the client. From: the back office they can't print. Sent: tech tomorrow."
    assert clean_note_text(plain) == plain
    gmail = "Card reader declines.\n\nOn Mon, Sep 28, 2026 at 9:14 AM Joe <joe@pizza.com> wrote:\n> old"
    assert clean_note_text(gmail) == "Card reader declines.\n[earlier emails in the thread left out]"


def test_closed_ticket_without_a_closed_date_counts_as_closed(monkeypatch):
    monkeypatch.setitem(TICKETS[1], "closedDate", None)
    result = json.loads(tools_by_name(make_client([]))["get_company_tickets"].call({"company_id": 42}))
    assert result["open_count"] == 1
    assert rows(result["tickets"])[1]["closed"] is True
    assert "by_item" not in result  # nobody fills it in, so it isn't sent
