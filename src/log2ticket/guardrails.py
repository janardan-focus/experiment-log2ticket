"""Limits on what the agent may do, enforced in code.

Dry run needs none of this: it connects to GitHub's `/readonly` MCP endpoint,
so the write tools are never loaded and the capability simply does not exist.
These guards exist for `--live`, where the tools are real.

The failure mode worth designing against is **reopening the wrong issue**. A
duplicate is noise; resurrecting an unrelated ticket is louder, confuses
whoever owns it, and buries a real bug in someone else's thread. Where the
agent is unsure, creating a new issue is the safe error.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool

from .config import Settings

# MCP issue tools that mutate state. Everything else is read-only.
#
# The `issues` toolset is bigger than the four tools this app was designed
# against — it also exposes get_label, list_issue_fields, list_issue_types,
# list_issues (all read-only) and sub_issue_write (a write). sub_issue_write
# was missing here for a while: it mutates a real issue exactly like
# issue_write does, but wasn't in this set, so in live mode it bypassed the
# repo allowlist and the write cap entirely.
WRITE_TOOLS = frozenset({"issue_write", "add_issue_comment", "sub_issue_write"})


class GuardrailError(RuntimeError):
    """Raised when a tool call is refused. Surfaced to the agent as a result."""


class WriteGuard:
    """Allowlist + per-run write cap, wrapped around the MCP write tools."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.writes_used = 0
        self.refusals: list[str] = []

    # --- checks ---

    def _check_repo(self, kwargs: dict[str, Any]) -> None:
        owner, repo = kwargs.get("owner"), kwargs.get("repo")
        if not owner or not repo:
            # The model omitted the target. Refusing beats guessing.
            raise GuardrailError(
                "Refused: the tool call did not name owner/repo. "
                f"Writes are only permitted to {self.settings.repo_allowlist}."
            )
        target = f"{owner}/{repo}"
        if target not in self.settings.repo_allowlist:
            raise GuardrailError(
                f"Refused: {target} is not on the allowlist "
                f"({self.settings.repo_allowlist}). Do not retry with another repo."
            )

    def _check_cap(self, tool_name: str) -> None:
        if self.writes_used >= self.settings.max_writes_per_run:
            raise GuardrailError(
                f"Refused: {tool_name} blocked — this run already made "
                f"{self.writes_used} writes, the limit is "
                f"{self.settings.max_writes_per_run}. Stop writing and "
                "summarise what you have done."
            )

    # --- wrapping ---

    def wrap(self, tools: list[BaseTool]) -> list[BaseTool]:
        """Return the tool list with every write tool gated.

        Read tools pass through untouched. If a write tool somehow appears in
        dry run, it is gated anyway — belt and braces behind the readonly
        endpoint.
        """
        return [
            self._gate(tool) if tool.name in WRITE_TOOLS else tool
            for tool in tools
        ]

    def _gate(self, tool: BaseTool) -> BaseTool:
        # getattr, not tool.coroutine: BaseTool doesn't declare .coroutine —
        # only StructuredTool does. Every MCP tool is a StructuredTool today,
        # but a plain sync Tool would have no such attribute at all, not just
        # a None one, and direct access would raise instead of falling
        # through to the guard below.
        original = getattr(tool, "coroutine", None)
        guard = self
        name = tool.name

        if original is None:  # sync-only tool; MCP tools are async, but be safe
            return tool

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                guard._check_repo(kwargs)
                guard._check_cap(name)
            except GuardrailError as refusal:
                guard.refusals.append(str(refusal))
                # Returned as a tool result, not raised: the agent should read
                # the refusal and adapt, not crash the run.
                return str(refusal)

            result = await original(*args, **kwargs)
            guard.writes_used += 1
            return result

        return tool.model_copy(update={"coroutine": guarded})

    # --- reporting ---

    def summary(self) -> dict[str, Any]:
        return {
            "writes_used": self.writes_used,
            "write_cap": self.settings.max_writes_per_run,
            "refusals": self.refusals,
        }
