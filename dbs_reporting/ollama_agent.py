"""Run the reporting agent on a local model through Ollama instead of Claude.

Uses Ollama's native /api/chat endpoint with tool calling, and the same tools and
system prompt as the Claude agent.
"""

import json
import os
import re
from collections.abc import Iterator

import httpx

from .connectwise import ConnectWiseClient
from .agent import TOOL_STATUS, dated
from .tools import build_tools

DEFAULT_MODEL = "qwen3:14b"
MAX_STEPS = 12


class OllamaAgent:
    def __init__(self, cw: ConnectWiseClient, system_prompt: str):
        self._system_prompt = system_prompt
        self._url = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
        self._model = os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        # Ticket lists are large; Ollama's default context window is too small for them.
        self._num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "32768"))
        self._http = httpx.Client(timeout=600)
        self._tools = {tool.name: tool for tool in build_tools(cw)}
        self._tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec["input_schema"],
                },
            }
            for spec in (tool.to_dict() for tool in self._tools.values())
        ]

    @property
    def description(self) -> str:
        return f"Ollama model {self._model} at {self._url}"

    @property
    def default_model(self) -> str:
        return self._model

    def model_choices(self) -> list[dict]:
        return [{"id": self._model, "label": f"{self._model} (local)"}]

    def _chat(self, messages: list) -> dict:
        response = self._http.post(
            f"{self._url}/api/chat",
            json={
                "model": self._model,
                "messages": messages,
                "tools": self._tool_specs,
                "stream": False,
                "options": {"num_ctx": self._num_ctx},
            },
        )
        if response.status_code == 404 and "not found" in response.text:
            raise RuntimeError(
                f"Ollama model {self._model!r} isn't downloaded. Run: ollama pull {self._model}"
            )
        response.raise_for_status()
        return response.json()["message"]

    def _run_tool(self, name: str, arguments) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"error": f"Unknown tool {name!r}. Available: {list(self._tools)}"})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return json.dumps({"error": "Tool arguments were not valid JSON."})
        try:
            return tool.call(arguments or {})
        except Exception as exc:
            return json.dumps({"error": f"Bad arguments for {name}: {exc}"})

    @property
    def models(self) -> list[str]:
        return [self._model]

    def respond(self, history: list, question: str, model: str | None = None) -> tuple[str, list]:
        """Same contract as ReportingAgent.respond. Only OLLAMA_MODEL is offered."""
        for event in self.respond_stream(history, question, model):
            if event["type"] == "done":
                return event["answer"], event["history"]
        raise RuntimeError("No response from Ollama")

    def respond_stream(self, history: list, question: str, model: str | None = None) -> Iterator[dict]:
        """Same events as ReportingAgent.respond_stream; the answer arrives in one piece."""
        if model is not None and model != self._model:
            raise ValueError(f"Model {model!r} isn't enabled. This server uses {self._model!r}.")
        system = {"role": "system", "content": self._system_prompt}
        messages = list(history) + [{"role": "user", "content": dated(question)}]

        for _ in range(MAX_STEPS):
            reply = self._chat([system] + messages)
            tool_calls = reply.get("tool_calls") or []
            messages.append({
                "role": "assistant",
                "content": reply.get("content") or "",
                **({"tool_calls": tool_calls} if tool_calls else {}),
            })
            if not tool_calls:
                break
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name", "")
                yield {"type": "status", "text": TOOL_STATUS.get(name, "Working…")}
                messages.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": self._run_tool(name, function.get("arguments")),
                })
        else:
            answer = "I couldn't finish that within the step limit. Try a narrower question."
            yield {"type": "done", "answer": answer, "history": list(history)}
            return

        # Some local models include their reasoning in <think> tags; managers don't need it.
        answer = re.sub(r"<think>.*?</think>", "", messages[-1].get("content") or "", flags=re.S).strip()
        answer = answer or "I couldn't produce an answer for that."
        yield {"type": "text", "text": answer}
        yield {"type": "done", "answer": answer, "history": messages}
