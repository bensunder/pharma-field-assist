"""Structured JSON logging with correlation IDs.

Every log line carries the request correlation ID so a single conversation turn can be traced
across Container App replicas in Log Analytics:

    ContainerAppConsoleLogs_CL | where Log_s has "<correlation_id>"
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


class JsonFormatter(logging.Formatter):
    _RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "correlation_id": correlation_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # "azure" covers azure.core's HTTP logging policy and the Azure Monitor exporter: at INFO they log
    # every telemetry upload, and that log line is itself exported -> one log per second, forever.
    for noisy in ("uvicorn.access", "httpx", "httpx2", "azure", "azure.monitor.opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
