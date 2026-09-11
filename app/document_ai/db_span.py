from __future__ import annotations

import logging
from contextlib import contextmanager
from itertools import count
from time import perf_counter

from django.db import connection

db_span_logger = logging.getLogger("db_span")


@contextmanager
def capture_db_spans():
    """Log each SQL statement executed while this context is active, with its
    sequence number and duration. Never logs bound parameter values - only the
    SQL template - since they may contain document content (search queries,
    chunk text). trace_id is attached automatically by the logging formatter."""
    seq = count(1)

    def wrapper(execute, sql, params, many, context):
        started = perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            db_span_logger.info(
                "seq=%s duration_ms=%s sql=%s",
                next(seq),
                round((perf_counter() - started) * 1000, 3),
                sql,
            )

    with connection.execute_wrapper(wrapper):
        yield
