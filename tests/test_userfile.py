import os
import time

from dbs_reporting.store import Store
from dbs_reporting.userfile import UsersFile


def write(path, text):
    path.write_text(text, encoding="utf-8")
    # Make sure the change is noticed even on filesystems with coarse timestamps.
    stamp = time.time() + write.bump
    write.bump += 5
    os.utime(path, (stamp, stamp))


write.bump = 5


def setup(tmp_path, text):
    store = Store(tmp_path / "db.sqlite")
    path = tmp_path / "users.txt"
    write(path, text)
    users = UsersFile(store, path)
    users.refresh()
    return store, users, path


def test_plain_passwords_are_hashed_and_work(tmp_path):
    store, users, path = setup(tmp_path, "# comment\njsmith | Jane Smith | Welcome2026! |\nboss | The Boss | SuperSecret99 | admin\n")
    text = path.read_text()
    assert "Welcome2026!" not in text and "SuperSecret99" not in text
    assert text.startswith("# comment\n")
    assert "jsmith | Jane Smith | scrypt$" in text
    assert store.authenticate("jsmith", "Welcome2026!")["display_name"] == "Jane Smith"
    assert store.authenticate("boss", "SuperSecret99")["is_admin"] == 1
    assert store.authenticate("jsmith", "wrong") is None

    # Re-reading the already-hashed file changes nothing.
    users.refresh()
    assert path.read_text() == text


def test_change_password_and_remove_user(tmp_path):
    store, users, path = setup(tmp_path, "jsmith | Jane Smith | Welcome2026! |\nbob | Bob | BobPassword1 |\n")
    user = store.authenticate("jsmith", "Welcome2026!")
    token = store.create_session(user["id"])
    chat = store.create_conversation(user["id"], "hello", "claude-opus-5-5")

    # Change Jane's password: old sessions end, new password works.
    lines = path.read_text().splitlines()
    write(path, "jsmith | Jane Smith | NewPassword2026 |\n" + lines[1] + "\n")
    users.refresh()
    assert store.session_user(token) is None
    assert store.authenticate("jsmith", "Welcome2026!") is None
    assert store.authenticate("jsmith", "NewPassword2026")

    # Remove Jane: she can't sign in, but her chats are kept and come back when re-added.
    write(path, path.read_text().splitlines()[1] + "\n")
    users.refresh()
    assert store.authenticate("jsmith", "NewPassword2026") is None
    assert store.user_count() == 1
    write(path, path.read_text() + "jsmith | Jane Smith | Returned2026 |\n")
    users.refresh()
    again = store.authenticate("jsmith", "Returned2026")
    assert [c["id"] for c in store.list_conversations(again["id"])] == [chat]


def test_bad_lines_are_reported_and_keep_existing_access(tmp_path):
    store, users, path = setup(tmp_path, "jsmith | Jane Smith | Welcome2026! |\n")
    write(path, "jsmith | Jane Smith | short |\nnot a valid line\nbob smith | Bob | Password123 |\n")
    users.refresh()
    assert len(users.problems) == 3
    assert "short" in path.read_text()  # bad password isn't hashed away
    assert store.authenticate("jsmith", "Welcome2026!")  # old password still works


def test_new_file_carries_over_existing_logins(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.add_user("legacy", "LegacyPass1", "Legacy User", is_admin=True)
    users = UsersFile(store, tmp_path / "users.txt")
    users.ensure_exists()
    users.refresh()
    text = (tmp_path / "users.txt").read_text()
    assert "legacy | Legacy User | scrypt$" in text and text.rstrip().endswith("admin")
    assert store.authenticate("legacy", "LegacyPass1")
