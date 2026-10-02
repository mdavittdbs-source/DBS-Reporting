"""Weekly email digest: go-lives coming up, last week's ticket volume and after-hours calls, the
oldest open tickets, and thumbs-down notes on David's answers.

It's built straight from ConnectWise and the chat database, without Claude, so it costs no tokens.
David runs on a laptop, so it isn't sent at a fixed time: whenever the web app is running from
Monday 7 AM Eastern (DIGEST_HOUR) and this week's digest hasn't gone out, it's built and sent.
Starting David on Tuesday still sends that week's digest, once. Each one is also saved in
data/digests/ so it can be opened in a browser.

    python -m dbs_reporting.digest            build it and save it (no email), to preview
    python -m dbs_reporting.digest --send     build it and email it now

Email settings (.env): DIGEST_TO, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, DIGEST_FROM.
"""

import html
import json
import logging
import os
import smtplib
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from . import eastern
from .store import PROJECT_ROOT, Store
from .tools import build_tools

log = logging.getLogger(__name__)
DIGEST_DIR = PROJECT_ROOT / "data" / "digests"
CHECK_EVERY = 15 * 60       # seconds between "is a digest due?" checks while David runs
RETRY_AFTER = 60 * 60       # after a failed send, wait this long before trying again
NAVY, INK, MUTED, LINE, SOFT = "#183e7c", "#1c1b19", "#6f6b63", "#e2dfd8", "#f4f6fa"


@dataclass(frozen=True)
class DigestSettings:
    to: list[str]
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    sender: str
    hour: int  # Eastern hour on Monday from which the week's digest is due

    @classmethod
    def from_env(cls) -> "DigestSettings":
        user = os.environ.get("SMTP_USER", "").strip()
        try:
            port = int(os.environ.get("SMTP_PORT", "") or 587)
        except ValueError:
            port = 587
        try:
            hour = max(0, min(23, int(os.environ.get("DIGEST_HOUR", "") or 7)))
        except ValueError:
            hour = 7
        return cls(
            to=[a.strip() for a in os.environ.get("DIGEST_TO", "").replace(";", ",").split(",") if a.strip()],
            smtp_host=os.environ.get("SMTP_HOST", "").strip(), smtp_port=port, smtp_user=user,
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            sender=os.environ.get("DIGEST_FROM", "").strip() or user, hour=hour,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.to and self.smtp_host and self.sender)


# --- Gathering ------------------------------------------------------------


def _rows(table: dict | None) -> list[dict]:
    """A tool's table back as one dict per row."""
    if not isinstance(table, dict):
        return []
    every_row = table.get("every_row", {})
    return [{**every_row, **dict(zip(table.get("columns", []), row))} for row in table.get("rows", [])]


