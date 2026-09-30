"""Activity log: who asked what, which ConnectWise lookups David ran, and what it answered.

Every answer (and every error) is written to the console running the bot and to
logs/activity.log (rotated at 5 MB, 10 old files kept). To read past conversations,
including ones from before this log existed, use:

    python -m dbs_reporting.activity [--days 7] [--user jsmith] [--search printer] [--full]
"""

import json
import logging
import os
import sys
import textwrap
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .store import PROJECT_ROOT, Store
from .usage import fmt_tokens

LOG_DIR = Path(os.environ.get("LOG_DIR") or PROJECT_ROOT / "logs")

_logger: logging.Logger | None = None


def logger() -> logging.Logger:
    global _logger
    if _logger is None:
        _logger = logging.getLogger("dbs_reporting.activity")
        _logger.setLevel(logging.INFO)
        _logger.propagate = False
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
        for handler in (
            RotatingFileHandler(LOG_DIR / "activity.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ):
            handler.setFormatter(formatter)
            _logger.addHandler(handler)
    return _logger


def tool_calls(new_messages: list) -> list[str]:
    """The ConnectWise lookups made while answering, e.g. get_ticket_totals(days=30, group_by='site')."""
    calls = []
    for message in new_messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"] \
            if isinstance(content, list) else []
        pairs = [(b.get("name"), b.get("input") or {}) for b in blocks]
        for name, args in pairs:
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            if name == "create_chart":  # the chart's data is in the answer; the title is enough here
                calls.append(f"create_chart({args.get('title', '')!r})")
                continue
            shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
            calls.append(f"{name}({shown})")
    return calls


def usage_line(usage: dict | None) -> str:
    if not usage:
        return "not recorded"
    total_in = usage["input_tokens"] + usage["cache_write_tokens"] + usage["cache_read_tokens"]
    cost = f" · ≈ ${usage['cost_usd']:.3f}" if usage.get("priced", True) else ""
    return (f"{fmt_tokens(total_in)} in ({fmt_tokens(usage['cache_read_tokens'])} cached) · "
            f"{fmt_tokens(usage['output_tokens'])} out · {usage['requests']} calls{cost}")


def _indent(text: str) -> str:
    return textwrap.indent(text.strip(), "      ")


def log_answer(user: dict, conversation_id: str, model: str, question: str, answer: str,
               new_messages: list, usage: dict | None, seconds: float) -> None:
    calls = tool_calls(new_messages)
    logger().info(
        f"{user['display_name']} ({user['username']}) · chat {conversation_id[:8]} · {model} · {seconds:.0f}s\n"
        f"  Q:\n{_indent(question)}\n"
        f"  Lookups: {'; '.join(calls) if calls else 'none'}\n"
        f"  A:\n{_indent(answer)}\n"
        f"  Usage: {usage_line(usage)}\n"
    )


def log_error(user: dict, conversation_id: str | None, model: str, question: str, error: str) -> None:
    logger().info(
        f"{user['display_name']} ({user['username']}) · chat {(conversation_id or 'new')[:8]} · {model} · ERROR\n"
        f"  Q:\n{_indent(question)}\n"
        f"  Error: {error}\n"
    )


def history_report(days: int, user: str | None, search: str | None, full: bool, limit: int) -> str:
    rows = Store().answered_turns(days)
    if user:
        rows = [r for r in rows if user.lower() in (r["username"].lower(), r["display_name"].lower())]
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in (r["question"] or "").lower() or needle in r["text"].lower()]
    rows = rows[-limit:]
    if not rows:
        return "No matching conversations."
    out = []
    for r in rows:
        answer = r["text"].strip()
        if not full and len(answer) > 600:
            answer = answer[:600].rstrip() + "\n… (use --full for the whole answer)"
        out.append(
            f"{r['created_at'][:16].replace('T', ' ')}  {r['display_name']} ({r['username']}) · "
            f"\"{r['title']}\" · {r['model'] or '?'}\n"
            f"  Q:\n{_indent(r['question'] or '')}\n"
            f"  A:\n{_indent(answer)}\n"
            f"  Usage: {usage_line(r['usage'])}\n"
        )
    return "\n".join(out) + f"\n{len(rows)} answer(s) shown."


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="python -m dbs_reporting.activity",
                                     description="Read past questions and answers from all users.")
    parser.add_argument("--days", type=int, default=7, help="how many days back (default 7)")
    parser.add_argument("--user", help="only this person (username or display name)")
    parser.add_argument("--search", help="only questions or answers containing this text")
    parser.add_argument("--full", action="store_true", help="show whole answers, not just the start")
    parser.add_argument("--limit", type=int, default=50, help="most recent N answers (default 50)")
    args = parser.parse_args()
    print(history_report(args.days, args.user, args.search, args.full, args.limit))
