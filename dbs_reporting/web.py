"""Web chat for managers: `uvicorn dbs_reporting.web:app`.

Each person signs in with their own login (listed in users.txt, see userfile.py) and sees
only their own saved chats.
"""

import json
import logging
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import anthropic
import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from . import activity
from .agent import create_agent
from .config import ConnectWiseSettings
from .connectwise import ConnectWiseClient
from .store import SESSION_DAYS, Store
from .userfile import UsersFile

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "dbs_session"

app = FastAPI(title="DBS Automated Virtual Information Desk")
store = Store()
users_file = UsersFile(store)
users_file.ensure_exists()
users_file.refresh()
agent = create_agent(ConnectWiseClient(ConnectWiseSettings.from_env()))

# One question at a time per conversation, so two tabs can't interleave a chat's history.
_conversation_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

# Simple brute-force protection: 5 failed logins for a username locks it for 15 minutes.
_failed_logins: dict[str, list[float]] = defaultdict(list)
MAX_FAILURES, LOCKOUT_SECONDS = 5, 15 * 60


def current_user(dbs_session: str | None = Cookie(default=None)) -> dict:
    users_file.refresh()
    user = store.session_user(dbs_session) if dbs_session else None
    if user is None:
        raise HTTPException(401, "Please sign in.")
    return user


# --- Pages ---------------------------------------------------------------


@app.get("/")
def index(dbs_session: str | None = Cookie(default=None)):
    users_file.refresh()
    if not dbs_session or store.session_user(dbs_session) is None:
        return RedirectResponse("/login")
    return FileResponse(STATIC / "index.html")


BRANDING = Path(__file__).resolve().parent.parent / "branding"
LOGO_TYPES = ("svg", "png", "webp", "jpg", "jpeg")


def _branding_file(stem: str) -> FileResponse:
    for ext in LOGO_TYPES:
        path = BRANDING / f"{stem}.{ext}"
        if path.is_file():
            return FileResponse(path, headers={"Cache-Control": "no-cache"})
    raise HTTPException(404, f"No {stem} in the branding folder.")


@app.get("/logo")
def logo() -> FileResponse:
    """Your logo, if one is saved in the branding folder. Public so the sign-in page can show it."""
    return _branding_file("logo")


@app.get("/logo-dark")
def logo_dark() -> FileResponse:
    """Optional version of the logo for dark mode (branding/logo-dark.*)."""
    return _branding_file("logo-dark")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(STATIC / "login.html")


# --- Sign in / out -------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginRequest, response: Response) -> dict:
    key = body.username.strip().lower()
    recent = [t for t in _failed_logins[key] if time.time() - t < LOCKOUT_SECONDS]
    _failed_logins[key] = recent
    if len(recent) >= MAX_FAILURES:
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")

    users_file.refresh()
    if store.user_count() == 0:
        raise HTTPException(403, "No logins have been set up yet. Ask your admin to add you to users.txt.")
    user = store.authenticate(body.username, body.password)
    if user is None:
        _failed_logins[key].append(time.time())
        raise HTTPException(401, "Wrong username or password.")
    _failed_logins.pop(key, None)

    response.set_cookie(
        COOKIE, store.create_session(user["id"]),
        max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
    )
    return {"username": user["username"], "display_name": user["display_name"]}


@app.post("/api/logout")
def logout(response: Response, dbs_session: str | None = Cookie(default=None)) -> dict:
    if dbs_session:
        store.delete_session(dbs_session)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"username": user["username"], "display_name": user["display_name"], "is_admin": bool(user["is_admin"])}


# --- Models and chats ----------------------------------------------------


@app.get("/api/models")
def models(user: dict = Depends(current_user)) -> dict:
    return {"default": agent.default_model, "models": agent.model_choices()}


@app.get("/api/conversations")
def conversations(user: dict = Depends(current_user)) -> list[dict]:
    return store.list_conversations(user["id"])


@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, user: dict = Depends(current_user)) -> dict:
    found = store.get_conversation(user["id"], conversation_id)
    if found is None:
        raise HTTPException(404, "Chat not found.")
    return {
        "id": found["id"], "title": found["title"], "model": found["model"],
        "turns": [
            {**t, "usage": t["usage"] if user["is_admin"] else None} for t in store.turns(conversation_id)
        ],
    }


class RenameRequest(BaseModel):
    title: str


@app.patch("/api/conversations/{conversation_id}")
def rename(conversation_id: str, body: RenameRequest, user: dict = Depends(current_user)) -> dict:
    if not body.title.strip() or not store.rename_conversation(user["id"], conversation_id, body.title):
        raise HTTPException(404, "Chat not found.")
    return {"ok": True}


@app.delete("/api/conversations/{conversation_id}")
def delete(conversation_id: str, user: dict = Depends(current_user)) -> dict:
    if not store.delete_conversation(user["id"], conversation_id):
        raise HTTPException(404, "Chat not found.")
    return {"ok": True}


class ChatRequest(BaseModel):
    question: str
    conversation_id: str | None = None
    model: str | None = None


