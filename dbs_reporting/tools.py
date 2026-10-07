"""Tools Claude can call to read ConnectWise data. All tools are read-only."""

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

import httpx
from anthropic import beta_tool

from . import eastern
from .charts import validate_chart
from .connectwise import ConnectWiseClient
from .spoton import build_spoton_tools

MAX_DAYS = 730
NOTE_CHARS = 1500
MAX_NOTES, FIRST_NOTES, LAST_NOTES = 25, 5, 15  # a ticket's notes sent to Claude (see _key_notes)
TOTALS_LIMIT = 20000


def _dumps(result) -> str:
    """Tool results go to Claude as JSON, with ConnectWise's UTC times turned into Eastern Time.
    No spaces after commas and colons, and letters like ’ and – as themselves rather than \\u2019
    escapes: the same data in about a quarter fewer tokens."""
    text = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
    # A broken character from ConnectWise (a lone surrogate) would stop the request being sent.
    return eastern.localize(text.encode("utf-8", "replace").decode("utf-8"))


def _table(rows: list[dict], columns: tuple[str, ...]) -> dict:
    """A list of records as a table: the field names once ("columns") and each record as its values
    in that order ("rows"), rather than every name repeated on every record, which costs about 40%
    fewer tokens on a long list. Fields blank on every record are left out, and a field with the same
    value on every record is given once, in "every_row"."""
    keep, every_row = [], {}
    for c in columns:
        values = [r.get(c) for r in rows]
        if all(v is None or v == "" for v in values):
            continue
        if len(rows) > 1 and all(v == values[0] for v in values):
            every_row[c] = values[0]
        else:
            keep.append(c)
    table: dict[str, Any] = {"columns": keep, "rows": [[r.get(c) for c in keep] for r in rows]}
    if every_row:
        table["every_row"] = every_row
    return table


def _together(*calls):
    """Run independent ConnectWise lookups at the same time and return their results in order. Each
    one mostly waits on ConnectWise, so together they take as long as the slowest, not the sum."""
    with ThreadPoolExecutor(len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [f.result() for f in futures]


def _nothing():
    return None


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
        # A closed ticket occasionally has no closed date; it's still closed.
        "closed": (ticket.get("closedDate") or True) if ticket.get("closedFlag") else None,
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


TICKET_COLUMNS = ("id", "summary", "entered", "closed", "board", "status", "type", "subtype", "item", "priority",
                  "source", "contact", "hours")
OPEN_COLUMNS = ("id", "summary", "company", "site", "board", "status", "priority", "type", "owner", "resources",
                "entered", "age_days", "days_since_update")
GO_LIVE_COLUMNS = ("date", "weekday", "time", "company", "project", "project_id", "ticket_id", "ticket", "status",
                   "software", "installers", "other_scheduled_days", "past", "tickets_after", "examples_after",
                   "followup_days_so_far")
SEARCH_COLUMNS = ("id", "kind", "summary", "company", "site", "board", "project", "phase", "status", "type",
                  "entered", "closed")


def ticket_breakdown(tickets: list[dict]) -> dict[str, Any]:
    rows = [summarize_ticket(t) for t in tickets]
    result: dict[str, Any] = {"ticket_count": len(rows), "open_count": sum(1 for r in rows if not r["closed"])}
    for name, field, n in (("by_type", "type", 15), ("by_subtype", "subtype", 15), ("by_item", "item", 15),
                           ("by_board", "board", 15), ("by_priority", "priority", 15), ("by_source", "source", 15),
                           ("by_contact", "contact", 10)):
        counts = Counter(r[field] or "(none)" for r in rows)
        if set(counts) != {"(none)"}:  # a field nobody fills in tells Claude nothing
            result[name] = [[k, v] for k, v in counts.most_common(n)]
    # Up to 1000 tickets go to Claude on every step of the answer, so they go as a table.
    result["tickets"] = _table(rows, TICKET_COLUMNS)
    return result


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
        "closed": (t.get("closedDate") or True) if t.get("closedFlag") else None,
        "budget_hours": _hours(t.get("budgetHours")),
        "actual_hours": _hours(t.get("actualHours")),
        "resources": t.get("resources"),
        "priority": _name(t, "priority"),
        "type": _name(t, "type"),
    })


PROJECT_COLUMNS = ("id", "name", "company", "status", "closed", "manager", "type", "board", "estimated_start",
                   "estimated_end", "actual_start", "actual_end", "percent_complete", "budget_hours", "actual_hours",
                   "scheduled_hours", "over_budget_hours")
