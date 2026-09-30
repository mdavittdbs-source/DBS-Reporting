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
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_by_conversation ON turns(conversation_id, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            db.executescript(SCHEMA)
            columns = {row[1] for row in db.execute("PRAGMA table_info(users)")}
            if "active" not in columns:  # databases created before users.txt support
                db.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            turn_columns = {row[1] for row in db.execute("PRAGMA table_info(turns)")}
            if "model" not in turn_columns:  # databases created before per-answer models
                db.execute("ALTER TABLE turns ADD COLUMN model TEXT")

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
                        "INSERT INTO users (username, display_name, password_hash, is_admin, active, created_at)"
                        " VALUES (?, ?, ?, ?, 1, ?)",
                        (u["username"], u["display_name"], u["password_hash"], int(u["is_admin"]), _now()),
                    )
                    continue
                if row["password_hash"] != u["password_hash"]:
                    db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
                db.execute(
                    "UPDATE users SET display_name = ?, password_hash = ?, is_admin = ?, active = 1 WHERE id = ?",
                    (u["display_name"], u["password_hash"], int(u["is_admin"]), row["id"]),
                )
            for row in db.execute("SELECT id, username FROM users WHERE active = 1").fetchall():
                if row["username"].lower() not in listed:
                    db.execute("UPDATE users SET active = 0 WHERE id = ?", (row["id"],))
                    db.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))

    def active_accounts(self) -> list[dict]:
        """Username, display name, hash and admin flag for every active login."""
        with self._db() as db:
            rows = db.execute(
                "SELECT username, display_name, password_hash, is_admin FROM users WHERE active = 1"
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
                "SELECT u.id, u.username, u.display_name, u.is_admin FROM sessions s"
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

    def get_conversation(self, user_id: int, conversation_id: str) -> dict | None:
        """Return the conversation only if it belongs to `user_id`."""
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id = ? AND user_id = ?", (conversation_id, user_id)
            ).fetchone()
        if row is None:
            return None
        conversation = dict(row)
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
                "SELECT role, text, model, created_at FROM turns WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_turn(
        self, conversation_id: str, question: str, answer: str, history: list, model: str | None = None
    ) -> None:
        """Record a question and answer. `model` is the model that answered; it also becomes the
        chat's current model, so the next question defaults to it."""
        now = _now()
        with self._db() as db:
            db.executemany(
                "INSERT INTO turns (conversation_id, role, text, model, created_at) VALUES (?, ?, ?, ?, ?)",
                [(conversation_id, "user", question, None, now), (conversation_id, "assistant", answer, model, now)],
            )
            db.execute(
                "UPDATE conversations SET history = ?, updated_at = ?, model = COALESCE(?, model) WHERE id = ?",
                (json.dumps(history), now, model, conversation_id),
            )

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
