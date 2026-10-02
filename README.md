# DBS-Reporting

An AI assistant that lets managers ask plain-English questions about ConnectWise Manage data, e.g.

> What are the most common issues in the past 30 days at Jimmy's Grille?

It uses Claude with a small set of **read-only** ConnectWise tools. Claude finds the company,
pulls the tickets, groups them into recurring problems from their summaries, and answers
with counts and example ticket numbers. Follow-up questions ("what about the last 90 days?",
"show me the resolution on #12345") continue the same conversation.

## How it works

```
Manager (web chat) ──► FastAPI ──► Claude (claude-sonnet-5-5) ──► tools ──► ConnectWise REST API
```

| Tool | What it does |
|---|---|
| `find_company` | Turns a name like "Joe's Pizza" into a ConnectWise company id |
| `get_company_tickets` | Tickets entered in the last N days, with breakdowns by type/subtype/item/board/priority/source/contact and a compact list of every ticket |
| `get_ticket_details` | One service or project ticket plus its notes (description, internal analysis, resolution) |
| `get_company_time` | Hours logged in the last N days by technician, work type and ticket |
| `get_ticket_totals` | Tickets across **all** clients in the last N days, ranked by client, site, board, type, priority, source or status, with open counts and top ticket types |
| `get_sla_performance` | In-SLA vs. breached tickets and first-response/resolution times, by client, board, priority or SLA |
| `get_open_tickets` | Every open ticket however old (for one client or all), oldest first, with age buckets and counts by status, board, priority and owner |
| `get_projects` | Projects for one client or all: status, manager, dates, percent complete, budget vs actual hours |
| `get_project_tickets` | Project tickets (tasks) for a project or client, by project, phase and status, with hours |
| `create_chart` | Adds a chart under the answer (bar, horizontal bar, line or stacked bar) |

Code layout:

- `dbs_reporting/connectwise.py`: ConnectWise API client (auth, paging, queries)
- `dbs_reporting/tools.py`: the tools Claude can call
- `dbs_reporting/agent.py`: system prompt, the Claude tool loop and conversation memory
- `dbs_reporting/charts.py` + `exports.py`: chart checks and the Excel export
- `dbs_reporting/web.py` + `static/index.html`: the web chat (`static/theme.css` holds the colours,
  moving background and shared styles used by both the sign-in and chat pages)
- `dbs_reporting/cli.py`: a terminal version for testing

## Setup

1. **ConnectWise API keys.** In ConnectWise Manage, go to *System > Members > API Members*
   and create an API member with a **read-only** security role that can see Companies,
   Service Tickets, Projects (including project tickets) and Time Entries. Generate a public/private key pair for it. Get a
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

## Charts and exports

Ask for a chart ("chart tickets by site for the last 30 days", "graph weekly tickets at Jimmy's
Grille") and David draws bar, horizontal bar, line or stacked bar charts under its answer, using the
numbers it pulled from ConnectWise. Charts cost extra tokens, so David only draws one when the question
says chart, graph, plot, visual(ize), diagram, pie or histogram; otherwise the chart tool refuses. Answers that include a chart or a table get two
download buttons. Both contain only the charts and tables, not the rest of the chat:

- **Excel** downloads an `.xlsx` with every table as its own sheet, and every chart as a sheet with
  its data and a native Excel chart.
- **PDF** opens a printable report (logo, tables and charts); choose *Save as PDF*.
- Each chart has **Show table** (the numbers behind it) and **Download PNG** (for emails and slides).

Charts are saved with the chat, so they come back when you reopen it. The page's libraries
(Chart.js, marked and DOMPurify) are bundled in `dbs_reporting/static/vendor`, so the chat works
even where the office network blocks public CDNs.

## Token usage and cost

Every answer records the tokens it used (across all of its steps) and an estimated cost.

- **Admins** see a line under each answer, e.g. `20.3k in · 1.0k out · ≈ $0.031`; hover it for the
  breakdown (new vs cached input, output, number of API calls). Other users don't see it.
- **Summary:** `python -m dbs_reporting.usage` (add `--days 7` for another range) prints totals by
  person, model and day, plus the most expensive questions.
- **Prompt caching** is on: the instructions and each chat's earlier context are re-read from
  Anthropic's cache at about a tenth of the normal input price. The cached share shows up in the
  usage line and summary.

- **Follow-ups stay cheap.** Once a question is answered, the raw ConnectWise data behind it (often
  thousands of tokens of ticket lists) is dropped from the chat; David's answer stays. A follow-up
  that needs the details again fetches them fresh. Small results such as company lookups are kept.
- **ConnectWise data is sent compactly.** Long lists (tickets, projects, go-lives) go to Claude as
  tables, with each field named once rather than on every ticket. Ticket notes leave out the earlier
  emails quoted under a reply, since each of those is a note of its own. List-heavy questions use
  roughly half the input tokens they used to.

Costs are estimates from list prices in `dbs_reporting/usage.py`; the Anthropic console
(Settings → Usage / Cost) has exact billing.

## Activity log

Every question and answer is logged with who asked, the ConnectWise lookups David ran, the full
answer, token usage and any errors:

- **Live:** printed in the terminal running the bot, and appended to `logs/activity.log` (open it in
  VS Code; rotated at 5 MB, 10 old files kept, gitignored). Set `LOG_DIR` to log elsewhere.
- **Past conversations** (including ones from before the log existed), from the database:
  `python -m dbs_reporting.activity --days 7 [--user jsmith] [--search printer] [--full]`

The chat sidebar tells people their questions and answers are logged for admins.


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