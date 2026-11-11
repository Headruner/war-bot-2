"""What the long-running services are doing right now, so a blocked event loop can be traced to its culprit.

A service wraps one run of its work in ``with activity("wars.cycle"):``; the event-loop monitor names the activities
that were open when the loop came back late. Not per task (a plain list): while one task blocks the loop, no other
task can change it, so what is open at wake-up is what was open during the block. Cheap: one append and one remove.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator

_open: list[tuple[str, float]] = []  # (name, monotonic start), oldest first


@contextlib.contextmanager
def activity(name: str) -> Iterator[None]:
    entry = (name, time.monotonic())
    _open.append(entry)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            _open.remove(entry)


def describe(now: float | None = None) -> str | None:
    """The open activities, the newest (innermost) first, each with how long it has been running; None if none."""
    if not _open:
        return None
    now = time.monotonic() if now is None else now
    return ", ".join(f"{name} (running {max(0.0, now - since):.0f} s)" for name, since in reversed(_open[-4:]))
