"""Structured JSON logging."""

import logging
import sys
from typing import Any

import structlog


def configure_logging(service: str, level: str = "INFO") -> None:
    """Send JSON log lines to stdout, each tagged with the service name."""
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s", force=True)
    for noisy in ("azure", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)


def get_logger(**context: Any) -> Any:
    """Return a logger that adds the given fields (for example run_id) to every line."""
    return structlog.get_logger().bind(**context)
