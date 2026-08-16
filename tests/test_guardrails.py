"""Guardrails are the only thing standing between the agent and a real
issue tracker in live mode. They are tested without a network."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import StructuredTool

from log2ticket.capture import capture_exception
from log2ticket.config import Settings
from log2ticket.guardrails import WriteGuard
from log2ticket.ticket_writer import (
    TicketWriter,
    _drop_non_string_enums,
    _sanitize_tool_schemas,
)

CALLS: list[dict] = []


def _settings(**overrides) -> Settings:
    base = dict(
        github_token="tok",
        github_repo="org/sandbox",
        repo_allowlist=["org/sandbox"],
        dry_run=False,
        # Fixed on purpose: TicketWriter._preflight() also checks the model
        # provider's API key, so leaving llm_model to fall through to whatever
        # LLM_MODEL happens to be in the developer's real .env would make
        # these tests pass or fail depending on ambient machine state instead
        # of the code under test.
        llm_model="anthropic:claude-opus-5",
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
def test_preflight_refuses_before_touching_the_network(overrides, expected, monkeypatch):
    # Pin the provider key present so these cases exercise the GitHub checks
    # they're named for, not whatever the ambient environment happens to be.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    result = asyncio.run(TicketWriter(_settings(**overrides)).run(_event()))

    assert result.error is not None
    assert expected in result.error
    assert result.ticket is None
    assert result.context is not None, "context is still worth showing on failure"


def test_preflight_checks_the_model_provider_key_first(monkeypatch):
    """The GitHub MCP connection is never opened if the model can't be called
    anyway — and the message must name the actual missing variable, not a
    generic 'auth failed', because the raw SDK error names nothing useful."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = asyncio.run(TicketWriter(_settings()).run(_event()))

    assert result.error is not None
    assert "ANTHROPIC_API_KEY" in result.error
    assert "anthropic:claude-opus-5" in result.error


def test_preflight_passes_when_provider_key_is_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    problem = TicketWriter(_settings())._preflight()

    assert problem is None


def test_unknown_provider_prefix_skips_the_key_check(monkeypatch):
    """LLM_MODEL naming a provider this app doesn't special-case shouldn't
    block the run — LangChain will surface its own error if the model is
    genuinely unreachable; preflight only guards the providers it knows."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    problem = TicketWriter(_settings(llm_model="some_future_provider:model-x"))._preflight()

    assert problem is None


def test_dry_run_uses_the_readonly_endpoint():
    assert _settings(dry_run=True).mcp_url.endswith("/x/issues/readonly")


def test_live_uses_the_writable_endpoint():
    url = _settings(dry_run=False).mcp_url
    assert url.endswith("/x/issues")
    assert not url.endswith("readonly")


# --- tool schema sanitization -------------------------------------------------
# Regression coverage for a real crash: GitHub's MCP server emits schemas like
# {"type": "boolean", "enum": [true]} for some fields. That's valid JSON
# Schema, but langchain-google-genai's stricter conversion requires every
# `enum` entry to be a string and raises a pydantic ValidationError on the
# whole tool otherwise — so a Gemini-backed run failed before the model was
# ever called, for a reason with nothing to do with this app's own code.


def test_boolean_enum_is_dropped_but_type_survives():
    schema = {"type": "boolean", "description": "clear this field", "enum": [True]}

    cleaned = _drop_non_string_enums(schema)

    assert "enum" not in cleaned
    assert cleaned["type"] == "boolean"
    assert cleaned["description"] == "clear this field"


def test_string_enum_is_left_alone():
    schema = {"type": "string", "enum": ["open", "closed"]}

    assert _drop_non_string_enums(schema) == schema


def test_nested_boolean_enum_is_found_and_dropped():
    """The real failure was nested three levels deep: properties -> items ->
    properties -> delete. The sanitizer has to recurse, not just check the top."""
    schema = {
        "type": "object",
        "properties": {
            "issue_fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "delete": {"type": "boolean", "enum": [True]},
                        "value": {"type": "string"},
                    },
                },
            }
        },
    }

    cleaned = _drop_non_string_enums(schema)

    delete_schema = cleaned["properties"]["issue_fields"]["items"]["properties"]["delete"]
    assert "enum" not in delete_schema
    assert delete_schema["type"] == "boolean"
    # Untouched sibling survives unchanged.
    assert cleaned["properties"]["issue_fields"]["items"]["properties"]["value"] == {
        "type": "string"
    }


def test_sanitize_tool_schemas_applies_to_every_tool():
    bad = StructuredTool.from_function(
        coroutine=_tool("issue_write").coroutine,
        name="issue_write",
        description="issue_write",
        args_schema={"type": "object", "properties": {"delete": {"type": "boolean", "enum": [True]}}},
    )
    fine = _tool("search_issues")  # args_schema is a pydantic model, not a dict

    cleaned = _sanitize_tool_schemas([bad, fine])

    assert "enum" not in cleaned[0].args_schema["properties"]["delete"]
    assert cleaned[1] is fine, "tools without a dict schema pass through untouched"


# --- sub_issue_write must be gated like any other write -----------------------
# GitHub's `issues` toolset is bigger than the four tools this app was
# designed against. sub_issue_write mutates a real issue exactly like
# issue_write does, but was missing from WRITE_TOOLS — meaning it bypassed the
# repo allowlist and the write cap entirely in live mode.


def test_sub_issue_write_is_gated_by_the_allowlist():
    guard = WriteGuard(_settings())
    tool = guard.wrap([_tool("sub_issue_write")])[0]

    result = _call(tool, owner="org", repo="production", parent_issue_number=1)

    assert "Refused" in result
    assert not CALLS


def test_sub_issue_write_counts_toward_the_write_cap():
    guard = WriteGuard(_settings(max_writes_per_run=1))
    write, sub_write = guard.wrap([_tool("issue_write"), _tool("sub_issue_write")])

    _call(write, owner="org", repo="sandbox")
    blocked = _call(sub_write, owner="org", repo="sandbox", parent_issue_number=1)

    assert "Refused" in blocked
    assert guard.writes_used == 1
