#!/usr/bin/env python3
"""Structured JSON logging for GHA runners.

One JSON object per line on stdout; ERROR+ additionally emits GitHub Actions
::error:: annotations so failures surface in the run summary.
"""

import json
import hashlib
import hmac
import logging
import os
import sys
from datetime import datetime, timezone

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message"}
_RUN_SALT = os.urandom(32)  # Anonymous identifiers cannot be dictionary-matched across runs.
_COUNTS = {"hit", "miss", "tw", "us", "fx", "valid_equities", "requested_quote_count",
           "observed_quote_count", "valid_quote_count"}
_NUMBERS = {"coverage", "min", "portfolio_quote_coverage", "requested_universe_coverage", "validated_universe_coverage"}
_ENUMS = {"report_type": {"tw_open", "tw_close", "us_open", "us_close"},
          "primary_market": {"TW", "US"}, "status": {"READY", "FULL", "DEGRADED", "PARTIAL", "BLOCKED"},
          "observed_session": {"REGULAR", "PREMARKET", "AFTER_HOURS", "PREVIOUS_CLOSE", "CLOSED_REFERENCE"},
          "provider": {"yfinance_batch", "yfinance_single", "twse_openapi", "twse_historical_close",
                       "yahoo_chart", "yahoo_chart_extended", "coingecko", "coingecko_crypto", "coinbase", "coinbase_crypto"}}


def _error_type(kind):
    name = getattr(kind, "__name__", "")
    return name if name in {"ValueError", "TypeError", "KeyError", "RuntimeError", "TimeoutError",
                           "ConnectionError", "HTTPError", "ReadTimeout", "ConnectTimeout", "JSONDecodeError"} else "OtherError"


class SafeDiagnosticAdapter(logging.LoggerAdapter):
    """Only static call-site messages and allowlisted metadata enter public logs."""
    def process(self, msg, kwargs):
        fields = {"source": self.logger.name}
        for key, value in (kwargs.get("extra") or {}).items():
            if key in _COUNTS and type(value) is int and value >= 0:
                fields[key] = value
            elif key in _NUMBERS and type(value) in (float, int) and 0 <= value <= 1:
                fields[key] = value
            elif key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
                fields[key] = value
            elif key == "symbol":
                fields["instrument_ref"] = hmac.new(_RUN_SALT, str(value).encode(), hashlib.sha256).hexdigest()[:16]
            elif key in {"portfolio_required", "public_market_core", "optional_context", "missing"} and isinstance(value, (list, tuple)):
                fields[key + "_count"] = len(value)
        kind = sys.exc_info()[0]
        if kind is not None:
            fields["error_type"] = _error_type(kind)
        kwargs.update(extra=fields, exc_info=False, stack_info=False)
        return msg, kwargs


class ExternalDiagnosticFilter(logging.Filter):
    """HTTP/client libraries must not echo URLs, parameters or request payloads."""
    def filter(self, record):
        root = record.name.split('.')[0]
        if root in {"yfinance", "urllib3", "requests", "httpx", "httpcore", "google", "peewee", "curl_cffi"}:
            for key in list(record.__dict__):
                if key not in _RESERVED:
                    del record.__dict__[key]
            record.msg, record.args = "external library diagnostic", ()
            record.name = root
            record.source = root
        if record.exc_info:
            record.error_type = _error_type(record.exc_info[0])
        record.exc_info = record.exc_text = None
        record.stack_info = None
        return True


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
    stream.addFilter(ExternalDiagnosticFilter())
    annot = GHAAnnotationHandler()
    annot.addFilter(ExternalDiagnosticFilter())
    annot.setLevel(logging.ERROR)
    root.handlers = [stream, annot]
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.WARNING)
    def safe_uncaught(kind, _value, _traceback):
        logging.getLogger("runtime").error("unhandled runtime failure", extra={"error_type": _error_type(kind)})
    sys.excepthook = safe_uncaught
