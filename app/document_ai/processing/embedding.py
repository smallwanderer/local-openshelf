"""Compatibility imports for the embedding executor domain.

New code should import from :mod:`document_ai.embedding.executor`. This module
remains temporarily so existing extensions do not break during the service-name
migration.
"""

from document_ai.embedding.executor import *  # noqa: F401,F403
from document_ai.embedding.executor import _group_chunks_into_batches
