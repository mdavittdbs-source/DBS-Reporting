"""The reporting agent: Claude plus the read-only ConnectWise tools."""

import json
import os
from collections.abc import Iterator
from datetime import date

import anthropic

from .connectwise import ConnectWiseClient
from .tools import build_tools

DEFAULT_MODEL = "claude-sonnet-5-5"

# What each model accepts. "effort" and "fallback" are only sent to models that support them;
# anything not listed here gets adaptive thinking only.
KNOWN_MODELS = {
    "claude-opus-5-5": {"label": "Claude Opus 5.5 (best)", "effort": True, "fallback": True},
    "claude-sonnet-5-5": {"label": "Claude Sonnet 5.5 (balanced)", "effort": True, "fallback": True},
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5 (cheapest)", "thinking": False},
    "claude-fable-5-1": {"label": "Claude Fable 5.1 (most capable, most expensive)", "effort": True, "fallback": True},
    "claude-opus-5": {"label": "Claude Opus 5", "effort": True, "fallback": True},
    "claude-sonnet-5": {"label": "Claude Sonnet 5", "effort": True},
}


def _model_list(value: str) -> list[str]:
    return [m.strip() for m in value.split(",") if m.strip()]


def configured_models() -> tuple[str, list[str]]:
    """Read CLAUDE_MODEL (default) and CLAUDE_MODELS (choices offered in the web chat)."""
    default = os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL
    choices = _model_list(os.environ.get("CLAUDE_MODELS", "")) or [default]
    if default not in choices:
        choices.insert(0, default)
    return default, choices


def model_label(model: str) -> str:
    return KNOWN_MODELS.get(model, {}).get("label", model)


def request_options(model: str, effort: str) -> dict:
    """Model-specific request parameters."""
    info = KNOWN_MODELS.get(model, {})
    options: dict = {}
    if info.get("thinking", True):
        options["thinking"] = {"type": "adaptive"}
    if info.get("effort"):
        options["output_config"] = {"effort": effort}
    if info.get("fallback"):
        options["betas"] = ["server-side-fallback-2026-07-01"]
        options["fallbacks"] = "default"
    return options

SYSTEM_PROMPT = """You are the DBS reporting assistant. Managers at an IT managed service provider \
ask you questions about their clients' service tickets and time in ConnectWise Manage.

Today's date is {today}.

How to work:
- Resolve client names with find_company before anything else. If the name matches several \
companies and it isn't obvious which one is meant, ask a short clarifying question.
- "Most common issues" means recurring problems, not just the ticket type field. Group tickets by \
what actually went wrong, based on their summaries (e.g. "printer offline", "Outlook password \
prompts", "POS terminal won't connect"), and give a count for each group. Mention the ticket \
type/subtype breakdown only when it adds something.
- Pull ticket details for a few representative tickets when root causes or resolutions matter.
- Every number you report must come from tool results. Don't estimate or invent data. If a tool \
returns an error, tell the user plainly what failed.

How to answer:
- Lead with the answer. Managers read this quickly.
- Use a short ranked list or small table for breakdowns, and cite example ticket numbers (#12345).
- State the date range and total ticket count you analyzed.
- End with one or two practical observations when the data supports them, such as a recurring \
issue that suggests a project or a user who needs training."""


# Shown in the chat while a tool runs.
TOOL_STATUS = {
    "find_company": "Looking up the company…",
    "get_company_tickets": "Pulling tickets from ConnectWise…",
    "get_ticket_details": "Reading ticket notes…",
    "get_company_time": "Adding up time entries…",
}


def _to_json(block) -> dict:
    """SDK content blocks -> plain dicts, so a conversation can be saved and replayed later."""
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return block


