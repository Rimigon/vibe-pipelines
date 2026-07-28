"""Client contract tests against mocked responses (no network, no rubles)."""

from __future__ import annotations

import httpx
import pytest

from vibe.client import VibeClient
from vibe.errors import VibeError
from tests.conftest import make_client, body


def _resp(json_data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=json_data)


async def _client(routes=None):
    return VibeClient("test-token", transport=make_client(routes=routes))


async def test_me_and_balance():
    c = await _client()
    assert (await c.me())["balance"] == 500.0
    assert await c.balance() == 500.0
    await c.aclose()


async def test_generate_sends_idempotency_key():
    captured: dict = {}

    def gen(request: httpx.Request) -> httpx.Response:
        captured.update(body(request))
        return _resp(
            {
                "status": "processing",
                "generation_id": 100,
                "task_id": "t1",
                "cost": 19,
                "balance_after": 481.0,
            }
        )

    c = await _client(routes={("POST", "/api/agent/generate"): gen})
    resp = await c.generate(
        {"type": "image", "model": "seedream-5-pro", "prompt": "p"},
        idempotency_key="run1:poster",
    )
    assert resp["generation_id"] == 100
    assert captured["idempotency_key"] == "run1:poster"
    await c.aclose()


async def test_poll_completes():
    calls = {"n": 0}

    def status(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return _resp({"status": "processing", "generation_id": 100})
        return _resp(
            {
                "status": "complete",
                "generation_id": 100,
                "display_url": "https://lk.vibemarketolog.ru/files/generation/100?sig=abc",
                "cost": 19.0,
                "refunded": False,
            }
        )

    c = await _client(routes={("GET", "/api/agent/generation/100/status"): status})
    res = await c.poll(100, interval=0.01, timeout=5)
    assert res.status == "complete"
    assert res.display_url is not None
    assert res.display_url.startswith("https://lk.vibemarketolog.ru/files/")
    await c.aclose()


async def test_429_respects_retry_after_then_succeeds(monkeypatch):
    import vibe.client as client_mod

    async def _instant(*_a, **_k):
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", _instant)

    state = {"n": 0}

    def gen(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return _resp(
                {
                    "status": "error",
                    "error": "rate_limit_exceeded",
                    "message": "slow down",
                    "retry_after": 0,
                },
                status=429,
            )
        return _resp({"status": "processing", "generation_id": 200, "cost": 10})

    c = await _client(routes={("POST", "/api/agent/generate"): gen})
    resp = await c.generate(
        {"type": "image", "model": "x", "prompt": "p"}, idempotency_key="k"
    )
    assert state["n"] == 2  # retried after 429
    assert resp["generation_id"] == 200
    await c.aclose()


async def test_402_insufficient_balance_not_retried():
    state = {"n": 0}

    def gen(_request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return _resp(
            {
                "status": "error",
                "error": "insufficient_balance",
                "message": "no money",
                "required": 100,
                "balance": 5,
            },
            status=402,
        )

    c = await _client(routes={("POST", "/api/agent/generate"): gen})
    with pytest.raises(VibeError) as exc:
        await c.generate({"type": "image", "model": "x", "prompt": "p"})
    assert exc.value.code == "insufficient_balance"
    assert state["n"] == 1  # NOT retried
    await c.aclose()


async def test_long_voiceover_poll_uses_status_url():
    calls = {"n": 0}

    def long_status(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return _resp(
                {
                    "status": "processing",
                    "stage": "processing",
                    "chunks_done": 1,
                    "chunks_total": 3,
                }
            )
        return _resp(
            {
                "status": "complete",
                "generation_id": 9001,
                "display_url": "https://lk.vibemarketolog.ru/files/9001",
                "duration": 120,
            }
        )

    c = await _client(routes={("GET", "/api/agent/voiceover/long/12"): long_status})
    res = await c.poll(voiceover_id=12, interval=0.01, timeout=5)
    assert res.status == "complete"
    assert res.generation_id == 9001
    await c.aclose()
