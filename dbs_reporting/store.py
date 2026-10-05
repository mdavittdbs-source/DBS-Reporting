"""SQLite storage for user accounts, login sessions and saved chats."""

import hashlib
import hmac
import json
import os
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
-- Small bits of app state, e.g. which week's email digest has been sent.
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _todo_id(prefix: str) -> str:
    return prefix + secrets.token_hex(5)


REMOVED_DAYS = 30  # how long removed items stay in Removed (and off new lists)
REMOVED_MAX = 60


def _removed(data: dict, made_at: str) -> list[dict]:
    """Items the person removed in the last REMOVED_DAYS, newest first, each with "removed_at". Lists saved by
    an earlier version kept only {ticket, title, at} under "dismissed"; those become plain removed items."""
    stash = data.get("removed", []) + [
        {"id": _todo_id("d"), "title": e.get("title") or f"Ticket #{e.get('ticket')}", "why": "", "priority": "later",
         "ticket": e.get("ticket"), "client": None, "when": None, "removed_at": e.get("at")}
        for e in data.get("dismissed", [])]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=REMOVED_DAYS)).isoformat(timespec="seconds")
    kept, ids = [], set()
    for item in sorted(stash, key=lambda i: i.get("removed_at") or made_at, reverse=True):
        if item["id"] not in ids and (item.get("removed_at") or made_at) >= cutoff:
            kept.append({**item, "removed_at": item.get("removed_at") or made_at})
            ids.add(item["id"])
    return kept[:REMOVED_MAX]


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
            turn_columns = {row[1] for row in db.execute("PRAGMA table_info(turns)")}
            if "model" not in turn_columns:  # databases created before per-answer models
                db.execute("ALTER TABLE turns ADD COLUMN model TEXT")
            if "usage" not in turn_columns:  # databases created before usage tracking
                db.execute("ALTER TABLE turns ADD COLUMN usage TEXT")
            if "charts" not in turn_columns:  # databases created before charts
                db.execute("ALTER TABLE turns ADD COLUMN charts TEXT")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
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
        with self._db() as db:
            listed = {k.lower() for k in keep}
            for u in users:
                listed.add(u["username"].lower())
                row = db.execute("SELECT id, password_hash FROM users WHERE username = ?",
                                 (u["username"],)).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO users (username, display_name, password_hash, is_admin, active, created_at,"
                        " cw_member) VALUES (?, ?, ?, ?, 1, ?, ?)",
                        (u["username"], u["display_name"], u["password_hash"], int(u["is_admin"]), _now(),
                         u.get("cw_member") or ""),
                    )
                    continue
                if row["password_hash"] != u["password_hash"]:
                    db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
                db.execute(
                    "UPDATE users SET display_name = ?, password_hash = ?, is_admin = ?, active = 1, cw_member = ?"
                    " WHERE id = ?",
                    (u["display_name"], u["password_hash"], int(u["is_admin"]), u.get("cw_member") or "", row["id"]),
                )
            for row in db.execute("SELECT id, username FROM users WHERE active = 1").fetchall():
                if row["username"].lower() not in listed:
                    db.execute("UPDATE users SET active = 0 WHERE id = ?", (row["id"],))
                    db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))

    def active_accounts(self) -> list[dict]:
        """Username, display name, hash and admin flag for every active login."""
        with self._db() as db:
            rows = db.execute(
                "SELECT username, display_name, password_hash, is_admin, cw_member FROM users WHERE active = 1"
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
                "SELECT u.id, u.username, u.display_name, u.is_admin, u.cw_member FROM sessions s"
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
        with self._db() as db:
            old = db.execute("SELECT data, done, created_at FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
            if old is not None:
                old_data = json.loads(old["data"])
                items, done = _with_ids(old_data.get("items", []), json.loads(old["done"]))
                data["items"] += [item for item in items if item.get("mine") and item["id"] not in done]
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
        return {"data": data, "done": done, "removed": removed, "model": row["model"],
                "usage": json.loads(row["usage"]) if row["usage"] else None, "created_at": row["created_at"]}

    def set_todo_done(self, user_id: int, item_id: str, done: bool) -> bool:
        """Tick or untick one item. False if there's no list or no such item."""
        with self._db() as db:
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
        the items in `remove` taken off. David's items keep their own wording; only their group can change.
        An item that's on the list but not in `items` stays (at the end of its group): the page that sent
        this may not have seen it yet, e.g. one added just before a reload or in another tab. Returns the
        saved items, or None if there's no list."""
        with self._db() as db:
            row = db.execute("SELECT data, done, created_at FROM todo_lists WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                return None
            data = json.loads(row["data"])
            stored, ticked = _with_ids(data.get("items", []), json.loads(row["done"]))
            # Removed items are kept aside (Removed on the page), so one can be put back, or a removal undone
            # after it saved. Only David's own wording comes back for his items.
            removed = _removed(data, row["created_at"])
            davids = {item["id"]: item for item in removed + stored if not item.get("mine")}
            saved, seen = [], set()
            for item in items:
                if item["id"] in seen:
                    continue
                if item.get("mine"):
                    saved.append({"id": item["id"], "title": item["title"], "why": item.get("why") or "",
                                  "priority": item["priority"], "ticket": item.get("ticket"), "client": None,
                                  "when": None, "mine": True})
                elif item["id"] in davids:
                    saved.append({k: v for k, v in {**davids[item["id"]], "priority": item["priority"]}.items()
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

    def delete_removed(self, user_id: int, ids: list[str] | None, undo: bool = False) -> list[dict] | None:
        """Delete items from Removed (all of them when `ids` is None), or bring them back with undo=True.
        Deleted tickets still stay off new lists until they change or REMOVED_DAYS pass; they just aren't
        listed. Returns what Removed now shows, or None if there's no list."""
        with self._db() as db:
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
