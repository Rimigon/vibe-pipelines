"""Async client for the VibeMarketolog Agent API.

A thin, correct wrapper over the REST surface used by the pipeline executor.
It implements the operational guarantees the docs require:

* ``idempotency_key`` on every ``/generate`` so a crash + retry never double-charges.
* exponential backoff (1→2→4→8s, max 5 attempts) that **honours ``retry_after``**
  and never retries 4xx validation errors (those would push the key into
  ``key_cooling_down``).
* typed :class:`~vibe.errors.VibeError` for every failure, with ``request_id``.
* both short generations (``generation_id``) and long voiceovers
  (``voiceover_id`` + ``status_url``).
"""

from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import CLIENT_ERRORS, VibeError, from_response

BASE_URL = "https://lk.vibemarketolog.ru/api/agent"
DEFAULT_TIMEOUT = 30.0
POLL_INTERVAL_DEFAULT = 12.0  # docs: every 10–15s for images, longer for video
MAX_ATTEMPTS = 5
BACKOFF = (1.0, 2.0, 4.0, 8.0)  # capped at 5 attempts


@dataclass(slots=True)
class GenerationResult:
    """Normalised result of a completed generation (or long-voiceover)."""

    generation_id: int | None
    status: str  # pending|processing|complete|error
    display_url: str | None = None
    result_urls: list[str] | None = None
    cost: float = 0.0
    refunded: bool = False
    error_message: str | None = None
    model: str | None = None
    type: str | None = None
    raw: dict[str, Any] | None = None


class VibeClient:
    """Stateless async client. One ``httpx.AsyncClient`` per instance."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "vibe-pipelines/0.1",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> VibeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # --- low-level ----------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Single HTTP request with retry/backoff per the docs' retry rule."""
        last_err: VibeError | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await self._client.request(
                    method, path, json=json, params=params
                )
            except httpx.HTTPError as exc:
                last_err = VibeError(code="transport_error", message=str(exc))
                await self._sleep_backoff(attempt, None)
                continue

            if resp.status_code < 400:
                return resp.json()

            body: dict[str, Any] = {}
            try:
                body = resp.json()
            except ValueError:
                body = {"error": "internal_error", "message": resp.text}

            err = from_response(resp.status_code, body)

            # Never retry client-side errors (validation, scope, balance...).
            if err.code in CLIENT_ERRORS:
                raise err

            # Throttles / server errors: respect retry_after, then backoff.
            if err.retryable and attempt < MAX_ATTEMPTS:
                last_err = err
                await self._sleep_backoff(attempt, err.retry_after)
                continue

            raise err

        assert last_err is not None
        raise last_err

    async def _sleep_backoff(self, attempt: int, retry_after: float | None) -> None:
        if retry_after is not None and retry_after > 0:
            await asyncio.sleep(retry_after)
            return
        idx = min(attempt - 1, len(BACKOFF) - 1)
        await asyncio.sleep(BACKOFF[idx])

    # --- read endpoints -----------------------------------------------------
    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def balance(self) -> float:
        data = await self._request("GET", "/balance")
        # /balance returns the current balance; tolerate shape variance.
        try:
            return float(data.get("balance", data.get("balance_rub", 0.0)))
        except (TypeError, ValueError) as exc:
            raise VibeError(
                code="internal_error",
                message=f"Unexpected /balance shape: {data!r}",
            ) from exc

    async def capabilities(self) -> dict[str, Any]:
        return await self._request("GET", "/capabilities")

    async def prices(self) -> dict[str, Any]:
        return await self._request("GET", "/prices")

    async def voices(self, **params: Any) -> dict[str, Any]:
        return await self._request("GET", "/voices", params=params or None)

    # --- generation ---------------------------------------------------------
    async def estimate(self, body: dict[str, Any]) -> dict[str, Any]:
        """Dry-run: validate + price WITHOUT charging. Scope: read."""
        return await self._request("POST", "/generate/estimate", json=body)

    async def generate(
        self,
        body: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Start a generation. ``idempotency_key`` enables safe crash-retries.

        Returns the raw response. For normal generations it contains
        ``generation_id``; for long voiceovers (>5000 chars) it contains
        ``voiceover_id`` + ``status_url`` instead — see :meth:`poll`.
        """
        payload = dict(body)
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return await self._request("POST", "/generate", json=payload)

    async def status(self, generation_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/generation/{generation_id}/status")

    async def long_voiceover_status(self, voiceover_id: int) -> dict[str, Any]:
        return await self._request("GET", f"/voiceover/long/{voiceover_id}")

    async def poll(
        self,
        generation_id: int | None = None,
        *,
        voiceover_id: int | None = None,
        status_url: str | None = None,
        interval: float = POLL_INTERVAL_DEFAULT,
        timeout: float = 1800.0,
    ) -> GenerationResult:
        """Poll until complete/error. Works for both regular and long voiceover.

        Uses the absolute ``status_url`` when given (long voiceover), otherwise
        the relative ``/generation/{id}/status`` path.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if status_url:
                data = await self._request_abs("GET", status_url)
            elif voiceover_id is not None:
                data = await self.long_voiceover_status(voiceover_id)
            elif generation_id is not None:
                data = await self.status(generation_id)
            else:
                raise ValueError("poll needs generation_id or voiceover_id/status_url")

            st = data.get("status")
            if st == "complete":
                return _norm(data)
            if st == "error":
                return _norm(data)
            await asyncio.sleep(interval)
        raise VibeError(code="poll_timeout", message=f"Polling exceeded {timeout}s")

    async def _request_abs(self, method: str, url: str) -> dict[str, Any]:
        resp = await self._client.request(method, url)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {"error": "internal_error", "message": resp.text}
            raise from_response(resp.status_code, body)
        return resp.json()

    # --- media --------------------------------------------------------------
    async def upload_media(self, path: str) -> dict[str, Any]:
        """Upload a local file via multipart. Returns a stable ~7-day URL."""
        try:
            with open(path, "rb") as fh:
                files = {"file": fh}
                resp = await self._client.post(
                    "/upload-media",
                    files=files,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        # Do not set Content-Type; httpx sets the multipart boundary.
                    },
                )
        except OSError as exc:
            raise VibeError(
                code="internal_error",
                message=f"Cannot open {path}: {exc}",
            ) from exc
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {"error": "internal_error", "message": resp.text}
            raise from_response(resp.status_code, body)
        return resp.json()

    # --- webhook self-test --------------------------------------------------
    async def webhook_test(self, callback_url: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/webhook-test", json={"callback_url": callback_url}
        )


def _norm(data: dict[str, Any]) -> GenerationResult:
    try:
        cost = float(data.get("cost") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return GenerationResult(
        generation_id=data.get("generation_id"),
        status=data.get("status", "processing"),
        display_url=data.get("display_url"),
        result_urls=data.get("result_urls"),
        cost=cost,
        refunded=bool(data.get("refunded")),
        error_message=data.get("error_message"),
        model=data.get("model"),
        type=data.get("type"),
        raw=data,
    )
