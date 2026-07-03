#!/usr/bin/env python3
"""Structured JSON logging for GHA runners.

One JSON object per line on stdout; ERROR+ additionally emits GitHub Actions
::error:: annotations so failures surface in the run summary.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, val in record.__dict__.items():        # extra={...} fields
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class GHAAnnotationHandler(logging.Handler):
    """Surface ERROR+ as ::error:: so failures appear in the run summary."""

    def emit(self, record: logging.LogRecord) -> None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            msg = record.getMessage().replace("\n", "%0A")
            print(f"::error title={record.name}::{msg}", file=sys.stderr)


def setup_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter())
    annot = GHAAnnotationHandler()
    annot.setLevel(logging.ERROR)
    root.handlers = [stream, annot]
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.WARNING)
