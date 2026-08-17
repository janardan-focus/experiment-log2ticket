# Code Walkthrough

A guided tour of this codebase for presenting it to someone else. It follows
one concrete exception — the "Divide by zero" demo button — from the moment
it's raised to the moment a ticket comes back, naming the exact module,
function, and data type at every handoff.

---

## Part 1 — Where the exception comes from

### The trigger

`samples/app/static/app.js` renders five buttons. Each one `POST`s to a
`/boom/*` route in `samples/app/main.py`:

```python
# samples/app/main.py
@app.post("/boom/zero-division")
async def boom_zero_division() -> dict[str, object]:
    """SKU-2 has quantity 0. create_order divides by it."""
    return orders.create_order("SKU-2")
```

That calls into `samples/app/orders.py`:

```python
# samples/app/orders.py
CATALOG = {
    "SKU-2": {"name": "Monitor arm", "quantity": 0, "total_cents": 0},
}

def create_order(sku: str) -> dict[str, Any]:
    item = CATALOG[sku]
    total = item["total_cents"]
    quantity = item["quantity"]

    unit_price = total / quantity  # ZeroDivisionError when quantity == 0
    ...
```

`quantity` is `0`. Line 29 raises `ZeroDivisionError: division by zero`. This
is a genuine bug in application code — not a `raise` statement staged for the
demo — because the whole system downstream depends on getting a real Python
exception object with a real traceback, not a synthetic one.

### Why there's only one handler

FastAPI lets you register a single catch-all for exceptions that no route
handles itself:

```python
# samples/app/main.py
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    ...
```

The `ZeroDivisionError` propagates up through `create_order` →
`boom_zero_division` → FastAPI's routing → straight into this one function,
without any of the intermediate code knowing it exists. That's deliberate: it
mirrors the shape the original Log2Ticket proposal describes — one place in a
real backend that sees every unhandled exception, regardless of which endpoint
raised it. `orders.py` and `payments.py` never import anything from
`log2ticket`; the handler is the only integration point.

At this instant, `exc` is a live Python `ZeroDivisionError` object. It still
has its `__traceback__` attribute attached — the actual call stack, with real
frame objects, real local variables, real file paths. That live object is the
raw material for everything that follows.

---

## Part 2 — How the exception becomes data

This is the part worth presenting slowly: a live exception object is not
serializable, not diffable, not something you can hand to an LLM. Each module
below takes it one step closer to something an LLM (and a human) can read,
and each step changes its *type*.

```
ZeroDivisionError (live object, has __traceback__)
        │
        │  capture_exception()          — log2ticket/capture.py
        ▼
IncidentEvent (pydantic model)
        │
        │  store.add(event)             — log2ticket/store.py
        ▼
IncidentStore (in-memory, holds IncidentEvent)
        │
        │  store.latest()               — log2ticket/store.py
        ▼
IncidentEvent (read back out)
        │
        │  ContextAssembler.build()     — log2ticket/context.py
        │  (redact.py runs inside this step)
        ▼
IncidentContext (pydantic model — redacted, capped)
        │
        │  .to_prompt_block()           — log2ticket/models.py
        ▼
str (markdown — what actually goes in the user turn)
        │
        │  TicketWriter.run()           — log2ticket/ticket_writer.py
        │  (LangChain agent + GitHub MCP tools, guardrails.py gates writes)
        ▼
TicketOutcome (pydantic model — the generated ticket)
        │
        │  packaged into JSON            — samples/app/main.py
        ▼
Browser (app.js renders it)
```

### 2.1 — `capture_exception`: live object → `IncidentEvent`

**`src/log2ticket/capture.py`**

```python
# samples/app/main.py — inside the handler
event = capture_exception(
    exc,
    settings=settings,
    endpoint=f"{request.method} {request.url.path}",   # "POST /boom/zero-division"
)
```

Inside `capture_exception`, one stdlib call turns the live traceback into
structured frames — no text, no parsing:

```python
# src/log2ticket/capture.py
summary = traceback.StackSummary.extract(
    traceback.walk_tb(exc.__traceback__),
    capture_locals=settings.capture_locals,
)
```

`summary` is a list of `FrameSummary` objects, each already carrying
`.filename`, `.lineno`, `.name`, and — if `CAPTURE_LOCALS=true` — `.locals` as
a dict of already-`repr()`'d strings. `capture_exception` walks that list and
builds one `Frame` per entry (defined in `models.py`):

