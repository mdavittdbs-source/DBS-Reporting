# DBS-Reporting

An AI assistant that lets managers ask plain-English questions about ConnectWise Manage data, e.g.

> What are the most common issues in the past 30 days at Joe's Pizza?

It uses Claude with a small set of **read-only** ConnectWise tools. Claude finds the company,
pulls the tickets, groups them into recurring problems from their summaries, and answers
with counts and example ticket numbers. Follow-up questions ("what about the last 90 days?",
"show me the resolution on #12345") continue the same conversation.

## How it works

```
Manager (web chat) ──► FastAPI ──► Claude (claude-opus-5-5) ──► tools ──► ConnectWise REST API
```

| Tool | What it does |
|---|---|
| `find_company` | Turns a name like "Joe's Pizza" into a ConnectWise company id |
| `get_company_tickets` | Tickets entered in the last N days, with breakdowns by type/subtype/item/board/priority/source/contact and a compact list of every ticket |
| `get_ticket_details` | One ticket plus its notes (description, internal analysis, resolution) |
| `get_company_time` | Hours logged in the last N days by technician, work type and ticket |

Code layout:

- `dbs_reporting/connectwise.py`: ConnectWise API client (auth, paging, queries)
- `dbs_reporting/tools.py`: the tools Claude can call
- `dbs_reporting/agent.py`: system prompt, the Claude tool loop and conversation memory
- `dbs_reporting/web.py` + `static/index.html`: the web chat
- `dbs_reporting/cli.py`: a terminal version for testing

## Setup

1. **ConnectWise API keys.** In ConnectWise Manage, go to *System > Members > API Members*
   and create an API member with a **read-only** security role that can see Companies,
   Service Tickets and Time Entries. Generate a public/private key pair for it. Get a
   `clientId` from <https://developer.connectwise.com/ClientID>.
2. **Anthropic API key** from <https://console.anthropic.com>.
3. Configure and install:

   ```bash
   cp .env.example .env        # fill in the values
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

## Run

Web chat (then open http://localhost:8000):

```bash
uvicorn dbs_reporting.web:app --host 0.0.0.0 --port 8000
```

Terminal:

```bash
python -m dbs_reporting.cli
```

Tests (no network or API keys needed):

```bash
pytest
```

## Running on a local model (Ollama)

Set `LLM_PROVIDER=ollama` in `.env` to use a free local model through
[Ollama](https://ollama.com) instead of Claude. Pick a model that supports tool calling,
download it with `ollama pull <model>`, and set `OLLAMA_MODEL` to its name. Run
`python -m dbs_reporting.check` to confirm Ollama is reachable. Answers are usually
slower and less accurate than Claude's, especially for clients with many tickets.
Set `LLM_PROVIDER=claude` to switch back.

## Notes

- **Access control.** Set `APP_USERNAME` / `APP_PASSWORD` to require a login. Run it on the
  internal network or behind your VPN/SSO proxy; it exposes client data to anyone who can log in.
- **Conversations** are kept in server memory and are lost on restart.
- **Limits.** A single ticket query is capped at 1000 tickets. Claude is told when this
  happens so it can suggest a narrower date range.
- **Adding questions.** To support a new kind of question (agreements, configurations,
  projects, etc.), add a method to `connectwise.py` and a `@beta_tool` function in `tools.py`.
  Claude picks it up automatically.
- **Model.** Uses `claude-opus-5-5` with adaptive thinking at `medium` effort, and has
  server-side refusal fallbacks turned on (`fallbacks="default"`).
