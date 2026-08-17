"""Regressions for the code-review findings.

One test per finding, each reproducing the original defect. These are the tests
the existing 42 did not have — every finding below passed the old suite.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from log2ticket.capture import capture_exception
from log2ticket.config import Settings
from log2ticket.context import ContextAssembler
from log2ticket.models import Frame, IncidentContext, IncidentEvent
from log2ticket.sanitize import sanitize


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "big.py").write_text(
        "\n".join(f"line_{i} = {i}" for i in range(1, 121)), encoding="utf-8"
    )
    return tmp_path


def _settings(**overrides) -> Settings:
    return Settings(github_token="", github_repo="", **overrides)


def _event(frames: list[Frame], **overrides) -> IncidentEvent:
    import datetime

    base = dict(
        incident_id="inc-1",
        exception_type="ValueError",
        exception_message="broke",
        frames=frames,
        timestamp=datetime.datetime.now(),
        raw_traceback="Traceback...",
    )
    base.update(overrides)
    return IncidentEvent(**base)


# --- 1: comma-separated allowlist from the environment -----------------------


def test_repo_allowlist_parses_from_env():
    """pydantic-settings JSON-decodes complex fields before validators run, so
    a plain CSV value crashed settings load and the app could not boot."""
    os.environ["REPO_ALLOWLIST"] = "org/repo-a, org/repo-b"
    try:
        settings = _settings()
        assert settings.repo_allowlist == ["org/repo-a", "org/repo-b"]
    finally:
        del os.environ["REPO_ALLOWLIST"]


def test_env_example_values_load():
    """Every value shipped in .env.example must actually parse."""
    os.environ["REPO_ALLOWLIST"] = "your-org/your-sandbox-repo"
    os.environ["CAPTURE_LOCALS"] = "false"
    try:
        assert _settings().repo_allowlist == ["your-org/your-sandbox-repo"]
    finally:
        del os.environ["REPO_ALLOWLIST"]
        del os.environ["CAPTURE_LOCALS"]


# --- 2: sanitize before truncating ---------------------------------------------


def test_long_secret_is_sanitized_before_truncation():
    """Truncating first cuts the terminator a rule anchors on, so the rule
    stops matching and the cleartext secret is sent."""
    password = "S3cretPassw0rd" * 3

    def boom() -> None:
        dsn = f"postgresql://payments_user:{password}@db.internal:5432/payments"  # noqa: F841
        raise ValueError("x")

    # The failing frame is in *this* file, so the root must cover it.
    settings = _settings(
        target_repo_path=Path(__file__).parent,
        capture_locals=True,
        max_local_repr_chars=60,
    )
    try:
        boom()
    except ValueError as exc:
        event = capture_exception(exc, settings=settings)

    result = ContextAssembler(settings).build(event).frames[-1].locals_repr
    assert password not in result["dsn"]
    assert "REDACTED" in result["dsn"]


def test_locals_still_truncated_for_display():
    def boom() -> None:
        blob = "x" * 5_000  # noqa: F841
        raise ValueError("x")

    settings = _settings(
        target_repo_path=Path(__file__).parent,
        capture_locals=True,
        max_local_repr_chars=80,
    )
    try:
        boom()
    except ValueError as exc:
        event = capture_exception(exc, settings=settings)

    assert len(ContextAssembler(settings).build(event).frames[-1].locals_repr["blob"]) <= 81


# --- 3: the byte budget must keep the failing line ---------------------------


def test_byte_cap_keeps_the_failing_line(repo: Path):
    """Clipping the tail dropped the '->' line — the only one that matters."""
    settings = _settings(
        target_repo_path=repo, source_context_lines=40, max_excerpt_bytes=300
    )
    frame = Frame(file=str(repo / "big.py"), line=80, function="f", in_repo=True)

    excerpt = ContextAssembler(settings).build(_event([frame])).frames[0].source_excerpt

    assert "->   80 |" in excerpt
    assert len(excerpt.encode("utf-8")) <= 300 + len("\n… (truncated)".encode())


def test_excerpt_stays_centred_on_the_failure(repo: Path):
    settings = _settings(
        target_repo_path=repo, source_context_lines=40, max_excerpt_bytes=400
    )
    frame = Frame(file=str(repo / "big.py"), line=80, function="f", in_repo=True)

    excerpt = ContextAssembler(settings).build(_event([frame])).frames[0].source_excerpt
    kept = [int(l.split("|")[0].strip().lstrip("->").strip())
            for l in excerpt.splitlines() if "|" in l]

    assert min(kept) < 80 < max(kept), "window should span both sides of the failure"


# --- 4: assigned_secret must not blank ordinary code -------------------------


@pytest.mark.parametrize(
    "code",
    [
        "secret_count = len(items)",
        "password = get_password()",
        "self.api_key = settings.gateway_key",
        "api_key_field = models.CharField()",
    ],
)
def test_ordinary_code_is_not_sanitized(code: str):
    """The excerpt exists so the model can read the code. Blanking real
    assignments defeats the purpose."""
    assert sanitize(code) == code


@pytest.mark.parametrize(
    "code",
    [
        'API_KEY = "hunter2hunter2"',
        "password: 'correct-horse-battery'",
        'GATEWAY_SECRET = "abcdef123456"',
    ],
)
def test_quoted_literal_secrets_still_sanitized(code: str):
    assert "REDACTED" in sanitize(code)


# --- 5: the report must describe what is actually sent -----------------------


def test_report_covers_source_excerpts(tmp_path: Path):
    (tmp_path / "creds.py").write_text(
        'API_KEY = "sk_test_FAKEKEYnotreal0000000"\n', encoding="utf-8"
    )
    settings = _settings(target_repo_path=tmp_path)
    frame = Frame(file=str(tmp_path / "creds.py"), line=1, function="f", in_repo=True)

    report = ContextAssembler(settings).report(_event([frame]))

    assert report, "the excerpt contains a key; the report must say so"


def test_report_ignores_the_unsent_traceback(tmp_path: Path):
    """raw_traceback is never sent, so counting it inflates the report."""
    settings = _settings(target_repo_path=tmp_path)
    event = _event([], raw_traceback="token=abcdefghijklmnop dev@example.com")

    assert ContextAssembler(settings).report(event) == {}


# --- 6: refused frames must not leak the host path ---------------------------


def test_refused_frame_is_reduced_to_a_bare_filename(tmp_path: Path):
    """A path with no site-packages marker used to pass through unchanged."""
    settings = _settings(target_repo_path=tmp_path)
    frame = Frame(
        file="/Users/someone/secret-project/lib/thing.py",
        line=4,
        function="f",
        in_repo=True,
    )

    result = ContextAssembler(settings).build(_event([frame])).frames[0]

    assert result.file == "thing.py"
    assert "/Users/someone" not in result.file
    assert result.excluded == "refused"


def test_refused_frames_are_not_collapsed_as_library_noise(tmp_path: Path):
    """A misconfigured root would otherwise hide the user's own code."""
    settings = _settings(target_repo_path=tmp_path)
    frame = Frame(
        file="/elsewhere/app/orders.py", line=9, function="create_order", in_repo=True
    )

    text = ContextAssembler(settings).build(_event([frame])).to_prompt_block()

    assert "orders.py:9 in create_order" in text
    assert "outside the configured source root" in text
    assert "library frame" not in text


