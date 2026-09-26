from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("event", "trade_id", "request_id", "slack_event_id", "stage", "reason", "status"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _configured_level() -> int:
    raw = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return logging._nameToLevel.get(raw, logging.INFO)


def configure_logging() -> None:
    root = logging.getLogger()
    level = _configured_level()
    if getattr(root, "_bot_json_configured", False):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    root._bot_json_configured = True


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int | str = logging.INFO,
    **fields: Any,
) -> None:
    resolved = logging._nameToLevel.get(level.upper(), logging.INFO) if isinstance(level, str) else int(level)
    logger.log(resolved, event, extra={"event": event, **fields})
