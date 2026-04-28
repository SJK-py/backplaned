"""bp_router.observability.logging — Structured JSON logging.

See `docs/observability.md` §3.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bp_router.settings import Settings


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line to stdout.

    Required fields per `observability.md` §3.2: ts, level, logger,
    event, plus any keyword extras the caller passed via `logger.info(
    event=..., trace_id=..., ...)`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }
        # Include free-form message only when no `event` is set;
        # `event` is the canonical signal.
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        if record.msg:
            payload["message"] = record.getMessage()

        # Attach trace context if present (OTel instrumentation injects these
        # via a LogRecordFactory; here we just include whatever is on the record).
        for key in ("trace_id", "span_id"):
            v = getattr(record, key, None)
            if v is not None:
                payload[key] = v

        # Attach any extra fields the caller bound (filtered to bp.* + a few standard).
        for key, value in record.__dict__.items():
            if key.startswith(("_", "args", "msg", "exc_", "stack_")):
                continue
            if key in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "event",
                "trace_id", "span_id", "message", "taskName",
            }:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(settings: "Settings") -> None:
    """Replace any pre-existing handlers with a JSON-formatted stdout handler."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(settings.log_level)
