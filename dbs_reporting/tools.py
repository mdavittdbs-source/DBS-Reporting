"""Tools Claude can call to read ConnectWise data. All tools are read-only."""

import json
from collections import Counter
from typing import Any

import httpx
from anthropic import beta_tool

from .connectwise import ConnectWiseClient

MAX_DAYS = 730
NOTE_CHARS = 1500
TOTALS_LIMIT = 20000


def _name(record: dict, key: str) -> str | None:
    value = record.get(key)
    return value.get("name") if isinstance(value, dict) else None


def _date_entered(ticket: dict) -> str | None:
    return ticket.get("dateEntered") or (ticket.get("_info") or {}).get("dateEntered")


def summarize_ticket(ticket: dict) -> dict:
    return {
        "id": ticket.get("id"),
        "summary": ticket.get("summary"),
        "entered": _date_entered(ticket),
        "closed": ticket.get("closedDate") if ticket.get("closedFlag") else None,
        "board": _name(ticket, "board"),
        "status": _name(ticket, "status"),
        "type": _name(ticket, "type"),
        "subtype": _name(ticket, "subType"),
        "item": _name(ticket, "item"),
        "priority": _name(ticket, "priority"),
        "source": _name(ticket, "source"),
        "contact": ticket.get("contactName"),
        "hours": ticket.get("actualHours"),
    }


def ticket_breakdown(tickets: list[dict]) -> dict[str, Any]:
    rows = [summarize_ticket(t) for t in tickets]

    def top(field: str, n: int = 15) -> list[list]:
        counts = Counter(r[field] or "(none)" for r in rows)
        return [[k, v] for k, v in counts.most_common(n)]

    return {
        "ticket_count": len(rows),
        "open_count": sum(1 for r in rows if not r["closed"]),
        "by_type": top("type"),
        "by_subtype": top("subtype"),
        "by_item": top("item"),
        "by_board": top("board"),
        "by_priority": top("priority"),
        "by_source": top("source"),
        "by_contact": top("contact", 10),
        "tickets": rows,
    }


def _error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return json.dumps({
            "error": f"ConnectWise returned HTTP {exc.response.status_code}",
            "detail": exc.response.text[:500],
        })
    return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def _clamp_days(days: int) -> int:
    return max(1, min(int(days), MAX_DAYS))


