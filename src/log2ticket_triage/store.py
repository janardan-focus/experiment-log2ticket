"""In-memory buffer of recent incidents.

Deliberately not durable. This is a prototype, and picking a persistence story
is a decision for the real implementation — not something to inherit by
accident because the demo happened to write somewhere.
"""

from __future__ import annotations

from collections import deque

from .models import IncidentEvent


class IncidentStore:
    """Bounded, process-local, newest last."""

    def __init__(self, maxlen: int = 50) -> None:
        self._events: deque[IncidentEvent] = deque(maxlen=maxlen)

    def add(self, event: IncidentEvent) -> str:
        self._events.append(event)
        return event.incident_id

    def latest(self) -> IncidentEvent | None:
        return self._events[-1] if self._events else None

    def get(self, incident_id: str) -> IncidentEvent | None:
        for event in reversed(self._events):
            if event.incident_id == incident_id:
                return event
        return None

    def all(self) -> list[IncidentEvent]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)
