# LLM Ticket Generation from Error Traces (Log2Ticket exploration)

## Context

The [Automated GitHub Incident Creation proposal](https://docs.google.com/document/d/1Iu1mvClIbEVURWnR3sW5nxkpWxt0yyE2-hSGKprB9Ec/edit) specifies a **fully deterministic** Phase 1: a FastAPI global exception handler hashes a fingerprint (exc type + message + file + func + line), looks it up in Redis, and creates or reopens a GitHub issue from a fixed template. The [Log-2-Ticket board](https://github.com/orgs/infocusp-fullstack/projects/10/views/1) has 11 unstarted draft items tracking exactly that.

This exploration (`ex-be-llm-integration-with-github`) tests whether an LLM can do materially better at the two places that design is weakest:

1. **The ticket body is a template.** It restates the traceback. It does not say *what is wrong* or *how to fix it*, because a template cannot read your code. An LLM given the traceback **plus the source of each failing frame** can propose a root cause and a concrete fix.
2. **Deduplication is a hash.** Two reports of the same underlying bug that differ by a line number, a refactor, or a wrapped exception hash differently and produce two tickets. An LLM can **search the existing issues and judge similarity semantically**, then reopen and comment on the real duplicate instead of filing a new one.

So fingerprinting is not merely cut from scope — it is **replaced** by LLM-driven semantic duplicate detection against the live issue tracker. That is why there is no `fingerprint.py` and no Redis-shaped store in this plan.

Intended outcome: a clickable demo, a read on whether LLM-written tickets and LLM duplicate-matching are good enough to trust, and a small set of tickets for the board.

**Decisions taken:**

| Decision | Choice |
|---|---|
| Language | Python, FastAPI-aligned |
| LLM access | **Provider-agnostic via LangChain** — `init_chat_model("<provider>:<model>")`. Not the Anthropic SDK. |
| Default model | `anthropic:claude-opus-5`, swappable with one env var |
| Code context | Bundled sample app in `samples/app/`, so frames resolve to real readable files |
| Capture | **In-process, from the live exception.** No log file, nothing written to disk. |
| Local variables | **Opt-in** (`CAPTURE_LOCALS`) — redacted, truncated, capped |
| GitHub access | **Remote GitHub MCP server over HTTP.** No Docker, no local binary. |
| Write mode | **Dry-run by default**; `--live` required to write |
| Demo | Browser UI to trigger errors and watch the triage run end to end |
| Scope | No fingerprinting, no Redis, no dedup hash |

**Tradeoff accepted with LangChain:** going provider-neutral gives up Anthropic-native features — `cache_control` prompt caching, `effort`, adaptive thinking, and `stop_reason: "refusal"` handling. Structured output comes from `.with_structured_output()` / `response_format` instead, which works across providers. Provider-specific knobs remain reachable through `model_kwargs` if a later measurement justifies them, but nothing here depends on them.

---

## Architecture

A single agent per incident, with the whole write surface gated at the MCP endpoint rather than in prompt text.

```
  Browser UI  ──click──►  sample FastAPI app  ──throws──►  global exception handler
  (Break it)                (samples/app/)                          │
                                                                    ▼
                                                       capture_exception(exc)
                                                    live frames — no text, no regex
                                                                    │
                                                                    ▼
                                                    IncidentStore (in memory)
                                                                    │
  Browser UI  ──click──►  POST /triage  ─────────────────────────────┤
  (Run triage)                                                      ▼
                                                          ContextAssembler
                                        ├─ source excerpt per in-repo frame (±15 lines)
                                        ├─ locals at failure (opt-in)
                                        ├─ library frames collapsed, paths repo-relative
                                        └─ redaction  ← BEFORE anything leaves the process
                                                                    │
                                                                    ▼
                                                       IncidentContext
                                                                    │
                                                                    ▼
                                                LLM agent (LangChain create_agent)
                                                                    │
                                            tools from remote GitHub MCP server
                              ┌─────────────────────────────────────┴──────────────┐
                     DRY-RUN (default)                                        --live
              /mcp/x/issues/readonly                                   /mcp/x/issues
              search_issues, issue_read                    + issue_write, add_issue_comment
              (write tools literally absent)                 wrapped in allowlist + cap
                                                                    │
                                                                    ▼
                                                   agent decides, in one pass:
                                                   1. search for a similar existing issue
                                                   2. found    → reopen + comment
                                                      not found → create issue with
                                                                  root cause + suggested fix
                                                                    │
                                                                    ▼
                                                    TicketOutcome ──► rendered back in the UI
```

Four load-bearing properties:

1. **Dry-run is enforced by GitHub, not by the prompt.** In dry-run the agent connects to `/mcp/x/issues/readonly`, so `issue_write` and `add_issue_comment` are never in `get_tools()`. The agent cannot write because the capability does not exist — no prompt injection or model error changes that.
2. **`/x/issues` cuts the surface to what the task needs.** The default MCP toolset spans repos, PRs, users, and more. Scoping to issues removes tools the agent has no business calling.
3. **Reading source and locals is the interesting capability and the main risk.** Resolving frames to files means the agent reads your codebase; capturing locals means it reads the values flowing through it. Frame paths are resolved against a configured root and rejected if they escape it; library frames are collapsed and never read; paths are rewritten repo-relative so the provider never sees a home directory; excerpts are byte-capped; locals are truncated, capped and redacted. All of it runs before the call, not just before GitHub.
4. **The UI shows the context, not just the result.** The interesting output of this spike is *what we sent*, not only *what came back*. The triage panel renders the assembled `IncidentContext` alongside the generated ticket so context bugs are visible at a glance.

---

## Files to create

```
pyproject.toml            # deps: langchain, langchain-anthropic, langchain-mcp-adapters,
                          #       fastapi, uvicorn, pydantic, pydantic-settings, typer, rich, pytest
.env.example              # LLM_MODEL, ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPO, TARGET_REPO_PATH
README.md                 # setup and the one command that runs the demo
docs/EXPLORATION-PLAN.md  # this plan
src/log2ticket_triage/
  config.py               # pydantic-settings: model, repo, allowlist, write cap, paths, dry_run
  models.py               # IncidentEvent, Frame, IncidentContext, TicketOutcome
  redact.py               # redact(text) -> text — tokens, keys, connection strings, emails, IPs
  capture.py              # capture_exception() — the SDK entry point
  store.py                # bounded in-memory buffer of recent incidents
  context.py              # ContextAssembler: frames → source excerpts + locals
  ticket_writer.py        # TicketWriter — context in, GitHub ticket out
  guardrails.py           # repo allowlist, per-run write cap
  cli.py                  # typer: `triage run`, `triage inspect`, `triage demo`, `triage doctor`
samples/app/              # the "monitored backend" — the code the agent reads
  main.py                 # FastAPI app, global exception handler, /triage endpoints
  orders.py payments.py   # where the bugs actually live
  static/index.html       # the demo UI
tests/
  test_capture.py test_context.py test_redact.py test_guardrails.py
```

### The demo UI

`samples/app/static/index.html` — one page, no build step, no framework. Plain HTML plus a little `fetch`. Two panels:

**Left — Break something.** A button per failure mode, each hitting an endpoint that genuinely throws:

| Button | Endpoint | Failure |
|---|---|---|
| Divide by zero | `POST /boom/zero-division` | `ZeroDivisionError` in `orders.py` |
| Missing key | `POST /boom/key-error` | `KeyError` on an unvalidated payload |
| Null reference | `POST /boom/type-error` | `TypeError` on a `None` |
| Upstream timeout | `POST /boom/timeout` | transient-looking failure in `payments.py` |
| Same bug, moved | `POST /boom/repeat-bug` | the divide-by-zero again from a different line — **the duplicate-detection test** |

The app's global exception handler catches each one, writes a full trace to `samples/logs/app_errors.log`, and returns a 500 with an incident id — exactly the shape the real proposal describes.

**Right — Run triage.** A button that calls `POST /triage`, which reads the newest incident from the log and runs the pipeline. The panel then renders, in order:

1. The assembled `IncidentContext` — frames, which ones had source pulled, the excerpts themselves, what got redacted
2. Which existing issues the agent searched and what it found
3. The resulting `TicketOutcome` — the ticket as it would appear on GitHub, with root cause and suggested fix

A mode badge shows **DRY RUN** or **LIVE** so it is never ambiguous whether a click will write to GitHub.

This turns the spike into something you can put in front of the team: click *Divide by zero*, click *Run triage*, read the ticket it wrote. Then click *Same bug, moved* and watch it recognise the duplicate instead of filing again.

### Key contracts

**`IncidentContext`** — what actually gets sent to the model. This is the object the exploration is really about.

```python
class Frame(BaseModel):
    file: str
    line: int
    function: str
    in_repo: bool                 # False for site-packages/stdlib — listed, not read
    source_excerpt: str | None    # ±15 lines around `line`, redacted, only when in_repo

class IncidentContext(BaseModel):
    exception_type: str
    exception_message: str
    frames: list[Frame]           # innermost last, as Python prints them
    endpoint: str | None          # parsed from the log line when present
    surrounding_log_lines: list[str]
    timestamp: datetime
```

**`TicketOutcome`** — the agent's structured record of what it did (or would do, in dry-run):

```python
class TicketOutcome(BaseModel):
    action: Literal["created", "reopened", "commented", "skipped"]
    issue_number: int | None
    issue_url: str | None
    duplicate_of: int | None      # set when it matched an existing issue
    title: str                    # "[Backend] ZeroDivisionError in orders.py:create_order"
    description_markdown: str
    root_cause: str
    suggested_fix: str            # concrete — a diff or a named change, not "add error handling"
    confidence: float
    reasoning: str                # why this was a duplicate / why not — logged, not posted
```

Title format matches the proposal doc (`[Backend] <ExceptionType> in <filename>:<function>`) so these are shape-compatible with deterministic tickets. The body keeps every field the doc lists and adds root cause and suggested fix — a superset, never a replacement.

**Agent construction** (`ticket_writer.py`) — remote MCP over HTTP, no local process:

```python
MCP_BASE = "https://api.githubcopilot.com/mcp/x/issues"

client = MultiServerMCPClient({"github": {
    "transport": "http",
    "url": f"{MCP_BASE}/readonly" if settings.dry_run else MCP_BASE,
    "headers": {"Authorization": f"Bearer {settings.github_token}"},
}})
tools = await client.get_tools()          # returns a plain list
tools = guardrails.wrap(tools)            # live mode only: allowlist + write cap
agent = create_agent(settings.llm_model, tools, response_format=TicketOutcome)
```

Issue tools exposed by the server: `search_issues` (natural-language search), `issue_read`, `issue_write` (create *and* update, so reopen is a state change through it), `add_issue_comment`.

*Offline fallback:* the same server runs locally via `docker run -i --rm ghcr.io/github/github-mcp-server stdio --toolsets issues [--read-only]` with a `stdio` transport. Only needed if the hosted endpoint is unreachable.

**Guardrails** (`guardrails.py`) — live mode only; dry-run needs none because the tools are absent:

| Guard | Default | Rationale |
|---|---|---|
| Repo allowlist | explicit config | The agent never picks a destination repo |
| Writes per run | `5` | One bad log file cannot flood the tracker |
| Duplicate-match confidence floor | `0.7` | Below it, create new rather than reopen the wrong ticket |
| Live mode | opt-in flag | Writing is never the default |

Reopening the *wrong* issue is the failure mode that matters most — it is louder and more confusing than a duplicate. When the agent is unsure, creating a new issue is the safer error.

---

## Implementation order

1. **Scaffold + models.** `pyproject.toml`, `config.py`, `models.py`, `.env.example`.
2. **Sample app + UI.** `samples/app/` with the five failure endpoints, a global exception handler that calls `capture_exception`, and `static/index.html` with the trigger buttons.
3. **Capture + redaction.** `capture.py`, `store.py`, `redact.py`, with unit tests.
4. **Context assembler.** `context.py` — frame resolution, path-escape rejection, source excerpts, locals redaction, byte caps. Wire the `IncidentContext` view into the UI's right panel. **Works with no LLM and no API key** — you can validate the whole context pipeline for free.
5. **Agent, dry-run only.** MCP over HTTP against `/x/issues/readonly`, `create_agent`, `TicketOutcome`, rendered in the UI. Zero write risk.
6. **Guardrails + live mode.** Allowlist, write cap, `--live` flag, mode badge in the UI.
7. **README.** `triage demo` boots the app and opens the browser.

---

## Verification

1. **Unit tests** — `pytest`. Traceback grouping across interleaved log lines; context assembler refuses a frame path escaping `TARGET_REPO_PATH`; redaction catches every planted secret in the sample app; guardrails reject the sixth write in a run.
2. **UI loop, no LLM** — `triage demo`, click each of the five buttons, confirm five distinct traces land in `app_errors.log`. Then open the context panel and check source excerpts resolve to the right lines, `site-packages` frames are listed but unread, and no secret survives redaction. **Do this before step 3** — it is the cheapest way to catch a context bug, and it costs nothing.
3. **Dry run** — click *Run triage*. Confirm the mode badge reads DRY RUN, no write tool appears in `get_tools()`, and each incident yields a `TicketOutcome` with a plausible root cause and a *specific* suggested fix.
4. **Duplicate detection** — file one of the sample bugs as a real issue in a sandbox repo, then click *Same bug, moved* and re-run triage. The agent should find it via `search_issues` and return `action="reopened"` with `duplicate_of` set, not `action="created"`.
5. **Live run** — `--live` against a **throwaway sandbox repo**. Confirm issues are created, the duplicate is reopened and commented rather than duplicated, and the write cap holds. **Do not point this at the real Log-2-Ticket repo.**
6. **Quality read** — for each generated ticket, judge by hand: is the root cause right, and is the suggested fix specific enough to act on? That judgment is the actual finding of this exploration.

**Prerequisites:** a GitHub PAT with issues access, and an API key for whichever provider `LLM_MODEL` names. `triage doctor` checks both, plus reachability of the MCP endpoint, before anything else runs. No Docker required.

---

## Tickets to add to the Log-2-Ticket project

Three, written in the board's existing bullet style so they can be pasted in as-is.

---

### 1 — Exploration: LLM Integration to Generate Ticket Content

**Type:** Spike / Exploration

Find out what information the LLM needs about an error, and whether what it writes is actually useful.

**Includes:**

- What to send the LLM: the stack, the source code around each failing line, the variable values at the point of failure
- How much surrounding code to include
- Which frames to skip (library and standard-library code)
- Stripping secrets and personal data before anything is sent
- Prompt design
- Is the root cause it identifies correct?
- Is the suggested fix specific enough to act on?
- When shown a bug already on the board, does it find the right existing issue?
- Cost, speed, and token usage across at least two providers

**Output:**

- An agreed list of what context to send, and why each piece is in or out
- A quality and cost baseline
- A list of the cases where it gets things wrong
- Go/no-go on using LLM-written tickets

---

### 2 — Implement Provider-Agnostic LLM Ticket Generator with GitHub MCP Server

**Type:** Implementation

**Includes:**

- Sample FastAPI app that throws errors on purpose, with a simple web page to trigger each one
- Capturing the exception in-process from the handler — no log file, nothing written to disk
- Pulling the source code around each failing line, and optionally the variable values at the point of failure
- Showing what context was assembled, so we can see exactly what the LLM was sent
- LLM calls through LangChain, so Claude / GPT / Gemini swap with one env var
- Generating title, description, root cause, suggested fix, severity, labels
- Connecting to the hosted GitHub MCP server over HTTP (no Docker needed)
- Searching existing issues for a similar one
- Reopening and commenting when a match is found; creating a new issue when not
- Dry run by default — real writes need a `--live` flag
- Repo allowlist and a cap on how many issues one run can write

**Output:**

- Working demo: click a button to break something, click another to see the ticket the LLM writes for it

**Note:** this uses LLM search instead of the fingerprint + Redis approach for spotting duplicates. It overlaps the existing "Implement Incident Deduplication & Lifecycle" ticket — we should pick one, not build both.

---

### 3 — Exploration: Cloud Log Source Integration (CloudWatch / GCP Cloud Logging)

**Type:** Spike / Exploration

**Includes:**

- Pulling logs on a schedule vs. subscribing to a stream
- Remembering where we left off after a restart
- Handling the same log entry arriving twice
- AWS IAM and GCP service-account permissions needed
- Cost of reading logs out of AWS / GCP
- Making cloud logs produce the same input the local file reader produces

**Output:**

- Whether the current log-source design works for both providers unchanged
- A rough implementation sketch for each

---

**Sequencing:** #1 and #2 run together — #1 is the question, #2 is the thing that answers it. #3 is independent and can start any time.
