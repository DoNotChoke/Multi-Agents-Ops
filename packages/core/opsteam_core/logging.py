"""Structured JSON logging that redacts every event before it is written."""

import json
import logging
from typing import cast

import structlog
from structlog.typing import EventDict, WrappedLogger

from opsteam_core.errors import RedactionFailed
from opsteam_core.redact import redact_value


def configure_logging(level: str) -> None:
    """Send JSON lines with a UTC timestamp to stdout, dropping events below `level`."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            _redact_event,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def _redact_event(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    try:
        # repr() arbitrary objects first so secrets hidden in them are redacted too.
        plain = json.loads(json.dumps(event_dict, default=repr))
        clean, _ = redact_value(plain)
    except (RedactionFailed, RecursionError, ValueError):
        return {"event": "log entry dropped: redaction failed", "level": "error"}
    return cast(EventDict, clean)