# --- 7: no line number means no excerpt --------------------------------------


def test_zero_line_number_yields_no_excerpt(repo: Path):
    """line=0 used to render the top of the file as the failure site."""
    settings = _settings(target_repo_path=repo)
    frame = Frame(file=str(repo / "big.py"), line=0, function="f", in_repo=True)

    assert ContextAssembler(settings).build(_event([frame])).frames[0].source_excerpt is None


# --- 8: HTTP Basic credentials -----------------------------------------------


def test_basic_auth_is_sanitized():
    out = sanitize("Authorization: Basic dXNlcjpwYXNzd29yZA==")
    assert "dXNlcjpwYXNzd29yZA==" not in out
    assert "REDACTED" in out


def test_bearer_still_sanitized():
    out = sanitize("Authorization: Bearer abcdef1234567890xyz")
    assert "abcdef1234567890xyz" not in out


# --- 10: the UI is handed the sanitizing evidence -----------------------------


def test_inspect_payload_includes_sanitizations(tmp_path: Path):
    """The demo's central safety claim has to be visible, not just computed."""
    (tmp_path / "creds.py").write_text('KEY = "sk_live_abcdefghij123456"\n', encoding="utf-8")
    settings = _settings(target_repo_path=tmp_path)
    frame = Frame(file=str(tmp_path / "creds.py"), line=1, function="f", in_repo=True)
    event = _event([frame])

    assembler = ContextAssembler(settings)
    payload = {
        "context_text": assembler.build(event).to_prompt_block(),
        "sanitizations": assembler.report(event),
    }

    assert payload["sanitizations"]
    assert isinstance(payload["context_text"], str)
