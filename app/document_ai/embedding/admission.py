from __future__ import annotations

import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field


_REQUEST_PRIORITIES = {
    "query": 0,
    "document": 10,
}


class AdmissionRejected(RuntimeError):
    """Raised when an embedding request cannot enter the bounded queue."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(order=True)
class _Waiter:
    priority: int
    sequence: int
    request_type: str = field(compare=False)


class AdmissionLease:
    def __init__(self, admission: "EmbeddingPriorityAdmission") -> None:
        self._admission = admission
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._admission._release()

    def __enter__(self) -> "AdmissionLease":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class EmbeddingPriorityAdmission:
    """Process-local, bounded admission queue in front of model.encode().

    Query requests outrank document-ingestion requests. Work that is already
    running is never preempted; priority is applied when the next model slot
    becomes available. Requests of the same type remain FIFO.
    """

    def __init__(
        self,
        *,
        concurrency: int,
        queue_capacity: int,
        query_reserve: int = 1,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if queue_capacity < 0:
            raise ValueError("queue_capacity must be >= 0")
        if not 0 <= query_reserve <= queue_capacity:
            raise ValueError("query_reserve must be between 0 and queue_capacity")

        self.concurrency = concurrency
        self.queue_capacity = queue_capacity
        self.query_reserve = query_reserve
        self._condition = threading.Condition()
        self._sequence = itertools.count()
        self._active = 0
        self._waiters: list[_Waiter] = []

    def acquire(self, request_type: str, *, timeout: float) -> AdmissionLease:
        priority = self._priority(request_type)
        deadline = time.monotonic() + max(timeout, 0.0)

        with self._condition:
            if self._active < self.concurrency and not self._waiters:
                self._active += 1
                return AdmissionLease(self)

            if not self._can_enqueue(request_type):
                raise AdmissionRejected("queue_full")

            waiter = _Waiter(
                priority=priority,
                sequence=next(self._sequence),
                request_type=request_type,
            )
            heapq.heappush(self._waiters, waiter)

            try:
                while True:
                    if (
                        self._active < self.concurrency
                        and self._waiters
                        and self._waiters[0] is waiter
                    ):
                        heapq.heappop(self._waiters)
                        self._active += 1
                        return AdmissionLease(self)

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._remove_waiter(waiter)
                        raise AdmissionRejected("queue_timeout")
                    self._condition.wait(timeout=remaining)
            except BaseException:
                self._remove_waiter(waiter)
                raise

    def try_acquire(self) -> AdmissionLease | None:
        """Acquire without queueing, used by the one-time readiness probe."""
        with self._condition:
            if self._active >= self.concurrency or self._waiters:
                return None
            self._active += 1
            return AdmissionLease(self)

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "active": self._active,
                "waiting": len(self._waiters),
                "query_waiting": sum(
                    waiter.request_type == "query" for waiter in self._waiters
                ),
                "document_waiting": sum(
                    waiter.request_type == "document" for waiter in self._waiters
                ),
            }

    def _can_enqueue(self, request_type: str) -> bool:
        waiting = len(self._waiters)
        if waiting >= self.queue_capacity:
            return False
        if request_type == "document":
            document_limit = self.queue_capacity - self.query_reserve
            document_waiting = sum(
                waiter.request_type == "document" for waiter in self._waiters
            )
            if document_waiting >= document_limit:
                return False
        return True

    def _remove_waiter(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return
        heapq.heapify(self._waiters)
        self._condition.notify_all()

    def _release(self) -> None:
        with self._condition:
            if self._active < 1:
                raise RuntimeError("embedding admission released without an active lease")
            self._active -= 1
            self._condition.notify_all()

    @staticmethod
    def _priority(request_type: str) -> int:
        try:
            return _REQUEST_PRIORITIES[request_type]
        except KeyError as exc:
            raise ValueError(f"unknown embedding request type: {request_type}") from exc