class ChatResponse(BaseModel):
    answer: str
    conversation_id: str
    title: str
    model: str
    usage: dict | None = None  # token usage and estimated cost; admins only


def _resolve(user: dict, request: ChatRequest) -> tuple[str | None, str, str]:
    """Check the request and work out (conversation_id or None for a new chat, title, model)."""
    if request.conversation_id:
        found = store.get_conversation(user["id"], request.conversation_id)
        if found is None:
            raise HTTPException(404, "Chat not found.")
        # A chat keeps the model it started with, so answers stay consistent and Claude keeps its
        # full reasoning. If that model has since been disabled, carry on with the default.
        model = found["model"]
        if model not in agent.models:
            model = agent.default_model
        return found["id"], found["title"], model
    model = request.model or agent.default_model
    if model not in agent.models:
        raise HTTPException(400, f"Model {model!r} isn't enabled.")
    return None, request.question.strip(), model


def _run(user: dict, question: str, conversation_id: str | None, title: str, model: str) -> Iterator[dict]:
    """Stream the agent's events, saving the chat when the answer is complete.

    Ends with {"type": "done", "answer", "conversation_id", "title", "model"}.
    """
    lock = _conversation_locks[conversation_id] if conversation_id else threading.Lock()
    started = time.monotonic()
    with lock:
        history = store.get_conversation(user["id"], conversation_id)["history"] if conversation_id else []
        try:
            for event in agent.respond_stream(history, question, model):
                if event["type"] != "done":
                    yield event
                    continue
                if conversation_id is None:
                    conversation_id = store.create_conversation(user["id"], title, model)
                usage = event.get("usage")
                store.save_turn(conversation_id, question, event["answer"], event["history"], model, usage)
                activity.log_answer(user, conversation_id, model, question, event["answer"],
                                    event["history"][len(history):], usage, time.monotonic() - started)
                yield {"type": "done", "answer": event["answer"], "conversation_id": conversation_id,
                       "title": title[:80], "model": model, "usage": usage if user["is_admin"] else None}
        except Exception as exc:
            activity.log_error(user, conversation_id, model, question, _friendly_error(exc)[1])
            raise


def _api_error_message(exc: anthropic.APIStatusError) -> str:
    """The explanation the Claude API gave, e.g. which part of the request it rejected."""
    body = exc.body if isinstance(exc.body, dict) else {}
    message = (body.get("error") or {}).get("message") or exc.message or ""
    return message[:400] or "no details were given."


def _friendly_error(exc: Exception) -> tuple[int, str]:
    """HTTP status and a message people can act on, for errors while answering."""
    if isinstance(exc, ValueError):
        return 400, str(exc)
    if isinstance(exc, anthropic.RateLimitError):
        return 429, "The AI service is busy. Please try again in a minute."
    if isinstance(exc, anthropic.APIStatusError):
        log.exception("Claude API error")
        return 502, f"AI service error ({exc.status_code}): {_api_error_message(exc)}"
    if isinstance(exc, anthropic.APIConnectionError):
        log.exception("Claude API connection error")
        return 502, "Couldn't reach the AI service."
    if isinstance(exc, httpx.ConnectError):
        log.exception("Ollama connection error")
        return 502, "Couldn't reach Ollama. Is it running on the server?"
    if isinstance(exc, httpx.TimeoutException):
        log.exception("Ollama timeout")
        return 504, "The local AI model took too long. Try a narrower question."
    if isinstance(exc, RuntimeError):
        log.exception("Agent error")
        return 502, str(exc)
    log.exception("Unexpected error while answering")
    return 500, "Something went wrong on the server. Please try again."


def _answer(user: dict, request: ChatRequest) -> ChatResponse:
    conversation_id, title, model = _resolve(user, request)
    for event in _run(user, request.question.strip(), conversation_id, title, model):
        if event["type"] == "done":
            return ChatResponse(**{k: v for k, v in event.items() if k != "type"})
    raise RuntimeError("No answer was produced.")


@app.post("/api/chat")
async def chat(request: ChatRequest, user: dict = Depends(current_user)) -> ChatResponse:
    """Ask a question and get the whole answer at once."""
    if not request.question.strip():
        raise HTTPException(400, "Question is empty")
    try:
        return await run_in_threadpool(_answer, user, request)
    except HTTPException:
        raise
    except Exception as exc:
        status, message = _friendly_error(exc)
        raise HTTPException(status, message)


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest, user: dict = Depends(current_user)) -> StreamingResponse:
    """Ask a question and receive the answer as it's written: one JSON event per line
    (status / reset / text, then done, or error)."""
    if not request.question.strip():
        raise HTTPException(400, "Question is empty")
    conversation_id, title, model = await run_in_threadpool(_resolve, user, request)

    def lines() -> Iterator[str]:
        try:
            for event in _run(user, request.question.strip(), conversation_id, title, model):
                yield json.dumps(event) + "\n"
        except Exception as exc:
            _, message = _friendly_error(exc)
            yield json.dumps({"type": "error", "message": message}) + "\n"

    return StreamingResponse(
        lines(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
