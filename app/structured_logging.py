from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

_CONTEXT_KEYS = (
    "event",
    "trade_id",
    "signal_id",
    "request_id",
    "slack_event_id",
    "stage",
    "reason",
    "status",
    "market",
    "asset_id",
    "budget_usdc",
    "price",
    "spread",
)
_VALID_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _CONTEXT_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configured_log_level() -> int:
    raw = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return _VALID_LEVELS.get(raw, logging.INFO)


def configure_logging() -> None:
    root = logging.getLogger()
    level = configured_log_level()
    handler = next(
        (h for h in root.handlers if getattr(h, "_bot_json_handler", False)),
        None,
    )
    if handler is None:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler._bot_json_handler = True
        root.handlers.clear()
        root.addHandler(handler)
    root.setLevel(level)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool | BaseException | tuple | None = None,
    **fields: Any,
) -> None:
    logger.log(level, event, extra={"event": event, **fields}, exc_info=exc_info)
