from __future__ import annotations

import logging
import os


logger = logging.getLogger(__name__)


def get_positive_int_env(name: str, default: int) -> int:
    """Read a positive integer setting without making workers fail to boot."""

    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default

    if value < 1:
        logger.warning("%s must be >= 1, falling back to %s", name, default)
        return default

    return value