PROJECT_TICKET_COLUMNS = ("id", "summary", "project", "project_id", "phase", "status", "entered", "closed",
                          "budget_hours", "actual_hours", "resources", "priority", "type")


def _error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return _dumps({
            "error": f"ConnectWise returned HTTP {exc.response.status_code}",
            "detail": exc.response.text[:500],
        })
    return _dumps({"error": f"{type(exc).__name__}: {exc}"})


# Where the earlier messages quoted under an email reply start: Outlook's From/Sent header block,
# "-----Original Message-----", or Gmail's "On <date>, <name> wrote:".
_QUOTED_EMAIL = re.compile(
    r"^[ \t>]*(?:-{2,} ?Original Message ?-{2,}|On [^\n]{4,200} wrote:[ \t]*$"
    r"|(?:From|De|Von): [^\n]+\n(?:[^\n]*\n){0,4}?[ \t>]*(?:Sent|Date|Envoy\u00e9|Gesendet): )",
    re.IGNORECASE | re.MULTILINE)


# Card data sometimes ends up in a ticket note (a client reading out a card to pay for a part). It's taken
# out before a note goes anywhere, so it never leaves the server and David can't repeat it.
_CARD_NUMBER = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_CARD_EXTRAS = re.compile(
    r"\b(?:cvv2?|cvc2?|cid|csc|security\s*code|sec\s*code|3[\s-]?digit\s*code)\b\W{0,6}\d{3,4}\b"
    r"|\b(?:exp(?:iration|iry|ires)?(?:\s*date)?|valid\s*thru)\b\W{0,6}\d{1,2}\s*[/-]\s*\d{2,4}\b",
    re.IGNORECASE)
CARD_REMOVED = "[card details removed]"


def _luhn(digits: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digits)):
        n = int(d) * (2 if i % 2 else 1)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def remove_card_data(text: str) -> str:
    """Card numbers (13-19 digits that pass the card checksum, so ticket and phone numbers stay), and
    CVV codes and expiration dates next to their labels."""
    def number(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group())
        return CARD_REMOVED if _luhn(digits) else m.group()
    text = _CARD_NUMBER.sub(number, text)
    if CARD_REMOVED in text or re.search(r"\b(?:card|visa|master\s*card|amex|discover|cc)\b", text, re.I):
        text = _CARD_EXTRAS.sub(CARD_REMOVED, text)
    return text


