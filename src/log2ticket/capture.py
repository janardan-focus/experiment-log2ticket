"""Capture a live exception as an IncidentEvent.

This is the SDK entry point — the function a real backend calls from inside its
exception handler. Because we hold the actual exception object, there is no
text to parse and nothing to reconstruct: `traceback` hands us real frames,
already structured.

The one thing a log file could never give us is here too: the local variables
at the moment of failure. That is off by default (`CAPTURE_LOCALS`), because
locals hold request bodies and credentials.
"""

from __future__ import annotations

import traceback
import uuid
from datetime import datetime

from .config import Settings
from .models import Frame, IncidentEvent

LIBRARY_MARKERS = ("site-packages", "dist-packages", "/lib/python", "<frozen ")

# Never worth sending: dunders, and anything that is a module/class/function
# rather than data.
_UNINTERESTING_PREFIXES = ("__",)
_UNINTERESTING_REPR_PREFIXES = (
    "<module ",
    "<class ",
    "<function ",
    "<built-in ",
    "<bound method ",
)


def is_library_path(path: str) -> bool:
    return any(marker in path for marker in LIBRARY_MARKERS)


def exception_chain(exc: BaseException, limit: int = 5) -> list[str]:
    """Summarise what this exception was raised *while handling*.

    The exception a handler receives is already the outermost one — `__cause__`
    and `__context__` point inward, at the earlier failures. Those are useful
    context ("this TypeError happened while handling a KeyError"), so we record
    them as a short chain rather than descending into them.

    Guards against cycles, which `raise X from X` can produce.
    """
    chain: list[str] = []
    seen: set[int] = {id(exc)}
    current: BaseException | None = exc

    while current is not None and len(chain) < limit:
        # An explicit `raise ... from None` suppresses the implicit context.
        nxt = current.__cause__
        if nxt is None and not current.__suppress_context__:
            nxt = current.__context__
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        chain.append(f"{type(nxt).__name__}: {nxt}")
        current = nxt

    return chain


# A coarse ceiling so a pathological repr cannot balloon memory. This is NOT
# the display limit — truncating to the display limit here would cut secrets
# mid-match and defeat sanitizing, so the real trim happens after sanitizing in
# ContextAssembler.
_MEMORY_GUARD_CHARS = 20_000


def _clean_locals(raw: dict[str, str], settings: Settings) -> dict[str, str] | None:
    """Select which locals are worth keeping. Does not truncate to the display
    limit and does not sanitize — both happen in ContextAssembler, in that order.

    Ordering matters and is the reason this function deliberately does less
    than it looks like it should: every high-value sanitizing rule is anchored
    on a terminator (a connection string needs its closing `@`, an assignment
    needs its closing quote). Clipping to 200 characters first can remove that
    terminator, and the rule then fails to match a secret that is plainly
    visible in the surviving text.
    """
    if not raw:
        return None

    kept: dict[str, str] = {}
    for name, value in raw.items():
        if name.startswith(_UNINTERESTING_PREFIXES):
            continue
        if value.startswith(_UNINTERESTING_REPR_PREFIXES):
            continue
        kept[name] = value[:_MEMORY_GUARD_CHARS]
        if len(kept) >= settings.max_locals_per_frame:
            break

    return kept or None


def capture_exception(
    exc: BaseException,
    *,
    settings: Settings,
    endpoint: str | None = None,
) -> IncidentEvent:
    """Build an IncidentEvent from a live exception. No file, no parsing."""
    summary = traceback.StackSummary.extract(
        traceback.walk_tb(exc.__traceback__),
        capture_locals=settings.capture_locals,
    )

    frames: list[Frame] = []
    for fs in summary:
        in_repo = not is_library_path(fs.filename)
        frames.append(
            Frame(
                file=fs.filename,
                line=fs.lineno or 0,
                function=fs.name,
                in_repo=in_repo,
                # Locals on library frames are noise; skip them outright.
                locals_repr=(
                    _clean_locals(fs.locals or {}, settings)
                    if (settings.capture_locals and in_repo)
                    else None
                ),
            )
        )

    return IncidentEvent(
        incident_id=str(uuid.uuid4()),
        exception_type=type(exc).__name__,
        exception_message=str(exc),
        frames=frames,
        timestamp=datetime.now(),
        endpoint=endpoint,
        chained_from=exception_chain(exc),
        raw_traceback="".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    )
