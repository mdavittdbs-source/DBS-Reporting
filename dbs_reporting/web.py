"""Web chat for managers: `uvicorn dbs_reporting.web:app`.

Each person signs in with their own login (listed in users.txt, see userfile.py) and sees
only their own saved chats.
"""

import logging
import threading
import time
from collections import defaultdict
from pathlib import Path

import anthropic
import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

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
    return {"username": user["username"], "display_name": user["display_name"]}


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
        "turns": store.turns(conversation_id),
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


def _answer(user: dict, request: ChatRequest, question: str) -> ChatResponse:
    if request.conversation_id:
        found = store.get_conversation(user["id"], request.conversation_id)
        if found is None:
            raise HTTPException(404, "Chat not found.")
        conversation_id, model, title = found["id"], found["model"], found["title"]
        if model not in agent.models:
            raise HTTPException(
                400, f"This chat used {model}, which is no longer enabled. Start a new chat."
            )
    else:
        model = request.model or agent.default_model
        if model not in agent.models:
            raise HTTPException(400, f"Model {model!r} isn't enabled.")
        conversation_id, title = None, question

    lock = _conversation_locks[conversation_id] if conversation_id else threading.Lock()
    with lock:
        history = store.get_conversation(user["id"], conversation_id)["history"] if conversation_id else []
        answer, history = agent.respond(history, question, model)
        if conversation_id is None:
            conversation_id = store.create_conversation(user["id"], title, model)
        store.save_turn(conversation_id, question, answer, history)
    return ChatResponse(answer=answer, conversation_id=conversation_id, title=title[:80])


@app.post("/api/chat")
async def chat(request: ChatRequest, user: dict = Depends(current_user)) -> ChatResponse:
    question = request.question.strip()
    if not question:
        raise HTTPException(400, "Question is empty")
    try:
        return await run_in_threadpool(_answer, user, request, question)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except anthropic.RateLimitError:
        raise HTTPException(429, "The AI service is busy. Please try again in a minute.")
    except anthropic.APIStatusError as exc:
        log.exception("Claude API error")
        raise HTTPException(502, f"AI service error ({exc.status_code}).")
    except anthropic.APIConnectionError:
        log.exception("Claude API connection error")
        raise HTTPException(502, "Couldn't reach the AI service.")
    except httpx.ConnectError:
        log.exception("Ollama connection error")
        raise HTTPException(502, "Couldn't reach Ollama. Is it running on the server?")
    except httpx.TimeoutException:
        log.exception("Ollama timeout")
        raise HTTPException(504, "The local AI model took too long. Try a narrower question.")
    except RuntimeError as exc:
        log.exception("Agent error")
        raise HTTPException(502, str(exc))
