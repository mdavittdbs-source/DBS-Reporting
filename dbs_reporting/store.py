"""SQLite storage for user accounts, login sessions and saved chats."""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    model TEXT NOT NULL,
    history TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_by_user ON conversations(user_id, updated_at);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    model TEXT,
    usage TEXT,
    charts TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_by_conversation ON turns(conversation_id, id);
-- Thumbs up / down on answers. A copy of the question and answer is kept, so feedback stays
-- readable even if the chat is deleted later.
CREATE TABLE IF NOT EXISTS feedback (
    turn_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rating INTEGER NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    chat_title TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (turn_id, user_id)
);
CREATE INDEX IF NOT EXISTS feedback_by_time ON feedback(updated_at);
-- Each person's latest to-do list (To Do tab) and which items they've ticked off.
CREATE TABLE IF NOT EXISTS todo_lists (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    done TEXT NOT NULL DEFAULT '[]',
    model TEXT,
    usage TEXT,
    created_at TEXT NOT NULL
);
-- Browsers that get reminders as pop-ups even with David closed (Web Push). The endpoint is the browser's
-- own address at its push service; signing in as someone else in that browser moves it to them.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Small bits of app state, e.g. which week's email digest has been sent.
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Each person's notes (private). After David cleans one up, "original" keeps what they wrote, for Undo,
-- and "actions" the action items he found.
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    original TEXT,
    actions TEXT,
    cleaned_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_by_user ON notes(user_id, updated_at);
-- SpotOn data uploaded from the SpotOn Exporter: one row per CSV file per restaurant. A new upload for a
-- restaurant replaces all of its files. columns and rows are JSON lists.
CREATE TABLE IF NOT EXISTS spoton_files (
    restaurant TEXT NOT NULL COLLATE NOCASE,
    file TEXT NOT NULL,
    columns TEXT NOT NULL,
    rows TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    PRIMARY KEY (restaurant, file)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _todo_id(prefix: str) -> str:
    return prefix + secrets.token_hex(5)


# Uploaded SpotOn data is a snapshot for an audit, so it's deleted this many days after it was uploaded
# (uploading a file again starts the count again).
SPOTON_KEEP_DAYS = int(os.environ.get("SPOTON_KEEP_DAYS") or 30)

REMOVED_DAYS = 30  # how long removed items stay in Removed (and off new lists)
REMOVED_MAX = 60


def _removed(data: dict, made_at: str) -> list[dict]:
    """Items the person removed in the last REMOVED_DAYS, newest first, each with "removed_at". Lists saved by
    an earlier version kept only {ticket, title, at} under "dismissed"; those become plain removed items."""
    stash = data.get("removed", []) + [
        {"id": "x" + hashlib.sha1(f"{e.get('ticket')}|{e.get('title')}|{e.get('at')}".encode()).hexdigest()[:10],
         "title": e.get("title") or f"Ticket #{e.get('ticket')}", "why": "", "priority": "later",
         "ticket": e.get("ticket"), "client": None, "when": None, "removed_at": e.get("at")}
        for e in data.get("dismissed", [])]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=REMOVED_DAYS)).isoformat(timespec="seconds")
    kept, ids = [], set()
    for item in sorted(stash, key=lambda i: i.get("removed_at") or made_at, reverse=True):
        if item["id"] not in ids and (item.get("removed_at") or made_at) >= cutoff:
            kept.append({**item, "removed_at": item.get("removed_at") or made_at})
            ids.add(item["id"])
    return kept[:REMOVED_MAX]


# The list's sections, in order. David's four built-in ones keep their names and their order among themselves
# (Now, Today, This week, Later); people add their own (keys "s-…") anywhere between them, and can rename, move
# and take those out. It all lasts across new lists.
SECTIONS = [{"key": "now", "name": "Now"}, {"key": "today", "name": "Today"},
            {"key": "this_week", "name": "This week"}, {"key": "later", "name": "Later"}]
BUILT_IN = {s["key"] for s in SECTIONS}
SECTION_KEY = re.compile(r"^s-[a-z0-9]{4,12}$")
MAX_SECTIONS = 12


def _sections(data: dict | None) -> list[dict]:
    """The list's sections as the person arranged them (cleaned): their own wherever they put them, and
    David's four with their own names, in their own order, any that's missing put back."""
    saved, seen = [], set()
    for s in (data or {}).get("sections") or []:
        key = s.get("key")
        if key in seen or not (key in BUILT_IN or SECTION_KEY.match(key or "")):
            continue
        seen.add(key)
        saved.append({"key": key, "name": " ".join(str(s.get("name") or "").split())[:40] or "Untitled"})
    for i, s in enumerate(SECTIONS):
        if s["key"] not in seen:
            saved.insert(min(i, len(saved)), dict(s))
    # David's sections fill their places in their own order, whatever order they were sent in.
    davids = iter(SECTIONS)
    saved = [dict(next(davids)) if s["key"] in BUILT_IN else s for s in saved]
    return saved[:MAX_SECTIONS]


