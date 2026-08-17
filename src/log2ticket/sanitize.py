"""Strip secrets and personal data before anything leaves the process.

This runs on log text *and* on source excerpts, before the model call — not
just before writing to GitHub. Sending a connection string to a model provider
is the same disclosure as posting it in a public issue.

Order matters: connection strings are matched before emails, because a DSN
contains an `@` and would otherwise be half-eaten by the email rule.
"""

from __future__ import annotations

import re

Rule = tuple[str, re.Pattern[str], str]

RULES: list[Rule] = [
    (
        "connection_string",
        re.compile(r"\b([a-z][a-z0-9+.\-]*)://([^\s:/@]+):([^\s@]+)@", re.I),
        r"\1://\2:[REDACTED_PASSWORD]@",
    ),
    (
        "auth_header",
        # Covers Basic too: `Authorization: Basic dXNlcjpwYXNzd29yZA==` is a
        # base64 user:password, and the base64 alphabet includes +/= which the
        # bearer-only value class excluded.
        re.compile(
            r"\b(bearer|basic|token|authorization)(\s*[:=]\s*|\s+)"
            r"(?:(bearer|basic)\s+)?([A-Za-z0-9._\-+/]{12,}={0,2})",
            re.I,
        ),
        r"\1\2\3 [REDACTED_TOKEN]",
    ),
    (
        "anthropic_key",
        re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{10,}"),
        "[REDACTED_API_KEY]",
    ),
    (
        "openai_stripe_key",
        re.compile(r"\bsk[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,}"),
        "[REDACTED_API_KEY]",
    ),
    (
        "github_token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"),
        "[REDACTED_GITHUB_TOKEN]",
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "[REDACTED_AWS_KEY]",
    ),
    (
        "google_key",
        # Real keys are AIza + 35 chars, but pinning the length exactly means a
        # near-miss sails through. A sanitizing rule should over-match.
        re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}"),
        "[REDACTED_API_KEY]",
    ),
    (
        "assigned_secret",
        # KEY = "..." / "password": "..." — hardcoded creds with no specific
        # pattern of their own.
        #
        # The value MUST be a quoted literal. An earlier version accepted any
        # non-delimiter run, which matched ordinary code: `secret_count =
        # len(items)`, `password = get_password()` and `self.api_key =
        # settings.gateway_key` were all rewritten to `[REDACTED]`. Blanking
        # real code defeats the purpose of sending an excerpt at all — the
        # model is there to read it.
        re.compile(
            r"""(?ix)
            \b(\w*(?:secret|password|passwd|api[_-]?key|access[_-]?token|private[_-]?key)\w*)
            \s*[:=]\s*
            (["'])(?P<value>(?:(?!\2).){6,}?)\2
            """
        ),
        r"\1=[REDACTED]",
    ),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    (
        "ipv4",
        # Skip loopback and link-local — those are noise, not disclosure.
        re.compile(r"\b(?!127\.0\.0\.1\b)(?!0\.0\.0\.0\b)(?:\d{1,3}\.){3}\d{1,3}\b"),
        "[REDACTED_IP]",
    ),
]


def sanitize(text: str) -> str:
    """Apply every rule in order. Safe to call on already-sanitized text."""
    if not text:
        return text
    for _name, pattern, replacement in RULES:
        text = pattern.sub(replacement, text)
    return text


def sanitize_report(text: str) -> dict[str, int]:
    """Which rules fired and how often. Used by the UI and by tests."""
    return {
        name: len(pattern.findall(text))
        for name, pattern, _ in RULES
        if pattern.search(text)
    }
