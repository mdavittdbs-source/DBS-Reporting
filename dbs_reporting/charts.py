"""Charts that Claude adds to an answer with the create_chart tool.

A chart spec is plain JSON, validated here, saved with the answer, drawn by the web page
(Chart.js) and written into Excel exports:

    {"title": str, "subtitle": str, "type": "bar" | "hbar" | "line" | "stacked_bar",
     "labels": [str, ...], "series": [{"name": str, "values": [number, ...]}, ...],
     "x_label": str, "y_label": str}
"""

import json

CHART_TYPES = ("bar", "hbar", "line", "stacked_bar")
MAX_SERIES = 8     # the categorical palette has 8 validated colors; never generate more
MAX_LABELS = 50


def validate_chart(spec: dict) -> tuple[dict | None, str | None]:
    """Return (clean spec, None) or (None, error message for Claude)."""
    if not isinstance(spec, dict):
        return None, "chart must be an object"
    chart_type = spec.get("chart_type") or spec.get("type")
    if chart_type not in CHART_TYPES:
        return None, f"chart_type must be one of: {', '.join(CHART_TYPES)}"
    labels = spec.get("labels")
    if not isinstance(labels, list) or not labels:
        return None, "labels must be a non-empty list"
    if len(labels) > MAX_LABELS:
        return None, f"at most {MAX_LABELS} labels; show the top items and fold the rest into 'Other'"
    series = spec.get("series")
    if not isinstance(series, list) or not series:
        return None, "series must be a non-empty list of {name, values}"
    if len(series) > MAX_SERIES:
        return None, f"at most {MAX_SERIES} series; fold the smallest into 'Other'"
    clean_series = []
    for s in series:
        if not isinstance(s, dict) or not isinstance(s.get("values"), list):
            return None, "each series needs a name and a values list"
        values = s["values"]
        if len(values) != len(labels):
            return None, f"series {s.get('name')!r} has {len(values)} values but there are {len(labels)} labels"
        try:
            numbers = [None if v is None else float(v) for v in values]
        except (TypeError, ValueError):
            return None, f"series {s.get('name')!r} has a value that isn't a number"
        clean_series.append({"name": str(s.get("name") or f"Series {len(clean_series) + 1}")[:60],
                             "values": [int(n) if n is not None and n.is_integer() else n for n in numbers]})
    title = str(spec.get("title") or "").strip()
    if not title:
        return None, "title is required"
    return {
        "title": title[:120],
        "subtitle": str(spec.get("subtitle") or "")[:200],
        "type": chart_type,
        "labels": [str(label)[:80] for label in labels],
        "series": clean_series,
        "x_label": str(spec.get("x_label") or "")[:60],
        "y_label": str(spec.get("y_label") or "")[:60],
    }, None


def extract_charts(new_messages: list) -> list[dict]:
    """Charts created while answering, in order, from the answer's new history messages.
    Only calls that the tool accepted (no error result) are included."""
    accepted = set()
    for message in new_messages:
        if isinstance(message, dict) and message.get("role") == "user" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result" and not block.get("is_error"):
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)
                    if '"chart_added": true' in text:
                        accepted.add(block.get("tool_use_id"))
    charts = []
    for message in new_messages:
        if isinstance(message, dict) and message.get("role") == "assistant" and isinstance(message.get("content"), list):
            for block in message["content"]:
                if (isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "create_chart"
                        and block.get("id") in accepted):
                    chart, _ = validate_chart(block.get("input") or {})
                    if chart:
                        charts.append(chart)
    return charts
