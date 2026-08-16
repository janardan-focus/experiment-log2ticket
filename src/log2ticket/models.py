"""The data that moves through the pipeline.

`IncidentContext` is the object this exploration is really about: it is the
complete set of what the model gets to see. If a generated ticket is wrong, look
here first.

`IncidentEvent` is the seam. It is produced in-process by `capture_exception`
today; a cloud log reader could produce one later without anything downstream
noticing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Frame(BaseModel):
    """One stack frame, optionally with the source and locals around it."""

    file: str
    line: int
    function: str
    in_repo: bool = Field(
        description="False for stdlib/site-packages frames. Those are listed "
        "for shape but their source and locals are never read."
    )
    excluded: Literal["library", "refused"] | None = Field(
        default=None,
        description="Why this frame has no source. 'library' is expected and "
        "collapsible; 'refused' means it resolved outside the repo root and is "
        "worth surfacing, since it usually means TARGET_REPO_PATH is wrong.",
    )
    source_excerpt: str | None = Field(
        default=None,
        description="Redacted source around `line`. Only set when in_repo.",
    )
    locals_repr: dict[str, str] | None = Field(
        default=None,
        description="Variable name -> truncated, redacted repr. Only when "
        "CAPTURE_LOCALS is on and the frame is in-repo.",
    )


class IncidentEvent(BaseModel):
    """One exception, captured live. No source code attached yet."""

    incident_id: str
    exception_type: str
    exception_message: str
    frames: list[Frame]
    timestamp: datetime
    endpoint: str | None = None
    chained_from: list[str] = Field(
        default_factory=list,
        description="Earlier exceptions this one was raised while handling.",
    )
    raw_traceback: str


class IncidentContext(BaseModel):
    """Everything the model is sent. Redacted, capped, ready to serialise."""

    incident_id: str
    exception_type: str
    exception_message: str
    frames: list[Frame]
    endpoint: str | None = None
    chained_from: list[str] = Field(default_factory=list)
    timestamp: datetime

    def to_prompt_block(self) -> str:
        """Render as the text actually placed in the user turn."""
        parts = [
            f"Exception: {self.exception_type}: {self.exception_message}",
            f"When: {self.timestamp.isoformat()}",
        ]
        if self.endpoint:
            parts.append(f"Endpoint: {self.endpoint}")
        if self.chained_from:
            parts.append("Raised while handling: " + " <- ".join(self.chained_from))

        parts.append("\n## Stack (innermost last)")

        # A FastAPI request arrives through ~15 frames of uvicorn/starlette
        # middleware. Listing each one buries the two frames that matter, so
        # collapse consecutive library frames into a single line.
        index = 0
        while index < len(self.frames):
            frame = self.frames[index]

            if frame.excluded == "refused":
                # Not collapsed: a refused frame is usually a misconfigured
                # TARGET_REPO_PATH hiding the user's own code, and silently
                # folding it into "library frames" would bury that.
                parts.append(
                    f"\n### {frame.file}:{frame.line} in {frame.function}"
                    "  [outside the configured source root — not read]"
                )
                index += 1
                continue

            if not frame.in_repo:
                run_end = index
                while (
                    run_end < len(self.frames)
                    and not self.frames[run_end].in_repo
                    and self.frames[run_end].excluded != "refused"
                ):
                    run_end += 1
                run = self.frames[index:run_end]
                parts.append(f"\n### … {_summarise_library_run(run)} …")
                index = run_end
                continue

            parts.append(f"\n### {frame.file}:{frame.line} in {frame.function}")
            if frame.source_excerpt:
                parts.append(f"```python\n{frame.source_excerpt}\n```")
            if frame.locals_repr:
                rendered = "\n".join(
                    f"{name} = {value}" for name, value in frame.locals_repr.items()
                )
                parts.append(f"Locals at failure:\n```\n{rendered}\n```")
            index += 1

        return "\n".join(parts)


def _summarise_library_run(run: list[Frame]) -> str:
    """'12 library frames (starlette, fastapi)' — enough to know what was skipped."""
    packages: list[str] = []
    for frame in run:
        head = frame.file.split("/", 1)[0]
        if head and head not in packages:
            packages.append(head)

    count = f"{len(run)} library frame{'s' if len(run) != 1 else ''}"
    if not packages:
        return count
    shown = ", ".join(packages[:4])
    if len(packages) > 4:
        shown += ", …"
    return f"{count} ({shown})"


class TicketOutcome(BaseModel):
    """What the agent did, or in dry-run what it would have done."""

    action: Literal["created", "reopened", "commented", "skipped"]
    title: str = Field(
        description="Format: [Backend] <ExceptionType> in <filename>:<function>"
    )
    description_markdown: str
    root_cause: str
    suggested_fix: str = Field(
        description="Concrete: a named change or a diff. Not 'add error handling'."
    )
    severity: Literal["critical", "high", "medium", "low"]
    labels: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(
        description="Why this was or wasn't a duplicate. Logged, never posted."
    )
    issue_number: int | None = None
    issue_url: str | None = None
    duplicate_of: int | None = Field(
        default=None, description="Set when matched to an existing issue."
    )