def clean_note_text(text: str) -> str:
    """A note's text without the extra blank lines and spaces, or the earlier emails quoted under a
    reply. Each of those emails is usually a note of its own, so the quotes only repeat them, and they
    were often most of a ticket's tokens. Card data is taken out."""
    text = remove_card_data(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", re.sub(r" ?\n ?", "\n", text)).strip()
    for m in _QUOTED_EMAIL.finditer(text):
        if text[:m.start()].strip():  # a forwarded email starts with its own header: keep that one
            return text[:m.start()].rstrip() + "\n[earlier emails in the thread left out]"
    return text


def _note(n: dict) -> dict:
    return {
        "created": n.get("dateCreated"),
        "by": n.get("createdBy"),
        "kind": "resolution" if n.get("resolutionFlag") else "internal" if n.get("internalAnalysisFlag")
        else "description",
        "text": clean_note_text(n.get("text") or "")[:NOTE_CHARS],
    }


def _key_notes(notes: list[dict]) -> tuple[list[dict], int]:
    """A long-running ticket can have hundreds of notes. Keep the first few (the problem), the
    latest (where it stands) and every resolution note; returns (notes, how many were left out)."""
    if len(notes) <= MAX_NOTES:
        return [_note(n) for n in notes], 0
    keep = set(range(FIRST_NOTES)) | set(range(len(notes) - LAST_NOTES, len(notes)))
    keep |= {i for i, n in enumerate(notes) if n.get("resolutionFlag")}
    return [_note(notes[i]) for i in sorted(keep)], len(notes) - len(keep)


def eastern_window(first, days: int) -> tuple[datetime, datetime]:
    """From Eastern midnight on the date `first`, for `days` days, in UTC."""
    zone = eastern.to_eastern(datetime(first.year, first.month, first.day, 12, tzinfo=timezone.utc)).tzinfo
    start = datetime(first.year, first.month, first.day, tzinfo=zone).astimezone(timezone.utc)
    return start, start + timedelta(days=days)


def entry_days(local: datetime, local_end: datetime | None) -> list:
    """The days a schedule entry covers. An entry can run over several days (training Tuesday 8:30 AM to
    Wednesday 5:00 PM), and ConnectWise shows it on each of them. A weekday block that spans a weekend
    skips Saturday and Sunday, as the calendar does; one ending at midnight doesn't include that day."""
    start_day, end_day = local.date(), local_end.date() if local_end else local.date()
    if local_end and local_end.hour == local_end.minute == 0 and end_day > start_day:
        end_day -= timedelta(days=1)
    covered = [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]
    if len(covered) > 1 and start_day.weekday() < 5 and end_day.weekday() < 5:
        covered = [d for d in covered if d.weekday() < 5]
    return covered


# Schedule entries that are time off rather than work, by their kind or title.
TIME_OFF = re.compile(r"vacation|time off|\bpto\b|holiday|sick|out of office|\bday off\b|\bleave\b", re.I)


def member_name(m: dict) -> str:
    """A staff member's full name; either part can be blank in ConnectWise."""
    return f"{m.get('firstName') or ''} {m.get('lastName') or ''}".strip() or m.get("identifier") or ""


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


def build_tools(cw: ConnectWiseClient, charts_allowed: bool = True, store=None) -> list:
    """The tools for one question. With charts_allowed=False, create_chart refuses (its
    definition stays the same, so the prompt cache still matches). With a store, the tools for
    uploaded SpotOn data are included."""

    def software_by_company() -> dict[int, str]:
        """Company id -> the POS software in the company's "Software" custom field. Worked out once per
        copy of the company list (which ConnectWiseClient keeps for an hour)."""
        companies = cw.company_custom_fields()
        cached = getattr(cw, "_software_lookup", None)
        if cached and cached[0] is companies:
            return cached[1]
        caption = _plain(cw.software_field)
        found, captions = {}, Counter()
        for c in companies:
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
        cw._software_lookup = (companies, found)
        return found

    def software_if_readable() -> dict[int, str] | None:
        """The software lookup when it's only an extra (a breakdown, not a filter): None if unreadable."""
        try:
            return software_by_company()
        except Exception:
            return None

    def staff_if_readable() -> list[dict] | None:
        """Everyone on staff, so people with an empty calendar count too; None if it can't be read."""
        try:
            return cw.staff()
        except Exception:
            return None

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
        plus a table of every ticket (id, summary, dates, status, classification; a ticket with no
        "closed" date is open). Use the ticket summaries, not
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
                found, notes = _together(lambda: cw.ticket(ticket_id), lambda: cw.ticket_notes(ticket_id))
                ticket = _compact(summarize_ticket(found))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                # Not a service ticket: project tickets are kept separately.
                found, notes = _together(lambda: cw.project_ticket(ticket_id),
                                         lambda: cw.project_ticket_notes(ticket_id))
                ticket = {"kind": "project ticket", **summarize_project_ticket(found)}
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
            by_software = group_by == "software" or bool(software)
            tickets, lookup = _together(lambda: cw.tickets_since(days, board_name or None, limit=TOTALS_LIMIT),
                                        software_by_company if by_software else _nothing)
            capped = len(tickets) >= TOTALS_LIMIT
            if by_software:
                of = ticket_software(lookup)
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
            by_software = group_by == "software" or bool(software)
            tickets, lookup = _together(
                lambda: cw.tickets_with_times(days, company_id or None, board_name or None, limit=TOTALS_LIMIT),
                software_by_company if by_software else _nothing)
            capped = len(tickets) >= TOTALS_LIMIT
            if by_software:
                keys["software"] = of = ticket_software(lookup)
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
    def get_open_tickets(company_id: int = 0, board_name: str = "", oldest: int = 50, software: str = "",
                         person: str = "") -> str:
        """Service tickets that are still open, however long ago they were entered, oldest first.

        Use for "oldest open tickets", "what's still open at Jimmy's Grille", "stale tickets",
        "open ticket backlog by board/technician", and with person for one technician's tickets ("review
        Mikey's tickets", "what's on Jon's plate"): the tickets they own and the ones they're a resource
        on, plus the open project tickets they're a resource on (project_tickets; project work, such as
        installs, is assigned that way). Unlike get_company_tickets, this isn't limited to
        a date range. Returns the open count, age buckets, median age, counts by status, board,
        priority, owner (and client, when looking at all clients), and the oldest tickets with
        their age in days and days since last update.

        Args:
            company_id: Optional ConnectWise company id from find_company; 0 for all clients.
            board_name: Optional exact service board name, e.g. "Help Desk".
            oldest: How many of the oldest tickets to list (most 200).
            software: Optional: only clients whose POS software matches, e.g. "SkyTab".
            person: Optional: only tickets this person owns or is a resource on (name or username contains this,
                e.g. "Mikey").
        """
        try:
            # The software filter needs the lookup; for all clients it's an extra breakdown, skipped if unreadable.
            tickets, lookup = _together(lambda: cw.open_tickets(company_id or None, board_name or None),
                                        software_by_company if software else
                                        _nothing if company_id else software_if_readable)
            capped = len(tickets) >= 5000
            of = ticket_software(lookup) if lookup else None
            if software:
                tickets = [t for t in tickets if software_matches(of(t), software)]
            if person:
                wanted = _plain(person)
                # Resources are listed by username only, so a name ("Mikey Davitt") is turned into usernames too.
                usernames = {(m.get("identifier") or "").lower() for m in staff_if_readable() or []
                             if wanted in _plain(f"{member_name(m)} {m.get('identifier') or ''}")} - {""}

                def theirs(t: dict) -> bool:
                    owner = t.get("owner") or {}
                    ident = (owner.get("identifier") or "").lower()
                    if wanted in _plain(f"{owner.get('name') or ''} {ident}") or ident in usernames:
                        return True
                    resources = {r.strip().lower() for r in re.split(r"[,;]", t.get("resources") or "") if r.strip()}
                    return any(wanted in _plain(r) or r in usernames for r in resources)

                tickets = [t for t in tickets if wanted and theirs(t)]
                if not usernames and wanted:  # staff list unreadable: go by what was asked, as a username
                    usernames = {person.strip().lower()}
                projects = {}
                for username in sorted(usernames):
                    for p in cw.member_project_tickets(username):
                        projects[p.get("id")] = p
            now = datetime.now(timezone.utc)

            def days_since(value) -> int | None:
                dt = parse_dt(value)
                return (now - dt).days if dt else None

            rows = []
            for t in tickets:
                owner = t.get("owner") or {}
                rows.append({
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
                })
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
                "oldest": _table(rows[:max(1, min(int(oldest), 200))], OPEN_COLUMNS),
            }
            if not company_id:
                result["by_company"] = top("company", 25)
                if of:
                    result["by_software"] = [[k, v] for k, v in Counter(of(t) for t in tickets).most_common(15)]
            if software:
                result["software"] = software
            if person:
                result["person"] = person
                project_rows = [{**summarize_project_ticket(p), "company": _name(p, "company"),
                                 "age_days": days_since(_date_entered(p))} for p in projects.values()]
                project_rows.sort(key=lambda r: -(r.get("age_days") if r.get("age_days") is not None else -1))
                result["project_tickets"] = _table(project_rows, ("id", "summary", "company", "project", "phase",
                                                                  "status", "priority", "resources", "age_days",
                                                                  "budget_hours", "actual_hours"))
                result["open_project_tickets"] = len(project_rows)
                if not rows and not project_rows:
                    result["note"] = f'Nobody matching "{person}" owns or is a resource on an open ticket.'
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

            def project_matches():  # a failure here only leaves project tickets out
                try:
                    return cw.search_project_tickets(phrases, company_id or None)
                except Exception as exc:
                    return exc

            tickets, found, lookup = _together(
                lambda: cw.search_tickets(phrases, days or None, company_id or None),
                project_matches if include_project_tickets else _nothing,
                software_by_company if software else _nothing)
            capped = len(tickets) >= 500
            of = ticket_software(lookup) if software else None
            if of:
                tickets = [t for t in tickets if software_matches(of(t), software)]
            rows = [{
                "id": t.get("id"),
                "kind": "service",
                "summary": t.get("summary"),
                "company": _name(t, "company"),
                "site": _name(t, "site"),
                "board": _name(t, "board"),
                "status": _name(t, "status"),
                "type": _name(t, "type"),
                "entered": _date_entered(t),
                "closed": (t.get("closedDate") or True) if t.get("closedFlag") else None,
            } for t in tickets]
            result = {"searched_for": [" ".join(p) for p in phrases], "match_count": len(rows)}
            if isinstance(found, Exception):
                result["project_ticket_error"] = json.loads(_error(found))["error"]
            elif found is not None:
                if days:
                    start = datetime.now(timezone.utc) - timedelta(days=days)
                    found = [t for t in found if (d := parse_dt(_date_entered(t))) is None or d >= start]
                if of:
                    found = [t for t in found if software_matches(of(t), software)]
                rows += [dict(summarize_project_ticket(t), company=_name(t, "company"), kind="project") for t in found]
                result["project_ticket_matches"] = len(found)
                result["match_count"] = len(rows)
            rows.sort(key=lambda r: r.get("entered") or "", reverse=True)
            result["by_company"] = [[k, v] for k, v in
                                    Counter(r.get("company") or "(none)" for r in rows).most_common(25)]
            result["tickets"] = _table(rows[:max(1, min(int(max_results), 100))], SEARCH_COLUMNS)
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
                result.update(software=software, match_count=len(matches),
                              matches=_table(matches[:500], ("id", "name", "software")))
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
                "projects": _table(projects, PROJECT_COLUMNS),
            })
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_project_tickets(project_id: int = 0, company_id: int = 0, days: int = 0,
                            include_closed: bool = True) -> str:
        """Get project tickets (the tasks within ConnectWise projects) for one project or one client.

        Project tickets are separate from service tickets; use this for questions about project
        work, phases or tasks. Returns counts by project, phase and status, open count, budget vs
        actual hours, and a table of every ticket (id, summary, project, phase, status,
        dates, hours, resources).

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
                "tickets": _table(rows, PROJECT_TICKET_COLUMNS),
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

        Go-lives are the Installation and Live ("Live", "Live Support", "Go-Live") project tickets in the Scheduled status (or
        closed, for past ones): the person scheduled on the ticket is the installer, and the day
        they're scheduled is the date. Open project tickets in other statuses haven't been picked up
        yet and are left out. A site can have both an Installation and a Live ticket, so each
        is listed and "site_count" counts sites (projects). Returns each go-live (date, client, project,
        ticket, installers, status, software), counts by week, installer and software, and Scheduled
        tickets with nobody on the calendar. With followup_days, also counts the support tickets each
        site opened in the days after going live, overall and by installer and software, a sign of
        how well installs went.

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
            # Support tickets for the follow-up count are fetched alongside, from the start of the window
            # (every past go-live is inside it), instead of after the go-lives are worked out.
            tickets, lookup, support = _together(
                lambda: cw.golive_tickets(closed_since, company_id or None),
                software_by_company if software else software_if_readable,  # only the filter needs it
                (lambda: cw.tickets_with_times(_clamp_days(days_back + 1), company_id or None, limit=TOTALS_LIMIT))
                if followup_days and days_back else _nothing)
            # Training tickets (e.g. management training) aren't go-lives.
            tickets = [t for t in tickets
                       if not any(w in (t.get("summary") or "").lower() for w in cw.golive_exclude)]
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
                "from": eastern.day(first_day), "to": eastern.day(last_day),
                "counted": f"project tickets named {' or '.join(' '.join(p) for p in cw.golive_names)}, "
                           f"status {cw.golive_status} (or closed)",
                "go_live_count": len(go_lives),
                "site_count": len({g.get("project_id") or g.get("company") for g in go_lives}),
                "upcoming": sum(1 for g in go_lives if not g.get("past")),
                "past": sum(1 for g in go_lives if g.get("past")),
            }
            weeks = Counter(eastern.day(g["_when"].date() - timedelta(days=g["_when"].weekday())) for g in go_lives)
            result["by_week_starting"] = [[w, n] for w, n in sorted(weeks.items(), key=lambda kv: kv[0][6:] + kv[0][:5])]
            result["by_installer"] = [[k, v] for k, v in
                                      Counter(m for g in go_lives for m in g["installers"]).most_common(20)]
            if of:
                result["by_software"] = [[k, v] for k, v in Counter(g.get("software") for g in go_lives).most_common(15)]

            if followup_days and support is not None:
                past = [g for g in go_lives if g.get("past")]
                if past:
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

            result["go_lives"] = _table(go_lives[:150], GO_LIVE_COLUMNS)
            if len(go_lives) > 150:
                result["note"] = f"Listing the first 150 of {len(go_lives)} go-lives; counts include all of them."
            if days_ahead and unscheduled:
                result["scheduled_but_not_on_calendar"] = _table(unscheduled[:50], GO_LIVE_COLUMNS)
            if software:
                result["software"] = software
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_schedule(person: str = "", start_day: int = 0, days: int = 7) -> str:
        """Everything on staff members' ConnectWise schedules: meetings, 1-on-1s, scheduled tickets and
        project work, appointments and time off, day by day in Eastern Time. For one person, several,
        or everyone at once.

        Use for "what's on Vanessa's schedule", "is Chris free Thursday", "what are Sam and Ana doing
        tomorrow", "everyone's calendar for Friday". Each entry has its time, title, kind (meeting,
        ticket, etc.), whether it's in the office or remote when set, and for tickets the ticket
        number, summary and client. For several people the entries come as one table, by person.
        For how booked people are in total, use get_workload instead.

        Args:
            person: One name or ConnectWise username ("Vanessa"), several separated by commas ("Sam, Ana Ruiz, kchen"), or "everyone" (or blank) for the whole staff.
            start_day: First day to show, counted from today: 0 today, 1 tomorrow, -7 a week ago.
            days: How many days to show (most 31; keep it short for everyone, e.g. 1 to 7).
        """
        try:
            full_name = member_name

            def pick(asked: str, found: list[dict]):
                """The one member meant by `asked`, else the list of candidates (none or several)."""
                if len(found) == 1:
                    return found[0]
                exact = [m for m in found if _plain(full_name(m)) == _plain(asked)
                         or _plain(m.get("firstName") or "") == _plain(asked)]
                return exact[0] if len(exact) == 1 else found

            days = max(1, min(int(days), 31))
            first = eastern.now().date() + timedelta(days=int(start_day))
            last = first + timedelta(days=days - 1)
            start, end = eastern_window(first, days)
            asked = [a.strip() for a in re.split(r",|;|\band\b|&", person or "") if a.strip()]
            everyone = not asked or any(_plain(a) in ("everyone", "everybody", "all", "allstaff", "team", "staff")
                                        for a in asked)
            chosen, not_found, unclear = [], [], {}
            if not everyone:
                for a, found in zip(asked, _together(*[(lambda a=a: cw.find_members(a)) for a in asked])):
                    member = pick(a, found)
                    if isinstance(member, dict):
                        chosen.append(member)
                    elif member:
                        unclear[a] = [_compact({"name": full_name(m), "username": m.get("identifier"),
                                                "title": m.get("title")}) for m in member]
                    else:
                        not_found.append(a)
                if len(asked) == 1 and not chosen:
                    if unclear:
                        return _dumps({"matches": unclear[asked[0]],
                                       "note": "Several people match; ask which one, or call again with the full name."})
                    return _dumps({"error": f'No active staff member matches "{asked[0]}".'})
                if not chosen:
                    return _dumps({"error": "None of those people could be found.", "not_found": not_found,
                                   "unclear": unclear or None})
            staff = None
            if len(chosen) == 1:
                entries = cw.member_schedule(chosen[0]["identifier"], start, end)
            elif everyone:
                # The staff list too, so everyone with an empty calendar shows as free, not missing.
                entries, staff = _together(lambda: cw.schedule_between(start, end), staff_if_readable)
                entries = [e for e in entries
                           if ((e.get("member") or {}).get("identifier") or "").lower() not in cw.staff_exclude]
            else:
                entries = cw.schedule_between(start, end, [m["identifier"] for m in chosen])

            def is_ticket(e: dict) -> bool:
                kind = e.get("type") or {}
                return (kind.get("identifier") or "").upper() in ("S", "P") or "ticket" in (kind.get("name") or "").lower()

            tickets = cw.tickets_by_id([e.get("objectId") for e in entries if is_ticket(e)])
            names = {(m.get("identifier") or "").lower(): full_name(m) for m in chosen + (staff or [])}
            placed: list[tuple] = []  # (person, day, start minute, order, row)
            hours: Counter = Counter()
            for n, e in enumerate(entries):
                begin, finish = parse_dt(e.get("dateStart")), parse_dt(e.get("dateEnd"))
                if not begin:
                    continue
                member = e.get("member") or {}
                who = (names.get((member.get("identifier") or "").lower()) or member.get("name")
                       or member.get("identifier") or "(unknown)")
                hours[who] += float(e.get("hoursScheduled") or 0)
                local = eastern.to_eastern(begin)
                local_end = eastern.to_eastern(finish) if finish and finish > begin else None
                row = {
                    "time": eastern.clock(local) + (f"–{eastern.clock(local_end)}" if local_end else ""),
                    "title": e.get("name"),
                    "kind": (e.get("type") or {}).get("name"),
                    "hours": e.get("hoursScheduled"),
                    "where": (e.get("where") or {}).get("name"),
                    "status": (e.get("status") or {}).get("name"),
                    "done": True if e.get("doneFlag") else None,
                }
                if is_ticket(e) and e.get("objectId"):
                    t = tickets.get(e["objectId"], {})
                    row.update(ticket=f"#{e['objectId']}", summary=t.get("summary"), client=_name(t, "company"),
                               project=(t.get("project") or {}).get("name"))
                # A multi-day entry is listed on each day it covers, at its daily hours, as ConnectWise shows it.
                covered = entry_days(local, local_end)
                if len(covered) > 1:
                    row["spans"] = (f"{covered[0]:%a} {eastern.day(covered[0])} to "
                                    f"{covered[-1]:%a} {eastern.day(covered[-1])}")
                for d in covered:
                    if first <= d <= last:
                        placed.append((who, d, local.hour * 60 + local.minute, n, row))
            placed.sort(key=lambda p: p[1:4])
            period = {"from": eastern.day(first), "to": eastern.day(last)}

            if len(chosen) == 1:  # one person: their days, one after another
                by_day: dict[str, list] = {}
                for _, d, _, _, row in placed:
                    by_day.setdefault(f"{d:%a} {eastern.day(d)}", []).append(_compact(row))
                return _dumps({
                    "person": full_name(chosen[0]), "title": chosen[0].get("title"), **period,
                    "entry_count": len({p[3] for p in placed}),
                    "hours_scheduled": round(sum(hours.values()), 2),
                    "days": [{"day": d, "entries": v} for d, v in by_day.items()],
                    "note": "Days not listed have nothing scheduled.",
                })

            # Several people or everyone: one table, by person, then day and time.
            placed.sort(key=lambda p: (p[0].lower(), p[1], p[2], p[3]))
            limit = 600
            rows = [{"person": who, "day": f"{d:%a} {eastern.day(d)}", **row} for who, d, _, _, row in placed[:limit]]
            result: dict[str, Any] = {
                **period,
                "who": ("everyone on staff" if staff is not None else "everyone with something scheduled")
                if everyone else ", ".join(full_name(m) for m in chosen),
                "people_with_entries": len(hours),
                "entry_count": len({p[3] for p in placed}),
                "hours_by_person": [[k, round(v, 2)] for k, v in sorted(hours.items(), key=lambda kv: kv[0].lower())],
                "schedule": _table(rows, ("person", "day", "time", "title", "kind", "where", "status", "ticket",
                                          "summary", "client", "project", "spans", "hours", "done")),
            }
            if not everyone or staff is not None:
                free = sorted({full_name(m) for m in (chosen or staff) if full_name(m) not in hours}, key=str.lower)
                if free:
                    result["nothing_scheduled"] = free
                if not_found:
                    result["not_found"] = not_found
                if unclear:
                    result["unclear"] = unclear
                    result["note"] = "Some names match several people; ask which one."
            if everyone and staff is None:
                result["note"] = ("The staff list couldn't be read, so people with nothing scheduled in this "
                                  "period aren't listed.")
            if len(placed) > limit:
                result["limit_note"] = f"Showing the first {limit} of {len(placed)} entries; use fewer days or people."
            return _dumps(result)
        except Exception as exc:
            return _error(exc)

    @beta_tool(eager_input_streaming=True)
    def get_workload(start_day: int = 0, days: int = 7, person: str = "", logged_days: int = 7) -> str:
        """Technician workload: who's booked and who has room. For everyone on staff (people with
        nothing booked show at 0%, the most room): hours scheduled in the period against their office hours
        (percent booked), time off, open service tickets they own and open tickets they're a resource
        on (with the oldest one's age), and hours logged over the last logged_days days. To list someone's
        tickets, use get_open_tickets with person.

        Use for "who has room this week", "who's overloaded", "how booked is Sam next week", "who has
        the most open tickets". Office hours are Mon and Wed-Fri 8:30 AM-5:00 PM, Tue 9:00 AM-5:00 PM.
        Scheduled hours include meetings and tickets; vacation, holidays and other time off are counted
        separately and taken off the hours available. Results are a table, most booked first.

        Args:
            start_day: First day of the period, counted from today: 0 today, 1 tomorrow, -7 a week ago.
            days: How many days the period covers (most 31), e.g. 7 for a week.
            person: Optional: only people whose name or username contains this.
            logged_days: How many past days of logged time to add up (0 to skip).
        """
        try:
            days = max(1, min(int(days), 31))
            logged_days = max(0, min(int(logged_days), 90))
            first = eastern.now().date() + timedelta(days=int(start_day))
            last = first + timedelta(days=days - 1)
            start, end = eastern_window(first, days)
            entries, open_tickets, logged, staff = _together(
                lambda: cw.schedule_between(start, end), lambda: cw.open_tickets(),
                (lambda: cw.time_entries_since(logged_days)) if logged_days else (lambda: []), staff_if_readable)

            people: dict[str, dict] = {}

            def person_row(member: dict | None) -> dict | None:
                member = member or {}
                key = (member.get("identifier") or member.get("name") or "").strip().lower()
                if not key:
                    return None
                row = people.setdefault(key, {"name": None, "username": member.get("identifier"), "scheduled": 0.0,
                                              "time_off": 0.0, "owned": 0, "assigned": 0, "oldest": None,
                                              "logged": 0.0})
                row["name"] = row["name"] or member.get("name")
                return row

            # Everyone on staff starts with a row, so people with nothing booked show up (they have the most room).
            for m in staff or []:
                person_row({"identifier": m.get("identifier"), "name": member_name(m)})

            # Hours on the calendar, shared out over the days an entry covers; only days in the period count.
            for e in entries:
                begin, finish = parse_dt(e.get("dateStart")), parse_dt(e.get("dateEnd"))
                row = person_row(e.get("member"))
                if not begin or row is None:
                    continue
                local = eastern.to_eastern(begin)
                local_end = eastern.to_eastern(finish) if finish and finish > begin else None
                covered = entry_days(local, local_end)
                hours = _hours(e.get("hoursScheduled"))
                if hours is None:
                    hours = (finish - begin).total_seconds() / 3600 if finish and finish > begin else 0
                share = hours * sum(1 for d in covered if first <= d <= last) / max(1, len(covered))
                kind = f"{(e.get('type') or {}).get('name') or ''} {e.get('name') or ''}"
                row["time_off" if TIME_OFF.search(kind) else "scheduled"] += share

            # Open service tickets: the owner, and everyone listed as a resource.
            now = datetime.now(timezone.utc)
            for t in open_tickets:
                entered = parse_dt(_date_entered(t))
                age = (now - entered).days if entered else None
                owner = t.get("owner") or {}
                owner_key = (owner.get("identifier") or "").lower()
                involved = set()
                if owner_key:
                    row = person_row(owner)
                    row["owned"] += 1
                    involved.add(owner_key)
                for ident in re.split(r"[,;\s]+", t.get("resources") or ""):
                    if ident and ident.lower() not in involved:
                        involved.add(ident.lower())
                        person_row({"identifier": ident})["assigned"] += 1
                for key in involved:
                    if age is not None and (people[key]["oldest"] is None or age > people[key]["oldest"]):
                        people[key]["oldest"] = age

            for e in logged:
                row = person_row(e.get("member"))
                if row is not None:
                    row["logged"] += float(e.get("actualHours") or 0)

            # Office hours each person could be booked for in the period.
            office = sum((eastern.BUSINESS_HOURS[d.weekday()][1] - eastern.BUSINESS_HOURS[d.weekday()][0]) / 60
                         for d in (first + timedelta(days=i) for i in range(days)) if d.weekday() in eastern.BUSINESS_HOURS)
            rows = []
            for key, p in people.items():
                if key in cw.staff_exclude:
                    continue
                if person and _plain(person) not in _plain(f"{p['name'] or ''} {p['username'] or ''}"):
                    continue
                available = max(0.0, office - p["time_off"])
                rows.append({
                    "name": p["name"] or p["username"], "username": p["username"],
                    "scheduled_hours": round(p["scheduled"], 1), "time_off_hours": round(p["time_off"], 1) or None,
                    "available_hours": round(available, 1),
                    "booked_pct": round(100 * p["scheduled"] / available) if available else None,
                    "open_owned": p["owned"], "open_as_resource": p["assigned"],
                    "oldest_open_days": p["oldest"],
                    "logged_hours": round(p["logged"], 1) if logged_days else None,
                })
            rows.sort(key=lambda r: (-(r["booked_pct"] or 0), -r["open_owned"], r["name"] or ""))
            if person and not rows:
                return _dumps({"error": f'Nobody matching "{person}" has anything scheduled, open or logged.'})
            result = {
                "from": eastern.day(first), "to": eastern.day(last),
                "office_hours_in_period": round(office, 1),
                "people": len(rows),
                "workload": _table(rows, ("name", "username", "scheduled_hours", "time_off_hours", "available_hours",
                                          "booked_pct", "open_owned", "open_as_resource", "oldest_open_days",
                                          "logged_hours")),
                "note": ("booked_pct is scheduled hours over office hours less time off. Open tickets are service "
                         "tickets; project work shows up as scheduled hours. "
                         + (f"logged_hours covers the last {logged_days} days." if logged_days else "")),
            }
            if len(open_tickets) >= 5000:
                result["limit_note"] = "Open tickets capped at 5000; ticket counts may be low."
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
            get_sla_performance, get_after_hours, get_open_tickets, get_go_lives, get_schedule, get_workload, search_tickets, get_clients_by_software, get_projects,
            get_project_tickets, *(build_spoton_tools(store) if store is not None else []), create_chart]
