"""Web chat for managers: `uvicorn dbs_reporting.web:app`.

Each person signs in with their own login (listed in users.txt, see userfile.py) and sees
only their own saved chats.
"""

import base64
import json
import logging
import os
import queue
import re
import threading
import time
from typing import Literal
from collections import defaultdict
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import activity, digest, exports, todo
from .charts import extract_charts
from .agent import create_agent
from .config import ConnectWiseSettings
from .connectwise import ConnectWiseClient
from .store import SESSION_DAYS, Store
from .userfile import UsersFile

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "dbs_session"

store = Store()
users_file = UsersFile(store)
users_file.ensure_exists()
users_file.refresh()
cw_settings = ConnectWiseSettings.from_env()
cw = ConnectWiseClient(cw_settings)
agent = create_agent(cw)


@asynccontextmanager
async def lifespan(_app):
    # The weekly digest goes out the first time David is running on or after Monday morning.
    scheduler = digest.DigestScheduler(cw, store, cw_settings.ticket_url)
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="DBS Autonomous Virtual Information Desk", lifespan=lifespan)

# One question at a time per conversation, so two tabs can't interleave a chat's history.
_conversation_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

# Simple brute-force protection: 5 failed logins for a username locks it for 15 minutes.
_failed_logins: dict[str, list[float]] = defaultdict(list)
MAX_FAILURES, LOCKOUT_SECONDS = 5, 15 * 60


def _forget_old_failures() -> None:
    """Drop names whose failed logins have all expired, so the record doesn't grow forever."""
    if len(_failed_logins) < 500:
        return
    cutoff = time.time() - LOCKOUT_SECONDS
    for name, times in list(_failed_logins.items()):
        if not times or times[-1] < cutoff:
            _failed_logins.pop(name, None)


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
    return _page("index.html")


BRANDING = Path(__file__).resolve().parent.parent / "branding"
LOGO_TYPES = ("svg", "png", "webp", "jpg", "jpeg")


LOGO_MIME = {"svg": "image/svg+xml", "png": "image/png", "webp": "image/webp", "jpg": "image/jpeg",
             "jpeg": "image/jpeg"}
INLINE_LOGO_BYTES = 150_000  # bigger logos are linked instead of embedded in every page


def _logo_path(stem: str) -> Path | None:
    for ext in LOGO_TYPES:
        path = BRANDING / f"{stem}.{ext}"
        if path.is_file():
            return path
    return None


def _branding_file(stem: str) -> FileResponse:
    path = _logo_path(stem)
    if path is None:
        raise HTTPException(404, f"No {stem} in the branding folder.")
    return FileResponse(path, headers={"Cache-Control": "no-cache"})


def _logo_src(path: Path, url: str) -> str:
    """The logo embedded in the page (data URI), so it's drawn with the page instead of after it."""
    data = path.read_bytes()
    if len(data) > INLINE_LOGO_BYTES:
        return url
    mime = LOGO_MIME[path.suffix.lower().lstrip(".")]
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


_pages: dict[str, tuple[tuple, str]] = {}


