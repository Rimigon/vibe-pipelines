"""Webhook signature verification + a minimal async receiver.

The platform signs every webhook with ``HMAC-SHA256(raw_body, webhook_secret)``
in the ``X-Vibe-Signature`` header. Legacy keys (pre 2026-07-09) instead use
``secret = sha256(raw_api_token)``. Verifying on **raw bytes before JSON parse**
is mandatory — re-serialising JSON changes whitespace and breaks the signature.

The receiver lets a pipeline run without polling: the platform POSTs
``generation.complete`` and we resolve the waiting step. It is optional and only
used when the caller provides a public ``callback_url``; otherwise the executor
falls back to polling.
"""

from __future__ import annotations
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiohttp import web

try:
    import aiohttp  # noqa: F401 — presence check only

    _HAS_AIOHTTP = True
except ImportError:  # pragma: no cover
    _HAS_AIOHTTP = False


def verify_signature(
    raw_body: bytes,
    webhook_secret: str,
    header_signature: str,
    *,
    legacy_token: str | None = None,
) -> bool:
    """Return True iff the header signature matches the body under the secret.

    Tries the modern scheme (``webhook_secret``) first, then the legacy scheme
    (``sha256(raw_token)``) if a ``legacy_token`` is supplied.
    """
    expected = hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, header_signature):
        return True
    if legacy_token is not None:
        legacy_secret = hashlib.sha256(legacy_token.encode()).hexdigest()
        expected_legacy = hmac.new(
            legacy_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected_legacy, header_signature)
    return False


# Event types the platform emits.
EVENT_GENERATION_COMPLETE = "generation.complete"
EVENT_GENERATION_ERROR = "generation.error"
EVENT_WEBHOOK_TEST = "webhook.test"
EVENT_AGENT_MESSAGE = "agent.message"


@dataclass(slots=True)
class WebhookEnvelope:
    event: str
    generation_id: int | None
    body: dict[str, Any]
    raw: bytes

    @property
    def status(self) -> str:
        return str(self.body.get("status", ""))

    @property
    def display_url(self) -> str | None:
        url = self.body.get("display_url") or self.body.get("result_url")
        if isinstance(url, str):
            return url
        urls = self.body.get("result_urls")
        return urls[0] if isinstance(urls, list) and urls else None


def parse_envelope(raw_body: bytes, headers: dict[str, str]) -> WebhookEnvelope:
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid webhook body: {exc}") from exc
    event = headers.get("X-Vibe-Event") or body.get("event") or ""
    gid = body.get("generation_id")
    return WebhookEnvelope(event=event, generation_id=gid, body=body, raw=raw_body)


Handler = Callable[[WebhookEnvelope], Awaitable[None]]


def build_app(
    webhook_secret: str,
    handler: Handler,
    *,
    legacy_token: str | None = None,
) -> web.Application:
    """Build an aiohttp app that verifies signatures and dispatches to handler.

    Requires ``aiohttp`` (``pip install aiohttp``). Returns 200 on success,
    401 on bad signature — never 5xx, so the platform counts delivery as ok and
    doesn't retry on our internal errors (we log them instead).
    """
    if not _HAS_AIOHTTP:  # pragma: no cover
        raise RuntimeError("aiohttp is required for build_app: pip install aiohttp")
    from aiohttp import web

    async def _handle(request: web.Request) -> web.Response:
        raw = await request.read()
        sig = request.headers.get("X-Vibe-Signature", "")
        if not verify_signature(raw, webhook_secret, sig, legacy_token=legacy_token):
            return web.Response(status=401)
        try:
            env = parse_envelope(raw, dict(request.headers))
            await handler(env)
        except Exception:  # noqa: BLE001 — log, don't fail the webhook
            return web.Response(status=200)
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/", _handle)
    app.router.add_post("/webhook", _handle)
    return app
