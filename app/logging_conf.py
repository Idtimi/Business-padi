"""Structured JSON logging with phone number masking."""

import json
import logging
import re
from datetime import datetime, timezone

from app.config import settings

PHONE_REGEX = re.compile(r"(\+?234\d{6,10}|\b0[789]\d{8,9}\b|\b234\d{10}\b)")


def mask_identifiers(text: str) -> str:
    """Mask phone numbers in log messages: 2348012345678 -> *********5678"""
    def _repl(match):
        raw = match.group(0)
        if len(raw) <= 4:
            return "****"
        return "*" * (len(raw) - 4) + raw[-4:]

    return PHONE_REGEX.sub(_repl, str(text))


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        raw_msg = record.getMessage()
        masked_msg = mask_identifiers(raw_msg)

        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": masked_msg,
        }

        if record.exc_info:
            data["exception"] = mask_identifiers(self.formatException(record.exc_info))

        return json.dumps(data)


MaskingJsonFormatter = JSONFormatter


def setup_logging():
    """Initializes logging configuration according to settings."""
    handler = logging.StreamHandler()
    if settings.log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    # Avoid duplicate handlers
    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
        root_logger.addHandler(handler)
