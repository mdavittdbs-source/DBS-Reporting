import datetime
import io
import json
import zipfile

import pytest

from dbs_reporting.spoton import UploadError, build_spoton_tools, parse_upload
from dbs_reporting.store import Store

MENU = ("Name,Price,MenuGroupNames,ReportGroupName\n"
        "Burger,12.50,\"Lunch, Dinner\",Food\n"
        "\"Fries, large\",4.00,Dinner,\n"
        "IPA,7.00,Drinks,Liquor\n").encode("utf-8-sig")


@pytest.fixture
def tools(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    name, files = parse_upload("menu_items.csv", MENU, "Test Pub & Grill")
    store.save_spoton(name, files, "Carol C")
    return store, {t.name: t for t in build_spoton_tools(store)}


def call(tool, **args):
    return json.loads(tool.call(args))


def test_parse_upload_reads_csv_and_needs_a_restaurant():
    name, files = parse_upload("Menu Items.csv", MENU, " Joe's ")
    assert name == "Joe's"
    columns, rows = files["menu_items"]
    assert columns == ["Name", "Price", "MenuGroupNames", "ReportGroupName"]
    assert rows[1] == ["Fries, large", "4.00", "Dinner", ""]
    assert parse_upload("Taco_Town menu.csv", MENU)[0] == "Taco Town"  # no restaurant given: from the file name
    assert parse_upload("Weekly notes.csv", MENU)[0] == "Weekly notes"  # nothing to go on: its own name
    with pytest.raises(UploadError):
        parse_upload("menu.pdf", MENU, "Joe's")
    with pytest.raises(UploadError):
        parse_upload("menu.zip", b"not a zip")


def test_list_and_filter(tools):
    store, t = tools
    listed = call(t["list_spoton_data"])["restaurants"]
    assert listed[0]["restaurant"] == "Test Pub & Grill" and listed[0]["files"][0]["rows"] == 3

    blank = call(t["get_spoton_data"], restaurant="test pub", file="menu_items",
                 where={"ReportGroupName": "(blank)"}, columns=["Name"])
    assert blank["matched"] == 1 and blank["rows"] == [["Fries, large"]]
    exact = call(t["get_spoton_data"], restaurant="Test Pub", file="menu_items", where={"reportgroupname": "=food"})
    assert [r[0] for r in exact["rows"]] == ["Burger"]
    assert call(t["get_spoton_data"], restaurant="Test", file="menu_items", search="ipa")["matched"] == 1


def test_group_by_splits_list_columns(tools):
    _, t = tools
    groups = call(t["get_spoton_data"], restaurant="Test", file="menu_items", group_by="MenuGroupNames")
    assert dict(groups["counts"]) == {"Dinner": 2, "Lunch": 1, "Drinks": 1}
    report = call(t["get_spoton_data"], restaurant="Test", file="menu_items", group_by="ReportGroupName")
    assert dict(report["counts"]) == {"Food": 1, "(blank)": 1, "Liquor": 1}


def test_errors_name_what_exists(tools):
    _, t = tools
    assert call(t["get_spoton_data"], restaurant="Nowhere", file="menu_items")["restaurants"] == ["Test Pub & Grill"]
    assert call(t["get_spoton_data"], restaurant="Test", file="modifiers")["files"] == ["menu_items"]
    assert "No column" in call(t["get_spoton_data"], restaurant="Test", file="menu_items", group_by="Color")["error"]


def test_new_upload_replaces_old(tools):
    store, t = tools
    store.save_spoton("test pub & grill", {"employees": (["FirstName"], [["Pat"]])}, "Alice A")
    files = store.spoton_files()
    assert [(f["file"], f["uploaded_by"]) for f in files] == [("employees", "Alice A")]


def _xlsx(sheets: dict) -> bytes:
    from openpyxl import Workbook
    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_excel_workbooks():
    one = _xlsx({"Sheet1": [[], ["Name", "Price", "Added", None], ["Burger", 12.5, datetime.date(2026, 10, 6)],
                            ["Fries", 4.0, None], [None, None, None], ["IPA", 7, datetime.datetime(2026, 10, 6, 14, 30)]]})
    name, files = parse_upload("Menu Items.xlsx", one, "Joe's")
    columns, rows = files["menu_items"]  # one sheet: named after the workbook
    assert columns == ["Name", "Price", "Added"]  # first non-empty row; empty trailing column dropped
    assert rows == [["Burger", "12.5", "2026-10-06"], ["Fries", "4", ""], ["IPA", "7", "2026-10-06 14:30"]]
    two = _xlsx({"Menu Items": [["Name"], ["Burger"]], "Employees": [["First", "Last"], ["Sam", "Ortiz"]], "Notes": []})
    assert sorted(parse_upload("export.xlsx", two, "Joe's")[1]) == ["employees", "menu_items"]  # one per sheet
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as z:
        z.writestr("about.txt", "Restaurant: Dock Bar\n")
        z.writestr("employees.xlsx", _xlsx({"Sheet1": [["First"], ["Sam"]]}))
    name, files = parse_upload("dock.zip", packed.getvalue())
    assert name == "Dock Bar" and files["employees"][1] == [["Sam"]]
    with pytest.raises(UploadError, match="can't be opened"):
        parse_upload("menu.xlsx", MENU, "Joe's")
    with pytest.raises(UploadError, match="empty"):
        parse_upload("blank.xlsx", _xlsx({"Sheet1": []}), "Joe's")


def test_a_single_file_goes_with_the_restaurant_in_its_name():
    from dbs_reporting.spoton import restaurant_for
    known = ["Taco Town", "Taco", "Joe's"]
    assert restaurant_for("Taco Town - Menu Items.xlsx", known) == "Taco Town"  # the longest match
    assert restaurant_for("joe's_employees.csv", known) == "Joe's"
    assert restaurant_for("Tacos menu.csv", known) == "Tacos"  # "Taco" is only part of a word; "menu" is a SpotOn file
    assert restaurant_for("Dock Bar - Menu Items.xlsx", []) == "Dock Bar"  # a new restaurant, before a SpotOn file name
    assert restaurant_for("dock_bar_audit_items_to_fix.csv", []) == "dock bar"
    assert restaurant_for("Menu Items.csv", []) == "Menu Items"  # nothing before it: its own name
    assert restaurant_for("Weekly notes.xlsx", []) == "Weekly notes"
    from dbs_reporting.spoton import single_file_key
    assert single_file_key("Taco Town - Menu Items.xlsx", "Taco Town") == "menu_items"
    assert single_file_key("Menu Items.csv", "Menu Items") == "menu_items"


def test_uploads_are_deleted_after_30_days(tmp_path):
    from datetime import datetime, timedelta, timezone
    store = Store(tmp_path / "db.sqlite")
    store.save_spoton("Old Pub", parse_upload("menu_items.csv", MENU, "Old Pub")[1], "Carol C")
    store.save_spoton("New Pub", parse_upload("menu_items.csv", MENU, "New Pub")[1], "Carol C")
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds")
    with store._db() as db:
        db.execute("UPDATE spoton_files SET uploaded_at = ? WHERE restaurant = 'Old Pub'", (old,))
    assert store.spoton_rows("Old Pub", "menu_items") is None
    assert [f["restaurant"] for f in store.spoton_files()] == ["New Pub"]
    listed = call({t.name: t for t in build_spoton_tools(store)}["list_spoton_data"])
    assert "30 days" in listed["note"] and listed["restaurants"][0]["files"][0]["uploaded_at"]


def test_files_that_arent_csv_get_a_readable_error():
    with pytest.raises(UploadError, match="isn't a CSV"):
        parse_upload("menu.csv", b"PK\x03\x04\x00\x00binary", "Joe's")
    # Bytes that aren't text in UTF-8 or Windows-1252 (0x81) are still read, not a crash
    name, files = parse_upload("menu.csv", b"Name,Price\nCaf\x81,3\n", "Joe's")
    assert files["menu"][1][0][1] == "3"
    with pytest.raises(UploadError, match="can't be read as a CSV"):
        parse_upload("menu.csv", b"Name\n\"" + b"x" * 200_000 + b"\"\n", "Joe's")  # a field over the csv limit
    with pytest.raises(UploadError, match="can't be opened as an Excel"):
        parse_upload("menu.xlsx", b"not a workbook", "Joe's")


def test_a_zip_that_unpacks_too_big_is_refused(monkeypatch):
    import dbs_reporting.spoton as spoton
    monkeypatch.setattr(spoton, "MAX_UNZIPPED_BYTES", 150)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("about.txt", "Restaurant: Big Pub\n")
        z.writestr("menu_items.csv", MENU)
        z.writestr("modifiers.csv", MENU)
    with pytest.raises(UploadError, match="too big"):
        parse_upload("big.zip", buffer.getvalue())