def _page(name: str) -> HTMLResponse:
    """Serve a page with the branding logo already in place. Without this, the built-in icon
    showed for a moment before a script swapped the logo in. Rebuilt when any file changes."""
    page, light, dark = STATIC / name, _logo_path("logo"), _logo_path("logo-dark")
    key = tuple((p, p.stat().st_mtime) for p in (page, light, dark) if p)
    cached = _pages.get(name)
    if cached and cached[0] == key:
        return HTMLResponse(cached[1], headers={"Cache-Control": "no-cache"})
    html = page.read_text(encoding="utf-8")
    if light:
        source = (f'<source data-dark srcset="{_logo_src(dark, "/logo-dark")}" media="(prefers-color-scheme: dark)">'
                  if dark else "")
        mark = (f'<span class="brand-mark has-logo" aria-hidden="true"><picture>{source}'
                f'<img src="{_logo_src(light, "/logo")}" alt=""></picture></span>')
        icon = '<link rel="icon" href="/logo">' + (
            '<link rel="icon" href="/logo-dark" media="(prefers-color-scheme: dark)">' if dark else "")
        html = re.sub(r"<!--brand-mark-->.*?<!--/brand-mark-->", lambda _: mark, html, count=1, flags=re.S)
        html = re.sub(r"<!--icon-->.*?<!--/icon-->", lambda _: icon, html, count=1, flags=re.S)
    _pages[name] = (key, html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/logo")
def logo() -> FileResponse:
    """Your logo, if one is saved in the branding folder. Public so the sign-in page can show it."""
    return _branding_file("logo")


@app.get("/logo-dark")
def logo_dark() -> FileResponse:
    """Optional version of the logo for dark mode (branding/logo-dark.*)."""
    return _branding_file("logo-dark")


# Bundled front-end libraries (Chart.js), served from here so charts work without a CDN.
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/api/answers/{answer_id}/export.xlsx")
def export_xlsx(answer_id: int, user: dict = Depends(current_user)) -> Response:
    """Download the tables and charts in one of your answers as Excel (not the rest of the chat)."""
    answer = store.get_answer(user["id"], answer_id)
    if answer is None:
        raise HTTPException(404, "Answer not found.")
    if not exports.has_report(answer):
        raise HTTPException(404, "This answer has no table or chart to download.")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", answer.get("title") or "report").strip("-")[:50] or "report"
    return Response(
        exports.answer_workbook(answer),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="David-{slug}-{answer_id}.xlsx"'},
    )


@app.get("/login")
def login_page(dbs_session: str | None = Cookie(default=None)):
    users_file.refresh()
    if dbs_session and store.session_user(dbs_session):  # already signed in
        return RedirectResponse("/")
    return _page("login.html")


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
        _forget_old_failures()
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
    return {"username": user["username"], "display_name": user["display_name"], "is_admin": bool(user["is_admin"]),
            "ticket_url": cw_settings.ticket_url}


# --- Models and chats ----------------------------------------------------


@app.get("/api/models")
def models(user: dict = Depends(current_user)) -> dict:
    return {"default": agent.default_model, "models": agent.model_choices()}


@app.get("/api/conversations")
def conversations(user: dict = Depends(current_user)) -> list[dict]:
    return store.list_conversations(user["id"])


@app.get("/api/conversations/{conversation_id}")
def conversation(conversation_id: str, user: dict = Depends(current_user)) -> dict:
    found = store.get_conversation(user["id"], conversation_id, with_history=False)
    if found is None:
        raise HTTPException(404, "Chat not found.")
    ratings = store.feedback_in_chat(user["id"], conversation_id)
    return {
        "id": found["id"], "title": found["title"], "model": found["model"],
        "turns": [
            {**t, "usage": t["usage"] if user["is_admin"] else None, "feedback": ratings.get(t["id"])}
            for t in store.turns(conversation_id)
        ],
    }


class FeedbackRequest(BaseModel):
    rating: int  # 1 thumbs up, -1 thumbs down, 0 take it back
    comment: str = ""


@app.post("/api/answers/{answer_id}/feedback")
def feedback(answer_id: int, body: FeedbackRequest, user: dict = Depends(current_user)) -> dict:
    """Thumbs up / down (with an optional note) on one of your answers."""
    if body.rating not in (-1, 0, 1):
        raise HTTPException(400, "rating must be 1, -1 or 0.")
    if not store.set_feedback(user["id"], answer_id, body.rating, body.comment):
        raise HTTPException(404, "Answer not found.")
    return {"ok": True}


def _require_admin(user: dict) -> None:
    if not user["is_admin"]:
        raise HTTPException(403, "Only admins can see feedback.")


@app.get("/api/feedback")
def all_feedback(user: dict = Depends(current_user)) -> dict:
    """Everyone's ratings and notes, newest first (admins only)."""
    _require_admin(user)
    rows = store.list_feedback()
    return {"items": rows, "up": sum(r["rating"] > 0 for r in rows), "down": sum(r["rating"] < 0 for r in rows)}


# --- To Do -----------------------------------------------------------------

_todo_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)


def _todo_view(user: dict, saved: dict | None) -> dict:
    if saved is None:
        return {"list": None}
    view = {"list": saved["data"], "done": saved["done"], "removed": saved["removed"], "created_at": saved["created_at"],
            "model": saved["model"]}
    if user["is_admin"]:
        view["usage"] = saved["usage"]
    return view


@app.get("/api/todo")
def get_todo(user: dict = Depends(current_user)) -> dict:
    """Your latest to-do list and what you've ticked off."""
    return _todo_view(user, store.get_todo(user["id"]))