REMINDER = ("remind_at", "reminded", "fired_at")  # an item's reminder: when (UTC), whether and when it went off


def _reminder(item: dict | None) -> dict:
    return {k: item[k] for k in REMINDER if item and k in item}


def _with_ids(items: list[dict], done: list) -> tuple[list[dict], list[str]]:
    """Lists saved before items had ids: number David's items d0, d1… and turn ticked positions into ids."""
    if all("id" in item for item in items):
        return items, [i for i in done if isinstance(i, str)]
    items = [item if "id" in item else {**item, "id": f"d{n}"} for n, item in enumerate(items)]
    return items, [items[i]["id"] if isinstance(i, int) and 0 <= i < len(items) else i for i in done
                   if isinstance(i, str) or (isinstance(i, int) and 0 <= i < len(items))]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(candidate.hex(), digest)
    except ValueError:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: str | Path | None = None):
        path = Path(path or os.environ.get("DB_PATH") or PROJECT_ROOT / "data" / "dbs_reporting.db")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._db() as db:
            # Write-ahead logging: reading chats doesn't wait while an answer is being saved.
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
            if "active" not in columns:  # databases created before users.txt support
                db.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            if "cw_member" not in columns:  # databases created before the To Do tab
                db.execute("ALTER TABLE users ADD COLUMN cw_member TEXT NOT NULL DEFAULT ''")
            if "can_upload" not in columns:  # databases created before SpotOn uploads
                db.execute("ALTER TABLE users ADD COLUMN can_upload INTEGER NOT NULL DEFAULT 0")
            turn_columns = {row[1] for row in db.execute("PRAGMA table_info(turns)")}
            if "model" not in turn_columns:  # databases created before per-answer models
                db.execute("ALTER TABLE turns ADD COLUMN model TEXT")
            if "usage" not in turn_columns:  # databases created before usage tracking
                db.execute("ALTER TABLE turns ADD COLUMN usage TEXT")
            if "charts" not in turn_columns:  # databases created before charts
                db.execute("ALTER TABLE turns ADD COLUMN charts TEXT")

    @contextmanager
    def _db(self, write: bool = False):
        """A connection, committed on success. write=True takes the write lock before the first read, so a
        read-then-write (e.g. a tick and a list save arriving together) can't interleave and lose one."""
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    # --- Users -----------------------------------------------------------

    def add_user(self, username: str, password: str, display_name: str = "", is_admin: bool = False) -> int:
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO users (username, display_name, password_hash, is_admin, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (username.strip(), display_name.strip() or username.strip(),
                 hash_password(password), int(is_admin), _now()),
            )
            return cursor.lastrowid

    def set_password(self, username: str, password: str) -> bool:
        with self._db() as db:
            cursor = db.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?", (hash_password(password), username)
            )
            if cursor.rowcount:
                # Sign the user out everywhere.
                db.execute(
                    "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = ?)",
                    (username,),
                )
            return cursor.rowcount > 0

    def remove_user(self, username: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM users WHERE username = ?", (username,)).rowcount > 0

    def sync_users(self, users: list[dict], keep: set[str] = frozenset()) -> None:
        """Make the accounts match a users file: `users` is a list of dicts with username,
        display_name, password_hash and is_admin. Anyone not listed is deactivated (signed out,
        can't sign in) but their chats are kept, so adding them back restores everything.
        Usernames in `keep` (lines with a typo) are left exactly as they are."""
        with self._db(write=True) as db:
            listed = {k.lower() for k in keep}
            for u in users:
                listed.add(u["username"].lower())
                row = db.execute("SELECT id, password_hash FROM users WHERE username = ?",
                                 (u["username"],)).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO users (username, display_name, password_hash, is_admin, active, created_at,"
                        " cw_member, can_upload) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                        (u["username"], u["display_name"], u["password_hash"], int(u["is_admin"]), _now(),
                         u.get("cw_member") or "", int(u.get("can_upload", False))),
                    )
                    continue
                if row["password_hash"] != u["password_hash"]:
                    db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
                db.execute(
                    "UPDATE users SET display_name = ?, password_hash = ?, is_admin = ?, active = 1, cw_member = ?,"
                    " can_upload = ? WHERE id = ?",
                    (u["display_name"], u["password_hash"], int(u["is_admin"]), u.get("cw_member") or "",
                     int(u.get("can_upload", False)), row["id"]),
                )
            for row in db.execute("SELECT id, username FROM users WHERE active = 1").fetchall():
                if row["username"].lower() not in listed:
                    db.execute("UPDATE users SET active = 0 WHERE id = ?", (row["id"],))
                    db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))

    def active_accounts(self) -> list[dict]:
        """Username, display name, hash and admin flag for every active login."""
        with self._db() as db:
            rows = db.execute(
                "SELECT username, display_name, password_hash, is_admin, can_upload, cw_member FROM users"
                " WHERE active = 1"
                " ORDER BY username"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_users(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT u.id, u.username, u.display_name, u.is_admin, u.active, u.created_at,"
                " (SELECT COUNT(*) FROM conversations c WHERE c.user_id = u.id) AS chats"
                " FROM users u ORDER BY u.username"
            ).fetchall()
            return [dict(r) for r in rows]

    def user_count(self) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM users WHERE active = 1").fetchone()[0]

    def authenticate(self, username: str, password: str) -> dict | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM users WHERE username = ? AND active = 1", (username.strip(),)
            ).fetchone()
        if row is None:
            verify_password(password, hash_password("timing-equaliser"))
            return None
        return dict(row) if verify_password(password, row["password_hash"]) else None

    # --- Sessions --------------------------------------------------------

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (_now(),))
            db.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (_token_hash(token), user_id, expires.isoformat(timespec="seconds")),
            )
        return token

    def session_user(self, token: str) -> dict | None:
        with self._db() as db:
            row = db.execute(
                "SELECT u.id, u.username, u.display_name, u.is_admin, u.can_upload, u.cw_member FROM sessions s"
                " JOIN users u ON u.id = s.user_id"
                " WHERE s.token_hash = ? AND s.expires_at > ? AND u.active = 1",
                (_token_hash(token), _now()),
            ).fetchone()
            return dict(row) if row else None

    def delete_session(self, token: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    # --- Conversations ---------------------------------------------------

    def create_conversation(self, user_id: int, title: str, model: str) -> str:
        conversation_id = uuid.uuid4().hex
        now = _now()
        with self._db() as db:
            db.execute(
                "INSERT INTO conversations (id, user_id, title, model, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (conversation_id, user_id, title[:80], model, now, now),
            )
        return conversation_id

    def get_conversation(self, user_id: int, conversation_id: str, with_history: bool = True) -> dict | None:
        """Return the conversation only if it belongs to `user_id`. The saved history holds every
        tool result and can be large, so leave it out (with_history=False) when it isn't needed."""
        columns = "*" if with_history else "id, user_id, title, model, created_at, updated_at"
        with self._db() as db:
            row = db.execute(
                f"SELECT {columns} FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
            ).fetchone()
        if row is None:
            return None
        conversation = dict(row)
        if with_history:
            conversation["history"] = json.loads(conversation["history"])
        return conversation

    def list_conversations(self, user_id: int) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id, title, model, updated_at FROM conversations WHERE user_id = ?"
                " ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def turns(self, conversation_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id, role, text, model, usage, charts, created_at FROM turns WHERE conversation_id = ?"
                " ORDER BY id",
                (conversation_id,),
            ).fetchall()
        turns = [dict(r) for r in rows]
        for turn in turns:
            turn["usage"] = json.loads(turn["usage"]) if turn["usage"] else None
            turn["charts"] = json.loads(turn["charts"]) if turn["charts"] else []
        return turns

    def get_answer(self, user_id: int, turn_id: int) -> dict | None:
        """One answer (with its question, chat title and charts), only if it belongs to `user_id`."""
        with self._db() as db:
            row = db.execute(
                "SELECT a.id, a.text, a.model, a.charts, a.created_at, c.title,"
                " (SELECT q.text FROM turns q WHERE q.conversation_id = a.conversation_id AND q.id < a.id"
                "  ORDER BY q.id DESC LIMIT 1) AS question"
                " FROM turns a JOIN conversations c ON c.id = a.conversation_id"
                " WHERE a.id = ? AND a.role = 'assistant' AND c.user_id = ?",
                (turn_id, user_id),
            ).fetchone()
        if row is None:
            return None
        answer = dict(row)
        answer["charts"] = json.loads(answer["charts"]) if answer["charts"] else []
        return answer

    # --- To-do lists ------------------------------------------------------
    # data holds {member, username, summary, counts, items}; every item has a lasting "id" so ticks, edits and
    # the order people drag things into survive changes to the list. Items people add themselves carry
    # "mine": true. done is the list of ticked item ids.

    def save_todo(self, user_id: int, data: dict, model: str, usage: dict | None) -> None:
        """Replace this person's list with a new one from David. Items they added and haven't ticked carry over."""
        data = {**data, "items": [{**item, "id": _todo_id("d")} for item in data.get("items", [])]}
        with self._db(write=True) as db:
            old = db.execute("SELECT data, done, created_at FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
            if old is not None:
                old_data = json.loads(old["data"])
                items, done = _with_ids(old_data.get("items", []), json.loads(old["done"]))
                # A reminder on one of David's items moves to the same ticket (or to-do) on the new list.
                timed = {(item.get("ticket") or item.get("title")): _reminder(item) for item in items
                         if not item.get("mine") and item.get("remind_at") and item["id"] not in done}
                data["items"] = [{**item, **timed.pop(item.get("ticket") or item.get("title"), {})}
                                 for item in data["items"]]
                data["items"] += [item for item in items if item.get("mine") and item["id"] not in done]
                data["sections"] = _sections(old_data)  # their own sections and names carry over
                # Removed items stay in Removed, except a ticket that's back on the new list (it changed since).
                back = {item.get("ticket") for item in data["items"] if item.get("ticket")}
                data["removed"] = [item for item in _removed(old_data, old["created_at"])
                                   if item.get("mine") or item.get("ticket") not in back]
            db.execute("INSERT INTO todo_lists (user_id, data, done, model, usage, created_at) VALUES (?, ?, '[]', ?, ?, ?)"
                       " ON CONFLICT (user_id) DO UPDATE SET data = excluded.data, done = '[]', model = excluded.model,"
                       " usage = excluded.usage, created_at = excluded.created_at",
                       (user_id, json.dumps(data), model, json.dumps(usage) if usage else None, _now()))

    def todo_dismissed(self, user_id: int) -> list[dict]:
        """David's items this person removed in the last REMOVED_DAYS: [{ticket, title, at}], for the next list
        to leave out (until the ticket changes)."""
        with self._db() as db:
            row = db.execute("SELECT data, created_at FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return []
        return [{"ticket": item.get("ticket"), "title": item.get("title") or "", "at": item["removed_at"]}
                for item in _removed(json.loads(row["data"]), row["created_at"]) if not item.get("mine")]

    def get_todo(self, user_id: int) -> dict | None:
        """{data, done (ticked item ids), removed (newest first), model, usage, created_at}, or None before the
        first list."""
        with self._db() as db:
            row = db.execute("SELECT * FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        data = json.loads(row["data"])
        data["items"], done = _with_ids(data.get("items", []), json.loads(row["done"]))
        removed = [item for item in _removed(data, row["created_at"]) if not item.get("deleted")]
        data.pop("removed", None), data.pop("dismissed", None)
        data["sections"] = _sections(data)
        return {"data": data, "done": done, "removed": removed, "model": row["model"],
                "usage": json.loads(row["usage"]) if row["usage"] else None, "created_at": row["created_at"]}

    def set_todo_done(self, user_id: int, item_id: str, done: bool) -> bool:
        """Tick or untick one item. False if there's no list or no such item."""
        with self._db(write=True) as db:
            row = db.execute("SELECT data, done FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                return False
            data = json.loads(row["data"])
            data["items"], ticked = _with_ids(data.get("items", []), json.loads(row["done"]))
            if not any(item["id"] == item_id for item in data["items"]):
                return False
            ticked = [i for i in ticked if i != item_id] + ([item_id] if done else [])
            db.execute("UPDATE todo_lists SET data = ?, done = ? WHERE user_id = ?",
                       (json.dumps(data), json.dumps(ticked), user_id))
            return True

    def set_todo_items(self, user_id: int, items: list[dict], remove: list[str] = ()) -> list[dict] | None:
        """Save the list as the person arranged it: their order and groups, their own items added or edited, and
        the items in `remove` taken off. David's items can be edited too (title, details, ticket); a page that
        sends one without a title (e.g. putting it back) leaves his wording as it was.
        An item that's on the list but not in `items` stays (at the end of its group): the page that sent
        this may not have seen it yet, e.g. one added just before a reload or in another tab. Returns the
        saved items, or None if there's no list."""
        with self._db(write=True) as db:
            row = db.execute("SELECT data, done, created_at FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            stored, ticked = _with_ids(data.get("items", []), json.loads(row["done"]))
            # Removed items are kept aside (Removed on the page), so one can be put back, or a removal undone
            # after it saved. Only David's own wording comes back for his items.
            removed = _removed(data, row["created_at"])
            davids = {item["id"]: item for item in removed + stored if not item.get("mine")}
            known = {item["id"]: item for item in removed + stored}
            keys = {s["key"] for s in _sections(data)}
            saved, seen = [], set()
            for item in items:
                if item["id"] in seen:
                    continue
                section = item["priority"] if item["priority"] in keys else "later"
                if item.get("mine"):
                    saved.append({"id": item["id"], "title": item["title"], "why": item.get("why") or "",
                                  "priority": section, "ticket": item.get("ticket"), "client": None,
                                  "when": None, "mine": True, **_reminder(known.get(item["id"]))})
                elif item["id"] in davids:
                    edits = ({"title": item["title"], "why": item.get("why") or "", "ticket": item.get("ticket")}
                             if item.get("title") else {})
                    saved.append({k: v for k, v in {**davids[item["id"]], **edits, "priority": section}.items()
                                  if k not in ("removed_at", "deleted")})
                else:
                    continue  # not one of David's, and not marked as theirs: ignore it
                seen.add(item["id"])
            remove = set(remove) - seen
            saved += [item for item in stored if item["id"] not in seen and item["id"] not in remove]
            seen |= {item["id"] for item in saved}
            data["items"] = saved
            gone = [{**item, "removed_at": _now()} for item in stored if item["id"] not in seen]
            data["removed"] = (gone + [item for item in removed if item["id"] not in seen])[:REMOVED_MAX]
            data.pop("dismissed", None)  # now part of "removed"
            db.execute("UPDATE todo_lists SET data = ?, done = ? WHERE user_id = ?",
                       (json.dumps(data), json.dumps([i for i in ticked if i in seen]), user_id))
            return saved

    def set_todo_sections(self, user_id: int, sections: list[dict]) -> list[dict] | None:
        """Save the list's sections (order and names; new ones added, own ones taken out). Items in a section
        that's gone move to Later. Returns the sections, or None if there's no list."""
        with self._db(write=True) as db:
            row = db.execute("SELECT data FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            data["sections"] = _sections({"sections": sections})
            keys = {s["key"] for s in data["sections"]}
            for item in data.get("items", []) + data.get("removed", []):
                if item.get("priority") not in keys:
                    item["priority"] = "later"
            db.execute("UPDATE todo_lists SET data = ? WHERE user_id = ?", (json.dumps(data), user_id))
            return data["sections"]

    def delete_removed(self, user_id: int, ids: list[str] | None, undo: bool = False) -> list[dict] | None:
        """Delete items from Removed (all of them when `ids` is None), or bring them back with undo=True.
        Deleted tickets still stay off new lists until they change or REMOVED_DAYS pass; they just aren't
        listed. Returns what Removed now shows, or None if there's no list."""
        with self._db(write=True) as db:
            row = db.execute("SELECT data, created_at FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            removed = _removed(data, row["created_at"])
            pick = set(ids) if ids is not None else {item["id"] for item in removed}
            data["removed"] = [{**item, "deleted": not undo} if item["id"] in pick else item for item in removed]
            data.pop("dismissed", None)
            db.execute("UPDATE todo_lists SET data = ? WHERE user_id = ?", (json.dumps(data), user_id))
            return [item for item in data["removed"] if not item.get("deleted")]

    def set_todo_reminder(self, user_id: int, item_id: str, at: str | None) -> dict | None:
        """Set (UTC ISO time) or clear (None) the reminder on one item. Returns the item, or None if there's no
        list or no such item."""
        with self._db(write=True) as db:
            row = db.execute("SELECT data, done FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            data["items"], ticked = _with_ids(data.get("items", []), json.loads(row["done"]))
            item = next((i for i in data["items"] if i["id"] == item_id), None)
            if item is None:
                return None
            for key in REMINDER:
                item.pop(key, None)
            if at:
                item.update(remind_at=at, reminded=False)
            db.execute("UPDATE todo_lists SET data = ?, done = ? WHERE user_id = ?",
                       (json.dumps(data), json.dumps(ticked), user_id))
            return item

    def take_due_reminders(self, user_id: int, since: str | None = None) -> tuple[list[dict], str | None]:
        """check_reminders, with what went off now and since together."""
        fresh, recent, upcoming = self.check_reminders(user_id, since)
        return recent + fresh, upcoming

    def check_reminders(self, user_id: int, since: str | None = None) -> tuple[list[dict], list[dict], str | None]:
        """(the reminders due now on items still to do, marked as gone off with "fired_at" so each goes off once;
        those that went off at or after `since`, e.g. sent as a push to a page that asked before then; when the
        next one is due, or None)."""
        now = _now()
        # Pages ask every half minute and the push check every 15 seconds, and almost always nothing is due:
        # look first, and take the write lock only to mark one as gone off (looking again under the lock, so
        # two checks at once can't both fire it).
        for write in (False, True):
            with self._db(write=write) as db:
                row = db.execute("SELECT data, done FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
                if row is None:
                    return [], [], None
                data = json.loads(row["data"])
                data["items"], ticked = _with_ids(data.get("items", []), json.loads(row["done"]))
                waiting = [i for i in data["items"]
                           if i.get("remind_at") and not i.get("reminded") and i["id"] not in ticked]
                due = [i for i in waiting if i["remind_at"] <= now]
                later = [i["remind_at"] for i in waiting if i["remind_at"] > now]
                recent = [i for i in data["items"] if since and i.get("reminded")
                          and (i.get("fired_at") or "") >= since and i["id"] not in ticked]
                if due and write:
                    for item in due:
                        item.update(reminded=True, fired_at=now)
                    db.execute("UPDATE todo_lists SET data = ?, done = ? WHERE user_id = ?",
                               (json.dumps(data), json.dumps(ticked), user_id))
                if not due or write:
                    return due, recent, min(later, default=None)
        raise AssertionError("unreachable")

    # --- Push subscriptions ----------------------------------------------------

    def add_push(self, user_id: int, subscription: dict) -> None:
        with self._db() as db:
            db.execute("INSERT INTO push_subscriptions (endpoint, user_id, data, created_at) VALUES (?, ?, ?, ?)"
                       " ON CONFLICT (endpoint) DO UPDATE SET user_id = excluded.user_id, data = excluded.data",
                       (subscription["endpoint"], user_id, json.dumps(subscription), _now()))

    def remove_push(self, endpoint: str, user_id: int | None = None) -> None:
        with self._db() as db:
            if user_id is None:
                db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
            else:
                db.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?", (endpoint, user_id))

    def push_subscriptions(self, user_id: int | None = None) -> list[tuple[int, dict]]:
        """[(user id, subscription)], for one person or everyone."""
        with self._db() as db:
            rows = db.execute("SELECT user_id, data FROM push_subscriptions"
                              + (" WHERE user_id = ?" if user_id is not None else ""),
                              () if user_id is None else (user_id,)).fetchall()
        return [(row["user_id"], json.loads(row["data"])) for row in rows]

    # --- Notes -------------------------------------------------------------

    @staticmethod
    def _note(row) -> dict:
        note = dict(row)
        note["actions"] = json.loads(note["actions"]) if note.get("actions") else []
        note.pop("user_id", None)
        return note

    def list_notes(self, user_id: int) -> list[dict]:
        """This person's notes, newest first, with the start of each as a preview."""
        with self._db() as db:
            rows = db.execute("SELECT id, title, label, substr(body, 1, 160) AS preview, cleaned_at, created_at,"
                              " updated_at FROM notes WHERE user_id = ? ORDER BY updated_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_note(self, user_id: int, note_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)).fetchone()
        return self._note(row) if row else None

    def save_note(self, user_id: int, note_id: str, title: str, label: str, body: str) -> dict | None:
        """Create or update a note. None if that id is someone else's."""
        now = _now()
        with self._db(write=True) as db:
            row = db.execute("SELECT user_id FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO notes (id, user_id, title, label, body, created_at, updated_at)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?)", (note_id, user_id, title, label, body, now, now))
            elif row["user_id"] != user_id:
                return None
            else:
                db.execute("UPDATE notes SET title = ?, label = ?, body = ?, updated_at = ? WHERE id = ?",
                           (title, label, body, now, note_id))
            return self._note(db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())

    def save_cleanup(self, user_id: int, note_id: str, title: str, body: str, actions: list[dict]) -> dict | None:
        """Put David's cleaned-up version in place, keeping what the person wrote (for Undo)."""
        now = _now()
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)).fetchone()
            if row is None:
                return None
            original = row["original"] if row["original"] is not None else json.dumps(
                {"title": row["title"], "body": row["body"]})
            db.execute("UPDATE notes SET title = ?, body = ?, original = ?, actions = ?, cleaned_at = ?, updated_at = ?"
                       " WHERE id = ?", (title, body, original, json.dumps(actions), now, now, note_id))
            return self._note(db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())

    def undo_cleanup(self, user_id: int, note_id: str) -> dict | None:
        """Back to what the person wrote before David cleaned it up."""
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)).fetchone()
            if row is None or row["original"] is None:
                return None
            original = json.loads(row["original"])
            db.execute("UPDATE notes SET title = ?, body = ?, original = NULL, actions = NULL, cleaned_at = NULL,"
                       " updated_at = ? WHERE id = ?", (original["title"], original["body"], _now(), note_id))
            return self._note(db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())

    def actions_to_todo(self, user_id: int, note_id: str, picks: list[int] | None, member: str = "") -> dict | None:
        """Add a note's action items (all, or the ones at `picks`) to the person's To Do list, once each: they're
        marked "added". Returns the note, or None if there's no such note."""
        # One transaction, so two clicks at once can't add the same item twice.
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)).fetchone()
            if row is None:
                return None
            note = self._note(row)
            actions = note["actions"]
            chosen = sorted({i for i in (range(len(actions)) if picks is None else picks)
                             if 0 <= i < len(actions) and not actions[i].get("added")})
            if chosen:
                label = note["label"] or note["title"]
                self._add_todo_items(db, user_id, [{"title": actions[i]["title"],
                                                    "priority": actions[i].get("priority") or "this_week",
                                                    "why": f"From your note: {label}" if label else "From your notes"}
                                                   for i in chosen], member)
                for i in chosen:
                    actions[i]["added"] = True
                db.execute("UPDATE notes SET actions = ? WHERE id = ? AND user_id = ?",
                           (json.dumps(actions), note_id, user_id))
        return {**note, "actions": actions}

    def delete_note(self, user_id: int, note_id: str) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id)).rowcount > 0

    def add_todo_items(self, user_id: int, items: list[dict], member: str = "") -> list[dict]:
        """Add items of the person's own to their To Do list (in their groups), starting a list if they have none.
        Returns the items added."""
        with self._db(write=True) as db:
            return self._add_todo_items(db, user_id, items, member)

    @staticmethod
    def _add_todo_items(db, user_id: int, items: list[dict], member: str) -> list[dict]:
        added = [{"id": _todo_id("m-"), "title": i["title"][:200], "why": (i.get("why") or "")[:500],
                  "priority": i.get("priority") or "today", "ticket": None, "client": None, "when": None, "mine": True}
                 for i in items]
        row = db.execute("SELECT data FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            data = {"member": member, "username": "", "summary": "", "counts": {}, "items": added}
            db.execute("INSERT INTO todo_lists (user_id, data, done, created_at) VALUES (?, ?, '[]', ?)",
                       (user_id, json.dumps(data), _now()))
        else:
            data = json.loads(row["data"])
            data["items"] = data.get("items", []) + added
            db.execute("UPDATE todo_lists SET data = ? WHERE user_id = ?", (json.dumps(data), user_id))
        return added

    # --- App state ---------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._db() as db:
            db.execute("INSERT INTO app_state (key, value) VALUES (?, ?)"
                       " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    # --- Feedback ----------------------------------------------------------

    def set_feedback(self, user_id: int, turn_id: int, rating: int, comment: str = "") -> bool:
        """Record (or with rating 0, remove) a user's thumbs up (1) / down (-1) on one of their own
        answers. Returns False if the answer isn't theirs."""
        answer = self.get_answer(user_id, turn_id)
        if answer is None:
            return False
        now = _now()
        with self._db() as db:
            if rating == 0:
                db.execute("DELETE FROM feedback WHERE turn_id = ? AND user_id = ?", (turn_id, user_id))
                return True
            db.execute(
                "INSERT INTO feedback (turn_id, user_id, rating, comment, question, answer, chat_title, model,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (turn_id, user_id) DO UPDATE SET rating = excluded.rating,"
                " comment = excluded.comment, updated_at = excluded.updated_at",
                (turn_id, user_id, rating, comment.strip()[:2000], answer.get("question") or "", answer["text"],
                 answer.get("title") or "", answer.get("model"), now, now),
            )
        return True

    def feedback_in_chat(self, user_id: int, conversation_id: str) -> dict[int, dict]:
        """This user's ratings on the answers in one chat: {turn id: {"rating", "comment"}}."""
        with self._db() as db:
            rows = db.execute(
                "SELECT f.turn_id, f.rating, f.comment FROM feedback f JOIN turns t ON t.id = f.turn_id"
                " WHERE f.user_id = ? AND t.conversation_id = ?", (user_id, conversation_id)).fetchall()
        return {r["turn_id"]: {"rating": r["rating"], "comment": r["comment"]} for r in rows}

    def list_feedback(self, limit: int = 500) -> list[dict]:
        """Everyone's feedback, newest first, with who gave it."""
        with self._db() as db:
            rows = db.execute(
                "SELECT f.turn_id AS answer_id, f.rating, f.comment, f.question, f.answer, f.chat_title, f.model,"
                " f.created_at, f.updated_at, u.username, u.display_name FROM feedback f"
                " JOIN users u ON u.id = f.user_id ORDER BY f.updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def feedback_count(self, rating: int, days: int) -> int:
        """How many ratings of this kind (1 or -1) were given or changed in the last `days` days."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM feedback WHERE rating = ? AND updated_at >= ?",
                              (rating, since)).fetchone()[0]

    def save_turn(
        self, conversation_id: str, question: str, answer: str, history: list, model: str | None = None,
        usage: dict | None = None, charts: list | None = None,
    ) -> int:
        """Record a question and answer. `model` is the model that answered; it also becomes the
        chat's current model, so the next question defaults to it. Returns the answer's id."""
        now = _now()
        with self._db() as db:
            updated = db.execute(
                "UPDATE conversations SET history = ?, updated_at = ?, model = COALESCE(?, model) WHERE id = ?",
                (json.dumps(history), now, model, conversation_id),
            ).rowcount
            if not updated:
                raise ValueError("This chat was deleted while David was answering, so the answer wasn't saved.")
            db.execute(
                "INSERT INTO turns (conversation_id, role, text, created_at) VALUES (?, 'user', ?, ?)",
                (conversation_id, question, now),
            )
            answer_id = db.execute(
                "INSERT INTO turns (conversation_id, role, text, model, usage, charts, created_at)"
                " VALUES (?, 'assistant', ?, ?, ?, ?, ?)",
                (conversation_id, answer, model, json.dumps(usage) if usage else None,
                 json.dumps(charts) if charts else None, now),
            ).lastrowid
        return answer_id

    def answered_turns(self, days: int) -> list[dict]:
        """Every answer in the last `days` days, oldest first, with who asked, the question,
        the chat title, the model and usage (None if not recorded)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._db() as db:
            rows = db.execute(
                "SELECT a.created_at, a.model, a.usage, a.text, u.username, u.display_name, c.title,"
                " (SELECT q.text FROM turns q WHERE q.conversation_id = a.conversation_id AND q.id < a.id"
                "  ORDER BY q.id DESC LIMIT 1) AS question"
                " FROM turns a JOIN conversations c ON c.id = a.conversation_id JOIN users u ON u.id = c.user_id"
                " WHERE a.role = 'assistant' AND a.created_at >= ? ORDER BY a.id",
                (since,),
            ).fetchall()
        result = [dict(r) for r in rows]
        for row in result:
            row["usage"] = json.loads(row["usage"]) if row["usage"] else None
        return result

    def usage_rows(self, days: int) -> list[dict]:
        """Answers in the last `days` days that have recorded usage."""
        return [row for row in self.answered_turns(days) if row["usage"]]

    def rename_conversation(self, user_id: int, conversation_id: str, title: str) -> bool:
        with self._db() as db:
            return db.execute(
                "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
                (title.strip()[:80], conversation_id, user_id),
            ).rowcount > 0

    def delete_conversation(self, user_id: int, conversation_id: str) -> bool:
        with self._db() as db:
            return db.execute(
                "DELETE FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
            ).rowcount > 0

    # --- SpotOn data -----------------------------------------------------

    def save_spoton(self, restaurant: str, files: dict[str, tuple[list, list]], uploaded_by: str,
                    replace_all: bool = True) -> None:
        """Save `files` ({file name: (columns, rows)}) for `restaurant`. replace_all (a full exporter zip) clears
        what was there first; otherwise only files with the same names are replaced and the rest stay."""
        now = _now()
        with self._db(write=True) as db:
            if replace_all:
                db.execute("DELETE FROM spoton_files WHERE restaurant = ?", (restaurant,))
            else:
                db.executemany("DELETE FROM spoton_files WHERE restaurant = ? AND file = ?",
                               [(restaurant, name) for name in files])
            for name, (columns, rows) in files.items():
                db.execute(
                    "INSERT INTO spoton_files (restaurant, file, columns, rows, row_count, uploaded_by, uploaded_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (restaurant, name, json.dumps(columns), json.dumps(rows), len(rows), uploaded_by, now),
                )

    def _drop_old_spoton(self, db) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=SPOTON_KEEP_DAYS)).isoformat(timespec="seconds")
        db.execute("DELETE FROM spoton_files WHERE uploaded_at < ?", (cutoff,))

    def spoton_files(self) -> list[dict]:
        """Every uploaded file (no rows): restaurant, file, columns, row_count, uploaded_by, uploaded_at. Files
        older than SPOTON_KEEP_DAYS are deleted first."""
        with self._db() as db:
            self._drop_old_spoton(db)
            rows = db.execute(
                "SELECT restaurant, file, columns, row_count, uploaded_by, uploaded_at FROM spoton_files"
                " ORDER BY restaurant, file"
            ).fetchall()
        return [{**dict(r), "columns": json.loads(r["columns"])} for r in rows]

    def spoton_rows(self, restaurant: str, file: str) -> tuple[list, list] | None:
        """(columns, rows) of one uploaded file, or None (also once it's older than SPOTON_KEEP_DAYS)."""
        with self._db() as db:
            self._drop_old_spoton(db)
            row = db.execute("SELECT columns, rows FROM spoton_files WHERE restaurant = ? AND file = ?",
                             (restaurant, file)).fetchone()
        return (json.loads(row["columns"]), json.loads(row["rows"])) if row else None

    def delete_spoton(self, restaurant: str, file: str | None = None) -> bool:
        """Delete a restaurant's data, or just one of its files. False if there was nothing to delete."""
        with self._db() as db:
            if file is None:
                return db.execute("DELETE FROM spoton_files WHERE restaurant = ?", (restaurant,)).rowcount > 0
            return db.execute("DELETE FROM spoton_files WHERE restaurant = ? AND file = ?",
                              (restaurant, file)).rowcount > 0
