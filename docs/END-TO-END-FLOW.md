# End-to-End Flow

How a backend exception becomes a GitHub ticket.

> **Status:** all 7 stages built. 1–5 verified running. 6–7 wired and
> unit-tested but not yet run against real GitHub (needs a PAT + model key).

Click a button → the sample app throws for real → the handler captures the
**live exception** (no serialising, nothing written to disk) → source and
locals are attached → sanitized → an LLM searches GitHub for a duplicate and
either reopens it or writes a new ticket with a root cause and a fix.

```
 1 Browser UI ──► 2 Sample app ──► 3 capture_exception()
   click a button    throws for real    live frames, no parsing
                                              │ IncidentEvent
        ┌─────────────────────────────────────┘
        ▼
 4 Assemble context ──► 5 Sanitize ──► 6 LLM agent + GitHub MCP ──► 7 Ticket
   source + locals       one gate      search → reopen or create    new/reopened
```

## Stages

| # | File → function | What it does |
|---|---|---|
| 1 | `samples/app/static/index.html`, `app.js` | 5 buttons trigger `/boom/*`; "Write a ticket" / "Show context only" call `/write-ticket` and `/context` |
| 2 | `samples/app/orders.py`, `payments.py` | Realistic bugs (missing guard, unvalidated key, assumed non-null). `payments.charge` also binds 3 fake secrets to locals — the sanitizing test fixture |
| 3 | `main.py` → `unhandled_exception_handler`<br>`capture.py` → `capture_exception` | Holds the live exception, so `traceback.StackSummary.extract(walk_tb(...), capture_locals=...)` gives structured frames directly — no log file, no regex parser. Chained exceptions recorded via `__cause__`/`__context__`. Result is an `IncidentEvent` in a bounded in-memory `IncidentStore` (`deque(maxlen=50)`) |
| 4 | `context.py` → `ContextAssembler.build` | For each in-repo frame: resolves the path, **refuses** (doesn't drop) anything outside `TARGET_REPO_PATH`; excerpt is ±15 lines, budget spent **outward from the failing line** so `->` never gets clipped; ~15 uvicorn/fastapi frames collapse to one line; paths rewritten repo-relative |
| 5 | `sanitize.py` | 10 rules at one gate — source, messages, locals all pass through it. Locals are sanitized **before** truncation (truncating first can cut the terminator a rule anchors on and let a secret through) |
| 6 | `ticket_writer.py` → `TicketWriter.run` | Connects to GitHub's hosted MCP server. Dry run hits `/readonly` — write tools are **absent from `get_tools()`**, not merely disabled by prompt. `search_issues` → `issue_read` → reopen+comment or create. Preflight checks token/repo/allowlist before any network call; a failed run still returns the assembled context |
| 7 | `guardrails.py` → `WriteGuard` | Live mode only: repo allowlist, 5 writes/run cap, 0.7 duplicate-confidence floor (below it, create new rather than risk reopening the wrong issue). A refused call is returned as a tool result, not raised |

## Build status

| Stage | Status |
|---|---|
| 1–2 | Built, verified |
| 3 | Built, verified — 17 frames/request, store bounded and evicting |
| 4 | Built, verified — 15 lib frames → 1, paths repo-relative |
| 5 | Built, verified — zero leaks with locals on |
| 6 | Built, unit-tested — **not yet run against real GitHub** |
| 7 | Built, tested |
| CLI | Pending |
| Tests | 77 passing |

Stages 1–5 need no API key — the whole capture→context pipeline is free to
validate, and a context bug found there is cheaper than one found via a model.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                                  # 77 tests, no key
.venv/bin/uvicorn samples.app.main:app --reload --port 8000
```

Open `localhost:8000` → **Divide by zero** → **Show context only** (stages
1–5, no key, no network).

For locals: `CAPTURE_LOCALS=true .venv/bin/uvicorn samples.app.main:app --port 8000`,
then **Upstream timeout** → **Show context only**. `order_id`/`amount_cents`
survive; the API key, password and email don't.

For the agent, add to `.env`:

```bash
GITHUB_TOKEN=ghp_...            # issues:read (issues:write for --live)
ANTHROPIC_API_KEY=sk-ant-...    # or whatever LLM_MODEL names
GITHUB_REPO=your-org/sandbox
REPO_ALLOWLIST=your-org/sandbox
DRY_RUN=true
```

`DRY_RUN=true` connects to the readonly MCP endpoint, so the first run can't
write regardless. Only set it `false` against a throwaway repo.

## Design decisions

| Question | Answer |
|---|---|
| Which model? | Any — `LLM_MODEL="provider:model"` via LangChain (`config.py`) |
| Why no log file? | Serialising to text then re-parsing it threw away structure already in hand |
| How is dry run enforced? | GitHub's `/readonly` endpoint omits write tools — structural, not a prompt rule |
| Path traversal? | `is_relative_to(repo_root)` in `context.py` → `_safe_resolve`; refuse on failure |
| Sanitizing order? | One gate, before the model call, before truncation |