def _make_todo(user: dict) -> dict:
    with _todo_locks[user["id"]]:  # one at a time per person, so a double click doesn't pay twice
        started = time.monotonic()
        model = os.environ.get("TODO_MODEL", "").strip() or agent.default_model
        data = todo.make(cw, agent.client, model, user, store.todo_dismissed(user["id"]))
        used = data.pop("usage")
        store.save_todo(user["id"], data, model, used)
        activity.log_todo(user, data, model, used, time.monotonic() - started)
        return _todo_view(user, store.get_todo(user["id"]))


@app.post("/api/todo")
async def make_todo(user: dict = Depends(current_user)) -> dict:
    """Make a fresh to-do list from your open ConnectWise tickets and calendar (replaces the last one)."""
    try:
        return await run_in_threadpool(_make_todo, user)
    except todo.TodoError as exc:
        raise HTTPException(400, str(exc))
    except httpx.HTTPStatusError as exc:
        log.warning("To Do: ConnectWise returned HTTP %s", exc.response.status_code)
        raise HTTPException(502, f"ConnectWise returned an error (HTTP {exc.response.status_code}). Try again shortly.")
    except Exception as exc:
        status, message = _friendly_error(exc)
        raise HTTPException(status, message)


TODO_ID = r"^[A-Za-z0-9_-]{1,40}$"


class TodoTick(BaseModel):
    item: str = Field(pattern=TODO_ID)
    done: bool


@app.post("/api/todo/done")
def tick_todo(body: TodoTick, user: dict = Depends(current_user)) -> dict:
    if not store.set_todo_done(user["id"], body.item, body.done):
        raise HTTPException(404, "That item isn't on your list any more.")
    return {"ok": True}


class TodoItem(BaseModel):
    id: str = Field(pattern=TODO_ID)
    priority: Literal["now", "today", "this_week", "later"]
    mine: bool = False
    title: str = Field(default="", max_length=200)
    why: str = Field(default="", max_length=500)
    ticket: int | None = Field(default=None, ge=1, le=999_999_999)  # the page allows 9 digits


class TodoItems(BaseModel):
    items: list[TodoItem] = Field(max_length=100)
    remove: list[str] = Field(default=[], max_length=100)  # ids taken off; anything else not listed stays


@app.put("/api/todo/items")
def arrange_todo(body: TodoItems, user: dict = Depends(current_user)) -> dict:
    """Save your list as you've arranged it: order, groups, items removed, and items you've added or edited."""
    for item in body.items:
        if item.mine and not item.title.strip():
            raise HTTPException(422, "Give the to-do a title.")
    items = [{**item.model_dump(), "title": item.title.strip(), "why": item.why.strip()} for item in body.items]
    saved = store.set_todo_items(user["id"], items, body.remove)
    if saved is None:
        raise HTTPException(404, "Make a list first.")
    return {"items": saved}


class TodoDelete(BaseModel):
    ids: list[str] | None = Field(default=None, max_length=100)  # None: everything in Removed
    undo: bool = False


@app.post("/api/todo/removed/delete")
def delete_removed(body: TodoDelete, user: dict = Depends(current_user)) -> dict:
    """Delete items from Removed for good (or undo that). Deleted tickets still stay off new lists."""
    removed = store.delete_removed(user["id"], body.ids, body.undo)
    if removed is None:
        raise HTTPException(404, "Make a list first.")
    return {"removed": removed}


@app.get("/todo")
def todo_page():
    return RedirectResponse("/#todo")


@app.get("/api/feedback/recent")
def recent_feedback(user: dict = Depends(current_user)) -> dict:
    """How many thumbs down came in over the last 7 days, for the admins' Feedback icon (counts only,
    so the chat page doesn't download every rated answer to show one number)."""
    _require_admin(user)
    return {"down_this_week": store.feedback_count(rating=-1, days=7)}


@app.get("/feedback")
def feedback_page(dbs_session: str | None = Cookie(default=None)):
    users_file.refresh()
    user = store.session_user(dbs_session) if dbs_session else None
    if user is None:
        return RedirectResponse("/login")
    if not user["is_admin"]:
        return RedirectResponse("/")
    return _page("feedback.html")


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
    answer_id: int | None = None
    charts: list[dict] = []
    usage: dict | None = None  # token usage and estimated cost; admins only


