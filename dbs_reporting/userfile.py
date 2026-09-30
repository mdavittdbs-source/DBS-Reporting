"""Manage logins by editing a plain text file (users.txt).

Each line is:   username | Display Name | password | admin

- Type a plain password; the bot replaces it with a scrambled (hashed) version the next
  time it reads the file, so readable passwords don't stay in the file.
- To change a password, type a new one over the scrambled text.
- Delete a line to remove someone's access. Their saved chats are kept, so adding the
  line back restores them.
- The last column is optional; write "admin" to mark an admin.
- Lines starting with # are ignored.

The file is re-read automatically when it changes; no restart needed.
"""

import logging
import os
import threading
from pathlib import Path

from .store import PROJECT_ROOT, Store, hash_password

log = logging.getLogger(__name__)
MIN_PASSWORD = 8
HASH_PREFIX = "scrypt$"

TEMPLATE = """\
# DBS Reporting logins. One person per line:
#
#   username | Display Name | password | admin
#
# - Type a plain password. The bot scrambles it the next time it reads this file, so it
#   won't stay readable. To change a password, type a new one over the scrambled text.
# - Delete a line to remove someone. Their chats are kept if you add them back later.
# - Write "admin" in the last column for admins; leave it off for everyone else.
# - Passwords need at least 8 characters and can't contain the | character.
# - Changes apply within a few seconds; no restart needed.
#
# Example (remove the # to use it):
# jsmith | Jane Smith | ChangeMe2026! |
"""


def users_file_path() -> Path:
    return Path(os.environ.get("USERS_FILE") or PROJECT_ROOT / "users.txt")


def parse_and_hash(path: Path) -> tuple[list[dict], list[str], set[str]]:
    """Read the file and hash any plain passwords in place.

    Returns (users, problems, usernames_with_problems)."""
    users: list[dict] = []
    problems: list[str] = []
    broken: set[str] = set()
    seen: set[str] = set()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    changed = False

    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 3 or len(parts) > 4:
            problems.append(f"line {number}: expected 'username | Display Name | password | admin'")
            continue
        username, display_name, password = parts[0], parts[1], parts[2]
        admin = len(parts) == 4 and parts[3].lower() == "admin"
        if not username or " " in username:
            problems.append(f"line {number}: username can't be blank or contain spaces")
            continue
        if len(parts) == 4 and parts[3] and parts[3].lower() != "admin":
            problems.append(f"line {number}: last column should be 'admin' or empty, not {parts[3]!r}")
        if username.lower() in seen:
            problems.append(f"line {number}: {username!r} is listed twice; only the first line is used")
            continue
        if not password.startswith(HASH_PREFIX):
            if len(password) < MIN_PASSWORD:
                problems.append(
                    f"line {number}: password for {username!r} needs at least {MIN_PASSWORD} characters"
                    " (their old password still works until this is fixed)"
                )
                broken.add(username)
                continue
            password = hash_password(password)
            parts[2] = password
            lines[number - 1] = " | ".join(parts[:3] + (["admin"] if admin else [""]))
            changed = True
        seen.add(username.lower())
        users.append({
            "username": username,
            "display_name": display_name or username,
            "password_hash": password,
            "is_admin": admin,
        })

    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return users, problems, broken


class UsersFile:
    """Keeps the accounts in the database in step with users.txt."""

    def __init__(self, store: Store, path: Path | None = None):
        self.store = store
        self.path = path or users_file_path()
        self.problems: list[str] = []
        self._mtime: float | None = None
        self._lock = threading.Lock()

    def ensure_exists(self) -> None:
        """Create users.txt, carrying over any logins that already exist in the database."""
        if self.path.exists():
            return
        lines = [
            " | ".join([a["username"], a["display_name"], a["password_hash"], "admin" if a["is_admin"] else ""])
            for a in self.store.active_accounts()
        ]
        self.path.write_text(TEMPLATE + "\n" + "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        log.warning("Created %s. Add logins there.", self.path)

    def refresh(self) -> None:
        """Re-read the file if it changed since last time. Cheap to call on every request."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self._mtime:
            return
        with self._lock:
            if mtime == self._mtime:
                return
            users, self.problems, broken = parse_and_hash(self.path)
            for problem in self.problems:
                log.warning("users.txt %s", problem)
            self.store.sync_users(users, keep=broken)
            # Hashing rewrites the file, so record the time after that.
            self._mtime = self.path.stat().st_mtime
