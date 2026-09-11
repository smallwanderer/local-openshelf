from __future__ import annotations

from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


class _ActivityHandler(FileSystemEventHandler):
    """Forwards every filesystem event as a plain activity signal.

    dotori-sync only needs to know *that* something changed under the watched
    root, not *what* changed: the next sync pass re-diffs the whole folder
    against the server manifest anyway. So every event (except directory
    "modified", which fires redundantly alongside the child event that caused
    it) collapses to a single ``on_activity()`` call.
    """

    def __init__(self, on_activity: Callable[[], None]) -> None:
        self._on_activity = on_activity

    def on_any_event(self, event) -> None:
        if event.is_directory and event.event_type == "modified":
            return
        self._on_activity()


def start_observer(root: Path, on_activity: Callable[[], None]) -> Observer:
    """Start a recursive watchdog Observer over ``root``.

    Calls ``on_activity()`` on any filesystem change underneath it. The
    caller owns the returned Observer's lifecycle: call ``.stop()`` then
    ``.join()`` to shut it down cleanly.
    """
    handler = _ActivityHandler(on_activity)
    observer = Observer()
    observer.schedule(handler, str(root), recursive=True)
    observer.start()
    return observer
