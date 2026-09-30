import io
import json

from openpyxl import load_workbook

from dbs_reporting.charts import extract_charts, validate_chart
from dbs_reporting.exports import answer_workbook, markdown_tables

CHART = {"title": "Tickets by site", "chart_type": "hbar", "labels": ["Downtown", "Airport"],
         "series": [{"name": "Tickets", "values": [31, 18]}], "subtitle": "Last 30 days", "y_label": "Tickets"}


def test_validate_chart():
    chart, error = validate_chart(CHART)
    assert error is None and chart["type"] == "hbar" and chart["series"][0]["values"] == [31, 18]
    assert validate_chart({**CHART, "chart_type": "pie"})[1].startswith("chart_type must be")
    assert "has 1 values but there are 2 labels" in validate_chart(
        {**CHART, "series": [{"name": "Tickets", "values": [1]}]})[1]
    assert "isn't a number" in validate_chart({**CHART, "series": [{"name": "T", "values": ["a", 1]}]})[1]
    nine = [{"name": f"s{i}", "values": [1, 2]} for i in range(9)]
    assert "at most 8 series" in validate_chart({**CHART, "series": nine})[1]
    assert validate_chart({**CHART, "title": " "})[1] == "title is required"


def test_extract_only_accepted_charts():
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "ok", "name": "create_chart", "input": CHART},
            {"type": "tool_use", "id": "bad", "name": "create_chart", "input": {**CHART, "chart_type": "pie"}},
            {"type": "tool_use", "id": "other", "name": "find_company", "input": {"name": "x"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "ok", "content": json.dumps({"chart_added": True})},
            {"type": "tool_result", "tool_use_id": "bad", "content": json.dumps({"error": "Chart not added"})},
            {"type": "tool_result", "tool_use_id": "other", "content": "[]"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    charts = extract_charts(history)
    assert [c["title"] for c in charts] == ["Tickets by site"]


ANSWER = """**Sites with the most tickets**

| Site | Tickets | Share |
|---|---:|---:|
| **Downtown** | 1,031 | 62.5% |
| Airport | 18 | 37.5% |

Some text after."""


def test_markdown_tables():
    tables = markdown_tables(ANSWER)
    assert tables == [[["Site", "Tickets", "Share"], ["**Downtown**", "1,031", "62.5%"], ["Airport", "18", "37.5%"]]]


def test_answer_workbook():
    chart, _ = validate_chart({**CHART, "chart_type": "stacked_bar",
                               "series": [{"name": "Open", "values": [5, 3]}, {"name": "Closed", "values": [26, 15]}]})
    data = answer_workbook({"text": ANSWER, "question": "sites with most tickets?", "model": "claude-sonnet-5-5",
                            "created_at": "2026-09-30T19:00:00+00:00", "charts": [chart], "title": "t"})
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Summary", "Table 1", "Chart 1"]
    assert wb["Summary"]["B1"].value == "sites with most tickets?"
    table = wb["Table 1"]
    assert [c.value for c in table[1]] == ["Site", "Tickets", "Share"]
    assert [c.value for c in table[2]] == ["Downtown", 1031, 0.625]   # real numbers, emphasis stripped
    assert table["C2"].number_format == "0.0%"
    sheet = wb["Chart 1"]
    values = [[c.value for c in row] for row in sheet.iter_rows(min_row=4, max_row=6)]
    assert values[0][:3] == ["Category", "Open", "Closed"] and values[1][:3] == ["Downtown", 5, 26]
    assert len(sheet._charts) == 1
