"""Structured (JSON) logging.

Pipeline code logs with extra={document_id, job_id, stage}. The current
request id (set by the API middleware, or restored in Celery tasks from the
`request_id` task header) is added to every record automatically.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from typing import Any, Final

_RESERVED: Final = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def bind_request_id(request_id: str | None) -> Token[str | None]:
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def current_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if (request_id := current_request_id()) is not None:
            payload["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
