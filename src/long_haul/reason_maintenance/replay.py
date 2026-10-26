"""Read-only projection of explicitly namespaced fixture events.

This is a standalone experiment, not the #138 materializer's integration API.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..events.store import Event
from .models import Change, Explanation, Snapshot

NAMESPACE = "reason_maintenance_experiment"


class Backend(Protocol):
    def apply(self, change: Change, event_id: str) -> None: ...
    def snapshot(self) -> Snapshot: ...
    def explain(self, assertion_id: str) -> Explanation: ...


def replay(events: Iterable[Event], backend: Backend) -> Snapshot:
    """Rebuild from ordered authoritative events; backend is disposable.

    Unrelated events are ignored. Duplicate event identities are rejected rather
    than interpreted as extra evidence. Callers supply a fresh backend.
    """
    seen: set[str] = set()
    for event in events:
        if not event.id or event.id in seen:
            raise ValueError("empty or duplicate authoritative event identity")
        seen.add(event.id)
        if NAMESPACE not in event.payload:
            continue
        if event.schema_version != 1 or event.event_type != "observation":
            raise ValueError("unsupported experimental event envelope")
        change = Change.model_validate(event.payload[NAMESPACE])
        backend.apply(change, event.id)
    return backend.snapshot()
