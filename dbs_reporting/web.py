"""Web chat for managers: `uvicorn dbs_reporting.web:app`."""

import logging
import os
import secrets
from pathlib import Path

import anthropic
import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .agent import create_agent
from .config import ConnectWiseSettings
from .connectwise import ConnectWiseClient

log = logging.getLogger(__name__)
security = HTTPBasic(auto_error=False)
STATIC = Path(__file__).parent / "static"


def require_login(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return
    username = os.environ.get("APP_USERNAME", "manager")
    ok = credentials is not None and (
        secrets.compare_digest(credentials.username.encode(), username.encode())
        & secrets.compare_digest(credentials.password.encode(), password.encode())
    )
    if not ok:
        raise HTTPException(401, "Login required", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="DBS Reporting Assistant", dependencies=[Depends(require_login)])
agent = create_agent(ConnectWiseClient(ConnectWiseSettings.from_env()))


class ChatRequest(BaseModel):
    question: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    conversation_id: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/api/chat")
async def chat(request: ChatRequest) -> ChatResponse:
    question = request.question.strip()
    if not question:
        raise HTTPException(400, "Question is empty")
    try:
        answer, conversation_id = await run_in_threadpool(agent.ask, question, request.conversation_id)
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
    return ChatResponse(answer=answer, conversation_id=conversation_id)