# A question goes to Claude in full, and again with every follow-up in that chat, so an accidental paste of
# a whole spreadsheet would cost a lot. 20,000 characters is plenty for a long email thread.
MAX_QUESTION = 20_000


def _check_question(request: ChatRequest) -> None:
    question = request.question.strip()
    if not question:
        raise HTTPException(400, "Question is empty")
    if len(question) > MAX_QUESTION:
        raise HTTPException(400, f"That question is too long ({len(question):,} characters). Keep it under "
                                 f"{MAX_QUESTION:,}: paste just the part David needs.")


def _resolve(user: dict, request: ChatRequest) -> tuple[str | None, str, str]:
    """Check the request and work out (conversation_id or None for a new chat, title, model)."""
    if request.conversation_id:
        found = store.get_conversation(user["id"], request.conversation_id, with_history=False)
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
        history = []
        if conversation_id:
            found = store.get_conversation(user["id"], conversation_id)
            if found is None:  # deleted in another tab since the question was sent
                raise ValueError("Chat not found. It may have been deleted.")
            history = found["history"]
        try:
            for event in agent.respond_stream(history, question, model):
                if event["type"] != "done":
                    yield event
                    continue
                if conversation_id is None:
                    conversation_id = store.create_conversation(user["id"], title, model)
                usage = event.get("usage")
                new_messages = event["history"][len(history):]
                charts = extract_charts(new_messages)
                answer_id = store.save_turn(conversation_id, question, event["answer"], event["history"],
                                            model, usage, charts)
                activity.log_answer(user, conversation_id, model, question, event["answer"],
                                    new_messages, usage, time.monotonic() - started)
                yield {"type": "done", "answer": event["answer"], "conversation_id": conversation_id,
                       "title": title[:80], "model": model, "answer_id": answer_id, "charts": charts,
                       "usage": usage if user["is_admin"] else None}
        except Exception as exc:
            activity.log_error(user, conversation_id, model, question, _friendly_error(exc)[1])
            raise


def _api_error_message(exc: anthropic.APIStatusError) -> str:
    """The explanation the Claude API gave, e.g. which part of the request it rejected."""
    body = exc.body if isinstance(exc.body, dict) else {}
    message = (body.get("error") or {}).get("message") or exc.message or ""
    return message[:400] or "no details were given."


def _friendly_error(exc: Exception, log_it: bool = True) -> tuple[int, str]:
    """HTTP status and a message people can act on, for errors while answering. Errors are
    logged where they happen (in _run), so callers passing the same error on use log_it=False."""
    if isinstance(exc, ValueError):
        return 400, str(exc)
    if isinstance(exc, anthropic.RateLimitError):
        return 429, "The AI service is busy. Please try again in a minute."
    if isinstance(exc, anthropic.APIStatusError):
        if log_it:
            log.exception("Claude API error")
        return 502, f"AI service error ({exc.status_code}): {_api_error_message(exc)}"
    if isinstance(exc, anthropic.APIConnectionError):
        if log_it:
            log.exception("Claude API connection error")
        return 502, "Couldn't reach the AI service."
    if isinstance(exc, RuntimeError):
        if log_it:
            log.exception("Agent error")
        return 502, str(exc)
    if log_it:
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
    _check_question(request)
    try:
        return await run_in_threadpool(_answer, user, request)
    except HTTPException:
        raise
    except Exception as exc:
        status, message = _friendly_error(exc, log_it=False)
        raise HTTPException(status, message)


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest, user: dict = Depends(current_user)) -> StreamingResponse:
    """Ask a question and receive the answer as it's written: one JSON event per line
    (status / reset / text, then done, or error)."""
    _check_question(request)
    conversation_id, title, model = await run_in_threadpool(_resolve, user, request)
    events: queue.Queue = queue.Queue()

    def work() -> None:
        # The answer is written on its own thread, so it's still finished and saved if the page is
        # closed or reloaded partway through (it shows up when the chat is opened again).
        try:
            for event in _run(user, request.question.strip(), conversation_id, title, model):
                events.put(event)
        except Exception as exc:
            _, message = _friendly_error(exc, log_it=False)
            events.put({"type": "error", "message": message})
        finally:
            events.put(None)

    threading.Thread(target=work, name="answer", daemon=True).start()

    def lines() -> Iterator[str]:
        while (event := events.get()) is not None:
            yield json.dumps(event) + "\n"

    return StreamingResponse(
        lines(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
