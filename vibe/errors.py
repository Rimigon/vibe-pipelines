"""Typed errors mapping the VibeMarketolog Agent API error schema.

The API returns a single unified error body::

    {"status":"error","error":"<code>","message":"...","details":{...},"request_id":"..."}

Every error has a stable machine code (``error``). We mirror the documented
codes so callers can branch on typed exceptions instead of string matching.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


# --- Scope / auth -----------------------------------------------------------
MISSING_TOKEN = "missing_token"
INVALID_TOKEN = "invalid_token"
INSUFFICIENT_SCOPE = "insufficient_scope"
IP_NOT_ALLOWED = "ip_not_allowed"

# --- Validation -------------------------------------------------------------
NOT_FOUND = "not_found"
METHOD_NOT_ALLOWED = "method_not_allowed"
VALIDATION_FAILED = "validation_failed"
MEDIA_VALIDATION_FAILED = "media_validation_failed"
INVALID_URL = "invalid_url"
UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
FILE_TOO_LARGE = "file_too_large"
MODEL_NOT_SUPPORTED = "model_not_supported"
UNKNOWN_OR_INCOMPATIBLE_PARAMS = "unknown_or_incompatible_params"
IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"
DUPLICATE_REQUEST = "duplicate_request"

# --- Money / limits ---------------------------------------------------------
INSUFFICIENT_BALANCE = "insufficient_balance"
DAILY_SPEND_LIMIT_EXCEEDED = "daily_spend_limit_exceeded"
RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
ALREADY_RUNNING = "already_running"
KEY_COOLING_DOWN = "key_cooling_down"

# --- Server -----------------------------------------------------------------
SESSION_EXPIRED = "session_expired"
GENERATION_FAILED = "generation_failed"
TOOL_FAILED = "tool_failed"
AI_UNAVAILABLE = "ai_unavailable"
WORDSTAT_UPSTREAM_UNAVAILABLE = "wordstat_upstream_unavailable"
INTERNAL_ERROR = "internal_error"


# Codes that mean "the request itself is wrong" — never worth retrying.
# Retrying a 422 validation error, for instance, just risks key_cooling_down.
CLIENT_ERRORS: frozenset[str] = frozenset(
    {
        MISSING_TOKEN,
        INVALID_TOKEN,
        INSUFFICIENT_SCOPE,
        IP_NOT_ALLOWED,
        NOT_FOUND,
        METHOD_NOT_ALLOWED,
        VALIDATION_FAILED,
        MEDIA_VALIDATION_FAILED,
        INVALID_URL,
        UNSUPPORTED_MEDIA_TYPE,
        FILE_TOO_LARGE,
        MODEL_NOT_SUPPORTED,
        UNKNOWN_OR_INCOMPATIBLE_PARAMS,
        IDEMPOTENCY_KEY_CONFLICT,
        DUPLICATE_REQUEST,
        SESSION_EXPIRED,
        INSUFFICIENT_BALANCE,
        DAILY_SPEND_LIMIT_EXCEEDED,
    }
)

# Codes that mean "the server / upstream is temporarily unavailable" —
# retryable with backoff, honouring ``retry_after``.
RETRYABLE_SERVER_ERRORS: frozenset[str] = frozenset(
    {
        GENERATION_FAILED,
        TOOL_FAILED,
        AI_UNAVAILABLE,
        INTERNAL_ERROR,
    }
)

# Transient throttles — retryable, MUST respect retry_after.
THROTTLE_ERRORS: frozenset[str] = frozenset(
    {
        RATE_LIMIT_EXCEEDED,
        ALREADY_RUNNING,
        KEY_COOLING_DOWN,
        WORDSTAT_UPSTREAM_UNAVAILABLE,
    }
)


@dataclass
class VibeError(Exception):
    """Base typed API error. Carries the machine ``code`` and ``request_id``."""

    code: str
    message: str
    request_id: str | None = None
    status_code: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    retry_after: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Compose a human-readable message for traceback/logs.
        msg = f"[{self.code}] {self.message}"
        if self.request_id:
            msg += f" (request_id={self.request_id})"
        super().__init__(msg)

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_SERVER_ERRORS or self.code in THROTTLE_ERRORS

    @property
    def is_throttle(self) -> bool:
        return self.code in THROTTLE_ERRORS

    @property
    def refunded(self) -> bool:
        return bool(self.raw.get("refunded"))


class BudgetExceeded(VibeError):
    """Raised before run when the estimated scenario cost exceeds the budget."""

    def __init__(self, estimated: float, budget: float, per_step: dict[str, float]):
        super().__init__(
            code="budget_exceeded",
            message=(
                f"Estimated scenario cost {estimated:.2f} RUB exceeds budget "
                f"{budget:.2f} RUB."
            ),
        )
        self.estimated = estimated
        self.budget = budget
        self.per_step = per_step


class FieldMappingError(VibeError):
    """Raised when a logical input cannot be mapped to the chosen model."""

    def __init__(
        self, message: str, *, model: str | None = None, logical: str | None = None
    ):
        super().__init__(code="field_mapping_failed", message=message)
        self.model = model
        self.logical = logical


def from_response(status_code: int, body: dict[str, Any]) -> VibeError:
    """Build the right ``VibeError`` from a JSON error body."""
    code = body.get("error") or body.get("code") or "internal_error"
    message = body.get("message") or body.get("error") or "Unknown error"
    request_id = body.get("request_id")
    details = body.get("details") or body.get("errors") or {}
    retry_after = body.get("retry_after")
    if retry_after is not None:
        try:
            retry_after = float(retry_after)
        except (TypeError, ValueError):
            retry_after = None
    return VibeError(
        code=code,
        message=message,
        request_id=request_id,
        status_code=status_code,
        details=details,
        retry_after=retry_after,
        raw=body,
    )
