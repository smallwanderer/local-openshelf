"""Marks an inference already covered by the process admission lease."""
from contextvars import ContextVar

admitted = ContextVar("embedding_admitted", default=False)
