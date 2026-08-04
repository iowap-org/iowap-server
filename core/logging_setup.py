"""JSON-Logging-Formatter für strukturierte Logs (T-109)."""
from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid

# Pro-Request-Trace-ID, von einer Middleware gesetzt.
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "relay_trace_id", default="")


def set_trace_id(trace_id: str) -> None:
    _trace_id_var.set(trace_id)


def current_trace_id() -> str:
    return _trace_id_var.get()


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


class JsonFormatter(logging.Formatter):
    """Formatiert LogRecords als eine JSON-Zeile pro Eintrag."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": current_trace_id(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)