```python
Frame(
    file="/…/samples/app/orders.py",
    line=29,
    function="create_order",
    in_repo=True,           # not under site-packages/dist-packages/stdlib
    locals_repr=None,       # capture_locals is off by default
)
```

It also walks `exc.__cause__` / `exc.__context__` (`exception_chain`) in case
this exception was raised while handling another one, and calls
`traceback.format_exception` once for a human-readable trace. All of it is
assembled into one `IncidentEvent`:

```python
IncidentEvent(
    incident_id="3f2a1c9e-...",       # uuid4, minted here
    exception_type="ZeroDivisionError",
    exception_message="division by zero",
    frames=[... 17 Frame objects ...], # includes uvicorn/starlette/fastapi frames
    timestamp=datetime(...),
    endpoint="POST /boom/zero-division",
    chained_from=[],
    raw_traceback="Traceback (most recent call last):\n  ...",
)
```

`IncidentEvent` is the seam of the whole system. `capture_exception` produces
it from a live exception today; nothing downstream cares how it was produced —
a future reader of AWS CloudWatch or GCP Cloud Logging could build the same
object from log text and every module past this point would work unchanged.

### 2.2 — `IncidentStore`: where it's held

**`src/log2ticket/store.py`**

```python
# samples/app/main.py
store.add(event)
```

`IncidentStore` is a `deque(maxlen=50)` — a bounded, process-local, in-memory
buffer. `store.add` appends and returns the incident id; nothing is written to
disk. This exists only so the two `/context` and `/write-ticket` endpoints
(triggered later by separate button clicks) can retrieve `store.latest()` — a
real backend would replace this with whatever queue or datastore fits its
architecture. It is deliberately the least interesting module in the pipeline.

### 2.3 — `ContextAssembler`: `IncidentEvent` → `IncidentContext`

**`src/log2ticket/context.py`**

When you click "Show context only" or "Write a ticket", `main.py` calls:

```python
event = store.latest()
context = assembler.build(event)
```

This is the module that decides what an LLM is actually allowed to see, and
it does three things to every in-repo frame:

**Resolves and reads the source.** `_safe_resolve` turns
`orders.py`'s frame path into an absolute path, checks it is inside
`TARGET_REPO_PATH`, and refuses it — not clips it — if it isn't. For
`create_order`, that resolves cleanly, and `_excerpt` pulls ±15 lines around
line 29:

```
     25 |     item = CATALOG[sku]
     26 |     total = item["total_cents"]
     27 |     quantity = item["quantity"]
     28 |
->   29 |     unit_price = total / quantity  # ZeroDivisionError when quantity == 0
     30 |
     31 |     return {"sku": sku, "unit_price_cents": unit_price, "quantity": quantity}
```

(trimmed here for space — the real excerpt spans ±15 lines around line 29)

**Rewrites the path.** `orders.py`'s absolute path
(`/Users/you/.../samples/app/orders.py`) becomes the repo-relative
`orders.py` — nothing about your machine or username reaches the model.

**Collapses library noise.** The 15 uvicorn/starlette/fastapi frames between
the HTTP layer and your code are folded into one line
(`… 15 library frames (starlette, fastapi) …`) by
`_summarise_library_run` in `models.py`, so the two frames that matter aren't
buried.

Every piece of text this step touches — the exception message, the source
excerpt, any locals — is passed through `redact()` before it's placed in the
result. The output is an `IncidentContext`:

```python
IncidentContext(
    incident_id="3f2a1c9e-...",
    exception_type="ZeroDivisionError",
    exception_message="division by zero",
    frames=[Frame(file="orders.py", line=29, source_excerpt="...", ...), ...],
    endpoint="POST /boom/zero-division",
    timestamp=datetime(...),
)
```

### 2.4 — `redact`: the one gate every string passes through

**`src/log2ticket/redact.py`**

Ten ordered regex rules, applied inside `ContextAssembler` — never anywhere
else. If a payment-related frame were on this stack (`payments.charge`, which
the "Upstream timeout" button exercises), its locals would include a live API
key, a database password, and an internal email address. All three are
redacted before they're placed in `IncidentContext`:

| Field | Before | After |
|---|---|---|
| local `api_key` | `sk_test_FAKEKEYnotreal0000000…` | `[REDACTED_API_KEY]` |
| local `dsn` | `postgresql://payments_user:hunter2@db…` | `postgresql://payments_user:[REDACTED_PASSWORD]@db…` |
| local `notify` | `payments-oncall@example.com` | `[REDACTED_EMAIL]` |

Redaction runs on the full value *before* it's truncated for display — every
high-value rule is anchored on a closing character (a `@`, a closing quote),
and truncating first can cut that anchor and let the rule silently fail to
match.

