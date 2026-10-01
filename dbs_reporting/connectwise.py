"""Thin read-only client for the ConnectWise Manage REST API."""

import base64
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from .config import ConnectWiseSettings

PAGE_SIZE = 1000  # ConnectWise maximum
PARALLEL_PAGES = 4  # pages fetched at once after the first, for big results
COMPANY_FIELDS_TTL = 3600  # seconds to reuse the list of companies' custom fields

# What SLA reporting needs from each ticket.
TICKET_SLA_FIELDS = (
    "id,closedFlag,closedDate,dateResponded,dateResolved,isInSla,slaStatus,company/id,company/name,"
    "board/name,priority/name,sla/name,_info/dateEntered"
)

# Just what cross-client totals need; far smaller than full ticket records.
TICKET_SUMMARY_FIELDS = (
    "id,summary,closedFlag,company/id,company/name,site/name,board/name,status/name,"
    "type/name,subType/name,priority/name,source/name"
)

# What open-ticket (aging) reports need.
TICKET_OPEN_FIELDS = (
    "id,summary,closedFlag,company/id,company/name,site/name,board/name,status/name,type/name,"
    "priority/name,owner/identifier,owner/name,resources,_info/dateEntered,_info/lastUpdated"
)

# When each ticket came in and who took it, for after-hours reports.
TICKET_TIMING_FIELDS = (
    "id,summary,closedFlag,company/id,company/name,site/name,board/name,type/name,priority/name,"
    "source/name,owner/identifier,owner/name,_info/dateEntered,_info/enteredBy"
)

# What a single client's ticket list needs (see tools.summarize_ticket).
TICKET_LIST_FIELDS = (
    "id,summary,closedFlag,closedDate,board/name,status/name,type/name,subType/name,item/name,"
    "priority/name,source/name,contactName,actualHours,_info/dateEntered"
)

# What project and project ticket summaries need (see tools.summarize_project*).
PROJECT_FIELDS = (
    "id,name,closedFlag,company/name,status/name,manager/name,manager/identifier,type/name,board/name,"
    "estimatedStart,estimatedEnd,actualStart,actualEnd,percentComplete,budgetHours,actualHours,scheduledHours"
)
PROJECT_TICKET_FIELDS = (
    "id,summary,closedFlag,closedDate,company/id,company/name,project/id,project/name,phase/name,"
    "status/name,budgetHours,actualHours,resources,priority/name,type/name,_info/dateEntered"
)

# What time summaries need. Full time entries include their notes, which can be long.
TIME_FIELDS = "actualHours,member/name,member/identifier,workType/name,chargeToType,chargeToId"

# When people are scheduled on tickets (go-live days are scheduled on the deployment ticket).
SCHEDULE_FIELDS = "id,objectId,type/identifier,type/name,member/identifier,member/name,dateStart,dateEnd,doneFlag"

# What a text search returns for each match.
TICKET_SEARCH_FIELDS = (
    "id,summary,closedFlag,closedDate,company/id,company/name,site/name,board/name,status/name,"
    "type/name,subType/name,priority/name,_info/dateEntered"
)


def cw_date(dt: datetime) -> str:
    """Format a datetime for a ConnectWise `conditions` clause, e.g. [2026-09-01T00:00:00Z]."""
    return "[" + dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "]"


def since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def quote(value: str) -> str:
    """Quote a user-supplied string for use inside a conditions clause."""
    return '"' + value.replace("\\", "").replace('"', "") + '"'


def summary_matches(phrases: list[list[str]]) -> str:
    """A conditions clause matching a summary that contains every word of any one phrase:
    [["handheld"], ["hand", "held"]] -> (summary like "%handheld%" or (summary like "%hand%" and ...))."""
    parts = []
    for words in phrases:
        likes = [f"summary like {quote('%' + w + '%')}" for w in words]
        parts.append(likes[0] if len(likes) == 1 else "(" + " and ".join(likes) + ")")
    return "(" + " or ".join(parts) + ")"


