"""Thin read-only client for the ConnectWise Manage REST API."""

import base64
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

from .config import ConnectWiseSettings

PAGE_SIZE = 1000  # ConnectWise maximum

# Just what cross-client totals need; far smaller than full ticket records.
TICKET_SUMMARY_FIELDS = (
    "id,summary,closedFlag,company/id,company/name,site/name,board/name,status/name,"
    "type/name,subType/name,priority/name,source/name"
)


def cw_date(dt: datetime) -> str:
    """Format a datetime for a ConnectWise `conditions` clause, e.g. [2026-09-01T00:00:00Z]."""
    return "[" + dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "]"


def since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def quote(value: str) -> str:
    """Quote a user-supplied string for use inside a conditions clause."""
    return '"' + value.replace("\\", "").replace('"', "") + '"'


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

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, **params: Any) -> Any:
        response = self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        response.raise_for_status()
        return response.json()

    def get_all(self, path: str, limit: int = 5000, **params: Any) -> Iterator[dict]:
        """Yield records across pages, stopping after `limit` records."""
        page = 1
        returned = 0
        while True:
            batch = self.get(path, page=page, pageSize=PAGE_SIZE, **params)
            for record in batch:
                if returned >= limit:
                    return
                returned += 1
                yield record
            if len(batch) < PAGE_SIZE:
                return
            page += 1

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
        try:
            return list(self.get_all("/service/tickets", limit=limit, conditions=conditions,
                                     orderBy="id desc", fields=TICKET_SUMMARY_FIELDS))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            # Some instances reject nested field lists; fall back to full records.
            return list(self.get_all("/service/tickets", limit=limit, conditions=conditions, orderBy="id desc"))

    def ticket(self, ticket_id: int) -> dict:
        return self.get(f"/service/tickets/{int(ticket_id)}")

    def ticket_notes(self, ticket_id: int) -> list[dict]:
        return list(self.get_all(f"/service/tickets/{int(ticket_id)}/notes", limit=200, orderBy="id asc"))

    # --- Time entries ----------------------------------------------------

    def time_entries_for_company(self, company_id: int, days: int, limit: int = 5000) -> list[dict]:
        conditions = f"company/id={int(company_id)} and timeStart>={cw_date(since(days))}"
        return list(self.get_all("/time/entries", limit=limit, conditions=conditions))