### 2.5 — `to_prompt_block`: `IncidentContext` → plain text

**`src/log2ticket/models.py`**

```python
context.to_prompt_block()
```

Turns the structured `IncidentContext` into the literal markdown string that
becomes the model's user turn — exception type and message, endpoint, the
stack with source excerpts, any locals. This is exactly what `POST /context`
returns, and exactly what the LLM sees. There is no other copy, no separate
formatting path — if you want to know what the model was told, this function
is where to look.

### 2.6 — `TicketWriter`: text → `TicketOutcome`

**`src/log2ticket/ticket_writer.py`**

```python
result = await ticket_writer.run(event)
```

`TicketWriter.run` re-derives the context (so `/write-ticket` doesn't depend
on a prior `/context` call), runs a preflight check (token set? repo set?
allowlist sane?), then opens a `streamable_http` connection to GitHub's hosted
MCP server:

```python
client = MultiServerMCPClient({
    "github": {
        "transport": "streamable_http",
        "url": self.settings.mcp_url,   # .../mcp/x/issues/readonly in dry run
        "headers": {"Authorization": f"Bearer {self.settings.github_token}"},
    }
})
tools = guard.wrap(await client.get_tools())
```

In dry run, `get_tools()` only ever returns `search_issues` and `issue_read` —
GitHub's `/readonly` endpoint doesn't expose `issue_write` or
`add_issue_comment` at all, so there's nothing for a prompt to accidentally
authorize. `WriteGuard.wrap` (`src/log2ticket/guardrails.py`) additionally
gates whichever write tools *do* exist in live mode, enforcing the repo
allowlist and the per-run write cap in code, not in the prompt.

The wrapped tools go into a LangChain agent:

```python
agent = create_agent(
    self.settings.llm_model,          # e.g. "anthropic:claude-opus-5"
    tools,
    system_prompt=self._system_prompt(),
    response_format=TicketOutcome,
)
result = await agent.ainvoke({"messages": [{"role": "user",
    "content": "Investigate this exception and write the ticket.\n\n" + context.to_prompt_block()}]})
```

The agent calls `search_issues` looking for something describing the same
underlying defect, reads promising candidates with `issue_read`, then either
reopens/comments on a genuine duplicate or creates a new issue — and returns
a `TicketOutcome`:

```python
TicketOutcome(
    action="created",
    title="[Backend] ZeroDivisionError in orders.py:create_order",
    description_markdown="...",
    root_cause="quantity is read from CATALOG without checking for zero...",
    severity="medium",
    labels=["bug", "backend"],
    confidence=0.95,
    reasoning="No existing issue matched this stack/defect.",
)
```

### 2.7 — Back to the browser

**`samples/app/main.py`**

`write_ticket()` packages `context`, `redactions`, the list of tool calls the
agent made, the guardrail summary, and the `TicketOutcome` into one JSON
response. `samples/app/static/app.js`'s `render()` function turns that into
the panels you see: what was redacted, what was sent, what the agent
searched, and the ticket itself.

---

## Module map

| Module | Receives | Returns | Role |
|---|---|---|---|
| `samples/app/main.py` | HTTP request | `IncidentEvent` (via capture) → JSON response | The one exception handler; wires every other module together |
| `samples/app/orders.py`, `payments.py` | — | raises real exceptions | The bugs. Never import `log2ticket` |
| `log2ticket/capture.py` | live exception object | `IncidentEvent` | Live traceback → structured data, no parsing |
| `log2ticket/store.py` | `IncidentEvent` | `IncidentEvent` | Bounded in-memory buffer, nothing persisted |
| `log2ticket/context.py` | `IncidentEvent` | `IncidentContext` | Reads source, attaches locals, calls `redact` |
| `log2ticket/redact.py` | any string | redacted string | The one gate every piece of text passes through |
| `log2ticket/models.py` | — | — | The pydantic types that flow between every module above, plus `to_prompt_block()` |
| `log2ticket/ticket_writer.py` | `IncidentEvent` | `TicketOutcome` | Runs the LangChain agent against GitHub's MCP server |
| `log2ticket/guardrails.py` | MCP tool list | gated tool list | Repo allowlist + write cap, enforced in code |
| `log2ticket/config.py` | `.env` | `Settings` | Everything above reads its limits from here, not from prompt text |

---

## See also

- [`END-TO-END-FLOW.md`](./END-TO-END-FLOW.md) — the condensed version of this
  same flow, one line per stage, plus how to run it.
