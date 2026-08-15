# End-to-End Flow

How a backend exception becomes a GitHub ticket, stage by stage, with the file
and function responsible for each step.

> **Status:** all seven stages are built. Stages 1–5 are verified running;
> stages 6–7 are wired and unit-tested but **not yet exercised against the real
> GitHub MCP server**, which needs a PAT and a model API key. See
> [Build status](#build-status).

---

## The short version

You click a button. The sample app genuinely throws. A global exception handler
catches the **live exception object** and — without serialising anything —
walks its stack frames, opens the source files those frames point at, and pulls
the code around each failing line. Optionally it also captures the variable
values at the moment of failure. Everything is redacted, then handed to an LLM
along with tools to search the GitHub issue tracker. The model looks for an
existing issue describing the same underlying bug. If it finds one, it reopens
and comments. If not, it writes a new ticket containing a root cause and a
concrete fix.

Nothing is written to disk at any point.

The whole point is stage 4. A template can restate a traceback; only something
that has read your code — and seen the values running through it — can tell you
what is actually wrong.

---

## The pipeline

```
 ┌─ 1 ─────────────┐   ┌─ 2 ──────────────┐   ┌─ 3 ─────────────┐
 │  Browser UI     │──►│  Sample app      │──►│  capture_       │
 │  click a button │   │  throws for real │   │  exception()    │
 └─────────────────┘   └──────────────────┘   └────────┬────────┘
                                                       │ IncidentEvent
        ┌──────────────────────────────────────────────┘
        ▼
 ┌─ 4 ─────────────┐   ┌─ 5 ──────────────┐   ┌─ 6 ─────────────┐
 │  Assemble       │──►│  Redact          │──►│  LLM agent      │
 │  + source code  │   │  one gate, all   │   │  + GitHub MCP   │
 │  + locals       │   │  paths           │   │                 │
 └─────────────────┘   └──────────────────┘   └────────┬────────┘
                                                       │
                                              ┌────────▼────────┐
                                              │  7  Ticket      │
                                              │  new or reopened│
                                              └─────────────────┘
```

---

## Stage 1 — Trigger

**`samples/app/static/index.html`**

Five buttons, one per failure mode. Each `POST`s to a `/boom/*` endpoint.

| Button | Endpoint | What breaks |
|---|---|---|
| Divide by zero | `/boom/zero-division` | `orders.create_order` divides by a quantity of 0 |
| Missing key | `/boom/key-error` | `orders.apply_discount` reads an absent key |
| Null reference | `/boom/attribute-error` | `orders.summarize_order` calls `.upper()` on `None` |
| Upstream timeout | `/boom/timeout` | `payments.charge` never gets an answer |
| Same bug, moved | `/boom/repeat-bug` | the divide-by-zero again, different function and line |

That last one is the duplicate-detection test. It is the *same defect* —
dividing by a quantity that is allowed to be zero — reached through
`recalculate_order` instead of `create_order`. A fingerprint hash of
(type, message, file, function, line) sees a brand-new bug. Semantic matching
should tie it back to the ticket already filed.

## Stage 2 — The app throws

**`samples/app/orders.py`, `samples/app/payments.py`**

The bugs are written the way such bugs actually appear — a missing guard, an
unvalidated key, an assumed non-null — not as `raise` statements. The LLM has
to read the code to work out what went wrong, which is the thing being tested.

`payments.charge` also binds three planted secrets to locals before it fails —
a `sk_live_…` key, a Postgres DSN with a password, an internal email. They are
live in the frame at the moment the exception is raised, which makes them the
fixture that proves redaction works on the riskiest field we send.

## Stage 3 — Capture

**`samples/app/main.py` → `unhandled_exception_handler`**
**`src/log2ticket_triage/capture.py` → `capture_exception`**

One handler for every unhandled exception, matching the shape the proposal
describes. It hands the live exception straight to `capture_exception` — this
is the SDK entry point a real service would call.

Because we hold the actual exception object, there is nothing to parse:

```python
traceback.StackSummary.extract(
    traceback.walk_tb(exc.__traceback__),
    capture_locals=settings.capture_locals,
)
```

One stdlib call replaces what used to be ~150 lines of regex hunting through
log text for where a traceback started. Frames arrive structured, with
`.filename`, `.lineno`, `.name`, and — when enabled — `.locals` already repr'd.

Chained exceptions are a two-line attribute walk (`__cause__` / `__context__`)
rather than a backward text scan, and they are recorded as *context*: the
exception the handler receives is already the outermost one, so the chain says
what it was raised *while handling*.

The result is an `IncidentEvent` with a UUID, dropped into a bounded in-memory
`IncidentStore` (`store.py`, a `deque(maxlen=50)`). Deliberately not durable —
choosing a persistence story is a decision for the real implementation, not
something to inherit by accident from a demo.

## Stage 4 — Assemble context

**`src/log2ticket_triage/context.py` → `ContextAssembler.build`**

The heart of the exploration: deciding what the model gets to see.

**Source.** For each in-repo frame, `_safe_resolve` resolves the path and
**refuses anything outside the configured repo root** — refuses, not clips. A
stack frame can point anywhere, and this is the boundary that keeps the agent
reading your application rather than your filesystem. A refused frame is
downgraded rather than dropped, so the stack shape survives without leaking
whether the file exists. Survivors get up to ±15 lines around the failing line,
numbered, with `->` marking the culprit:

```
     27 |     quantity = item["quantity"]
     28 |
->   29 |     unit_price = total / quantity  # ZeroDivisionError when quantity == 0
     30 |
```

The byte budget is spent **outward from the failing line**, not by clipping the
tail. Clipping the tail drops the `->` line the moment the window exceeds the
budget, handing the model the code *above* the failure and nothing else — a
smaller correct window beats a larger truncated one. Frames with no line number
get no excerpt at all, rather than a window centred on nothing.

**Locals** *(opt-in — `CAPTURE_LOCALS=true`)*. The values running through the
frame when it failed. This is what no log file could ever provide:

```
order_id = 'ord_8812'
amount_cents = 4999
api_key = '[REDACTED_API_KEY]'
dsn = 'postgresql://payments_user:[REDACTED_PASSWORD]@db.internal:5432/payments'
notify = '[REDACTED_EMAIL]'
```

**Noise control.** Two problems only visible once it was running:

- A FastAPI request arrives through **15 frames of uvicorn/starlette
  middleware**. Listing each one buried the two that mattered, so consecutive
  library frames collapse to `… 15 library frames (starlette, fastapi) …`.
- Frame paths were absolute, shipping `/Users/yourname/…` to the model
  provider. In-repo paths are rewritten repo-relative (`orders.py:29`);
  library paths are trimmed at `site-packages/` (`fastapi/routing.py`); a
  **refused** frame is reduced to its bare filename, since it often has no
  marker to trim at.

A refused frame is also reported separately rather than collapsed into the
library run. Refusal usually means `TARGET_REPO_PATH` is misconfigured, and
folding your own application frames into "15 library frames" would hide exactly
the thing you need to see.

## Stage 5 — Redact

**`src/log2ticket_triage/redact.py`**

Ten ordered rules, applied at **one** gate in `ContextAssembler` — source
excerpts, exception messages, chained exceptions and locals all pass through
the same point, so there is exactly one place to audit.

Two orderings matter, and both were wrong at some point:

- **Connection strings before emails.** A DSN contains an `@` and would
  otherwise be half-eaten by the email rule.
- **Redact before truncating.** Every high-value rule is anchored on a
  terminator — a DSN needs its closing `@`, an assignment its closing quote. A
  value clipped to 200 characters first can lose that terminator, and the rule
  then fails to match a password sitting in plain sight. Locals are therefore
  captured whole, redacted, and only then trimmed for display.

| Before | After |
|---|---|
| `sk_test_FAKEKEYnotr…` | `[REDACTED_API_KEY]` |
| `postgresql://user:hunter2@db…` | `postgresql://user:[REDACTED_PASSWORD]@db…` |
| `payments-oncall@example.com` | `[REDACTED_EMAIL]` |

Note what *survives*: the DSN keeps its host and username, and `order_id` and
`amount_cents` come through untouched. Redaction that eats the debugging signal
along with the secrets would defeat the purpose — which is also why the
"assigned secret" rule only matches **quoted literals**. An earlier version
matched any right-hand side, so `password = get_password()` and `self.api_key =
settings.gateway_key` were blanked out of the source excerpt. Hiding real code
from a model whose whole job is to read it is a bug, not caution.

The UI renders the redaction report alongside the context, so the safety claim
is visible rather than merely computed.

**`POST /triage/inspect` returns exactly this** — the fully assembled context,
as text, before any model sees it. It needs no API key and costs nothing.

## Stage 6 — The agent

**`src/log2ticket_triage/ticket_writer.py` → `TicketWriter.run`**

Tools come from GitHub's hosted MCP server over HTTP — no Docker, no local
binary. The URL carries the permissions:

| Mode | Endpoint | Tools loaded |
|---|---|---|
| Dry run *(default)* | `…/mcp/x/issues/readonly` | `search_issues`, `issue_read` |
| `--live` | `…/mcp/x/issues` | the above **+** `issue_write`, `add_issue_comment` |

Dry-run safety is structural. The write tools are not disabled by prompt
instruction — they are **absent from `get_tools()`**, because GitHub's endpoint
never offers them. No model error or prompt injection can reach a capability
that was never loaded.

Connection is `streamable_http` with the PAT as a bearer token. Then one pass
per incident:

1. `search_issues` for something describing the same underlying bug
2. `issue_read` the promising candidates before deciding
3. **Found** → `issue_write` to reopen, `add_issue_comment` with the analysis
4. **Not found** → `issue_write` to create, with root cause and suggested fix

The system prompt differs by mode. In dry run the agent is told it cannot write
and must report what it *would* do; in live mode it is told its writes take
effect immediately. Both carry the same instruction about the duplicate
threshold, and both demand a concrete fix — "add error handling" is explicitly
called out as not being one.

**Preflight runs before any network call.** A missing `GITHUB_TOKEN`, a missing
`GITHUB_REPO`, or live mode with an empty allowlist fails immediately with a
message saying which, rather than a stack trace from inside the MCP client. A
triage failure of any kind degrades to a 502 carrying the assembled context —
the context is still worth reading even when the model call did not happen.

## Stage 7 — Guardrails and output

**`src/log2ticket_triage/guardrails.py` → `WriteGuard`**

Live mode only — dry run needs none, because the tools are gone.

| Guard | Default | Why |
|---|---|---|
| Repo allowlist | explicit config | The agent never chooses a destination |
| Writes per run | 5 | One bad run cannot flood the tracker |
| Duplicate confidence floor | 0.7 | Below it, create new rather than reopen the wrong ticket |

That last one encodes the failure mode that matters most. **Reopening the wrong
issue is worse than filing a duplicate** — it is louder, it confuses whoever
owns that ticket, and it buries a real bug under an unrelated thread. When the
agent is unsure, the safe error is a new issue.

`WriteGuard.wrap` replaces the coroutine on each write tool, leaving name,
description and schema intact so the agent still binds them normally. A refused
call is **returned as a tool result, not raised** — the agent reads the refusal
and adapts instead of the request crashing. The cap counts across tools, so one
run cannot get five creates *and* five comments.

The UI renders writes-used, the cap, and any refusals, so the limits are
visible rather than merely asserted.

---

## Build status

| Stage | Component | Status |
|---|---|---|
| 1 | Demo UI | Built — verified, all five buttons |
| 2 | Sample app with planted bugs | Built — verified |
| 3 | `capture_exception` | Built — verified, 17 frames captured per request |
| 3 | `IncidentStore` | Built — verified, bounded and evicting |
| 4 | Context assembler | Built — verified against a live exception |
| 4 | Library-frame collapse, path shortening | Built — verified 15 → 1 |
| 5 | Redaction | Built — verified, zero leaks with locals on |
| 5 | `POST /triage/inspect` | Built — verified |
| 5 | Redaction panel in the UI | Built |
| 6 | LLM agent + MCP client | Built — unit-tested; **not yet run against real GitHub** |
| 7 | Guardrails | Built — allowlist, cap, refusal-as-result all tested |
| — | CLI (`triage` command) | **Pending** |
| — | Tests | Built — 77 passing |

Stages 1–5 need no API key and cost nothing to run. That is deliberate: the
entire capture → context pipeline can be validated for free, and a context bug
found there is far cheaper than one found through a model's output.

---

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                       # 77 tests, no key needed
.venv/bin/uvicorn samples.app.main:app --reload --port 8000
```

Open `http://localhost:8000`, click **Divide by zero**, then **Show context
only**. That exercises stages 1 through 5 with no key and no network.

To see local variables — and the redaction that protects them:

```bash
CAPTURE_LOCALS=true .venv/bin/uvicorn samples.app.main:app --port 8000
```

Click **Upstream timeout**, then **Show context only**. `order_id` and
`amount_cents` come through; the API key, password and email do not.

---

## Running the agent (stages 6–7)

Stages 1–5 need nothing. The agent needs two credentials in `.env`:

```bash
GITHUB_TOKEN=ghp_...            # PAT with issues:read (issues:write for --live)
ANTHROPIC_API_KEY=sk-ant-...    # or the key matching whatever LLM_MODEL names
GITHUB_REPO=your-org/sandbox
REPO_ALLOWLIST=your-org/sandbox
DRY_RUN=true
```

With `DRY_RUN=true` the agent connects to the readonly MCP endpoint and cannot
write, so the first run is safe by construction. Set `DRY_RUN=false` only
against a throwaway repo.

---

## Where the design decisions live

| Question | Answer | Where |
|---|---|---|
| Which model? | Any — `LLM_MODEL="provider:model"` via LangChain | `config.py` |
| Why no log file? | Serialising to text then re-parsing it threw away structure we already had | `capture.py` |
| How is dry run enforced? | GitHub's `/readonly` endpoint omits write tools | `config.py` → `mcp_url` |
| What stops path traversal? | `is_relative_to(repo_root)`, refuse on failure | `context.py` → `_safe_resolve` |
| When does redaction run? | One gate, before the model call — and before truncation | `context.py`, `redact.py` |
| Why `NoDecode` on the allowlist? | pydantic-settings JSON-decodes complex fields before validators run; without it a CSV `REPO_ALLOWLIST` crashes startup | `config.py` |
| Why no fingerprinting? | Replaced by semantic search over real issues | [EXPLORATION-PLAN.md](./EXPLORATION-PLAN.md) |
