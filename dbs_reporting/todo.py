"""To-do lists: David turns one person's open ConnectWise work into a short, prioritized list.

What's pulled costs no tokens: the person's open service tickets (as owner or resource), the project
tickets they're a resource on, and their calendar for the next 7 days. Then one Claude request,
with the reply held to a JSON schema, ranks it into Now / Today / This week / Later.

Each David login is matched to a ConnectWise member by display name, or by the ConnectWise username
in the login's line in users.txt when the name isn't enough.
"""

import json
from datetime import datetime, timedelta, timezone

import anthropic

from . import eastern
from . import usage as usage_mod
from .agent import request_options
from .tools import (_compact, _date_entered, _dumps, _name, _plain, _table, _together, eastern_window,
                    entry_days, member_name, parse_dt)

PRIORITIES = ("now", "today", "this_week", "later")
MAX_TICKETS = 120
MAX_ITEMS = 15
TODO_EFFORT = "low"  # ranking a short list doesn't need deep reasoning


class TodoError(Exception):
    """Something the person can act on, e.g. their login isn't linked to ConnectWise."""


def find_member(cw, user: dict) -> dict:
    """The ConnectWise member a David login belongs to."""
    username = (user.get("cw_member") or "").strip()
    if username:
        found = [m for m in cw.find_members(username) if (m.get("identifier") or "").lower() == username.lower()]
        if not found:
            raise TodoError(f'ConnectWise has no active member with the username "{username}" (set for you in '
                            "users.txt). Ask your admin to check the spelling.")
        return found[0]
    name = (user.get("display_name") or "").strip()
    found = cw.find_members(name) if name else []
    exact = [m for m in found if _plain(member_name(m)) == _plain(name)]
    if len(exact) == 1:
        return exact[0]
    if len(found) == 1:
        return found[0]
    if not found and len(name.split()) > 1:  # "Mikey Davitt" is "Michael Davitt" in ConnectWise
        by_last = cw.find_members(name.split()[-1])
        if len(by_last) == 1:
            return by_last[0]
    raise TodoError(f'Couldn\'t tell which ConnectWise member "{name}" is. Ask your admin to add your ConnectWise '
                    "username as the last column of your line in users.txt.")


def gather_work(cw, member: dict, now: datetime | None = None) -> dict:
    """The person's open tickets and the next 7 days of their calendar, compact for Claude."""
    now = now or eastern.now()
    today = now.date()
    ident = member["identifier"]
    start, end = eastern_window(today, 7)
    (service, project), entries = _together(lambda: cw.member_open_tickets(ident),
                                            lambda: cw.member_schedule(ident, start, end))
    utc_now = datetime.now(timezone.utc)

    def days_since(value) -> int | None:
        dt = parse_dt(value)
        return (utc_now - dt).days if dt else None

    rows = []
    for t in service:
        owner = ((t.get("owner") or {}).get("identifier") or "").lower()
        rows.append({
            "id": t.get("id"), "kind": "service", "summary": t.get("summary"), "client": _name(t, "company"),
            "site": _name(t, "site"), "status": _name(t, "status"), "priority": _name(t, "priority"),
            "role": "owner" if owner == ident.lower() else "resource", "board": _name(t, "board"),
            "age_days": days_since(_date_entered(t)),
            "days_since_update": days_since((t.get("_info") or {}).get("lastUpdated")),
        })
    for t in project:
        rows.append({
            "id": t.get("id"), "kind": "project", "summary": t.get("summary"), "client": _name(t, "company"),
            "project": (t.get("project") or {}).get("name"), "phase": _name(t, "phase"),
            "status": _name(t, "status"), "priority": _name(t, "priority"), "role": "resource",
            "age_days": days_since(_date_entered(t)),
        })
    rows.sort(key=lambda r: (r["kind"] != "service", -(r.get("age_days") or 0)))

    calendar = []
    for e in entries:
        begin, finish = parse_dt(e.get("dateStart")), parse_dt(e.get("dateEnd"))
        if not begin:
            continue
        local = eastern.to_eastern(begin)
        local_end = eastern.to_eastern(finish) if finish and finish > begin else None
        kind = e.get("type") or {}
        for d in entry_days(local, local_end):
            if today <= d < today + timedelta(days=7):
                calendar.append({
                    "day": f"{d:%a} {eastern.day(d)}",
                    "time": eastern.clock(local) + (f"–{eastern.clock(local_end)}" if local_end else ""),
                    "title": e.get("name"), "kind": kind.get("name"),
                    "ticket": e.get("objectId") if (kind.get("identifier") or "").upper() in ("S", "P") else None,
                    "where": (e.get("where") or {}).get("name"), "_sort": (d, local.hour * 60 + local.minute),
                })
    calendar.sort(key=lambda c: c.pop("_sort"))
    return _compact({
        "person": member_name(member),
        "now": f"{now:%A} {eastern.day(today)} {eastern.clock(now)} ET",
        "open_service_tickets": len(service),
        "open_project_tickets": len(project),
        "tickets": _table(rows[:MAX_TICKETS], ("id", "kind", "summary", "client", "site", "project", "phase", "status",
                                               "priority", "role", "board", "age_days", "days_since_update")),
        "calendar_next_7_days": _table(calendar, ("day", "time", "title", "kind", "ticket", "where")),
        "note": f"Listing {MAX_TICKETS} of {len(rows)} tickets." if len(rows) > MAX_TICKETS else None,
    })


