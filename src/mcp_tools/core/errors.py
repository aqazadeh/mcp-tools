"""Domain errors.

`ApiError` is the only exception core code raises on purpose. The MCP facade turns it
into an `is_error` tool result with a compact JSON body the calling model can act on.
Messages are built from fixed templates: never put secrets or raw provider text in
`message` or `hint` (provider text goes into `details["provider_message"]`, truncated).
"""

from typing import Any

# Stable error codes (part of the tool contract).
INVALID_PARAMS = "invalid_params"
UNKNOWN_ARGUMENT = "unknown_argument"
UNKNOWN_MODEL = "unknown_model"
NOT_FOUND = "not_found"
CONFLICT = "conflict"
CONFIRMATION_REQUIRED = "confirmation_required"
INSUFFICIENT_CREDITS = "insufficient_credits"
BUDGET_EXCEEDED = "budget_exceeded"
IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
PROVIDER_UNAVAILABLE = "provider_unavailable"
QUOTA_EXCEEDED = "quota_exceeded"
UNSUPPORTED = "unsupported"
RESULT_TOO_LARGE = "result_too_large"
BUSY = "busy"
INTERNAL_ERROR = "internal_error"

_PROVIDER_TEXT_LIMIT = 500


class ApiError(Exception):
    """An anticipated failure with a machine-readable code and a recovery hint."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        recovery_tool: str | None = None,
        retryable: bool = False,
        allowed: list[Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hint = hint
        self.recovery_tool = recovery_tool
        self.retryable = retryable
        self.allowed = allowed
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.hint:
            body["hint"] = self.hint
        if self.recovery_tool:
            body["recovery_tool"] = self.recovery_tool
        if self.allowed is not None:
            body["allowed"] = self.allowed
        if self.details:
            body["details"] = self.details
        return body


def provider_text(text: str | None) -> str | None:
    """Truncate untrusted provider text before it is attached to an error."""
    if not text:
        return None
    cleaned = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in text)
    return cleaned[:_PROVIDER_TEXT_LIMIT]