def gather(cw, store: Store) -> dict:
    """Everything the digest shows. Each section is fetched at the same time; one that fails says so
    in the digest rather than stopping it."""
    tools = {t.name: t for t in build_tools(cw, charts_allowed=False)}
    calls = {
        "go_lives": ("get_go_lives", {"days_ahead": 7}),
        "week": ("get_ticket_totals", {"days": 7, "top": 5}),
        "two_weeks": ("get_ticket_totals", {"days": 14, "top": 1}),
        "after_hours": ("get_after_hours", {"days": 7, "top": 5}),
        "open": ("get_open_tickets", {"oldest": 10}),
    }

    def run(item):
        key, (name, args) = item
        try:
            return key, json.loads(tools[name].call(args))
        except Exception as exc:  # the tools return their own errors; this is a last resort
            return key, {"error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(len(calls)) as pool:
        data = dict(pool.map(run, calls.items()))
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    data["feedback"] = [f for f in store.list_feedback() if f["rating"] < 0 and f["updated_at"] >= since]
    return data


# --- Layout -----------------------------------------------------------------


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _ticket(ticket_id, ticket_url: str) -> str:
    if not ticket_id:
        return ""
    label = f"#{_esc(ticket_id)}"
    if not ticket_url:
        return label
    href = _esc(ticket_url.replace("{id}", str(ticket_id)))
    return f'<a href="{href}" style="color:{NAVY};text-decoration:none">{label}</a>'


def _table(headers: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
    """An email-safe table; cells are already escaped HTML."""
    th = "".join(f'<th style="text-align:{"right" if i in right else "left"};padding:6px 10px;border-bottom:1px solid '
                 f'{LINE};color:{MUTED};font-weight:600;font-size:12px">{_esc(h)}</th>' for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(
        f'<td style="text-align:{"right" if i in right else "left"};padding:6px 10px;border-bottom:1px solid {LINE};'
        f'vertical-align:top">{c}</td>' for i, c in enumerate(r)) + "</tr>" for r in rows)
    return (f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px">'
            f"<tr>{th}</tr>{body}</table>")


def _section(title: str, inner: str, note: str = "") -> str:
    sub = f'<p style="margin:2px 0 10px;color:{MUTED};font-size:13px">{_esc(note)}</p>' if note else ""
    return (f'<tr><td style="padding:22px 28px 6px"><h2 style="margin:0;font-size:17px;color:{INK}">{_esc(title)}</h2>'
            f'{sub}<div style="font-size:14px;line-height:1.5">{inner}</div></td></tr>')


def _stat(label: str, value: str, detail: str = "") -> str:
    extra = f'<div style="color:{MUTED};font-size:12px">{_esc(detail)}</div>' if detail else ""
    return (f'<td style="padding:12px 14px;background:{SOFT};border-radius:10px;width:33%">'
            f'<div style="color:{MUTED};font-size:12px">{_esc(label)}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{INK}">{_esc(value)}</div>{extra}</td>')


def _failed(part: dict) -> str | None:
    return part.get("error") if isinstance(part, dict) else "no data"


def _problem(message: str) -> str:
    return f'<p style="color:#b42318;margin:0">Couldn\'t load this: {_esc(message)}</p>'


def render(data: dict, ticket_url: str, today=None) -> tuple[str, str, str]:
    """(subject, html, plain text) for the digest."""
    today = today or eastern.now().date()
    monday = today - timedelta(days=today.weekday())
    subject = f"David weekly digest: week of {eastern.day(monday)}"
    parts, text = [], [subject, ""]

    # Last week in numbers
    week, two, after = data["week"], data["two_weeks"], data["after_hours"]
    if _failed(week):
        parts.append(_section("Last 7 days", _problem(_failed(week))))
    else:
        count = week.get("ticket_count", 0)
        change = ""
        if not _failed(two):
            before = two.get("ticket_count", 0) - count
            if before:
                pct = round(100 * (count - before) / before)
                change = f"{'+' if pct >= 0 else ''}{pct}% vs the 7 days before ({before})"
        after_txt, after_detail = "–", ""
        if not _failed(after):
            p = after.get("by_period", {})
            after_txt = f"{p.get('after_hours', 0)}"
            after_detail = f"{p.get('after_hours_pct', 0)}% · {p.get('evening', 0)} evening, {p.get('weekend', 0)} weekend"
        still_open = f"{week.get('open_count', 0):,}"
        stats = ('<table cellpadding="0" cellspacing="8" style="width:100%;border-collapse:separate"><tr>'
                 + _stat("Tickets in", f"{count:,}", change) + _stat("Still open from those", still_open)
                 + _stat("After hours", after_txt, after_detail) + "</tr></table>")
        top = [[_esc(g["name"]), f"{g['tickets']:,}", f"{g['open']:,}"] for g in week.get("groups", [])]
        inner = stats + (f'<p style="margin:14px 0 6px;font-weight:600">Busiest clients</p>'
                         + _table(["Client", "Tickets", "Open"], top, {1, 2}) if top else "")
        parts.append(_section("Last 7 days", inner))
        text += [f"Last 7 days: {count} tickets in{f' ({change})' if change else ''}; {after_txt} after hours."]
        text += [f"  {g['name']}: {g['tickets']}" for g in week.get("groups", [])]

    # Go-lives coming up
    go = data["go_lives"]
    if _failed(go):
        parts.append(_section("Go-lives in the next 7 days", _problem(_failed(go))))
    else:
        rows = _rows(go.get("go_lives"))
        if rows:
            inner = _table(["Day", "Client", "Ticket", "Installer"], [[
                _esc(f"{r.get('weekday', '')} {r.get('date', '')} {r.get('time', '')}".strip()),
                _esc(r.get("company")), f"{_ticket(r.get('ticket_id'), ticket_url)} {_esc(r.get('ticket'))}",
                _esc(", ".join(r.get("installers") or [])),
            ] for r in rows])
        else:
            inner = f'<p style="margin:0;color:{MUTED}">None scheduled.</p>'
        waiting = _rows(go.get("scheduled_but_not_on_calendar"))
        if waiting:
            inner += (f'<p style="margin:12px 0 4px;font-weight:600">Marked Scheduled, but nobody is on the calendar</p>'
                      + "<br>".join(f"{_ticket(w.get('ticket_id'), ticket_url)} {_esc(w.get('company'))}: "
                                    f"{_esc(w.get('ticket'))}" for w in waiting[:10]))
        sites = go.get("site_count", len(rows))
        parts.append(_section("Go-lives in the next 7 days", inner, f"{sites} site{'s' if sites != 1 else ''}"))
        text += ["", f"Go-lives in the next 7 days: {sites} sites"]
        text += [f"  {r.get('date')} {r.get('company')} – {', '.join(r.get('installers') or [])}" for r in rows]

    # Oldest open tickets
    op = data["open"]
    if _failed(op):
        parts.append(_section("Oldest open tickets", _problem(_failed(op))))
    else:
        rows = _rows(op.get("oldest"))
        inner = _table(["Ticket", "Client", "Summary", "Days open"], [[
            _ticket(r.get("id"), ticket_url), _esc(r.get("company")), _esc(r.get("summary")),
            _esc(r.get("age_days")),
        ] for r in rows], {3})
        parts.append(_section("Oldest open tickets", inner, f"{op.get('open_count', 0):,} open in all"))
        text += ["", f"Oldest open tickets ({op.get('open_count', 0)} open in all):"]
        text += [f"  #{r.get('id')} {r.get('company')}: {r.get('summary')} ({r.get('age_days')} days)" for r in rows]

    # Feedback
    fb = data["feedback"]
    if fb:
        inner = "".join(
            f'<div style="padding:8px 0;border-bottom:1px solid {LINE}"><div style="color:{MUTED};font-size:12px">'
            f"{_esc(f['display_name'])}</div><div style=\"font-weight:600\">{_esc(f['question'])}</div>"
            + (f"<div>{_esc(f['comment'])}</div>" if f.get("comment") else "") + "</div>" for f in fb[:10])
        parts.append(_section("Thumbs down on David's answers", inner, f"{len(fb)} in the last 7 days"))
        text += ["", f"Thumbs down on David's answers: {len(fb)}"]
        text += [f"  {f['display_name']}: {f['question']}" + (f" – {f['comment']}" if f.get("comment") else "")
                 for f in fb[:10]]

    page = (
        f'<!doctype html><html><body style="margin:0;background:{SOFT};font-family:Segoe UI,Arial,sans-serif;'
        f'color:{INK}"><table cellpadding="0" cellspacing="0" style="width:100%;background:{SOFT}"><tr><td align="center"'
        f' style="padding:24px 12px"><table cellpadding="0" cellspacing="0" style="width:100%;max-width:720px;'
        f'background:#ffffff;border-radius:16px">'
        f'<tr><td style="padding:24px 28px 4px;border-bottom:3px solid {NAVY}"><div style="color:{MUTED};'
        f'font-size:12px;letter-spacing:.06em;text-transform:uppercase">David · weekly digest</div>'
        f'<h1 style="margin:4px 0 14px;font-size:22px">Week of {eastern.day(monday)}</h1></td></tr>'
        + "".join(parts) +
        f'<tr><td style="padding:20px 28px 24px;color:{MUTED};font-size:12px">From live ConnectWise data on '
        f"{_esc(eastern.stamp(datetime.now(timezone.utc)))}. Times are Eastern.</td></tr>"
        "</table></td></tr></table></body></html>")
    return subject, page, "\n".join(text)


# --- Sending ----------------------------------------------------------------


def explain_send_error(exc: Exception, settings: DigestSettings) -> str:
    """What went wrong sending the email, in words someone can act on."""
    where = f"{settings.smtp_host}:{settings.smtp_port}"
    if isinstance(exc, socket.gaierror):
        return (f"Couldn't find the mail server {settings.smtp_host!r}. Check SMTP_HOST in .env (no quotes or "
                "spaces). For Microsoft 365, run  nslookup -type=mx yourcompany.com  and use the name ending in "
                ".mail.protection.outlook.com.")
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionRefusedError)):
        return (f"Couldn't connect to {where}. The network may block that port (port 25 often is); "
                "try from the office network, or ask IT.")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (f"{where} rejected the sign-in for {settings.smtp_user}. Many company mailboxes don't allow "
                "password sign-in for sending; ask IT, or leave SMTP_USER and SMTP_PASSWORD empty to use Direct Send.")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"{where} refused the recipient(s) {', '.join(settings.to)} (Direct Send only reaches your own company)."
    if isinstance(exc, smtplib.SMTPException):
        return f"{where} refused the email: {exc}"
    return f"Couldn't send through {where}: {exc}"


