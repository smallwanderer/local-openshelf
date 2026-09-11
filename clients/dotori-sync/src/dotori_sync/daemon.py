from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from dotori_cli.http_client import DotoriClientError
from dotori_cli.sync import SyncPlanError, run_sync

from .debounce import DebouncedTrigger

logger = logging.getLogger("dotori_sync")


class SyncRunner:
    """Runs one-way sync passes through the shared dotori-cli sync engine.

    dotori-sync does not reimplement manifest diffing, upload ordering, or the
    sync HTTP contract: it calls ``dotori_cli.sync.run_sync`` with
    ``apply=True`` on the same folder every pass. This class only adds the
    bookkeeping (error capture, logging) needed to run that function
    unattended, repeatedly, without crashing the process on a bad pass.
    """

    def __init__(
        self,
        client,
        root: Path,
        *,
        root_name: str,
        allow_delete: bool = False,
        ai_processing_enabled: bool = True,
    ) -> None:
        self.client = client
        self.root = root
        self.root_name = root_name
        self.allow_delete = allow_delete
        self.ai_processing_enabled = ai_processing_enabled
        self.last_error: str | None = None
        self.run_count = 0

    def run_once(self) -> dict:
        self.run_count += 1
        try:
            payload = run_sync(
                self.client,
                self.root,
                root_name=self.root_name,
                apply=True,
                allow_delete=self.allow_delete,
                ai_processing_enabled=self.ai_processing_enabled,
            )
        except (SyncPlanError, DotoriClientError) as exc:
            message = str(getattr(exc, "message", exc))
            self.last_error = message
            logger.warning("Sync pass failed: %s", message)
            return {"ok": False, "error": message}
        except Exception as exc:  # pragma: no cover - keeps the daemon alive
            self.last_error = str(exc)
            logger.exception("Sync pass raised an unexpected error.")
            return {"ok": False, "error": str(exc)}

        if payload.get("ok"):
            self.last_error = None
        else:
            errors = [r.get("error", "") for r in payload.get("results", []) if r.get("error")]
            self.last_error = "; ".join(errors) or "One or more sync actions failed."
        summary = payload.get("summary") or {}
        logger.info(
            "Sync pass complete: applied=%s failed=%s skipped=%s "
            "(mkdir=%s upload=%s update=%s delete=%s)",
            payload.get("applied", 0),
            payload.get("failed", 0),
            payload.get("skipped", 0),
            summary.get("mkdir", 0),
            summary.get("upload", 0),
            summary.get("update", 0),
            summary.get("delete", 0),
        )
        return payload


class WatchLoop:
    """Decides when a sync pass is due and drives a ``SyncRunner`` accordingly.

    This class owns no filesystem watching itself; an adapter (see
    ``watcher.py``) reports activity through ``notify()``. Keeping the
    scheduling decision separate from the OS-level watcher makes it possible
    to unit-test "does a burst of events collapse into one sync pass" without
    a real filesystem or real threads.

    A sync pass runs when either is true:
    - ``debounce_seconds`` have passed with no further filesystem activity
      since the last event, or
    - ``fallback_interval_seconds`` have passed since the last pass, even
      with no observed activity (covers watcher events missed by the OS,
      e.g. on some network filesystems).
    """

    def __init__(
        self,
        runner: SyncRunner,
        *,
        debounce_seconds: float = 5.0,
        fallback_interval_seconds: float = 300.0,
        now_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.runner = runner
        self.debounce = DebouncedTrigger(debounce_seconds)
        self.fallback_interval_seconds = fallback_interval_seconds
        self._now = now_fn
        self._sleep = sleep_fn
        self._last_run: float | None = None
        self._stop = False

    def notify(self) -> None:
        """Called by the watcher adapter whenever a filesystem event occurs."""
        self.debounce.mark(self._now())

    def stop(self) -> None:
        self._stop = True

    def tick(self) -> bool:
        """Run a sync pass now if one is due. Returns True if it ran."""
        now = self._now()
        due_to_activity = self.debounce.ready(now)
        due_to_fallback = (
            self._last_run is None or (now - self._last_run) >= self.fallback_interval_seconds
        )
        if not (due_to_activity or due_to_fallback):
            return False
        self.debounce.consume()
        self._last_run = now
        self.runner.run_once()
        return True

    def run_forever(self, poll_seconds: float = 1.0) -> None:
        """Run an initial pass, then poll until ``stop()`` is called."""
        self.runner.run_once()
        self._last_run = self._now()
        while not self._stop:
            self._sleep(poll_seconds)
            if self._stop:
                break
            self.tick()
