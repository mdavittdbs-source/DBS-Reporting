# DBS-Reporting

An AI assistant that lets managers ask plain-English questions about ConnectWise Manage data, e.g.

> What are the most common issues in the past 30 days at Jimmy's Grille?

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

## Logins

Everyone signs in with their own account and sees only their own saved chats. Accounts are
listed in **`users.txt`** in the project folder, created automatically on first run. Edit it
in any editor, one person per line:

```
username | Display Name | password | admin
jsmith   | Jane Smith   | Welcome2026! |
mdavitt  | Matt Davitt  | S0mething-Long | admin
```

- Type plain passwords. The bot replaces them with a scrambled (hashed) version the next
  time it reads the file, within seconds of saving.
- To change a password, type a new one over the scrambled text.
- Delete a line to remove access. That person is signed out immediately, and their chats
  are kept if you add them back.
- Run `python -m dbs_reporting.users` to apply the file now and see any problems.

`users.txt` and the chat database (`data/dbs_reporting.db`, override with `DB_PATH`) are
gitignored. Back up both.

## Logo

Save your logo as `branding/logo.svg` (or `.png`, `.webp`, `.jpg`). It replaces the built-in
logo in the sidebar, on the sign-in page and in the browser tab. Add `branding/logo-dark.*` for a
version shown to people using dark mode. See `branding/README.md`.

## Run

Web chat (then open http://localhost:8000 and sign in):

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

- **Access control.** Run it on the internal network or VPN only; anyone with a login can ask
  about any client. Five wrong passwords lock a username for 15 minutes. Sign-ins last 14 days.
- **Chats** are saved per person with their full history, so follow-up questions keep context
  even after a restart.
- **Streaming.** Answers appear as they're written, with status lines ("Pulling tickets from
  ConnectWise…") while data is fetched. The web chat uses `POST /api/chat/stream`
  (one JSON event per line); `POST /api/chat` still returns the whole answer at once.
- **Limits.** A single ticket query is capped at 1000 tickets. Claude is told when this
  happens so it can suggest a narrower date range.
- **Adding questions.** To support a new kind of question (agreements, configurations,
  projects, etc.), add a method to `connectwise.py` and a `@beta_tool` function in `tools.py`.
  Claude picks it up automatically.
- **Models.** Defaults to `claude-sonnet-5-5`. Set `CLAUDE_MODEL` in `.env` to change the default,
  and list several in `CLAUDE_MODELS` to show a model picker next to the send button. The model is
  chosen when a chat starts and stays fixed for that chat; start a new chat to use another one.