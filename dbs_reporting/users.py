"""Check the logins in users.txt.

Logins are managed by editing users.txt in the project folder (see the notes at the top
of that file). This command applies the file right away and shows who can sign in:

    python -m dbs_reporting.users
"""

import logging
import sys

from .store import Store
from .userfile import UsersFile


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("add", "password", "remove"):
        print("Logins are now managed in users.txt. Open it in VS Code, edit, and save.")
        print("Then run: python -m dbs_reporting.users   to check it.")
        return 1

    # This command prints problems itself; skip the duplicate log lines.
    logging.getLogger("dbs_reporting.userfile").setLevel(logging.ERROR)
    store = Store()
    users_file = UsersFile(store)
    existed = users_file.path.exists()
    users_file.ensure_exists()
    users_file.refresh()

    print(f"Users file: {users_file.path}")
    if not existed:
        print("Created it. Open it in VS Code and add a line per person.")
    if users_file.problems:
        print("\nProblems (fix these lines and save):")
        for problem in users_file.problems:
            print(f"  - {problem}")

    accounts = [u for u in store.list_users() if u["active"]]
    print(f"\n{len(accounts)} login(s) can sign in:")
    for u in accounts:
        admin = " (admin)" if u["is_admin"] else ""
        print(f"  {u['username']:<20} {u['display_name']:<25} {u['chats']:>4} saved chats{admin}")
    inactive = [u for u in store.list_users() if not u["active"]]
    if inactive:
        print(f"\nRemoved from users.txt (chats kept, can't sign in): {', '.join(u['username'] for u in inactive)}")
    return 1 if users_file.problems else 0


if __name__ == "__main__":
    sys.exit(main())
