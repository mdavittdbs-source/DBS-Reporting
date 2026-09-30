"""Manage login accounts for the web chat.

    python -m dbs_reporting.users add jsmith --name "Jane Smith" [--admin]
    python -m dbs_reporting.users list
    python -m dbs_reporting.users password jsmith
    python -m dbs_reporting.users remove jsmith
"""

import argparse
import getpass
import sqlite3
import sys

from .store import Store

MIN_PASSWORD = 8


def ask_password() -> str:
    while True:
        password = getpass.getpass("Password (typing is hidden): ")
        if len(password) < MIN_PASSWORD:
            print(f"Use at least {MIN_PASSWORD} characters.")
            continue
        if getpass.getpass("Type it again: ") != password:
            print("Passwords didn't match. Try again.")
            continue
        return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dbs_reporting.users", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="create a login")
    add.add_argument("username")
    add.add_argument("--name", default="", help="display name, e.g. \"Jane Smith\"")
    add.add_argument("--admin", action="store_true", help="mark as an admin")
    commands.add_parser("list", help="show all logins")
    password = commands.add_parser("password", help="reset someone's password")
    password.add_argument("username")
    remove = commands.add_parser("remove", help="delete a login and all of their chats")
    remove.add_argument("username")
    args = parser.parse_args(argv)

    store = Store()

    if args.command == "add":
        try:
            store.add_user(args.username, ask_password(), args.name, args.admin)
        except sqlite3.IntegrityError:
            print(f"A login named {args.username!r} already exists.")
            return 1
        print(f"Created login {args.username!r}.")
    elif args.command == "list":
        users = store.list_users()
        if not users:
            print("No logins yet. Create one with: python -m dbs_reporting.users add <username>")
        for u in users:
            admin = " (admin)" if u["is_admin"] else ""
            print(f"{u['username']:<20} {u['display_name']:<25} {u['chats']:>4} chats{admin}")
    elif args.command == "password":
        if not store.set_password(args.username, ask_password()):
            print(f"No login named {args.username!r}.")
            return 1
        print(f"Password updated for {args.username!r}. They've been signed out everywhere.")
    elif args.command == "remove":
        answer = input(f"Delete {args.username!r} and all of their saved chats? Type yes to confirm: ")
        if answer.strip().lower() != "yes":
            print("Cancelled.")
            return 1
        if not store.remove_user(args.username):
            print(f"No login named {args.username!r}.")
            return 1
        print(f"Deleted {args.username!r}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
