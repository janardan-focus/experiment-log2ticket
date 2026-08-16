"""Guardrails are the only thing standing between the agent and a real
issue tracker in live mode. They are tested without a network."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import StructuredTool

from log2ticket.ticket_writer import TicketWriter
from log2ticket.capture import capture_exception
from log2ticket.config import Settings
from log2ticket.guardrails import WriteGuard

CALLS: list[dict] = []


def _settings(**overrides) -> Settings:
    base = dict(
        github_token="tok",
        github_repo="org/sandbox",
        repo_allowlist=["org/sandbox"],
        dry_run=False,
    )
    base.update(overrides)
    return Settings(**base)


def _tool(name: str) -> StructuredTool:
    async def run(**kwargs) -> str:
        CALLS.append({"tool": name, **kwargs})
        return f"{name} ok"

    return StructuredTool.from_function(coroutine=run, name=name, description=name)


@pytest.fixture(autouse=True)
def _clear():
    CALLS.clear()


def _call(tool: StructuredTool, **kwargs) -> str:
    return asyncio.run(tool.coroutine(**kwargs))


# --- allowlist ---------------------------------------------------------------


def test_write_to_allowlisted_repo_passes():
    guard = WriteGuard(_settings())
    tool = guard.wrap([_tool("issue_write")])[0]

    assert _call(tool, owner="org", repo="sandbox", title="x") == "issue_write ok"
    assert guard.writes_used == 1
    assert CALLS


def test_write_to_other_repo_is_refused():
    guard = WriteGuard(_settings())
    tool = guard.wrap([_tool("issue_write")])[0]

    result = _call(tool, owner="org", repo="production", title="x")

    assert "Refused" in result
    assert not CALLS, "the underlying tool must never run"
    assert guard.writes_used == 0


def test_missing_repo_argument_is_refused_not_guessed():
    guard = WriteGuard(_settings())
    tool = guard.wrap([_tool("add_issue_comment")])[0]

    assert "Refused" in _call(tool, issue_number=4)
    assert not CALLS


def test_refusal_is_returned_not_raised():
    """The agent should read the refusal and adapt, not crash the request."""
    guard = WriteGuard(_settings())
    tool = guard.wrap([_tool("issue_write")])[0]

    result = _call(tool, owner="evil", repo="repo")

    assert isinstance(result, str)
    assert guard.refusals


# --- write cap ---------------------------------------------------------------


def test_write_cap_holds():
    guard = WriteGuard(_settings(max_writes_per_run=3))
    tool = guard.wrap([_tool("issue_write")])[0]

    results = [_call(tool, owner="org", repo="sandbox") for _ in range(5)]

    assert sum(r == "issue_write ok" for r in results) == 3
    assert sum("Refused" in r for r in results) == 2
    assert len(CALLS) == 3
    assert guard.writes_used == 3


def test_cap_counts_across_different_write_tools():
    """One bad run must not get 5 creates *and* 5 comments."""
    guard = WriteGuard(_settings(max_writes_per_run=2))
    write, comment = guard.wrap([_tool("issue_write"), _tool("add_issue_comment")])

    _call(write, owner="org", repo="sandbox")
    _call(comment, owner="org", repo="sandbox", issue_number=1)
    blocked = _call(write, owner="org", repo="sandbox")

    assert "Refused" in blocked
    assert guard.writes_used == 2


# --- read tools --------------------------------------------------------------


def test_read_tools_are_untouched():
    guard = WriteGuard(_settings())
    tools = {t.name: t for t in guard.wrap([_tool("search_issues"), _tool("issue_read")])}

    assert _call(tools["search_issues"], query="anything") == "search_issues ok"
    assert _call(tools["issue_read"], issue_number=99) == "issue_read ok"
    assert guard.writes_used == 0, "reads must not consume the write budget"


def test_wrapping_preserves_tool_identity():
    """The agent binds tools by name and schema; wrapping must not disturb them."""
    original = _tool("issue_write")
    wrapped = WriteGuard(_settings()).wrap([original])[0]

    assert wrapped.name == original.name
    assert wrapped.description == original.description
    assert wrapped.args_schema == original.args_schema


# --- preflight ---------------------------------------------------------------


def _event():
    def boom() -> None:
        quantity = 0  # noqa: F841
        _ = 100 / quantity

    try:
        boom()
    except ZeroDivisionError as exc:
        return capture_exception(exc, settings=Settings(github_token="", github_repo=""))


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"github_token": ""}, "GITHUB_TOKEN"),
        ({"github_repo": ""}, "GITHUB_REPO"),
        ({"dry_run": False, "repo_allowlist": []}, "allowlist"),
        ({"dry_run": False, "repo_allowlist": ["other/repo"]}, "not"),
    ],
)
def test_preflight_refuses_before_touching_the_network(overrides, expected):
    result = asyncio.run(TicketWriter(_settings(**overrides)).run(_event()))

    assert result.error is not None
    assert expected in result.error
    assert result.ticket is None
    assert result.context is not None, "context is still worth showing on failure"


def test_dry_run_uses_the_readonly_endpoint():
    assert _settings(dry_run=True).mcp_url.endswith("/x/issues/readonly")


def test_live_uses_the_writable_endpoint():
    url = _settings(dry_run=False).mcp_url
    assert url.endswith("/x/issues")
    assert not url.endswith("readonly")
