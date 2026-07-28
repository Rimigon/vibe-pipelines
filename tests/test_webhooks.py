"""Webhook signature verification (modern + legacy scheme)."""

from __future__ import annotations
import hashlib
import hmac

import pytest

from vibe.webhooks import verify_signature, parse_envelope, WebhookEnvelope


def _sign(raw: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_modern_scheme_validates():
    raw = b'{"event":"generation.complete","generation_id":5811}'
    sig = _sign(raw, "wh_secret_123")
    assert verify_signature(raw, "wh_secret_123", sig) is True


def test_wrong_secret_rejected():
    raw = b'{"event":"generation.complete"}'
    sig = _sign(raw, "wh_secret_123")
    assert verify_signature(raw, "different_secret", sig) is False


def test_tampered_body_rejected():
    raw = b'{"event":"generation.complete","generation_id":5811}'
    sig = _sign(raw, "wh_secret_123")
    # flip one byte in the body after signing
    tampered = b'{"event":"generation.complete","generation_id":5812}'
    assert verify_signature(tampered, "wh_secret_123", sig) is False


def test_legacy_scheme_fallback():
    raw = b'{"event":"generation.complete"}'
    legacy_token = "oc_legacy_token"
    legacy_secret = hashlib.sha256(legacy_token.encode()).hexdigest()
    sig = _sign(raw, legacy_secret)
    assert (
        verify_signature(raw, "not_the_modern_secret", sig, legacy_token=legacy_token)
        is True
    )


def test_parse_envelope_extracts_fields():
    raw = b'{"event":"generation.complete","generation_id":42,"display_url":"https://x/y","status":"complete"}'
    env = parse_envelope(raw, {"X-Vibe-Event": "generation.complete"})
    assert isinstance(env, WebhookEnvelope)
    assert env.event == "generation.complete"
    assert env.generation_id == 42
    assert env.display_url == "https://x/y"
    assert env.status == "complete"


def test_parse_envelope_bad_json_raises():
    with pytest.raises(ValueError):
        parse_envelope(b"not json", {})
