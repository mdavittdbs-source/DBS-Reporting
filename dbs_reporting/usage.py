"""Token usage and estimated cost per answer.

Prices are USD per million tokens (input, output, cache read), from Anthropic's published
list prices. Cache writes cost 1.25x input. These are estimates: the Anthropic console
(Settings -> Usage / Cost) is the source of truth for billing.
"""

PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
}
CACHE_WRITE_MULTIPLIER = 1.25

FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def empty() -> dict:
    return {"requests": 0, **{f: 0 for f in FIELDS}, "cost_usd": 0.0, "priced": True}


def cost(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> float | None:
    """Estimated USD cost, or None for a model without a known price."""
    price = PRICES.get(model)
    if price is None:
        return None
    inp, out, read = price
    return (input_tokens * inp + output_tokens * out + cache_read * read
            + cache_write * inp * CACHE_WRITE_MULTIPLIER) / 1_000_000


def add_message(totals: dict, message, model: str) -> None:
    """Add one API response's usage to `totals`. Uses the per-attempt breakdown when the response
    includes one (e.g. a refusal fallback ran on another model), else the top-level numbers."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return
    attempts = getattr(usage, "iterations", None) or [usage]
    for part in attempts:
        values = (
            getattr(part, "input_tokens", 0) or 0,
            getattr(part, "output_tokens", 0) or 0,
            getattr(part, "cache_read_input_tokens", 0) or 0,
            getattr(part, "cache_creation_input_tokens", 0) or 0,
        )
        for field, value in zip(FIELDS, values):
            totals[field] += value
        price = cost(getattr(part, "model", None) or model, *values)
        if price is None:
            totals["priced"] = False
        else:
            totals["cost_usd"] += price
    totals["requests"] += 1


def combine(items: list[dict]) -> dict:
    totals = empty()
    for item in items:
        for key in ("requests", *FIELDS):
            totals[key] += item.get(key, 0)
        totals["cost_usd"] += item.get("cost_usd", 0.0)
        totals["priced"] = totals["priced"] and item.get("priced", True)
    return totals


def fmt_tokens(n: int) -> str:
    return f"{n / 1_000_000:.2f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fmt_cost(totals: dict) -> str:
    return f"${totals['cost_usd']:.2f}" + ("" if totals["priced"] else "+ (some models unpriced)")


def report(days: int = 30, top: int = 10) -> str:
    """Plain-text usage summary for the last `days` days."""
    from collections import defaultdict

    from .store import Store

    rows = Store().usage_rows(days)
    lines = [f"Usage for the last {days} days (estimated; the Anthropic console has exact billing)", ""]
    if not rows:
        return "\n".join(lines + ["No answers with recorded usage yet."])

    def table(title: str, key) -> None:
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            groups[key(row)].append(row["usage"])
        lines.append(title)
        lines.append(f"  {'':<28}{'answers':>8}{'input':>10}{'output':>10}{'cached':>10}{'est. cost':>12}")
        for name, items in sorted(groups.items(), key=lambda kv: -combine(kv[1])["cost_usd"]):
            t = combine(items)
            lines.append(f"  {name[:27]:<28}{len(items):>8}{fmt_tokens(t['input_tokens'] + t['cache_write_tokens']):>10}"
                         f"{fmt_tokens(t['output_tokens']):>10}{fmt_tokens(t['cache_read_tokens']):>10}{fmt_cost(t):>12}")
        lines.append("")

    total = combine([r["usage"] for r in rows])
    lines += [f"Answers: {len(rows)}   Est. cost: {fmt_cost(total)}   "
              f"Average per answer: ${total['cost_usd'] / len(rows):.3f}", ""]
    table("By person", lambda r: r["display_name"])
    table("By model", lambda r: r["model"] or "(unknown)")
    table("By day", lambda r: r["created_at"][:10])

    lines.append("Most expensive questions")
    for row in sorted(rows, key=lambda r: -r["usage"]["cost_usd"])[:top]:
        u = row["usage"]
        question = " ".join((row["question"] or row["title"] or "").split())
        lines.append(f"  ${u['cost_usd']:.3f}  {row['created_at'][:10]}  {row['display_name'][:16]:<16}  "
                     f"{fmt_tokens(u['input_tokens'] + u['cache_write_tokens'] + u['cache_read_tokens'])} in / "
                     f"{fmt_tokens(u['output_tokens'])} out  {question[:60]}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="python -m dbs_reporting.usage",
                                     description="Token usage and estimated cost of David's answers.")
    parser.add_argument("--days", type=int, default=30, help="how many days back (default 30)")
    parser.add_argument("--top", type=int, default=10, help="how many of the most expensive questions to list")
    args = parser.parse_args()
    print(report(args.days, args.top))
