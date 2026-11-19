"""Structured JSONL audit logging with strict secret redaction."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "secret",
    "private_key",
    "secret_key",
    "root_secret",
    "shared_secret",
    "enc_key",
    "confirm_key",
    "api_key",
    "token",
    "password",
    "signature",
)


def redact_secrets(obj: Any) -> Any:
    """Recursively redact sensitive fields before writing to logs."""
    if isinstance(obj, dict):
        sanitized: dict[str, Any] = {}
        for key, value in obj.items():
            key_lower = str(key).lower()
            if any(tok in key_lower for tok in SENSITIVE_KEY_TOKENS):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = redact_secrets(value)
        return sanitized
    if isinstance(obj, list):
        return [redact_secrets(item) for item in obj]
    if isinstance(obj, (bytes, bytearray)):
        return f"<bytes len={len(obj)}>"
    return obj


class SecurityAuditLogger:
    """Thread-safe JSONL logger for integrated three-layer decisions."""

    def __init__(self, log_file: str | Path) -> None:
        self.log_file = Path(log_file)
        self._lock = threading.Lock()

    def log_event(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event_type": event_type,
            "payload": redact_secrets(payload),
        }
        line = json.dumps(record, sort_keys=True, allow_nan=False) + "\n"
        with self._lock:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(line)
