"""Reminders as Windows pop-ups even when David isn't open in a tab (Web Push).

A browser that's allowed notifications subscribes at its push service (Google's for Chrome, Microsoft's for
Edge) and gives David the address. When a reminder comes due, David sends it there, signed with this
server's own key (VAPID, made on first run and kept in the database), and the browser shows it through
static/sw.js. The browser has to be running (Chrome and Edge keep running in the background on Windows by
default), and the server needs internet access to reach the push service.

Browsers only allow this at http://localhost or over HTTPS. Needs the pywebpush package; without it, reminders
still pop up while David is open in a tab.
"""

import base64
import json
import logging
import os
import threading

from .store import Store

log = logging.getLogger(__name__)

CHECK_EVERY = 15  # seconds
TTL = 12 * 3600   # how long the push service holds a reminder for a browser that's off


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Push:
    def __init__(self, store: Store):
        self.store = store
        self._vapid = None
        self.public_key: str | None = None
        try:
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            from py_vapid import Vapid02
            import pywebpush  # noqa: F401
        except ImportError:
            log.warning("Reminder pop-ups with David closed are off: run  pip install -r requirements.txt")
            return
        pem = store.get_state("vapid_private_pem")
        if pem is None:
            vapid = Vapid02()
            vapid.generate_keys()
            pem = vapid.private_pem().decode()
            store.set_state("vapid_private_pem", pem)
        self._vapid = Vapid02.from_pem(pem.encode())
        self.public_key = _b64(self._vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))

    @property
    def enabled(self) -> bool:
        return self._vapid is not None

    def send(self, user_id: int, payload: dict) -> int:
        """Send to each of this person's browsers. Returns how many took it; addresses that are gone are dropped."""
        from pywebpush import WebPushException, webpush
        sent = 0
        for _, subscription in self.store.push_subscriptions(user_id):
            try:
                webpush(subscription, json.dumps(payload), vapid_private_key=self._vapid, ttl=TTL, timeout=10,
                        vapid_claims={"sub": os.environ.get("PUSH_CONTACT", "").strip() or "mailto:david@localhost"})
                sent += 1
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):  # unsubscribed, or the browser's gone: stop sending there
                    self.store.remove_push(subscription["endpoint"])
                else:
                    log.warning("Reminder push to %s failed: %s", subscription["endpoint"][:40], exc)
            except Exception as exc:  # e.g. no internet
                log.warning("Reminder push failed: %s", exc)
        return sent

    def remind(self) -> int:
        """Send every reminder that's come due to people with push turned on. Returns how many were sent."""
        sent = 0
        for user_id in sorted({user_id for user_id, _ in self.store.push_subscriptions()}):
            due, _, _ = self.store.check_reminders(user_id)
            sent += self.notify(user_id, due)
        return sent

    def notify(self, user_id: int, items: list[dict]) -> int:
        """Push these reminders to the person's browsers. Returns how many reached at least one."""
        return sum(self.send(user_id, {k: item.get(k) for k in ("id", "title", "ticket", "remind_at", "fired_at")}) > 0
                   for item in items)


class PushScheduler:
    """Checks every CHECK_EVERY seconds while the web app runs."""

    def __init__(self, push: Push):
        self.push = push
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(CHECK_EVERY):
            try:
                self.push.remind()
            except Exception:
                log.exception("Reminder push check failed")

    def start(self) -> None:
        if self.push.enabled:
            threading.Thread(target=self.run, name="push", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
