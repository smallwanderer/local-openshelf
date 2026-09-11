from __future__ import annotations


class DebouncedTrigger:
    """Coalesces a burst of activity into a single delayed fire.

    The trigger is armed by ``mark(now)`` on every observed event and becomes
    ``ready`` only once ``quiet_seconds`` have passed without a further
    ``mark`` call. This is pure logic with an injected clock, so it can be
    driven deterministically from tests without real threads or sleeps, while
    a real caller drives it with a wall-clock reading on each poll tick.
    """

    def __init__(self, quiet_seconds: float) -> None:
        if quiet_seconds < 0:
            raise ValueError("quiet_seconds must not be negative.")
        self.quiet_seconds = quiet_seconds
        self._last_activity: float | None = None
        self._pending = False

    def mark(self, now: float) -> None:
        """Record activity at time ``now``; arms the trigger."""
        self._last_activity = now
        self._pending = True

    def ready(self, now: float) -> bool:
        """Return True once ``quiet_seconds`` have elapsed since the last mark."""
        if not self._pending or self._last_activity is None:
            return False
        return (now - self._last_activity) >= self.quiet_seconds

    def consume(self) -> None:
        """Clear the pending flag after acting on a ready trigger."""
        self._pending = False

    @property
    def pending(self) -> bool:
        return self._pending
