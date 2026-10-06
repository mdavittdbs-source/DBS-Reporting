import json

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
    with pytest.raises(UploadError):
        parse_upload("menu.csv", MENU)
    with pytest.raises(UploadError):
        parse_upload("menu.xlsx", MENU, "Joe's")
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
