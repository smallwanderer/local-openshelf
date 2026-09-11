from __future__ import annotations

import logging
from contextvars import ContextVar
from uuid import uuid4

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    return uuid4().hex


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def set_trace_id(value: str | None) -> None:
    _trace_id_var.set(value)


class TraceIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id() or "-"
        return True