def build_tools(cw: ConnectWiseClient) -> list:
    @beta_tool(eager_input_streaming=True)
    def find_company(name: str) -> str:
        """Look up ConnectWise companies (clients) by name.

        Always call this first to turn a client name like "Joe's Pizza" into a company_id.
        Returns up to 10 partial matches; if several plausible matches come back, ask the user
        which one they mean.

        Args:
            name: Full or partial company name to search for.
        """
        try:
            return json.dumps(cw.find_companies(name))
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_company_tickets(company_id: int, days: int = 30, board_name: str = "") -> str:
        """Get service tickets opened for a company in the last N days.

        Returns counts broken down by type, subtype, item, board, priority, source and contact,
        plus a compact list of every ticket (id, summary, dates, status, classification). Use the ticket summaries, not
        just the type fields, to identify recurring issues, because technicians often leave the
        type fields blank or generic.

        Args:
            company_id: ConnectWise company id from find_company.
            days: How many days back to look, based on the date each ticket was entered.
            board_name: Optional exact service board name to filter to, e.g. "Help Desk".
        """
        try:
            tickets = cw.tickets_for_company(company_id, _clamp_days(days), board_name or None)
            result = ticket_breakdown(tickets)
            if len(tickets) >= 1000:
                result["note"] = "Result capped at 1000 tickets; narrow the date range for a complete picture."
            return json.dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_ticket_details(ticket_id: int) -> str:
        """Get one ticket's full details and its notes.

        Notes include the description, internal analysis and resolution. Use this to understand the root cause or resolution of specific tickets.

        Args:
            ticket_id: ConnectWise service ticket number.
        """
        try:
            ticket = summarize_ticket(cw.ticket(ticket_id))
            ticket["notes"] = [
                {
                    "created": n.get("dateCreated"),
                    "by": n.get("createdBy"),
                    "kind": "resolution" if n.get("resolutionFlag")
                    else "internal" if n.get("internalAnalysisFlag")
                    else "description",
                    "text": (n.get("text") or "")[:NOTE_CHARS],
                }
                for n in cw.ticket_notes(ticket_id)
            ]
            return json.dumps(ticket)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_company_time(company_id: int, days: int = 30) -> str:
        """Summarize time logged against a company in the last N days.

        Returns total hours and hours by technician, work type, and ticket. Use this for questions about effort, hours, or who
        has been working on a client.

        Args:
            company_id: ConnectWise company id from find_company.
            days: How many days back to look, based on each time entry's start time.
        """
        try:
            entries = cw.time_entries_for_company(company_id, _clamp_days(days))
            by_member: Counter = Counter()
            by_work_type: Counter = Counter()
            by_ticket: Counter = Counter()
            for e in entries:
                hours = float(e.get("actualHours") or 0)
                member = e.get("member") or {}
                by_member[member.get("name") or member.get("identifier") or "(unknown)"] += hours
                by_work_type[_name(e, "workType") or "(none)"] += hours
                if e.get("chargeToType") == "ServiceTicket":
                    by_ticket[e.get("chargeToId")] += hours

            def rounded(counter: Counter, n: int = 15) -> list[list]:
                return [[k, round(v, 2)] for k, v in counter.most_common(n)]

            return json.dumps({
                "entry_count": len(entries),
                "total_hours": round(sum(by_member.values()), 2),
                "by_member": rounded(by_member),
                "by_work_type": rounded(by_work_type),
                "top_tickets_by_hours": rounded(by_ticket, 20),
            })
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_ticket_totals(days: int = 30, group_by: str = "company", board_name: str = "", top: int = 25) -> str:
        """Count tickets across ALL clients in the last N days, ranked from most to fewest.

        Use this for questions that compare or rank clients, sites, boards and so on, e.g. "which
        clients had the most tickets this month", "sites with the most tickets", "ticket volume by
        board". Don't look clients up one at a time for these. Returns totals, open counts, each
        group's share of all tickets and its top ticket types.

        Args:
            days: How many days back to look, based on the date each ticket was entered.
            group_by: What to rank: "company" (client), "site" (client + site/location on the ticket), "board", "type", "priority", "source" or "status".
            board_name: Optional exact service board name to count only, e.g. "Help Desk".
            top: How many groups to return (the rest are summarised as a count).
        """
        keys = {
            "company": lambda t: _name(t, "company") or "(no company)",
            "site": lambda t: f"{_name(t, 'company') or '(no company)'} – {_name(t, 'site') or '(no site)'}",
            "board": lambda t: _name(t, "board") or "(none)",
            "type": lambda t: _name(t, "type") or "(none)",
            "priority": lambda t: _name(t, "priority") or "(none)",
            "source": lambda t: _name(t, "source") or "(none)",
            "status": lambda t: _name(t, "status") or "(none)",
        }
        if group_by not in keys:
            return json.dumps({"error": f"group_by must be one of: {', '.join(keys)}"})
        try:
            days = _clamp_days(days)
            tickets = cw.tickets_since(days, board_name or None, limit=TOTALS_LIMIT)
            key = keys[group_by]
            totals: Counter = Counter()
            open_counts: Counter = Counter()
            types: dict[str, Counter] = {}
            for t in tickets:
                k = key(t)
                totals[k] += 1
                if not t.get("closedFlag"):
                    open_counts[k] += 1
                types.setdefault(k, Counter())[_name(t, "type") or "(none)"] += 1
            top = max(1, min(int(top), 100))
            result = {
                "days": days,
                "group_by": group_by,
                "ticket_count": len(tickets),
                "open_count": sum(open_counts.values()),
                "group_count": len(totals),
                "groups": [
                    {
                        "name": k,
                        "tickets": n,
                        "open": open_counts[k],
                        "share_pct": round(100 * n / len(tickets), 1),
                        "top_types": [[name, c] for name, c in types[k].most_common(3)],
                    }
                    for k, n in totals.most_common(top)
                ],
            }
            if len(totals) > top:
                result["other_groups"] = {"groups": len(totals) - top,
                                          "tickets": sum(n for _, n in totals.most_common()[top:])}
            if len(tickets) >= TOTALS_LIMIT:
                result["note"] = f"Capped at {TOTALS_LIMIT} tickets; narrow the date range for exact totals."
            return json.dumps(result)
        except Exception as exc:
            return _error(exc)

    return [find_company, get_company_tickets, get_ticket_details, get_company_time, get_ticket_totals]
