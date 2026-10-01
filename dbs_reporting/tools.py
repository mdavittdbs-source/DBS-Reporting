"""Tools Claude can call to read ConnectWise data. All tools are read-only."""

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

import httpx
from anthropic import beta_tool

from . import eastern
from .charts import validate_chart
from .connectwise import ConnectWiseClient

MAX_DAYS = 730
NOTE_CHARS = 1500
MAX_NOTES, FIRST_NOTES, LAST_NOTES = 25, 5, 15  # a ticket's notes sent to Claude (see _key_notes)
TOTALS_LIMIT = 20000


def _dumps(result) -> str:
    """Tool results go to Claude as JSON, with ConnectWise's UTC times turned into Eastern Time."""
    return eastern.localize(json.dumps(result))


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

    # Blank fields are left out of the ticket list: up to 1000 rows go to Claude on every step
    # of the answer and every follow-up, so empty values would cost tokens for nothing.
    compact = [_compact(r) for r in rows]

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
        "tickets": compact,
    }


def _plain(text: str) -> str:
    """Lowercase letters and digits only, so "Sky Tab" matches "SkyTab" and "Shift 4" matches "Shift4"."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def software_matches(value: str | None, wanted: str) -> bool:
    return bool(value) and _plain(wanted) in _plain(value)


def _hours(value) -> float | None:
    try:
        return round(float(value), 2) if value is not None else None
    except (TypeError, ValueError):
        return None


def _compact(row: dict) -> dict:
    """Drop blank fields; long lists go to Claude on every step, so empty values cost tokens."""
    return {k: v for k, v in row.items() if v is not None and v != ""}


def summarize_project(p: dict) -> dict:
    budget, actual = _hours(p.get("budgetHours")), _hours(p.get("actualHours"))
    manager = p.get("manager") or {}
    return _compact({
        "id": p.get("id"),
        "name": p.get("name"),
        "company": _name(p, "company"),
        "status": _name(p, "status"),
        "closed": bool(p.get("closedFlag")),
        "manager": manager.get("name") or manager.get("identifier"),
        "type": _name(p, "type"),
        "board": _name(p, "board"),
        "estimated_start": p.get("estimatedStart"),
        "estimated_end": p.get("estimatedEnd"),
        "actual_start": p.get("actualStart"),
        "actual_end": p.get("actualEnd"),
        "percent_complete": p.get("percentComplete"),
        "budget_hours": budget,
        "actual_hours": actual,
        "scheduled_hours": _hours(p.get("scheduledHours")),
        "over_budget_hours": round(actual - budget, 2) if budget and actual is not None and actual > budget else None,
    })


def summarize_project_ticket(t: dict) -> dict:
    project = t.get("project") or {}
    return _compact({
        "id": t.get("id"),
        "summary": t.get("summary"),
        "project": project.get("name"),
        "project_id": project.get("id"),
        "phase": _name(t, "phase"),
        "status": _name(t, "status"),
        "entered": _date_entered(t),
        "closed": t.get("closedDate") if t.get("closedFlag") else None,
        "budget_hours": _hours(t.get("budgetHours")),
        "actual_hours": _hours(t.get("actualHours")),
        "resources": t.get("resources"),
        "priority": _name(t, "priority"),
        "type": _name(t, "type"),
    })


def _error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return _dumps({
            "error": f"ConnectWise returned HTTP {exc.response.status_code}",
            "detail": exc.response.text[:500],
        })
    return _dumps({"error": f"{type(exc).__name__}: {exc}"})


def _note(n: dict) -> dict:
    return {
        "created": n.get("dateCreated"),
        "by": n.get("createdBy"),
        "kind": "resolution" if n.get("resolutionFlag") else "internal" if n.get("internalAnalysisFlag")
        else "description",
        "text": (n.get("text") or "")[:NOTE_CHARS],
    }


def _key_notes(notes: list[dict]) -> tuple[list[dict], int]:
    """A long-running ticket can have hundreds of notes. Keep the first few (the problem), the
    latest (where it stands) and every resolution note; returns (notes, how many were left out)."""
    if len(notes) <= MAX_NOTES:
        return [_note(n) for n in notes], 0
    keep = set(range(FIRST_NOTES)) | set(range(len(notes) - LAST_NOTES, len(notes)))
    keep |= {i for i, n in enumerate(notes) if n.get("resolutionFlag")}
    return [_note(notes[i]) for i in sorted(keep)], len(notes) - len(keep)


def _clamp_days(days: int) -> int:
    return max(1, min(int(days), MAX_DAYS))


# --- SLA helpers ---------------------------------------------


def parse_dt(value) -> datetime | None:
    """ConnectWise timestamps like 2026-09-20T10:00:00Z (or a date) -> aware datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hours_between(start, end) -> float | None:
    a, b = parse_dt(start), parse_dt(end)
    if not a or not b or b < a:
        return None
    return (b - a).total_seconds() / 3600


def _stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "median_hours": None, "average_hours": None}
    return {"count": len(values), "median_hours": round(median(values), 1),
            "average_hours": round(sum(values) / len(values), 1)}


