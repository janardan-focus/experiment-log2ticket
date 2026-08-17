from __future__ import annotations

import pytest

from log2ticket.sanitize import sanitize, sanitize_report

SECRETS = [
    ("anthropic key", "sk-ant-api03-AbCdEfGh1234567890xyz", "sk-ant-api03"),
    ("stripe key", "sk_test_FAKEKEYnotreal0000000", "sk_test_FAKEKEY"),
    ("github pat", "ghp_16C7e42F292c6912E7710c838347Ae178B4a", "ghp_16C7e42F"),
    ("github fine-grained", "github_pat_11ABCDE0Y_abcdefghij1234567890", "github_pat_11"),
    ("aws key", "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7"),
    ("google key", "AIzaSyD-1234567890abcdefghijklmnopqrstuv", "AIzaSyD-123"),
]


@pytest.mark.parametrize("label,secret,fragment", SECRETS, ids=[s[0] for s in SECRETS])
def test_known_key_formats_are_removed(label: str, secret: str, fragment: str):
    out = sanitize(f"the key is {secret} ok")
    assert fragment not in out
    assert "REDACTED" in out


def test_connection_string_password_removed_host_kept():
    """The password goes; the host stays, because it is useful for debugging."""
    out = sanitize("postgresql://payments_user:hunter2@db.internal:5432/payments")
    assert "hunter2" not in out
    assert "payments_user" in out
    assert "db.internal" in out


def test_connection_string_beats_email_rule():
    """A DSN contains an '@'. Order matters, or the email rule eats half of it."""
    out = sanitize("mongodb://svc:p4ssw0rd@cluster.example.com:27017/db")
    assert "p4ssw0rd" not in out
    assert "REDACTED_PASSWORD" in out


def test_email_removed():
    assert "payments-oncall@example.com" not in sanitize("ping payments-oncall@example.com")


def test_bearer_token_removed():
    out = sanitize("Authorization: Bearer abcdef1234567890xyz")
    assert "abcdef1234567890xyz" not in out


def test_assigned_secret_by_name():
    """Catches hardcoded creds whose value matches no known key format."""
    out = sanitize('GATEWAY_PASSWORD = "correct-horse-battery"')
    assert "correct-horse-battery" not in out


def test_public_ip_removed_loopback_kept():
    out = sanitize("upstream 203.0.113.42 failed; bound to 127.0.0.1")
    assert "203.0.113.42" not in out
    assert "127.0.0.1" in out


def test_ordinary_text_survives():
    """Sanitizing must not eat the content that makes a ticket useful."""
    text = "ZeroDivisionError: division by zero in create_order at orders.py:30"
    assert sanitize(text) == text


def test_idempotent():
    once = sanitize("key sk_test_FAKEKEYnotreal0000000")
    assert sanitize(once) == once


def test_empty_input():
    assert sanitize("") == ""


def test_report_names_what_fired():
    report = sanitize_report(
        "sk-ant-api03-AbCdEfGh1234567890xyz and dev@example.com"
    )
    assert "anthropic_key" in report
    assert "email" in report