SYSTEM = """You turn one DBS staff member's open ConnectWise work into a short to-do list for them. \
DBS is a point-of-sale (POS) dealer: it installs and supports POS systems, mostly for restaurants and \
bars, whose busiest times are lunch, dinner and weekends.

You get their open service tickets (as owner or resource), project tickets they're a resource on, and \
their calendar for the next 7 days, as tables ("columns" names the fields once; each row gives the \
values in that order; "every_row" holds fields that are the same on every row).

Rules:
- Use only this data. Every item comes from a ticket or calendar entry in it; never invent tasks, \
times, clients or ticket numbers.
- Sort each item into "now" (do first: urgent or high-priority tickets, anything on the calendar in \
the next few hours, a client waiting on them the longest), "today" (today's calendar, tickets that \
need a response or follow-up today), "this_week" (the rest of this week's calendar and active \
tickets), or "later" (low priority, or waiting on someone else).
- Project tickets in "Open" status haven't been picked up yet and are usually "later"; "Scheduled" \
means work is booked. Project tickets have no "in progress" status.
- A service ticket with no update in 7 or more days needs a follow-up item.
- A calendar entry and the ticket it's for are one item, not two.
- 5 to 15 items, the most important first within each group. Fewer is fine when there's little work.
- title: a short instruction, e.g. "Call Taco Town about the kitchen printer". why: one short sentence \
with the reason from the data (priority, age, status, time). when: the date and time if it's on the \
calendar, e.g. "Tue 10/06/2026 9:00 AM", else null. ticket: the ticket number, else null.
- summary: one or two sentences on their week, e.g. how many tickets are open and the biggest thing.
- Write to the person directly, in the second person: "You have 7 open tickets", "your 2:00 PM \
visit". Never use their name or "they".
- Dates are month/day/year; times are 12-hour Eastern."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "priority": {"type": "string", "enum": list(PRIORITIES)},
                    "ticket": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "client": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "when": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["title", "why", "priority", "ticket", "client", "when"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "items"],
    "additionalProperties": False,
}


def rank(client: anthropic.Anthropic, model: str, work: dict) -> tuple[dict, dict]:
    """One Claude request turning the work into a to-do list. Returns (list, usage)."""
    options = request_options(model, TODO_EFFORT)
    options["output_config"] = {**options.get("output_config", {}), "format": {"type": "json_schema", "schema": SCHEMA}}
    if not work.get("tickets", {}).get("rows") and not work.get("calendar_next_7_days", {}).get("rows"):
        return {"summary": "Nothing open or on your calendar in ConnectWise for the next 7 days.", "items": []}, \
            usage_mod.empty()
    response = client.beta.messages.create(
        model=model, max_tokens=8000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _dumps(work)}],
        **options,
    )
    used = usage_mod.empty()
    usage_mod.add_message(used, response, model)
    if response.stop_reason == "refusal":
        raise TodoError("David couldn't make a list from this data. Try again in a minute.")
    if response.stop_reason == "max_tokens":
        raise TodoError("The list came out too long. Try again.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise TodoError("David's list came back garbled. Try again.") from None
    order = {p: i for i, p in enumerate(PRIORITIES)}
    items = [i for i in data.get("items", []) if isinstance(i, dict) and i.get("title")]
    items.sort(key=lambda i: order.get(i.get("priority"), len(PRIORITIES)))  # stable: keeps Claude's order inside a group
    return {"summary": data.get("summary") or "", "items": items[:MAX_ITEMS]}, used


def make(cw, client: anthropic.Anthropic, model: str, user: dict) -> dict:
    """Everything the To Do tab shows for `user`: who they are in ConnectWise and their ranked list."""
    member = find_member(cw, user)
    work = gather_work(cw, member)
    todo, used = rank(client, model, work)
    return {"member": member_name(member), "username": member.get("identifier"), **todo,
            "counts": {"service": work.get("open_service_tickets", 0), "project": work.get("open_project_tickets", 0)},
            "usage": used}