# Words that mean someone asked for a chart. Charts cost extra tokens, so David only draws one
# when the question asks for it.
CHART_REQUEST = re.compile(r"\b(chart|graph|plot|visual|visuali[sz]|diagram|pie|histogram)", re.IGNORECASE)


def wants_chart(question: str) -> bool:
    return bool(CHART_REQUEST.search(question))


def build_tools(cw: ConnectWiseClient, charts_allowed: bool = True) -> list:
    """The tools for one question. With charts_allowed=False, create_chart refuses (its
    definition stays the same, so the prompt cache still matches)."""

    def software_by_company() -> dict[int, str]:
        """Company id -> the POS software in the company's "Software" custom field."""
        caption = _plain(cw.software_field)
        found, captions = {}, Counter()
        for c in cw.company_custom_fields():
            for f in c["customFields"]:
                captions[f.get("caption") or ""] += 1
                if _plain(f.get("caption") or "") != caption:
                    continue
                value = f.get("value")
                value = ", ".join(map(str, value)) if isinstance(value, list) else str(value or "").strip()
                if value:
                    found[c["id"]] = value
        # Spellings that differ only in spaces or case ("Sky Tab", "SkyTab") count as one: the commonest.
        spellings = Counter(found.values())
        best = {}
        for v, _ in spellings.most_common():
            best.setdefault(_plain(v), v)
        found = {i: best[_plain(v)] for i, v in found.items()}
        if not found:
            seen = ", ".join(f'"{k}"' for k, _ in captions.most_common(15) if k) or "none"
            raise ValueError(f'No client has a "{cw.software_field}" custom field filled in. Custom fields '
                             f"found on clients: {seen}. (CW_SOFTWARE_FIELD sets which field to use.)")
        return found

    def ticket_software(lookup: dict[int, str]):
        return lambda t: lookup.get((t.get("company") or {}).get("id")) or "(software not set)"

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
            return _dumps(cw.find_companies(name))
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_company_tickets(company_id: int, days: int = 30, board_name: str = "") -> str:
        """Get service tickets opened for a company in the last N days.

        Returns counts broken down by type, subtype, item, board, priority, source and contact,
        plus a compact list of every ticket (id, summary, dates, status, classification; blank fields
        are left out, so a ticket with no "closed" date is open). Use the ticket summaries, not
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
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_ticket_details(ticket_id: int) -> str:
        """Get one ticket's full details and its notes. Works for service and project tickets.

        Notes include the description, internal analysis and resolution. Use this to understand the root cause or resolution of specific tickets.

        Args:
            ticket_id: ConnectWise ticket number (service or project ticket).
        """
        try:
            try:
                ticket = summarize_ticket(cw.ticket(ticket_id))
                notes = cw.ticket_notes(ticket_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                # Not a service ticket: project tickets are kept separately.
                ticket = {"kind": "project ticket", **summarize_project_ticket(cw.project_ticket(ticket_id))}
                notes = cw.project_ticket_notes(ticket_id)
            ticket["notes"], left_out = _key_notes(notes)
            if left_out:
                ticket["notes_left_out"] = (f"{left_out} notes from the middle of this ticket were left out "
                                            f"to save tokens; the first, latest and resolution notes are included.")
            return _dumps(ticket)
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
                if e.get("chargeToType") in ("ServiceTicket", "ProjectTicket"):
                    by_ticket[e.get("chargeToId")] += hours

            def rounded(counter: Counter, n: int = 15) -> list[list]:
                return [[k, round(v, 2)] for k, v in counter.most_common(n)]

            return _dumps({
                "entry_count": len(entries),
                "total_hours": round(sum(by_member.values()), 2),
                "by_member": rounded(by_member),
                "by_work_type": rounded(by_work_type),
                "top_tickets_by_hours": rounded(by_ticket, 20),
            })
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_ticket_totals(days: int = 30, group_by: str = "company", board_name: str = "", top: int = 25,
                          software: str = "") -> str:
        """Count tickets across ALL clients in the last N days, ranked from most to fewest.

        Use this for questions that compare or rank clients, sites, boards and so on, e.g. "which
        clients had the most tickets this month", "sites with the most tickets", "ticket volume by
        board". Don't look clients up one at a time for these. Returns totals, open counts, each
        group's share of all tickets and its top ticket types.

        Args:
            days: How many days back to look, based on the date each ticket was entered.
            group_by: What to rank: "company" (client), "site" (client + site/location on the ticket), "software" (the client's POS software), "board", "type", "priority", "source" or "status".
            board_name: Optional exact service board name to count only, e.g. "Help Desk".
            top: How many groups to return (the rest are summarised as a count).
            software: Optional: only count clients whose POS software matches, e.g. "SkyTab".
        """
        keys = {
            "company": lambda t: _name(t, "company") or "(no company)",
            "site": lambda t: f"{_name(t, 'company') or '(no company)'} – {_name(t, 'site') or '(no site)'}",
            "board": lambda t: _name(t, "board") or "(none)",
            "type": lambda t: _name(t, "type") or "(none)",
            "priority": lambda t: _name(t, "priority") or "(none)",
            "source": lambda t: _name(t, "source") or "(none)",
            "status": lambda t: _name(t, "status") or "(none)",
            "software": None,
        }
        if group_by not in keys:
            return _dumps({"error": f"group_by must be one of: {', '.join(keys)}"})
        try:
            days = _clamp_days(days)
            tickets = cw.tickets_since(days, board_name or None, limit=TOTALS_LIMIT)
            capped = len(tickets) >= TOTALS_LIMIT
            if group_by == "software" or software:
                of = ticket_software(software_by_company())
                keys["software"] = of
                if software:
                    tickets = [t for t in tickets if software_matches(of(t), software)]
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
                shown = sum(g["tickets"] for g in result["groups"])
                result["other_groups"] = {"groups": len(totals) - top, "tickets": len(tickets) - shown}
            if software:
                result["software"] = software
            if capped:
                result["note"] = f"Capped at {TOTALS_LIMIT} tickets; narrow the date range for exact totals."
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_after_hours(days: int = 30, group_by: str = "company", company_id: int = 0, board_name: str = "",
                        software: str = "", source: str = "", top: int = 25) -> str:
        """When tickets come in: during office hours, weekday evenings, or weekends (Eastern Time).

        Office hours are Mon and Wed-Fri 8:30 AM-5:00 PM, Tue 9:00 AM-5:00 PM. "Evening" is any
        weekday time outside those hours (early mornings included); "weekend" is all of Saturday and
        Sunday. Use for after-hours or weekend call volume, which clients or techs it falls on, the
        busiest after-hours times, and whether it's growing. Returns counts by period, by weekday,
        by hour (evening and weekend), by week (for trends), by source, a ranked breakdown, and
        recent after-hours tickets. Holidays aren't treated as after hours.

        Args:
            days: How many days back to look, based on when each ticket was entered.
            group_by: Breakdown to rank by after-hours tickets: "company", "site", "software", "board", "type", "priority", "source", "owner" (assigned tech) or "entered_by" (who logged it, usually who took the call).
            company_id: Optional ConnectWise company id from find_company; 0 for all clients.
            board_name: Optional exact service board name, e.g. "Help Desk".
            software: Optional: only clients whose POS software matches, e.g. "SkyTab".
            source: Optional ticket source to count only, e.g. "Phone" for calls.
            top: How many groups to return.
        """
        def owner(t: dict) -> str:
            o = t.get("owner") or {}
            return o.get("name") or o.get("identifier") or "(unassigned)"

        keys = {
            "company": lambda t: _name(t, "company") or "(no company)",
            "site": lambda t: f"{_name(t, 'company') or '(no company)'} – {_name(t, 'site') or '(no site)'}",
            "software": None,
            "board": lambda t: _name(t, "board") or "(none)",
            "type": lambda t: _name(t, "type") or "(none)",
            "priority": lambda t: _name(t, "priority") or "(none)",
            "source": lambda t: _name(t, "source") or "(none)",
            "owner": owner,
            "entered_by": lambda t: (t.get("_info") or {}).get("enteredBy") or "(unknown)",
        }
        if group_by not in keys:
            return _dumps({"error": f"group_by must be one of: {', '.join(keys)}"})
        try:
            days = _clamp_days(days)
            tickets = cw.tickets_with_times(days, company_id or None, board_name or None, limit=TOTALS_LIMIT)
            capped = len(tickets) >= TOTALS_LIMIT
            if group_by == "software" or software:
                keys["software"] = of = ticket_software(software_by_company())
                if software:
                    tickets = [t for t in tickets if software_matches(of(t), software)]
            if source:
                tickets = [t for t in tickets if _plain(_name(t, "source") or "") == _plain(source)]
            periods = ("business hours", "evening", "weekend")
            weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
            by_period: Counter = Counter()
            by_weekday = {d: Counter() for d in weekdays}
            by_hour = {"evening": Counter(), "weekend": Counter()}
            by_week: dict[str, Counter] = {}
            groups: dict[str, Counter] = {}
            source_after: Counter = Counter()
            after = []
            key = keys[group_by]
            for t in tickets:
                dt = parse_dt(_date_entered(t))
                if not dt:
                    continue
                local = eastern.to_eastern(dt)
                p = eastern.period(dt)
                by_period[p] += 1
                by_weekday[weekdays[local.weekday()]][p] += 1
                week = eastern.day((local - timedelta(days=local.weekday())).date())
                by_week.setdefault(week, Counter())[p] += 1
                groups.setdefault(key(t), Counter())[p] += 1
                if p != "business hours":
                    by_hour[p][local.hour] += 1
                    source_after[_name(t, "source") or "(none)"] += 1
                    after.append((dt, t, p))
            counted = sum(by_period.values())
            after_count = by_period["evening"] + by_period["weekend"]

            def split(c: Counter) -> dict:
                row = {p: c[p] for p in periods}
                total = sum(row.values())
                row["after_hours"] = row["evening"] + row["weekend"]
                row["after_hours_pct"] = round(100 * row["after_hours"] / total, 1) if total else 0
                return row

            def hour_label(h: int) -> str:
                return f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}"

            ranked = sorted(groups.items(), key=lambda kv: (-(kv[1]["evening"] + kv[1]["weekend"]), kv[0]))
            top = max(1, min(int(top), 100))
            after.sort(key=lambda x: x[0], reverse=True)
            result = {
                "days": days,
                "office_hours": eastern.HOURS_TEXT,
                "ticket_count": counted,
                "by_period": split(by_period),
                "by_weekday": {d: {p: c[p] for p in periods} for d, c in by_weekday.items()},
                "evening_by_hour": [[hour_label(h), n] for h, n in sorted(by_hour["evening"].items())],
                "weekend_by_hour": [[hour_label(h), n] for h, n in sorted(by_hour["weekend"].items())],
                "by_week_starting": [dict(week=w, **{p: c[p] for p in periods})
                                     for w, c in sorted(by_week.items(), key=lambda kv: kv[0][6:] + kv[0][:5])],
                "after_hours_by_source": [[k, v] for k, v in source_after.most_common(10)],
                "group_by": group_by,
                "groups": [dict(name=k, **split(c)) for k, c in ranked[:top]],
                "recent_after_hours": [_compact({
                    "id": t.get("id"), "summary": t.get("summary"), "company": _name(t, "company"),
                    "entered": _date_entered(t), "period": p,
                    "entered_by": (t.get("_info") or {}).get("enteredBy")}) for _, t, p in after[:15]],
            }
            if len(groups) > top:
                result["other_groups"] = len(groups) - top
            if not after_count:
                result["note"] = "No tickets came in outside office hours in this period."
            if software:
                result["software"] = software
            if capped:
                result["limit_note"] = f"Capped at {TOTALS_LIMIT} tickets; narrow the date range for exact totals."
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_sla_performance(days: int = 30, group_by: str = "company", company_id: int = 0,
                            board_name: str = "", top: int = 25) -> str:
        """SLA performance for tickets entered in the last N days, for all clients or one.

        For each group: tickets, how many ConnectWise marks as within SLA vs. breached, the percent
        within SLA, and median/average hours to first response and to resolution, plus open count.
        Use for "which clients had the most SLA breaches", "average response time by board", etc.

        Args:
            days: How many days back to look, based on the date each ticket was entered.
            group_by: How to break it down: "company", "board", "priority", "sla" (the SLA name) or "overall".
            company_id: Optional ConnectWise company id from find_company to look at one client; 0 for all.
            board_name: Optional exact service board name to include only, e.g. "Help Desk".
            top: How many groups to return, ranked by number of SLA breaches then ticket count.
        """
        keys = {
            "company": lambda t: _name(t, "company") or "(no company)",
            "board": lambda t: _name(t, "board") or "(none)",
            "priority": lambda t: _name(t, "priority") or "(none)",
            "sla": lambda t: _name(t, "sla") or "(no SLA)",
            "overall": lambda t: "All tickets",
        }
        if group_by not in keys:
            return _dumps({"error": f"group_by must be one of: {', '.join(keys)}"})
        try:
            days = _clamp_days(days)
            tickets = cw.tickets_for_sla(days, company_id or None, board_name or None, limit=TOTALS_LIMIT)
            groups: dict[str, dict] = {}
            for t in tickets:
                g = groups.setdefault(keys[group_by](t), {"tickets": 0, "open": 0, "in_sla": 0, "breached": 0,
                                                           "sla_unknown": 0, "respond": [], "resolve": []})
                g["tickets"] += 1
                if not t.get("closedFlag"):
                    g["open"] += 1
                flag = t.get("isInSla")
                g["in_sla" if flag is True else "breached" if flag is False else "sla_unknown"] += 1
                entered = _date_entered(t)
                if (h := _hours_between(entered, t.get("dateResponded"))) is not None:
                    g["respond"].append(h)
                if (h := _hours_between(entered, t.get("dateResolved") or t.get("closedDate"))) is not None:
                    g["resolve"].append(h)

            def row(name: str, g: dict) -> dict:
                known = g["in_sla"] + g["breached"]
                return {"name": name, "tickets": g["tickets"], "open": g["open"], "in_sla": g["in_sla"],
                        "breached": g["breached"], "sla_unknown": g["sla_unknown"],
                        "percent_in_sla": round(100 * g["in_sla"] / known, 1) if known else None,
                        "first_response": _stats(g["respond"]), "resolution": _stats(g["resolve"])}

            ranked = sorted(groups.items(), key=lambda kv: (-kv[1]["breached"], -kv[1]["tickets"]))
            top = max(1, min(int(top), 100))
            everything = {"tickets": 0, "open": 0, "in_sla": 0, "breached": 0, "sla_unknown": 0,
                          "respond": [], "resolve": []}
            for g in groups.values():
                for k in everything:
                    everything[k] += g[k]
            result = {
                "days": days,
                "group_by": group_by,
                "overall": row("All tickets", everything),
                "groups": [row(name, g) for name, g in ranked[:top]],
                "note": ("in_sla/breached use ConnectWise's own SLA flag on each ticket. Response and "
                         "resolution times are calendar hours from entry, not business hours, so they can "
                         "be longer than the SLA clock."),
            }
            if len(ranked) > top:
                result["other_groups"] = len(ranked) - top
            if len(tickets) >= TOTALS_LIMIT:
                result["limit_note"] = f"Capped at {TOTALS_LIMIT} tickets; narrow the date range."
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_open_tickets(company_id: int = 0, board_name: str = "", oldest: int = 50, software: str = "") -> str:
        """Service tickets that are still open, however long ago they were entered, oldest first.

        Use for "oldest open tickets", "what's still open at Jimmy's Grille", "stale tickets",
        "open ticket backlog by board/technician". Unlike get_company_tickets, this isn't limited to
        a date range. Returns the open count, age buckets, median age, counts by status, board,
        priority, owner (and client, when looking at all clients), and the oldest tickets with
        their age in days and days since last update.

        Args:
            company_id: Optional ConnectWise company id from find_company; 0 for all clients.
            board_name: Optional exact service board name, e.g. "Help Desk".
            oldest: How many of the oldest tickets to list (most 200).
            software: Optional: only clients whose POS software matches, e.g. "SkyTab".
        """
        try:
            tickets = cw.open_tickets(company_id or None, board_name or None)
            capped = len(tickets) >= 5000
            of = None
            if software or not company_id:
                try:
                    of = ticket_software(software_by_company())
                except Exception:
                    if software:
                        raise  # only the optional breakdown by software is skipped when it can't be read
            if software:
                tickets = [t for t in tickets if software_matches(of(t), software)]
            now = datetime.now(timezone.utc)

            def days_since(value) -> int | None:
                dt = parse_dt(value)
                return (now - dt).days if dt else None

            rows = []
            for t in tickets:
                owner = t.get("owner") or {}
                rows.append(_compact({
                    "id": t.get("id"),
                    "summary": t.get("summary"),
                    "company": _name(t, "company"),
                    "site": _name(t, "site"),
                    "board": _name(t, "board"),
                    "status": _name(t, "status"),
                    "priority": _name(t, "priority"),
                    "type": _name(t, "type"),
                    "owner": owner.get("name") or owner.get("identifier"),
                    "resources": t.get("resources"),
                    "entered": _date_entered(t),
                    "age_days": days_since(_date_entered(t)),
                    "days_since_update": days_since((t.get("_info") or {}).get("lastUpdated")),
                }))
            rows.sort(key=lambda r: -(r.get("age_days") if r.get("age_days") is not None else -1))
            ages = [r["age_days"] for r in rows if r.get("age_days") is not None]

            def top(field: str, n: int = 15) -> list[list]:
                return [[k, v] for k, v in Counter(r.get(field) or "(none)" for r in rows).most_common(n)]

            buckets = {"0-7 days": 0, "8-30 days": 0, "31-90 days": 0, "91-365 days": 0, "over 1 year": 0}
            for a in ages:
                key = ("0-7 days" if a <= 7 else "8-30 days" if a <= 30 else "31-90 days" if a <= 90
                       else "91-365 days" if a <= 365 else "over 1 year")
                buckets[key] += 1
            result = {
                "open_count": len(rows),
                "median_age_days": round(median(ages)) if ages else None,
                "age_buckets": buckets,
                "by_status": top("status"),
                "by_board": top("board"),
                "by_priority": top("priority"),
                "by_owner": top("owner"),
                "oldest": rows[:max(1, min(int(oldest), 200))],
            }
            if not company_id:
                result["by_company"] = top("company", 25)
                if of:
                    result["by_software"] = [[k, v] for k, v in Counter(of(t) for t in tickets).most_common(15)]
            if software:
                result["software"] = software
            if capped:
                result["note"] = "Capped at 5000 open tickets; filter by client or board for exact figures."
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def search_tickets(text: str, days: int = 0, company_id: int = 0, include_project_tickets: bool = True,
                       max_results: int = 40, software: str = "") -> str:
        """Search ticket summaries across ALL clients (or one) for words, e.g. to find how a problem
        was handled elsewhere, every ticket mentioning "handheld", or similar past issues.

        Matches ticket summaries (titles), not notes; call get_ticket_details on the most relevant
        few to read how they were fixed. Returns how many matched, counts by client, and the newest
        matches (id, summary, client, site, board, status, dates).

        Args:
            text: Short keywords, not a sentence. Separate alternatives with commas; a ticket matches
                if its summary contains every word of any one alternative. E.g.
                "handheld, hand held, scanner sync" or "printer offline, printer not printing".
                Include spelling variants and synonyms.
            days: Only tickets entered in the last N days; 0 for any time.
            company_id: Optional ConnectWise company id from find_company; 0 for all clients.
            include_project_tickets: Also search project tickets.
            max_results: How many matches to list (most 100).
            software: Optional: only clients whose POS software matches, e.g. "SkyTab".
        """
        phrases = [p.split()[:4] for p in text.split(",") if p.split()][:8]
        if not phrases:
            return _dumps({"error": "Give one or more keywords to search for."})
        try:
            days = _clamp_days(days) if days else 0
            tickets = cw.search_tickets(phrases, days or None, company_id or None)
            capped = len(tickets) >= 500
            of = ticket_software(software_by_company()) if software else None
            if of:
                tickets = [t for t in tickets if software_matches(of(t), software)]
            rows = [_compact({
                "id": t.get("id"),
                "summary": t.get("summary"),
                "company": _name(t, "company"),
                "site": _name(t, "site"),
                "board": _name(t, "board"),
                "status": _name(t, "status"),
                "type": _name(t, "type"),
                "entered": _date_entered(t),
                "closed": t.get("closedDate") if t.get("closedFlag") else None,
            }) for t in tickets]
            result = {"searched_for": [" ".join(p) for p in phrases], "match_count": len(rows)}
            if include_project_tickets:
                try:
                    found = cw.search_project_tickets(phrases, company_id or None)
                    if days:
                        start = datetime.now(timezone.utc) - timedelta(days=days)
                        found = [t for t in found if (d := parse_dt(_date_entered(t))) is None or d >= start]
                    if of:
                        found = [t for t in found if software_matches(of(t), software)]
                    projects = [dict(summarize_project_ticket(t), company=_name(t, "company"), kind="project")
                                for t in found]
                    rows += [_compact(r) for r in projects]
                    result["project_ticket_matches"] = len(projects)
                    result["match_count"] = len(rows)
                except Exception as exc:
                    result["project_ticket_error"] = json.loads(_error(exc))["error"]
            rows.sort(key=lambda r: r.get("entered") or "", reverse=True)
            result["by_company"] = [[k, v] for k, v in
                                    Counter(r.get("company") or "(none)" for r in rows).most_common(25)]
            result["tickets"] = rows[:max(1, min(int(max_results), 100))]
            if software:
                result["software"] = software
            if capped:
                result["note"] = "Capped at the 500 newest service tickets; add a date range or narrower words."
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_clients_by_software(software: str = "") -> str:
        """Which POS software each client uses, from the "Software" field on the client's company
        record in ConnectWise. This is the reliable source for a client's brand, rather than ticket text.

        With no software given: how many clients use each software. With software (e.g. "SkyTab"):
        the clients that use it, with their company ids. To count or search tickets by software, use
        the software option on get_ticket_totals, get_open_tickets or search_tickets instead.

        Args:
            software: Optional software to list clients for, e.g. "SkyTab" or "Shift4 Dine".
        """
        try:
            lookup = software_by_company()
            names = {c["id"]: c["name"] for c in cw.company_custom_fields()}
            result = {
                "clients": len(names),
                "with_software_set": len(lookup),
                "by_software": [[k, v] for k, v in Counter(lookup.values()).most_common(30)],
            }
            if software:
                matches = sorted(({"id": i, "name": names.get(i), "software": v} for i, v in lookup.items()
                                  if software_matches(v, software)), key=lambda c: c["name"] or "")
                result.update(software=software, match_count=len(matches), matches=matches[:500])
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_projects(company_id: int = 0, include_closed: bool = False) -> str:
        """List ConnectWise projects (project work, not service tickets), for one client or all clients.

        For each project: name, client, status, manager, type, estimated/actual dates, percent
        complete, and budget vs actual hours (with hours over budget). Also counts by status,
        manager and client. Use for "what projects do we have open", "which projects are over
        budget", "projects for Jimmy's Grille". Then use get_project_tickets for a project's tasks.

        Args:
            company_id: Optional ConnectWise company id from find_company; 0 for all clients.
            include_closed: Also include closed projects.
        """
        try:
            projects = [summarize_project(p) for p in cw.projects(company_id or None, include_closed)]

            def top(field: str) -> list[list]:
                return [[k, v] for k, v in Counter(p.get(field) or "(none)" for p in projects).most_common(15)]

            budget = sum(p.get("budget_hours") or 0 for p in projects)
            actual = sum(p.get("actual_hours") or 0 for p in projects)
            return _dumps({
                "project_count": len(projects),
                "open_count": sum(1 for p in projects if not p.get("closed")),
                "over_budget_count": sum(1 for p in projects if p.get("over_budget_hours")),
                "budget_hours": round(budget, 2),
                "actual_hours": round(actual, 2),
                "by_status": top("status"),
                "by_manager": top("manager"),
                "by_company": top("company"),
                "projects": projects,
            })
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_project_tickets(project_id: int = 0, company_id: int = 0, days: int = 0,
                            include_closed: bool = True) -> str:
        """Get project tickets (the tasks within ConnectWise projects) for one project or one client.

        Project tickets are separate from service tickets; use this for questions about project
        work, phases or tasks. Returns counts by project, phase and status, open count, budget vs
        actual hours, and a compact list of every ticket (id, summary, project, phase, status,
        dates, hours, resources; blank fields are left out).

        Args:
            project_id: ConnectWise project id from get_projects. Give this or company_id.
            company_id: ConnectWise company id from find_company, for all of that client's projects.
            days: Optional: only tickets entered in the last N days; 0 for all.
            include_closed: Also include closed project tickets.
        """
        if not project_id and not company_id:
            return _dumps({"error": "Give a project_id (from get_projects) or a company_id (from find_company)."})
        try:
            tickets = cw.project_tickets(project_id or None, company_id or None, include_closed)
            capped = len(tickets) >= 1000
            if days:
                start = datetime.now(timezone.utc) - timedelta(days=_clamp_days(days))
                tickets = [t for t in tickets if (d := parse_dt(_date_entered(t))) is None or d >= start]
            rows = [summarize_project_ticket(t) for t in tickets]

            def top(field: str) -> list[list]:
                return [[k, v] for k, v in Counter(r.get(field) or "(none)" for r in rows).most_common(15)]

            result = {
                "ticket_count": len(rows),
                "open_count": sum(1 for r in rows if not r.get("closed")),
                "budget_hours": round(sum(r.get("budget_hours") or 0 for r in rows), 2),
                "actual_hours": round(sum(r.get("actual_hours") or 0 for r in rows), 2),
                "by_project": top("project"),
                "by_phase": top("phase"),
                "by_status": top("status"),
                "tickets": rows,
            }
            if capped:
                result["note"] = "Capped at the 1000 newest project tickets."
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_go_lives(days_ahead: int = 30, days_back: int = 0, company_id: int = 0, software: str = "",
                     followup_days: int = 0) -> str:
        """POS installs going live: upcoming and/or recent go-lives, from ConnectWise projects.

        Each project ticket has a phase. When a ticket's phase is Deployment and someone is scheduled
        on that ticket, the day they're scheduled is the site's go-live day, and they're the installer. Returns each go-live (date, client,
        project, deployment ticket, installers, status, software), counts by week, installer and
        software, and open deployment tickets nobody is scheduled on yet. With followup_days, also
        counts the support tickets each site opened in the days after going live, overall and by
        installer and software, a sign of how well installs went.

        Args:
            days_ahead: Days from today to look ahead for upcoming go-lives (0 for none).
            days_back: Days before today to include past go-lives (0 for none), e.g. 30 for last month's.
            company_id: Optional ConnectWise company id from find_company; 0 for all clients.
            software: Optional: only clients whose POS software matches, e.g. "SkyTab".
            followup_days: For go-lives already past: count support tickets opened within this many days after go-live (e.g. 30). 0 to skip.
        """
        try:
            days_ahead = max(0, min(int(days_ahead), 365))
            days_back = max(0, min(int(days_back), MAX_DAYS))
            followup_days = max(0, min(int(followup_days), 180))
            today = eastern.now().date()
            first_day, last_day = today - timedelta(days=days_back), today + timedelta(days=days_ahead)
            closed_since = datetime.now(timezone.utc) - timedelta(days=days_back + 2) if days_back else None
            tickets = cw.deployment_tickets(closed_since, company_id or None)
            # Other work in the Deployment phase, like management training, isn't a go-live.
            tickets = [t for t in tickets
                       if not any(w in (t.get("summary") or "").lower() for w in cw.golive_exclude)]
            lookup = None
            try:
                lookup = software_by_company()
            except Exception:
                if software:
                    raise  # only needed for the filter; otherwise just leave software out
            of = ticket_software(lookup) if lookup else None
            if software:
                tickets = [t for t in tickets if software_matches(of(t), software)]

            def for_tickets(e: dict) -> bool:  # leave out CRM activities that happen to share an id
                kind = e.get("type") or {}
                return (kind.get("identifier") or "").upper() != "C" and "activit" not in (kind.get("name") or "").lower()

            scheduled: dict[int, list[tuple[datetime, str]]] = {}
            for e in cw.schedule_entries([t["id"] for t in tickets if t.get("id")]) if tickets else []:
                start = parse_dt(e.get("dateStart"))
                if start and for_tickets(e):
                    member = e.get("member") or {}
                    scheduled.setdefault(e.get("objectId"), []).append(
                        (eastern.to_eastern(start), member.get("name") or member.get("identifier") or "(unknown)"))

            go_lives, unscheduled = [], []
            for t in tickets:
                slots = sorted(scheduled.get(t.get("id"), []))
                project = t.get("project") or {}
                base = {"company": _name(t, "company"), "project": project.get("name"), "project_id": project.get("id"),
                        "ticket_id": t.get("id"), "ticket": t.get("summary"), "status": _name(t, "status")}
                if of:
                    base["software"] = of(t)
                if not slots:
                    if not t.get("closedFlag"):
                        unscheduled.append(_compact(base))
                    continue
                in_window = [s for s in slots if first_day <= s[0].date() <= last_day]
                if not in_window:
                    continue
                when = in_window[0][0]
                days = sorted({s[0].date() for s in slots})
                go_lives.append(_compact({
                    **base,
                    "date": eastern.day(when.date()), "weekday": f"{when:%a}", "time": eastern.clock(when) + " ET",
                    "installers": sorted({m for d, m in slots if d.date() == when.date()}),
                    "other_scheduled_days": [eastern.day(d) for d in days if d != when.date()] or None,
                    "past": when.date() < today or None,
                    "_when": when, "_company_id": (t.get("company") or {}).get("id"),
                }))
            go_lives.sort(key=lambda g: g["_when"])

            result: dict[str, Any] = {
                "from": eastern.day(first_day), "to": eastern.day(last_day), "phase": cw.golive_phase,
                "go_live_count": len(go_lives),
                "upcoming": sum(1 for g in go_lives if not g.get("past")),
                "past": sum(1 for g in go_lives if g.get("past")),
            }
            weeks = Counter(eastern.day(g["_when"].date() - timedelta(days=g["_when"].weekday())) for g in go_lives)
            result["by_week_starting"] = [[w, n] for w, n in sorted(weeks.items(), key=lambda kv: kv[0][6:] + kv[0][:5])]
            result["by_installer"] = [[k, v] for k, v in
                                      Counter(m for g in go_lives for m in g["installers"]).most_common(20)]
            if of:
                result["by_software"] = [[k, v] for k, v in Counter(g.get("software") for g in go_lives).most_common(15)]

            if followup_days:
                past = [g for g in go_lives if g.get("past")]
                if past:
                    start = min(g["_when"] for g in past).astimezone(timezone.utc)
                    span = (datetime.now(timezone.utc) - start).days + 1
                    support = cw.tickets_with_times(_clamp_days(span), company_id or None, limit=TOTALS_LIMIT)
                    by_company: dict[int, list[tuple[datetime, dict]]] = {}
                    for s_ in support:
                        entered = parse_dt(_date_entered(s_))
                        if entered:
                            by_company.setdefault((s_.get("company") or {}).get("id"), []).append((entered, s_))
                    for g in past:
                        begin = g["_when"].astimezone(timezone.utc)
                        end = begin + timedelta(days=followup_days)
                        after = sorted((e, s_) for e, s_ in by_company.get(g["_company_id"], [])
                                       if begin <= e < end and s_.get("id") != g["ticket_id"])
                        g["tickets_after"] = len(after)
                        g["examples_after"] = [f"#{s_.get('id')} {s_.get('summary') or ''}".strip()
                                               for _, s_ in after[:3]] or None
                        if end > datetime.now(timezone.utc):
                            g["followup_days_so_far"] = (datetime.now(timezone.utc) - begin).days

                    def average(groups: dict[str, list[int]]) -> list[list]:
                        rows = [[k, len(v), round(sum(v) / len(v), 1)] for k, v in groups.items() if v]
                        return sorted(rows, key=lambda r: (-r[2], r[0]))

                    installers: dict[str, list[int]] = {}
                    brands: dict[str, list[int]] = {}
                    for g in past:
                        for m in g["installers"]:
                            installers.setdefault(m, []).append(g["tickets_after"])
                        if of:
                            brands.setdefault(g.get("software"), []).append(g["tickets_after"])
                    result["followup"] = {
                        "days_after_go_live": followup_days,
                        "average_tickets_after": round(sum(g["tickets_after"] for g in past) / len(past), 1),
                        "by_installer": average(installers),  # [installer, go-lives, average tickets after]
                    }
                    if of:
                        result["followup"]["by_software"] = average(brands)

            result["go_lives"] = [{k: v for k, v in g.items() if not k.startswith("_")} for g in go_lives[:150]]
            if len(go_lives) > 150:
                result["note"] = f"Listing the first 150 of {len(go_lives)} go-lives; counts include all of them."
            if days_ahead and unscheduled:
                result["deployment_not_scheduled"] = unscheduled[:50]
                result["deployment_not_scheduled_count"] = len(unscheduled)
            if software:
                result["software"] = software
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def create_chart(title: str, chart_type: str, labels: list[str], series: list[dict],
                     subtitle: str = "", x_label: str = "", y_label: str = "") -> str:
        """Add a chart to your answer. It's drawn below your text and can be exported.

        Only use this when the user asks for a chart, graph, plot or visual. Never add one
        unprompted. Use numbers from your tool results only. Don't chart a single number. At most
        two charts per answer.

        Args:
            title: Short title, e.g. "Tickets by site, last 30 days".
            chart_type: "hbar" to rank named items (best for clients/sites, long names), "bar" for a few short categories, "line" for a trend over time (labels are dates/weeks/months in order), "stacked_bar" for parts of a whole within each label.
            labels: Category or time labels, in display order (for rankings, largest first). At most 50.
            series: One or more series, each {"name": "Tickets", "values": [31, 18, ...]} with one number per label. Use one series unless comparing groups; at most 8 series. All series share one axis, so only combine numbers in the same unit.
            subtitle: Optional one-line context, e.g. the date range.
            x_label: Optional axis label for the categories.
            y_label: Optional axis label for the values, e.g. "Tickets" or "Hours".
        """
        if not charts_allowed:
            return _dumps({"error": "Chart not added: the user didn't ask for a chart. Answer in text."})
        chart, error = validate_chart({"title": title, "chart_type": chart_type, "labels": labels,
                                       "series": series, "subtitle": subtitle, "x_label": x_label,
                                       "y_label": y_label})
        if error:
            return _dumps({"error": f"Chart not added: {error}"})
        return _dumps({"chart_added": True, "note": "The chart appears below your answer; refer to it "
                           "rather than repeating every value."})

    return [find_company, get_company_tickets, get_ticket_details, get_company_time, get_ticket_totals,
            get_sla_performance, get_after_hours, get_open_tickets, get_go_lives, search_tickets, get_clients_by_software, get_projects,
            get_project_tickets, create_chart]
