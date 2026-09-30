"""The reporting agent: Claude plus the read-only ConnectWise tools."""

import os
import threading
import uuid
from datetime import date

import anthropic

from .connectwise import ConnectWiseClient
from .tools import build_tools

MODEL = "claude-opus-5-5"

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


class ReportingAgent:
    description = f"Claude ({MODEL})"

    def __init__(self, cw: ConnectWiseClient, client: anthropic.Anthropic | None = None):
        self._client = client or anthropic.Anthropic()
        self._tools = build_tools(cw)
        self._conversations: dict[str, list] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def new_conversation(self) -> str:
        conversation_id = uuid.uuid4().hex
        with self._guard:
            self._conversations[conversation_id] = []
            self._locks[conversation_id] = threading.Lock()
        return conversation_id

    def ask(self, question: str, conversation_id: str | None = None) -> tuple[str, str]:
        """Answer a question, continuing the conversation if an id is given.

        Returns (answer_text, conversation_id).
        """
        if not conversation_id or conversation_id not in self._conversations:
            conversation_id = self.new_conversation()

        with self._locks[conversation_id]:
            history = self._conversations[conversation_id]
            # Work on a copy so a failed turn leaves the stored history untouched.
            messages = history + [{"role": "user", "content": question}]

            runner = self._client.beta.messages.tool_runner(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM_PROMPT.format(today=date.today().isoformat()),
                tools=self._tools,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                max_iterations=20,
            )

            final = None
            for message in runner:
                final = message
                # Mirror the history: the runner keeps its own copy and doesn't expose it.
                messages.append({"role": "assistant", "content": message.content})
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    messages.append(tool_response)

            if final is None:
                raise RuntimeError("No response from Claude")
            if final.stop_reason == "refusal":
                return "Sorry, I can't help with that request.", conversation_id

            answer = "\n".join(b.text for b in final.content if b.type == "text").strip()
            if final.stop_reason == "max_tokens":
                answer += "\n\n_(Answer was cut off. Try a narrower question.)_"

            self._conversations[conversation_id] = messages
            return answer or "I couldn't produce an answer for that.", conversation_id


def create_agent(cw: ConnectWiseClient):
    """Build the agent for the provider chosen by LLM_PROVIDER in .env ("claude" or "ollama")."""
    provider = os.environ.get("LLM_PROVIDER", "claude").strip().lower()
    if provider == "ollama":
        from .ollama_agent import OllamaAgent

        return OllamaAgent(cw, SYSTEM_PROMPT)
    if provider != "claude":
        raise RuntimeError(f"LLM_PROVIDER must be 'claude' or 'ollama', not {provider!r}")
    return ReportingAgent(cw)
