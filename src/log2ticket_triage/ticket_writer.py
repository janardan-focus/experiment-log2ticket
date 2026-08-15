"""Turns a captured exception into a GitHub ticket.

Tools come from GitHub's hosted MCP server over HTTP — no Docker, no local
binary. The URL carries the permissions:

    dry run   .../mcp/x/issues/readonly    search_issues, issue_read
    --live    .../mcp/x/issues             + issue_write, add_issue_comment

Dry-run safety is structural rather than instructional. The write tools are
absent from `get_tools()` because GitHub's endpoint never offers them, so no
prompt injection or model error can reach a capability that was never loaded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

from .config import Settings
from .context import ContextAssembler
from .guardrails import WriteGuard
from .models import IncidentContext, IncidentEvent, TicketOutcome

READ_ONLY_PROMPT = """\
You are triaging a backend exception for the repository {repo}.

You are in DRY RUN. You have read-only access: you can search and read issues, \
but you cannot create, reopen or comment on anything. Do not claim you did.

Do this:
1. Use `search_issues` to look for an existing issue describing the SAME \
underlying defect. Search on the exception type, the function, and the concept \
of the bug — not just the literal message. The same defect often surfaces from \
a different line or function after a refactor.
2. Read the most promising candidates with `issue_read` before deciding.
3. Decide what you WOULD do, and report it:
   - a genuine duplicate exists -> action "reopened", set duplicate_of to its \
number, and write the comment you would post as description_markdown
   - no match -> action "created", and write the full issue body

Judgement: reopening the wrong issue is worse than filing a duplicate. It is \
louder, it confuses whoever owns that ticket, and it buries a real bug in an \
unrelated thread. If your confidence that two reports share ONE root cause is \
below {floor}, treat it as new.
"""

LIVE_PROMPT = """\
You are triaging a backend exception for the repository {repo}.

You are LIVE. Your writes take effect immediately on a real issue tracker.

Do this:
1. Use `search_issues` to look for an existing issue describing the SAME \
underlying defect. Search on the exception type, the function, and the concept \
of the bug — not just the literal message. The same defect often surfaces from \
a different line or function after a refactor.
2. Read the most promising candidates with `issue_read` before deciding.
3. Act:
   - a genuine duplicate exists -> reopen it with `issue_write` if it is \
closed, then `add_issue_comment` with your analysis. Set action "reopened" \
(or "commented" if it was already open) and duplicate_of to its number.
   - no match -> create one with `issue_write`. Set action "created".

Judgement: reopening the wrong issue is worse than filing a duplicate. It is \
louder, it confuses whoever owns that ticket, and it buries a real bug in an \
unrelated thread. If your confidence that two reports share ONE root cause is \
below {floor}, file a new issue instead.

Write only to {repo}. A refused tool call means a guardrail stopped you — read \
it, and do not retry against a different repository.
"""

OUTPUT_RULES = """\

The ticket you produce must be worth more than the traceback it came from:

- title: [Backend] <ExceptionType> in <filename>:<function>
- description_markdown: what happens, where, and under what conditions. \
Include the stack and any local variable values you were given.
- root_cause: the actual defect in the code you were shown. Not a restatement \
of the exception.
- suggested_fix: a specific change — the guard to add, the line to alter, ideally \
a diff. "Add error handling" is not a fix.
- reasoning: why this is or is not a duplicate. This is logged, never posted.

You were given the source around each failing line, and possibly the variable \
values at the moment of failure. Use them. If the source shows the bug, say so \
precisely.
"""


@dataclass
class TicketResult:
    context: IncidentContext
    ticket: TicketOutcome | None
    tool_calls: list[str] = field(default_factory=list)
    guardrails: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class TicketWriter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.assembler = ContextAssembler(settings)

    def _preflight(self) -> str | None:
        """Fail with something actionable rather than a stack trace."""
        if not self.settings.github_token:
            return "GITHUB_TOKEN is not set. The agent needs it to reach the GitHub MCP server."
        if not self.settings.github_repo:
            return "GITHUB_REPO is not set. The agent needs a repository to search."
        if not self.settings.dry_run and not self.settings.repo_allowlist:
            return (
                "Live mode with an empty REPO_ALLOWLIST. Refusing to start — "
                "the allowlist is what stops writes going somewhere unintended."
            )
        if not self.settings.dry_run and self.settings.github_repo not in self.settings.repo_allowlist:
            return (
                f"Live mode, but GITHUB_REPO ({self.settings.github_repo}) is not "
                f"on REPO_ALLOWLIST ({self.settings.repo_allowlist})."
            )
        return None

    def _system_prompt(self) -> str:
        template = READ_ONLY_PROMPT if self.settings.dry_run else LIVE_PROMPT
        return (
            template.format(
                repo=self.settings.github_repo,
                floor=self.settings.duplicate_confidence_floor,
            )
            + OUTPUT_RULES
        )

    async def run(self, event: IncidentEvent) -> TicketResult:
        context = self.assembler.build(event)

        problem = self._preflight()
        if problem:
            return TicketResult(context=context, ticket=None, error=problem)

        guard = WriteGuard(self.settings)

        try:
            client = MultiServerMCPClient(
                {
                    "github": {
                        "transport": "streamable_http",
                        "url": self.settings.mcp_url,
                        "headers": {
                            "Authorization": f"Bearer {self.settings.github_token}"
                        },
                    }
                }
            )
            tools = guard.wrap(await client.get_tools())

            agent = create_agent(
                self.settings.llm_model,
                tools,
                system_prompt=self._system_prompt(),
                response_format=TicketOutcome,
            )

            result = await agent.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Triage this exception.\n\n"
                                + context.to_prompt_block()
                            ),
                        }
                    ]
                }
            )
        except Exception as exc:  # noqa: BLE001 — a failed triage must not crash the app
            return TicketResult(
                context=context,
                ticket=None,
                guardrails=guard.summary(),
                error=f"{type(exc).__name__}: {exc}",
            )

        return TicketResult(
            context=context,
            ticket=result.get("structured_response"),
            tool_calls=_describe_tool_calls(result.get("messages", [])),
            guardrails=guard.summary(),
        )


def _describe_tool_calls(messages: list[Any]) -> list[str]:
    """A readable trace of what the agent actually did, for the UI.

    The point of showing this is that 'it found a duplicate' is only
    believable if you can see the search it ran.
    """
    described: list[str] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name", "?")
            args = call.get("args", {}) or {}
            interesting = {
                k: v
                for k, v in args.items()
                if k in {"query", "issue_number", "title", "state", "owner", "repo", "method"}
            }
            described.append(f"{name}({interesting})" if interesting else name)
    return described
