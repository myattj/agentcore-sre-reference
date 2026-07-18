"""Tests for `bridge.adapters.slack.SlackAdapter.verify_signature`.

The HMAC scheme is documented at:
  https://api.slack.com/authentication/verifying-requests-from-slack

These tests construct synthetic Request-shaped objects (we don't need
the full FastAPI machinery — `verify_signature` only touches `.headers`
and `.body()`).
"""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from bridge.adapters.slack import SlackAdapter, SlackSignatureError


_SECRET = "shhh-test-signing-secret"


class FakeRequest:
    """Minimal request shim with the two attributes verify_signature reads."""

    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        # FastAPI normalizes header keys to lowercase; mimic that.
        self.headers = {k.lower(): v for k, v in headers.items()}

    async def body(self) -> bytes:
        return self._body


def _sign(body: bytes, ts: int, secret: str = _SECRET) -> str:
    basestring = b"v0:" + str(ts).encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_valid_signature_passes():
    body = b'{"event":{"type":"message"}}'
    ts = int(time.time())
    request = FakeRequest(
        body=body,
        headers={
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": _sign(body, ts),
        },
    )
    adapter = SlackAdapter(signing_secret=_SECRET)
    await adapter.verify_signature(request)  # should not raise


@pytest.mark.asyncio
async def test_tampered_signature_raises():
    body = b'{"event":{"type":"message"}}'
    ts = int(time.time())
    request = FakeRequest(
        body=body,
        headers={
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": "v0=deadbeef" * 8,
        },
    )
    adapter = SlackAdapter(signing_secret=_SECRET)
    with pytest.raises(SlackSignatureError):
        await adapter.verify_signature(request)


@pytest.mark.asyncio
async def test_tampered_body_raises():
    body = b'{"event":{"type":"message"}}'
    ts = int(time.time())
    sig = _sign(body, ts)
    # Different body, original signature → mismatch.
    tampered = FakeRequest(
        body=b'{"event":{"type":"message","extra":"injected"}}',
        headers={
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": sig,
        },
    )
    adapter = SlackAdapter(signing_secret=_SECRET)
    with pytest.raises(SlackSignatureError):
        await adapter.verify_signature(tampered)


@pytest.mark.asyncio
async def test_stale_timestamp_raises():
    body = b'{"event":{"type":"message"}}'
    stale_ts = int(time.time()) - 60 * 10  # 10 minutes old
    request = FakeRequest(
        body=body,
        headers={
            "X-Slack-Request-Timestamp": str(stale_ts),
            "X-Slack-Signature": _sign(body, stale_ts),
        },
    )
    adapter = SlackAdapter(signing_secret=_SECRET)
    with pytest.raises(SlackSignatureError):
        await adapter.verify_signature(request)


@pytest.mark.asyncio
async def test_missing_headers_raises():
    request = FakeRequest(body=b"{}", headers={})
    adapter = SlackAdapter(signing_secret=_SECRET)
    with pytest.raises(SlackSignatureError):
        await adapter.verify_signature(request)


def test_no_signing_secret_fails_closed_by_default():
    with pytest.raises(RuntimeError, match="SLACK_SIGNING_SECRET"):
        SlackAdapter(signing_secret=None)


@pytest.mark.asyncio
async def test_explicit_local_dev_can_skip_signature_verification():
    """The unsigned path exists only behind an explicit LOCAL_DEV opt-in."""
    request = FakeRequest(body=b"{}", headers={})
    adapter = SlackAdapter(signing_secret=None, allow_unsigned_requests=True)
    await adapter.verify_signature(request)  # no raise
