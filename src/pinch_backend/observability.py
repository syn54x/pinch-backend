"""Observability facade: structured logging, tracing, and one-call configuration."""

from __future__ import annotations

import logging
import os
from typing import Literal

import logfire
import structlog

from pinch_backend.settings import settings

_CONFIGURED = False


def _send_to_logfire() -> bool | Literal["if-token-present"]:
    raw = os.environ.get("LOGFIRE_SEND_TO_LOGFIRE", "if-token-present")
    if raw.lower() in ("false", "0", "no", "off"):
        return False
    return "if-token-present"


def configure_logfire(*, service_name: str) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logfire.configure(
        service_name=service_name,
        environment=settings.environment,
        send_to_logfire=_send_to_logfire(),
        console=False,
    )
    logfire.instrument_pydantic_ai(include_content=True)
    _CONFIGURED = True


span = logfire.span


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def configure_observability(*, service_name: str, verbose: bool = False) -> None:
    configure_logfire(service_name=service_name)

    level = logging.DEBUG if verbose else getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
