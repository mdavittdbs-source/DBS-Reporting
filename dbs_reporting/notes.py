"""Notes: anyone can jot notes in David (meetings, calls, site visits). "Clean up" has David tidy one: typos
fixed, organized under short headings, every fact kept, and the action items pulled out so they can go
straight to the person's To Do list.

One Claude request per clean-up, with the reply held to a JSON schema. Nothing is sent until the person asks.
"""

import json

import anthropic

from . import eastern
from . import usage as usage_mod
from .agent import request_options

CLEANUP_EFFORT = "low"  # tidying a note doesn't need deep reasoning
MAX_NOTE = 60_000       # characters; about 15,000 words


class NoteError(Exception):
    """Something the person can act on, worded for them."""


SYSTEM = """You clean up notes a DBS staff member typed quickly, e.g. during a meeting, a phone call or a \
site visit. DBS is a point-of-sale (POS) dealer that installs and supports POS systems, mostly for \
restaurants and bars.

Return the same note, easier to read:
- Keep every fact: names, numbers, dates, times, ticket numbers, prices, decisions, open questions. \
Never add facts, guesses or advice that aren't in the note. If something is unclear, keep it as written.
- Fix spelling, typos and shorthand that's obvious (e.g. "w/" -> "with", "mtg" -> "meeting"). Keep \
names, product names and jargon (KDS, OLO, RMA, handheld) as they are.
- Organize it with short Markdown "## " headings that fit the note, such as Summary, Discussed, Decisions, \
Next steps or Open questions; use "- " bullets under them. A short note (a few lines) needs no headings: \
just tidy the lines. Start with a one or two sentence summary when the note is longer than a few lines.
- Keep the writer's voice. Plain words, no filler.
- title: a short title for the note. Keep the writer's title if it fits; otherwise write one from the \
content, e.g. "Taco Town KDS rollout call".
- action_items: each concrete thing someone has to do, as a short instruction ("Send Joe the new \
menu PDF"). Include who and when if the note says. priority: "now" (urgent), "today", "this_week" or \
"later", from what the note says; "this_week" when it doesn't say. Leave the list empty if there are none. \
Put them under a "Next steps" heading in the body too."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body": {"type": "string"},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "priority": {"type": "string", "enum": ["now", "today", "this_week", "later"]},
                },
                "required": ["title", "priority"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "body", "action_items"],
    "additionalProperties": False,
}


def clean_up(client: anthropic.Anthropic, model: str, title: str, label: str, body: str) -> tuple[dict, dict]:
    """David's tidy version of a note: ({title, body, action_items}, usage)."""
    if not body.strip():
        raise NoteError("Write something first, then David can clean it up.")
    if len(body) > MAX_NOTE:
        raise NoteError(f"That note is too long for David to clean up at once (over {MAX_NOTE:,} characters).")
    today = eastern.now()
    note = {"today": f"{today:%A} {eastern.day(today)}", "title": title, "label": label, "note": body}
    options = request_options(model, CLEANUP_EFFORT)
    options["output_config"] = {**options.get("output_config", {}), "format": {"type": "json_schema", "schema": SCHEMA}}
    response = client.beta.messages.create(
        model=model, max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": json.dumps(note, ensure_ascii=False)}],
        **options,
    )
    used = usage_mod.empty()
    usage_mod.add_message(used, response, model)
    if response.stop_reason == "refusal":
        raise NoteError("David couldn't clean up this note.")
    if response.stop_reason == "max_tokens":
        raise NoteError("The cleaned-up note came out too long. Try a shorter note.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise NoteError("David's cleaned-up note came back garbled. Try again.") from None
    actions = [{"title": a["title"].strip()[:200], "priority": a.get("priority") or "this_week"}
               for a in data.get("action_items", []) if isinstance(a, dict) and (a.get("title") or "").strip()]
    return {"title": (data.get("title") or title).strip()[:200], "body": (data.get("body") or body).strip(),
            "action_items": actions[:20]}, used