class ConnectWiseClient:
    def __init__(self, settings: ConnectWiseSettings, transport: httpx.BaseTransport | None = None):
        token = base64.b64encode(
            f"{settings.company_id}+{settings.public_key}:{settings.private_key}".encode()
        ).decode()
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers={
                "Authorization": f"Basic {token}",
                "clientId": settings.client_id,
                "Accept": "application/json",
            },
            timeout=60,
            transport=transport,
        )
        # Field lists this ConnectWise server has rejected, so later queries skip straight to
        # full records instead of failing first every time.
        self._rejected_fields: set[str] = set()
        self.software_field = settings.software_field
        self.golive_phase = settings.golive_phase
        self._company_fields: tuple[float, list[dict]] | None = None
        self._company_fields_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, **params: Any) -> Any:
        response = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        response.raise_for_status()
        return response.json()

    def get_all(self, path: str, limit: int = 5000, **params: Any) -> Iterator[dict]:
        """Yield records across pages, stopping after `limit` records. When the first page is
        full, the next few pages are fetched at the same time, which is much faster for big
        results (ConnectWise takes a while per page)."""
        page_size = max(1, min(PAGE_SIZE, limit))

        def fetch(page: int) -> list:
            return self.get(path, page=page, pageSize=page_size, **params)

        batch = fetch(1)
        yield from batch[:limit]
        returned = len(batch)
        if len(batch) < page_size or returned >= limit:
            return
        page = 2
        with ThreadPoolExecutor(PARALLEL_PAGES) as pool:
            while True:
                count = min(PARALLEL_PAGES, -(-(limit - returned) // page_size))
                for batch in pool.map(fetch, range(page, page + count)):
                    yield from batch[:limit - returned]
                    returned += len(batch)
                    if len(batch) < page_size or returned >= limit:
                        return
                page += count

    def _fetch(self, path: str, conditions: str | None, limit: int, fields: str,
               order_by: str = "id desc") -> list[dict]:
        """Records matching `conditions`, asking for just `fields`. Some servers reject nested
        field lists (HTTP 400); then full records are fetched instead, and remembered."""
        params = {"limit": limit, "conditions": conditions, "orderBy": order_by}
        key = f"{path} {fields}"
        if key not in self._rejected_fields:
            try:
                return list(self.get_all(path, fields=fields, **params))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
            records = list(self.get_all(path, **params))
            self._rejected_fields.add(key)  # only once full records worked, so it was the fields
            return records
        return list(self.get_all(path, **params))

    def _tickets(self, conditions: str, limit: int, fields: str) -> list[dict]:
        return self._fetch("/service/tickets", conditions, limit, fields)

    # --- Companies -------------------------------------------------------

    def find_companies(self, name: str, max_results: int = 10) -> list[dict]:
        conditions = f"name like {quote('%' + name + '%')} and deletedFlag=false"
        return self.get(
            "/company/companies",
            conditions=conditions,
            orderBy="name asc",
            pageSize=max_results,
            fields="id,identifier,name,status/name,type/name",
        )

    def company_custom_fields(self) -> list[dict]:
        """Every active company's id, name and custom fields (such as "Software"). Kept for an hour,
        since it's one big list that rarely changes."""
        with self._company_fields_lock:
            if self._company_fields and time.monotonic() - self._company_fields[0] < COMPANY_FIELDS_TTL:
                return self._company_fields[1]
            records = self._fetch("/company/companies", "deletedFlag=false", 20000, "id,name,customFields",
                                  order_by="name asc")
            companies = [{"id": c.get("id"), "name": c.get("name"), "customFields": c.get("customFields") or []}
                         for c in records]
            self._company_fields = (time.monotonic(), companies)
            return companies

    # --- Service tickets -------------------------------------------------

    def tickets_for_company(
        self,
        company_id: int,
        days: int,
        board_name: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        conditions = f"company/id={int(company_id)} and dateEntered>={cw_date(since(days))}"
        if board_name:
            conditions += f" and board/name={quote(board_name)}"
        return self._tickets(conditions, limit, TICKET_LIST_FIELDS)

    def tickets_since(self, days: int, board_name: str | None = None, limit: int = 20000) -> list[dict]:
        """Every client's tickets entered in the last `days` days (up to `limit`)."""
        conditions = f"dateEntered>={cw_date(since(days))}"
        if board_name:
            conditions += f" and board/name={quote(board_name)}"
        return self._tickets(conditions, limit, TICKET_SUMMARY_FIELDS)

    def tickets_with_times(self, days: int, company_id: int | None = None, board_name: str | None = None,
                           limit: int = 20000) -> list[dict]:
        """Tickets entered in the last `days` days, with when they came in and who entered them."""
        conditions = f"dateEntered>={cw_date(since(days))}"
        if company_id:
            conditions += f" and company/id={int(company_id)}"
        if board_name:
            conditions += f" and board/name={quote(board_name)}"
        return self._tickets(conditions, limit, TICKET_TIMING_FIELDS)

    def open_tickets(self, company_id: int | None = None, board_name: str | None = None,
                     limit: int = 5000) -> list[dict]:
        """Service tickets that are still open, however long ago they were entered."""
        conditions = "closedFlag=false"
        if company_id:
            conditions += f" and company/id={int(company_id)}"
        if board_name:
            conditions += f" and board/name={quote(board_name)}"
        return self._tickets(conditions, limit, TICKET_OPEN_FIELDS)

    def search_tickets(self, phrases: list[list[str]], days: int | None = None, company_id: int | None = None,
                       limit: int = 500) -> list[dict]:
        """Service tickets for any client whose summary matches, newest first."""
        conditions = summary_matches(phrases)
        if days:
            conditions += f" and dateEntered>={cw_date(since(days))}"
        if company_id:
            conditions += f" and company/id={int(company_id)}"
        return self._tickets(conditions, limit, TICKET_SEARCH_FIELDS)

    def ticket(self, ticket_id: int) -> dict:
        return self.get(f"/service/tickets/{int(ticket_id)}")

    def ticket_notes(self, ticket_id: int) -> list[dict]:
        return list(self.get_all(f"/service/tickets/{int(ticket_id)}/notes", limit=200, orderBy="id asc"))

    def tickets_for_sla(self, days: int, company_id: int | None = None, board_name: str | None = None,
                        limit: int = 20000) -> list[dict]:
        """Tickets entered in the last `days` days with their SLA flag and response/resolution times."""
        conditions = f"dateEntered>={cw_date(since(days))}"
        if company_id:
            conditions += f" and company/id={int(company_id)}"
        if board_name:
            conditions += f" and board/name={quote(board_name)}"
        return self._tickets(conditions, limit, TICKET_SLA_FIELDS)

    # --- Projects --------------------------------------------------------
    # Project tickets live under /project, separate from service tickets.

    def projects(self, company_id: int | None = None, include_closed: bool = False, limit: int = 500) -> list[dict]:
        conditions = []
        if company_id:
            conditions.append(f"company/id={int(company_id)}")
        if not include_closed:
            conditions.append("closedFlag=false")
        return self._fetch("/project/projects", " and ".join(conditions) or None, limit, PROJECT_FIELDS)

    def project_tickets(self, project_id: int | None = None, company_id: int | None = None,
                        include_closed: bool = True, limit: int = 1000) -> list[dict]:
        """Project tickets for one project or one client's projects, newest first."""
        conditions = []
        if project_id:
            conditions.append(f"project/id={int(project_id)}")
        if company_id:
            conditions.append(f"company/id={int(company_id)}")
        if not include_closed:
            conditions.append("closedFlag=false")
        return self._fetch("/project/tickets", " and ".join(conditions) or None, limit, PROJECT_TICKET_FIELDS)

    def search_project_tickets(self, phrases: list[list[str]], company_id: int | None = None,
                               limit: int = 200) -> list[dict]:
        """Project tickets for any client whose summary matches, newest first."""
        conditions = summary_matches(phrases)
        if company_id:
            conditions += f" and company/id={int(company_id)}"
        return self._fetch("/project/tickets", conditions, limit, PROJECT_TICKET_FIELDS)

    def deployment_tickets(self, closed_since: datetime | None = None, company_id: int | None = None,
                           limit: int = 5000) -> list[dict]:
        """Project tickets whose phase is the go-live phase ("Deployment" unless CW_GOLIVE_PHASE says otherwise):
        open ones, plus ones closed since `closed_since`. If this server won't filter on the phase
        name, project tickets are fetched and filtered here instead."""
        conditions = []
        if closed_since:
            conditions.append(f"(closedFlag=false or closedDate>={cw_date(closed_since)})")
        else:
            conditions.append("closedFlag=false")
        if company_id:
            conditions.append(f"company/id={int(company_id)}")
        phase = f"phase/name like {quote('%' + self.golive_phase + '%')}"
        try:
            return self._fetch("/project/tickets", " and ".join([phase] + conditions), limit, PROJECT_TICKET_FIELDS)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
        wanted = self.golive_phase.lower()
        return [t for t in self._fetch("/project/tickets", " and ".join(conditions), 20000, PROJECT_TICKET_FIELDS)
                if wanted in ((t.get("phase") or {}).get("name") or "").lower()][:limit]

    def schedule_entries(self, ticket_ids: list[int]) -> list[dict]:
        """Everyone scheduled on these tickets, in batches of 100 ids, a few batches at a time."""
        ids = sorted({int(i) for i in ticket_ids})
        batches = [ids[i:i + 100] for i in range(0, len(ids), 100)]

        def fetch(batch: list[int]) -> list[dict]:
            return self._fetch("/schedule/entries", f"objectId in ({','.join(map(str, batch))})", 2000,
                               SCHEDULE_FIELDS)

        with ThreadPoolExecutor(PARALLEL_PAGES) as pool:
            return [e for found in pool.map(fetch, batches) for e in found]

    def project_ticket(self, ticket_id: int) -> dict:
        return self.get(f"/project/tickets/{int(ticket_id)}")

    def project_ticket_notes(self, ticket_id: int) -> list[dict]:
        return list(self.get_all(f"/project/tickets/{int(ticket_id)}/notes", limit=200, orderBy="id asc"))

    # --- Time entries ----------------------------------------------------

    def time_entries_for_company(self, company_id: int, days: int, limit: int = 5000) -> list[dict]:
        conditions = f"company/id={int(company_id)} and timeStart>={cw_date(since(days))}"
        return self._fetch("/time/entries", conditions, limit, TIME_FIELDS)
