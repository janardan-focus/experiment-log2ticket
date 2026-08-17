from __future__ import annotations

from pathlib import Path

import pytest

from log2ticket.capture import capture_exception
from log2ticket.config import Settings
from log2ticket.context import ContextAssembler
from log2ticket.models import Frame, IncidentEvent


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny 'application' the assembler is allowed to read."""
    (tmp_path / "app.py").write_text(
        "\n".join(f"line_{i} = {i}" for i in range(1, 61)), encoding="utf-8"
    )
    (tmp_path / "creds.py").write_text(
        'API_KEY = "sk_test_FAKEKEYnotreal0000000"\n'
        'DSN = "postgresql://user:hunter2@db.internal:5432/app"\n',
        encoding="utf-8",
    )
    return tmp_path


def _settings(repo: Path, **overrides) -> Settings:
    return Settings(
        github_token="", github_repo="", target_repo_path=repo, **overrides
    )


def _event(frames: list[Frame], **overrides) -> IncidentEvent:
    base = dict(
        incident_id="inc-1",
        exception_type="ValueError",
        exception_message="something broke",
        frames=frames,
        timestamp=__import__("datetime").datetime.now(),
        raw_traceback="Traceback...",
    )
    base.update(overrides)
    return IncidentEvent(**base)


def test_excerpt_centres_on_the_failing_line(repo: Path):
    assembler = ContextAssembler(_settings(repo, source_context_lines=3))
    frame = Frame(file=str(repo / "app.py"), line=30, function="f", in_repo=True)

    result = assembler.build(_event([frame])).frames[0]

    assert result.source_excerpt is not None
    assert "->   30 | line_30 = 30" in result.source_excerpt
    assert "    27 | line_27 = 27" in result.source_excerpt
    assert "line_26" not in result.source_excerpt  # outside the ±3 window


def test_path_outside_repo_root_is_refused(repo: Path):
    """A frame can point anywhere. The root is the boundary."""
    assembler = ContextAssembler(_settings(repo))
    frame = Frame(file="/etc/passwd", line=1, function="f", in_repo=True)

    result = assembler.build(_event([frame])).frames[0]

    assert result.source_excerpt is None
    assert result.in_repo is False


def test_traversal_out_of_repo_root_is_refused(repo: Path):
    assembler = ContextAssembler(_settings(repo))
    frame = Frame(
        file=str(repo / ".." / ".." / "etc" / "passwd"),
        line=1,
        function="f",
        in_repo=True,
    )

    result = assembler.build(_event([frame])).frames[0]

    assert result.source_excerpt is None
    assert result.in_repo is False


def test_refused_frame_drops_its_locals(repo: Path):
    """Locals came from the same untrusted frame — they go too."""
    assembler = ContextAssembler(_settings(repo))
    frame = Frame(
        file="/etc/passwd",
        line=1,
        function="f",
        in_repo=True,
        locals_repr={"secret": "'hunter2'"},
    )

    assert assembler.build(_event([frame])).frames[0].locals_repr is None


def test_missing_file_is_refused_not_crashed(repo: Path):
    assembler = ContextAssembler(_settings(repo))
    frame = Frame(file=str(repo / "gone.py"), line=1, function="f", in_repo=True)

    assert assembler.build(_event([frame])).frames[0].source_excerpt is None


def test_secrets_in_source_are_sanitized(repo: Path):
    assembler = ContextAssembler(_settings(repo))
    frame = Frame(file=str(repo / "creds.py"), line=1, function="f", in_repo=True)

    excerpt = assembler.build(_event([frame])).frames[0].source_excerpt

    assert "sk_test_FAKEKEYnotr" not in excerpt
    assert "hunter2" not in excerpt
    assert "REDACTED" in excerpt


def test_secrets_in_locals_are_sanitized(repo: Path):
    assembler = ContextAssembler(_settings(repo))
    frame = Frame(
        file=str(repo / "app.py"),
        line=1,
        function="charge",
        in_repo=True,
        locals_repr={
            "api_key": "'sk_test_FAKEKEYnotreal0000000'",
            "dsn": "'postgresql://user:hunter2@db.internal:5432/app'",
            "quantity": "0",
        },
    )

    result = assembler.build(_event([frame])).frames[0].locals_repr

    assert "sk_test_FAKEKEYnotr" not in result["api_key"]
    assert "hunter2" not in result["dsn"]
    assert result["quantity"] == "0"  # ordinary values survive intact


def test_excerpt_is_byte_capped(repo: Path):
    assembler = ContextAssembler(
        _settings(repo, source_context_lines=100, max_excerpt_bytes=200)
    )
    frame = Frame(file=str(repo / "app.py"), line=30, function="f", in_repo=True)

    excerpt = assembler.build(_event([frame])).frames[0].source_excerpt

    # The suffix is multibyte ("…" is 3 bytes), so compare in bytes, not chars.
    suffix_bytes = len("\n… (truncated)".encode("utf-8"))
    assert len(excerpt.encode("utf-8")) <= 200 + suffix_bytes
    assert excerpt.endswith("… (truncated)")


def test_library_frames_get_no_source(repo: Path):
    assembler = ContextAssembler(_settings(repo))
    frame = Frame(
        file="/usr/lib/python3.13/json/decoder.py",
        line=10,
        function="decode",
        in_repo=False,
    )

    assert assembler.build(_event([frame])).frames[0].source_excerpt is None


def test_prompt_block_marks_library_frames(repo: Path):
    assembler = ContextAssembler(_settings(repo))
    context = assembler.build(
        _event(
            [
                Frame(file="/lib/python3.13/x.py", line=1, function="lib", in_repo=False),
                Frame(file=str(repo / "app.py"), line=5, function="mine", in_repo=True),
            ]
        )
    )

    text = context.to_prompt_block()
    assert "1 library frame" in text  # collapsed, not listed
    assert "line_5 = 5" in text


def test_end_to_end_capture_then_assemble(repo: Path):
    """The real path: a live exception in a file under the repo root."""
    module = repo / "buggy.py"
    module.write_text(
        "def divide(total, quantity):\n"
        "    return total / quantity\n",
        encoding="utf-8",
    )
    namespace: dict = {}
    exec(compile(module.read_text(), str(module), "exec"), namespace)  # noqa: S102

    settings = _settings(repo, capture_locals=True)
    try:
        namespace["divide"](100, 0)
    except ZeroDivisionError as exc:
        event = capture_exception(exc, settings=settings)

    context = ContextAssembler(settings).build(event)
    failing = context.frames[-1]

    assert failing.function == "divide"
    assert "-> " in failing.source_excerpt
    assert failing.locals_repr["quantity"] == "0"
    assert "Locals at failure" in context.to_prompt_block()


def test_library_frames_are_collapsed_in_prompt(repo: Path):
    """A FastAPI stack is ~15 middleware frames. They must not bury the signal."""
    assembler = ContextAssembler(_settings(repo))
    lib = [
        Frame(file=f"/x/.venv/lib/python3.13/site-packages/starlette/m{i}.py",
              line=1, function="call", in_repo=False)
        for i in range(12)
    ]
    mine = Frame(file=str(repo / "app.py"), line=5, function="mine", in_repo=True)

    text = assembler.build(_event([*lib, mine])).to_prompt_block()

    assert "12 library frames (starlette)" in text
    assert text.count("starlette/m") == 0     # individually listed nowhere
    assert "line_5 = 5" in text               # our frame survives intact


def test_in_repo_paths_are_repo_relative(repo: Path):
    """Absolute paths leak the developer's home directory to the provider."""
    assembler = ContextAssembler(_settings(repo))
    frame = Frame(file=str(repo / "app.py"), line=5, function="f", in_repo=True)

    result = assembler.build(_event([frame])).frames[0]

    assert result.file == "app.py"
    assert str(repo) not in result.file


def test_library_paths_are_shortened(repo: Path):
    assembler = ContextAssembler(_settings(repo))
    frames = [
        Frame(file="/home/me/.venv/lib/python3.13/site-packages/fastapi/routing.py",
              line=1, function="app", in_repo=False),
        Frame(file="/usr/lib/python3.13/json/decoder.py",
              line=2, function="decode", in_repo=False),
    ]

    result = assembler.build(_event(frames)).frames

    assert result[0].file == "fastapi/routing.py"
    assert result[1].file == "json/decoder.py"
    assert all("/home/me" not in f.file for f in result)
