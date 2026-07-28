"""Shared fixtures: a /capabilities payload and an httpx MockTransport.

Mock responses mirror the JSON shapes documented in the API reference so the
tests exercise the real request/response contract without spending rubles.
"""

from __future__ import annotations
import json
from typing import Any

import httpx
import pytest

CAPABILITIES: dict[str, Any] = {
    "models": [
        {
            "model": "seedream-5-pro",
            "type": "image",
            "price": 19,
            "required": ["prompt"],
            "optional": ["aspect_ratio", "quality", "output_format", "image_input"],
        },
        {
            "model": "nano-banana-2-lite",
            "type": "image",
            "price": 6,
            "required": ["prompt"],
            "optional": ["aspect_ratio", "image_input"],
        },
        {
            "model": "seedance-2-fast",
            "type": "video",
            "price": 120,
            "required": ["prompt"],
            "optional": [
                "aspect_ratio",
                "duration",
                "resolution",
                "first_frame_url",
                "last_frame_url",
                "reference_image_urls",
                "reference_video_urls",
                "reference_audio_urls",
                "generate_audio",
            ],
        },
        {
            "model": "kling-3.0-pro",
            "type": "video",
            "price": 180,
            "required": ["prompt"],
            "optional": ["aspect_ratio", "duration", "image_urls", "sound"],
        },
        {
            "model": "veo3_fast",
            "type": "video",
            "price": 149,
            "required": ["prompt"],
            "optional": [
                "aspect_ratio",
                "duration",
                "resolution",
                "image_urls",
                "generation_type",
                "generate_audio",
            ],
        },
        {
            "model": "omnihuman-1-5",
            "type": "video",
            "price": 320,
            "required": ["prompt", "image_url", "audio_url"],
            "optional": ["resolution", "mask_url", "pe_fast_mode"],
        },
        {
            "model": "el-tts-multilingual-v2",
            "type": "voice",
            "price": 39,
            "required": ["prompt"],
            "optional": ["voice_id", "language_code", "stability", "similarity_boost"],
        },
        {
            "model": "suno-v5.5",
            "type": "music",
            "price": 99,
            "required": ["prompt"],
            "optional": [
                "lyrics",
                "music_style",
                "style_tags",
                "vocal_gender",
                "persona_id",
            ],
        },
    ]
}


def make_client(
    token: str = "test-token", routes: dict | None = None
) -> httpx.MockTransport:
    """Build a MockTransport that answers the given (method, path) → handler map."""
    handlers = routes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key in handlers:
            return handlers[key](request)
        # defaults
        if request.url.path == "/api/agent/me":
            return httpx.Response(200, json={"balance": 500.0})
        if request.url.path == "/api/agent/capabilities":
            return httpx.Response(200, json=CAPABILITIES)
        if request.url.path == "/api/agent/balance":
            return httpx.Response(200, json={"balance": 500.0})
        return httpx.Response(
            404,
            json={
                "status": "error",
                "error": "not_found",
                "message": f"no mock for {key}",
            },
        )

    return httpx.MockTransport(handler)


def body(request: httpx.Request) -> dict[str, Any]:
    try:
        return json.loads(request.content.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


@pytest.fixture
def caps() -> dict[str, Any]:
    return CAPABILITIES
