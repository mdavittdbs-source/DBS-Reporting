"""Thin read-only client for the ConnectWise Manage REST API."""

import base64
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from .config import ConnectWiseSettings

PAGE_SIZE = 1000  # ConnectWise maximum
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
        self._company_fields: tuple[float, list[dict]] | None = None
        self._company_fields_lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, **params: Any) -> Any:
        response = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        response.raise_for_status()
        return response.json()

    def get_all(self, path: str, limit: int = 5000, **params: Any) -> Iterator[dict]:
        """Yield records across pages, stopping after `limit` records."""
        page_size = max(1, min(PAGE_SIZE, limit))
        page = 1
        returned = 0
        while returned < limit:
            batch = self.get(path, page=page, pageSize=page_size, **params)
            for record in batch[:limit - returned]:
                yield record
            returned += len(batch)
            if len(batch) < page_size:
                return
            page += 1

    def _tickets(self, conditions: str, limit: int, fields: str) -> list[dict]:
        """Tickets matching `conditions`, asking for just `fields`. Some servers reject nested
        field lists (HTTP 400); then full records are fetched instead, and remembered."""
        params = {"limit": limit, "conditions": conditions, "orderBy": "id desc"}
        if fields not in self._rejected_fields:
            try:
                return list(self.get_all("/service/tickets", fields=fields, **params))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
            records = list(self.get_all("/service/tickets", **params))
            self._rejected_fields.add(fields)  # only once full records worked, so it was the fields
            return records
        return list(self.get_all("/service/tickets", **params))

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
            params = {"limit": 20000, "conditions": "deletedFlag=false", "orderBy": "name asc"}
            fields = "id,name,customFields"
            if fields in self._rejected_fields:
                records = list(self.get_all("/company/companies", **params))
            else:
                try:
                    records = list(self.get_all("/company/companies", fields=fields, **params))
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 400:
                        raise
                    records = list(self.get_all("/company/companies", **params))
                    self._rejected_fields.add(fields)
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
        return list(
            self.get_all("/service/tickets", limit=limit, conditions=conditions, orderBy="id desc")
        )

    def tickets_since(self, days: int, board_name: str | None = None, limit: int = 20000) -> list[dict]:
        """Every client's tickets entered in the last `days` days (up to `limit`)."""
        conditions = f"dateEntered>={cw_date(since(days))}"
        if board_name:
            conditions += f" and board/name={quote(board_name)}"
        return self._tickets(conditions, limit, TICKET_SUMMARY_FIELDS)

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
        return list(self.get_all("/project/projects", limit=limit, conditions=" and ".join(conditions) or None,
                                 orderBy="id desc"))

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
        return list(self.get_all("/project/tickets", limit=limit, conditions=" and ".join(conditions) or None,
                                 orderBy="id desc"))

    def search_project_tickets(self, phrases: list[list[str]], company_id: int | None = None,
                               limit: int = 200) -> list[dict]:
        """Project tickets for any client whose summary matches, newest first."""
        conditions = summary_matches(phrases)
        if company_id:
            conditions += f" and company/id={int(company_id)}"
        return list(self.get_all("/project/tickets", limit=limit, conditions=conditions, orderBy="id desc"))

    def project_ticket(self, ticket_id: int) -> dict:
        return self.get(f"/project/tickets/{int(ticket_id)}")

    def project_ticket_notes(self, ticket_id: int) -> list[dict]:
        return list(self.get_all(f"/project/tickets/{int(ticket_id)}/notes", limit=200, orderBy="id asc"))

    # --- Time entries ----------------------------------------------------

    def time_entries_for_company(self, company_id: int, days: int, limit: int = 5000) -> list[dict]:
        conditions = f"company/id={int(company_id)} and timeStart>={cw_date(since(days))}"
        return list(self.get_all("/time/entries", limit=limit, conditions=conditions))
