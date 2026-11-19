"""Keep secrets out of logs: bearer tokens, URL query strings and registered secret values."""

import logging
import re

_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_QUERY = re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']+")
_KEYISH = re.compile(r"(?i)\b(api[_-]?key|token|secret|signature|sig)=([^\s&\"']{6,})")

_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember a secret string so it is masked wherever it appears in a log record."""
    if value and len(value) >= 8:
        _secrets.add(value)


def redact(text: str) -> str:
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, "***")
    text = _BEARER.sub(r"\1***", text)
    text = _QUERY.sub(r"\1?***", text)
    return _KEYISH.sub(r"\1=***", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must not break logging
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        return True
