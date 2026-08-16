"""Turn a captured incident into the exact payload the model receives.

This is the heart of the exploration. Two rules govern everything here:

1. Only source inside the configured repo root is ever read. A frame path that
   resolves outside it is refused, not clipped — a stack frame can point
   anywhere, and this is the boundary that keeps the agent reading your
   application rather than your filesystem.
2. Everything is redacted here, at one point, before it is returned. Source
   excerpts, exception messages, and locals all pass through the same gate, so
   there is exactly one place to audit.
"""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .models import Frame, IncidentContext, IncidentEvent
from .redact import redact, redaction_report

# Everything before these markers is machine-specific noise.
_PATH_TRIM_MARKERS = ("site-packages/", "dist-packages/")


def _shorten_library_path(path: str) -> str:
    """`/Users/me/proj/.venv/lib/python3.13/site-packages/starlette/routing.py`
    becomes `starlette/routing.py`. Keeps the useful half, drops the leak."""
    for marker in _PATH_TRIM_MARKERS:
        _, sep, tail = path.partition(marker)
        if sep:
            return tail
    if "/lib/python" in path:
        # Stdlib: keep the last two segments, e.g. `json/decoder.py`.
        return "/".join(Path(path).parts[-2:])
    return path


class ContextAssembler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repo_root = settings.target_repo_path.resolve()

    def build(self, event: IncidentEvent) -> IncidentContext:
        return IncidentContext(
            incident_id=event.incident_id,
            exception_type=event.exception_type,
            exception_message=redact(event.exception_message),
            frames=[self._hydrate(frame) for frame in event.frames],
            endpoint=event.endpoint,
            chained_from=[redact(item) for item in event.chained_from],
            timestamp=event.timestamp,
        )

    def _hydrate(self, frame: Frame) -> Frame:
        """Attach source and redact locals, if this frame is one we may read."""
        if not frame.in_repo:
            return frame.model_copy(
                update={
                    "file": _shorten_library_path(frame.file),
                    "excluded": "library",
                    "locals_repr": None,
                }
            )

        resolved = self._safe_resolve(frame.file)
        if resolved is None:
            # Outside the root, missing, or unreadable. Keep the frame so the
            # stack shape survives, but say nothing about its contents — and
            # drop the locals, which came from the same untrusted frame.
            #
            # Bare filename, not the shortened path: a refused frame often has
            # no site-packages marker to trim at, so _shorten_library_path
            # would hand the provider the full absolute host path.
            return frame.model_copy(
                update={
                    "file": Path(frame.file).name,
                    "in_repo": False,
                    "excluded": "refused",
                    "locals_repr": None,
                }
            )

        return frame.model_copy(
            update={
                # Repo-relative. Absolute paths leak the developer's home
                # directory and machine layout to the model provider, and add
                # nothing the model can use.
                "file": str(resolved.relative_to(self.repo_root)),
                "source_excerpt": self._excerpt(resolved, frame.line),
                "locals_repr": self._redact_locals(frame.locals_repr),
            }
        )

    def _redact_locals(self, values: dict[str, str] | None) -> dict[str, str] | None:
        """Redact every local value, *then* truncate.

        Locals are the highest-risk thing in the payload — a source excerpt
        leaks what is written in the file, locals leak what was flowing through
        it.

        The order is the whole point. Redaction rules are anchored on
        terminators, so truncating first can cut a secret's closing delimiter
        and silently stop the rule from matching. Redact the full value, then
        trim the (now safe) result for display.
        """
        if not values:
            return None

        limit = self.settings.max_local_repr_chars
        out: dict[str, str] = {}
        for name, value in values.items():
            safe = redact(value)
            out[name] = safe if len(safe) <= limit else safe[:limit] + "…"
        return out

    def _safe_resolve(self, raw_path: str) -> Path | None:
        """Resolve a frame's path, refusing anything outside the repo root."""
        try:
            candidate = Path(raw_path)
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self.repo_root / candidate).resolve()
            )
        except (OSError, RuntimeError):
            return None

        if not resolved.is_relative_to(self.repo_root):
            return None
        if not resolved.is_file():
            return None
        return resolved

    def _excerpt(self, path: Path, line: int) -> str | None:
        """±span lines around `line`, redacted, fitted to the byte budget.

        The budget is spent *outward from the failing line* rather than by
        clipping the tail. Clipping the tail drops the `->` line whenever the
        window is bigger than the budget — handing the model the code above the
        failure and nothing else, which is worse than a smaller correct window.
        """
        if line < 1:
            # No line number (some frames have none). A window centred on
            # nothing would present the top of the file as the failure site.
            return None

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        if not lines:
            return None

        target = min(line, len(lines))
        span = self.settings.source_context_lines
        budget = self.settings.max_excerpt_bytes

        def render(index: int) -> str:
            marker = "->" if index == line else "  "
            return f"{marker} {index:>4} | {lines[index - 1]}"

        centre = redact(render(target))
        used = len(centre.encode("utf-8"))
        kept: dict[int, str] = {target: centre}

        # Grow alternately up and down so the failing line stays centred.
        truncated = False
        for step in range(1, span + 1):
            for index in (target - step, target + step):
                if not (1 <= index <= len(lines)) or index in kept:
                    continue
                rendered = redact(render(index))
                cost = len(rendered.encode("utf-8")) + 1  # + newline
                if used + cost > budget:
                    truncated = True
                    continue
                kept[index] = rendered
                used += cost

        excerpt = "\n".join(kept[i] for i in sorted(kept))
        if truncated:
            excerpt += "\n… (truncated)"
        return excerpt

    def report(self, event: IncidentEvent) -> dict[str, int]:
        """What redaction caught on its way into the context. Shown in the UI.

        Measured against the *pre-redaction* material that actually reaches the
        payload — the source excerpts and the raw locals — not against
        `raw_traceback`, which is never sent. An earlier version reported the
        traceback and omitted the excerpts, so it simultaneously over-counted
        things that were never at risk and missed the largest part of the
        payload.
        """
        material: list[str] = [event.exception_message, *event.chained_from]

        for frame in event.frames:
            if not frame.in_repo:
                continue
            resolved = self._safe_resolve(frame.file)
            if resolved is not None and frame.line >= 1:
                try:
                    material.append(
                        resolved.read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    pass
            if frame.locals_repr:
                material.extend(frame.locals_repr.values())

        return redaction_report("\n".join(material))