def send_email(settings: DigestSettings, subject: str, html_body: str, text_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, settings.sender, ", ".join(settings.to)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=60)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60)
    with server:
        if settings.smtp_port != 465:
            server.starttls(context=context)
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def build_and_save(cw, store: Store, ticket_url: str) -> tuple[str, str, str, Path]:
    subject, page, text = render(gather(cw, store), ticket_url)
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = DIGEST_DIR / f"digest-{eastern.now():%Y-%m-%d}.html"
    path.write_text(page, encoding="utf-8")
    return subject, page, text, path


def week_key(now=None) -> str:
    """The Monday (Eastern) of the current week, e.g. 2026-10-05."""
    today = (now or eastern.now()).date()
    return (today - timedelta(days=today.weekday())).isoformat()


def is_due(store: Store, hour: int, now=None) -> bool:
    """This week's digest hasn't gone out and it's Monday `hour` o'clock (Eastern) or later."""
    now = now or eastern.now()
    monday = now.date() - timedelta(days=now.weekday())
    starts = datetime(monday.year, monday.month, monday.day, hour, tzinfo=now.tzinfo)
    return now >= starts and store.get_state("digest_week") != week_key(now)


class DigestScheduler:
    """Checks every 15 minutes while the web app runs, and sends the week's digest once when due."""

    def __init__(self, cw, store: Store, ticket_url: str, settings: DigestSettings | None = None):
        self.cw, self.store, self.ticket_url = cw, store, ticket_url
        self.settings = settings or DigestSettings.from_env()
        self._lock = threading.Lock()
        self._failed_at = 0.0
        self._stop = threading.Event()

    def check(self) -> bool:
        """Send the digest if it's due. Returns whether one was sent."""
        if not self.settings.enabled:
            return False
        if self._failed_at and time.monotonic() - self._failed_at < RETRY_AFTER:
            return False
        with self._lock:
            if not is_due(self.store, self.settings.hour):
                return False
            try:
                subject, page, text, path = build_and_save(self.cw, self.store, self.ticket_url)
                send_email(self.settings, subject, page, text)
            except Exception as exc:
                self._failed_at = time.monotonic()
                log.error("Weekly digest not sent (trying again in an hour). %s", explain_send_error(exc, self.settings))
                return False
            self.store.set_state("digest_week", week_key())
            log.warning("Weekly digest sent to %s (saved in %s)", ", ".join(self.settings.to), path)
            return True

    def run(self) -> None:
        self._stop.wait(60)  # let the app finish starting
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                log.exception("Weekly digest check failed")
            self._stop.wait(CHECK_EVERY)

    def start(self) -> None:
        if self.settings.enabled:
            threading.Thread(target=self.run, name="digest", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


if __name__ == "__main__":
    import argparse
    import webbrowser

    from .config import ConnectWiseSettings
    from .connectwise import ConnectWiseClient

    parser = argparse.ArgumentParser(prog="python -m dbs_reporting.digest", description="The weekly email digest.")
    parser.add_argument("--send", action="store_true", help="email it now (to DIGEST_TO) as well as saving it")
    args = parser.parse_args()
    cw_settings = ConnectWiseSettings.from_env()
    store = Store()
    subject, page, text, path = build_and_save(ConnectWiseClient(cw_settings), store, cw_settings.ticket_url)
    print(f"Saved {path}")
    if args.send:
        settings = DigestSettings.from_env()
        if not settings.enabled:
            raise SystemExit("Set DIGEST_TO, SMTP_HOST and SMTP_USER (or DIGEST_FROM) in .env to send it.")
        try:
            send_email(settings, subject, page, text)
        except Exception as exc:
            raise SystemExit(f"Not sent. {explain_send_error(exc, settings)}")
        store.set_state("digest_week", week_key())
        print(f"Sent to {', '.join(settings.to)}")
    else:
        webbrowser.open(path.as_uri())
