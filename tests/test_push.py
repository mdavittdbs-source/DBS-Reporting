"""Reminders as Windows pop-ups with David closed: subscribing, sending, and what a browser receives."""

import base64
import json
from datetime import datetime, timedelta, timezone

import http_ece
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_todo import _list
from test_web import login, web  # noqa: F401  (fixture)

NOW = datetime.now(timezone.utc)
b64 = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()  # noqa: E731


def browser():
    """A browser's push keys: (subscription it would send David, its private key, its auth secret)."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = b"0123456789abcdef"
    return {"endpoint": "https://push.example.com/send/abc", "keys": {"p256dh": b64(public), "auth": b64(auth)}}, private, auth


class Session:
    """Stands in for the push service; keeps what was posted."""
    def __init__(self, status=201):
        self.posts, self.status = [], status

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append((url, data, headers))
        response = type("R", (), {"status_code": self.status, "text": "", "headers": {}, "reason": "",
                                  "url": url})()
        return response


def test_reminders_reach_a_closed_browser(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, items = _list(module, monkeypatch)
    assert bob.get("/api/push/key").json()["key"] == module.push.public_key
    subscription, private, auth = browser()
    assert bob.post("/api/push/subscribe", json=subscription).status_code == 200
    assert bob.post("/api/push/subscribe", json={**subscription, "endpoint": "http://x"}).status_code == 422
    bob.put("/api/todo/reminder", json={"item": items[1]["id"], "at": (NOW - timedelta(seconds=5)).isoformat()})
    bob.put("/api/todo/reminder", json={"item": items[0]["id"], "at": (NOW + timedelta(hours=1)).isoformat()})

    session = Session()
    import pywebpush
    original = pywebpush.webpush
    monkeypatch.setattr(pywebpush, "webpush", lambda *a, **k: original(*a, **k, requests_session=session))
    assert module.push.remind() == 1
    assert module.push.remind() == 0  # once only
    url, body, headers = session.posts[0]
    assert url == subscription["endpoint"] and {k.lower(): v for k, v in headers.items()}["authorization"].startswith("vapid t=")
    message = json.loads(http_ece.decrypt(body, private_key=private, auth_secret=auth, version="aes128gcm"))
    assert message["id"] == items[1]["id"] and message["title"] == items[1]["title"] and message["fired_at"]

    # A page that last asked before the push went off still gets it, to show its card
    got = bob.post("/api/todo/reminders", json={"since": (NOW - timedelta(minutes=1)).isoformat(timespec="seconds")}).json()
    assert [i["id"] for i in got["due"]] == [items[1]["id"]]
    later = (NOW + timedelta(minutes=1)).isoformat(timespec="seconds")  # (same-second repeats: the page skips them)
    assert bob.post("/api/todo/reminders", json={"since": later}).json()["due"] == []

    # A browser that's gone (410) is dropped
    session.status = 410
    bob.put("/api/todo/reminder", json={"item": items[2]["id"], "at": (NOW - timedelta(seconds=5)).isoformat()})
    assert module.push.remind() == 0
    assert module.store.push_subscriptions() == []


def test_signing_out_stops_pushes(web, monkeypatch):  # noqa: F811
    module, _ = web
    bob, _ = _list(module, monkeypatch)
    subscription, _, _ = browser()
    bob.post("/api/push/subscribe", json=subscription)
    alice = login(module, "alice", "password-a")
    alice.post("/api/push/unsubscribe", json={"endpoint": subscription["endpoint"]})  # not hers: no effect
    assert len(module.store.push_subscriptions()) == 1
    alice.post("/api/push/subscribe", json=subscription)  # same browser, now signed in as Alice
    assert [u for u, _ in module.store.push_subscriptions()] == [module.store.authenticate("alice", "password-a")["id"]]
    alice.post("/api/push/unsubscribe", json={"endpoint": subscription["endpoint"]})
    assert module.store.push_subscriptions() == []


def test_service_worker_is_served(web):  # noqa: F811
    module, _ = web
    res = TestClient(module.app).get("/sw.js")
    assert res.status_code == 200 and "showNotification" in res.text and "javascript" in res.headers["content-type"]