class ReportingAgent:
    def __init__(self, cw: ConnectWiseClient, client: anthropic.Anthropic | None = None):
        self._client = client or anthropic.Anthropic()
        self._tools = build_tools(cw)
        self.default_model, self.models = configured_models()
        self._effort = os.environ.get("CLAUDE_EFFORT", "medium").strip() or "medium"

    @property
    def description(self) -> str:
        return f"Claude ({self.default_model})"

    def model_choices(self) -> list[dict]:
        return [{"id": m, "label": model_label(m)} for m in self.models]

    def respond(self, history: list, question: str, model: str | None = None) -> tuple[str, list]:
        """Answer `question` given a conversation's saved `history`.

        Returns (answer_text, updated_history). The history is plain JSON; pass it back
        unchanged on the next turn. On a refusal the original history is returned.
        """
        for event in self.respond_stream(history, question, model):
            if event["type"] == "done":
                return event["answer"], event["history"]
        raise RuntimeError("No response from Claude")

    def respond_stream(self, history: list, question: str, model: str | None = None) -> Iterator[dict]:
        """Like respond(), but yields events as the answer is produced:

        {"type": "status", "text": ...}   what David is doing (looking up the company, ...)
        {"type": "reset"}                 a new model turn started; discard streamed text so far
        {"type": "text", "text": ...}     a piece of answer text
        {"type": "done", "answer": ..., "history": [...]}   always last
        """
        model = model or self.default_model
        if model not in self.models:
            raise ValueError(f"Model {model!r} isn't enabled. Choose one of: {', '.join(self.models)}")

        messages = list(history) + [{"role": "user", "content": question}]
        final = None
        json_retries = 0
        while True:
            runner = self._client.beta.messages.tool_runner(
                model=model,
                max_tokens=64000,
                system=SYSTEM_PROMPT.format(today=date.today().isoformat()),
                tools=self._tools,
                messages=messages,
                max_iterations=20,
                stream=True,
                **request_options(model, self._effort),
            )
            try:
                for stream in runner:
                    yield {"type": "reset"}
                    for event in stream:
                        if event.type == "text":
                            yield {"type": "text", "text": event.text}
                        elif event.type == "content_block_start" and event.content_block.type == "tool_use":
                            yield {"type": "status", "text": TOOL_STATUS.get(event.content_block.name, "Working…")}
                    final = stream.get_final_message()
                    # Mirror the history: the runner keeps its own copy and doesn't expose it.
                    messages.append({"role": "assistant", "content": [_to_json(b) for b in final.content]})
                    has_tool_use = any(b.type == "tool_use" for b in final.content)
                    if final.stop_reason == "refusal" or (final.stop_reason == "max_tokens" and has_tool_use):
                        break  # don't run tools from a turn that was cut off or declined
                    tool_response = runner.generate_tool_call_response()
                    if tool_response is not None:
                        messages.append(json.loads(json.dumps(tool_response, default=_to_json)))
                break
            except ValueError:
                # Eager input streaming: a tool input the SDK couldn't parse. The broken turn was
                # never added to `messages`, so re-issue it from the mirrored history (bounded).
                json_retries += 1
                if json_retries > 2:
                    raise RuntimeError("Claude sent a malformed tool request. Please try again.")
                yield {"type": "status", "text": "Retrying…"}

        if final is None:
            raise RuntimeError("No response from Claude")
        if final.stop_reason == "refusal":
            yield {"type": "done", "answer": "Sorry, I can't help with that request.", "history": list(history)}
            return
        answer = "\n".join(b.text for b in final.content if b.type == "text").strip()
        if final.stop_reason == "max_tokens":
            answer += "\n\n_(Answer was cut off. Try a narrower question.)_"
        yield {"type": "done", "answer": answer or "I couldn't produce an answer for that.", "history": messages}


def create_agent(cw: ConnectWiseClient):
    """Build the agent for the provider chosen by LLM_PROVIDER in .env ("claude" or "ollama")."""
    provider = os.environ.get("LLM_PROVIDER", "claude").strip().lower()
    if provider == "ollama":
        from .ollama_agent import OllamaAgent

        return OllamaAgent(cw, SYSTEM_PROMPT)
    if provider != "claude":
        raise RuntimeError(f"LLM_PROVIDER must be 'claude' or 'ollama', not {provider!r}")
    return ReportingAgent(cw)
