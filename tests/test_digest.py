"""The weekly email digest: what's in it, and that it goes out once a week whenever David is running."""

from datetime import datetime, timedelta, timezone

import pytest
from test_go_lives import client as golive_client

from dbs_reporting import digest, eastern
from dbs_reporting.store import Store

SETTINGS = digest.DigestSettings(to=["me@dbs.com"], smtp_host="smtp.example.com", smtp_port=587,
                                 smtp_user="david@dbs.com", smtp_password="pw", sender="david@dbs.com", hour=7)
URL = "https://cw.example.com/ticket?recid={id}"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "d.db")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """An Eastern time on a weekday of the week of Monday 10/05/2026 (0 = Monday)."""
    utc = datetime(2026, 10, 5 + day, hour + 4, minute, tzinfo=timezone.utc)  # EDT
    return eastern.to_eastern(utc)


def test_digest_content(store):
    data = digest.gather(golive_client([]), store)
    subject, page, text = digest.render(data, URL, today=at(0, 8).date())
    assert subject == "David weekly digest: week of 10/05/2026"
    assert "Go-lives in the next 7 days" in page and "Big Owl&#x27;s" in page and "Ana, Sam" in page
    assert 'href="https://cw.example.com/ticket?recid=501"' in page  # ticket numbers open in ConnectWise
    assert "Marked Scheduled, but nobody is on the calendar" in page and "Burger Barn" in page
    assert "Last 7 days" in page and "Oldest open tickets" in page
    assert "Big Owl's" in text  # a plain-text copy for mail apps that don't show HTML


def test_a_section_that_fails_says_so(store):
    data = {"go_lives": {"error": "ConnectWise returned HTTP 503"}, "week": {"ticket_count": 3, "open_count": 1,
            "groups": []}, "two_weeks": {"ticket_count": 5}, "after_hours": {"error": "x"},
            "open": {"open_count": 0, "oldest": {"columns": [], "rows": []}}, "feedback": []}
    _, page, _ = digest.render(data, "")
    assert "Couldn't load this: ConnectWise returned HTTP 503" in page
    assert "+50% vs the 7 days before (2)" in page


def test_thumbs_down_notes_are_included(store, monkeypatch):
    monkeypatch.setattr(store, "list_feedback", lambda: [
        {"rating": -1, "updated_at": datetime.now(timezone.utc).isoformat(), "display_name": "Bob",
         "question": "Printer issues?", "comment": "Missed two tickets"},
        {"rating": 1, "updated_at": datetime.now(timezone.utc).isoformat(), "display_name": "Ann",
         "question": "Fine", "comment": ""}])
    _, page, _ = digest.render(digest.gather(golive_client([]), store), URL)
    assert "Missed two tickets" in page and "1 in the last 7 days" in page and "Fine" not in page


def test_due_from_monday_morning_once_a_week(store):
    assert not digest.is_due(store, 7, now=at(0, 6, 59))
    assert digest.is_due(store, 7, now=at(0, 7))
    assert digest.is_due(store, 7, now=at(1, 10))  # David first started on Tuesday: still sent
    store.set_state("digest_week", digest.week_key(at(1, 10)))
    assert not digest.is_due(store, 7, now=at(4, 9))
    assert digest.is_due(store, 7, now=at(7, 7, 30))  # next Monday


def test_scheduler_sends_once_and_waits_after_a_failure(store, monkeypatch, tmp_path):
    monkeypatch.setattr(digest, "DIGEST_DIR", tmp_path)
    monkeypatch.setattr(digest, "is_due", lambda s, hour: s.get_state("digest_week") != digest.week_key())
    sent, fail = [], [True]

    def fake_send(settings, subject, page, text):
        if fail[0]:
            raise OSError("mail server unreachable")
        sent.append(subject)

    monkeypatch.setattr(digest, "send_email", fake_send)
    scheduler = digest.DigestScheduler(golive_client([]), store, URL, SETTINGS)
    assert not scheduler.check()
    fail[0] = False
    assert not scheduler.check()  # waits an hour after a failure rather than retrying every check
    scheduler._failed_at -= digest.RETRY_AFTER
    assert scheduler.check() and len(sent) == 1
    assert not scheduler.check() and len(sent) == 1  # once per week
    assert list(tmp_path.glob("digest-*.html"))  # a copy is kept


def test_not_sent_without_email_settings(store):
    off = digest.DigestSettings(to=[], smtp_host="", smtp_port=587, smtp_user="", smtp_password="", sender="", hour=7)
    assert not digest.DigestScheduler(golive_client([]), store, URL, off).check()


def test_email_is_sent_over_tls_with_login(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, msg):
            calls.append(("send", msg["To"], msg["Subject"], msg.get_content_type()))

    monkeypatch.setattr(digest.smtplib, "SMTP", FakeSMTP)
    digest.send_email(SETTINGS, "Digest", "<p>hi</p>", "hi")
    assert calls == [("connect", "smtp.example.com", 587), ("starttls",), ("login", "david@dbs.com"),
                     ("send", "me@dbs.com", "Digest", "multipart/alternative")]


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DIGEST_TO", "a@dbs.com; b@dbs.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.office365.com")
    monkeypatch.setenv("SMTP_USER", "david@dbs.com")
    monkeypatch.delenv("DIGEST_FROM", raising=False)
    s = digest.DigestSettings.from_env()
    assert s.to == ["a@dbs.com", "b@dbs.com"] and s.sender == "david@dbs.com" and s.smtp_port == 587 and s.enabled


def test_send_errors_are_explained():
    import smtplib
    import socket
    assert "Couldn't find the mail server 'smtp.example.com'" in digest.explain_send_error(
        socket.gaierror(11001, "getaddrinfo failed"), SETTINGS)
    assert "port 25 often is" in digest.explain_send_error(TimeoutError(), SETTINGS)
    assert "rejected the sign-in" in digest.explain_send_error(smtplib.SMTPAuthenticationError(535, b"no"), SETTINGS)
