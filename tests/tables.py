"""Tool results send long lists as tables; this turns one back into a dict per row for tests."""


def rows(table: dict) -> list[dict]:
    """{"columns", "rows", "every_row"} -> one dict per row, with blank values left out."""
    every_row = table.get("every_row", {})
    return [{**every_row, **{c: v for c, v in zip(table["columns"], row) if v is not None}}
            for row in table["rows"]]
