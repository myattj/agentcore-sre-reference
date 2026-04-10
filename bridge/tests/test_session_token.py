"""Tests for the HMAC-signed session tokens minted by the OAuth callback
and verified by `/api/tenants/*` routes.

The TypeScript verifier in `onboarding/lib/session.ts` has to byte-match
this logic — if any of these tests change, the TS side has to change
too.
"""
from __future__ import annotations

import time

import pytest

from bridge.slack_oauth import (
    _SESSION_TTL_SECONDS,
    make_session_token,
    make_state_token,
    verify_session_token,
    verify_state_token,
)


def test_session_token_round_trip() -> None:
    """Mint and verify, recover the embedded tenant_id."""
    token = make_session_token("slack-t123")
    assert verify_session_token(token) == "slack-t123"


def test_session_token_wrong_signature_rejected() -> None:
    """Flip a byte of the HMAC and verification fails."""
    token = make_session_token("slack-t123")
    # The HMAC is hex-encoded and is the final dot-delimited part.
    tenant, nonce, ts, sig = token.split(".")
    tampered_sig = ("0" if sig[-1] != "0" else "1") + sig[:-1]
    tampered = f"{tenant}.{nonce}.{ts}.{tampered_sig}"
    assert verify_session_token(tampered) is None


def test_session_token_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Advance time beyond the TTL — the token is rejected."""
    token = make_session_token("slack-t123")
    real_time = time.time

    def later() -> float:
        return real_time() + _SESSION_TTL_SECONDS + 1

    monkeypatch.setattr("bridge.slack_oauth.time.time", later)
    assert verify_session_token(token) is None


def test_session_token_wrong_tenant_returned() -> None:
    """A token minted for A returns 'A' on verification (it's the caller's
    job to assert equality with the URL tenant)."""
    token_a = make_session_token("slack-aaa")
    assert verify_session_token(token_a) == "slack-aaa"
    assert verify_session_token(token_a) != "slack-bbb"


def test_session_token_malformed_rejected() -> None:
    """Too few parts, non-integer ts, empty string, etc."""
    assert verify_session_token("") is None
    assert verify_session_token("only-one-part") is None
    assert verify_session_token("a.b.c") is None  # 3 parts = state token shape
    assert verify_session_token("a.b.c.d.e") is None  # 5 parts
    assert verify_session_token("a.b.not-a-number.d") is None


def test_session_token_empty_tenant_rejected() -> None:
    """Leading dot → empty tenant field — not a valid token."""
    assert verify_session_token(".nonce.123.deadbeef") is None


def test_make_session_token_rejects_tenant_with_period() -> None:
    """Mint time is the point we fail loud on period-containing tenants."""
    with pytest.raises(ValueError):
        make_session_token("has.period")


def test_state_token_still_works_after_session_addition() -> None:
    """Sanity: the pre-existing state-token flow wasn't regressed by
    adding session tokens in the same module."""
    state = make_state_token()
    assert verify_state_token(state)
    assert not verify_state_token("garbage")
    # Ensure state and session tokens are NOT interchangeable — a state
    # token shouldn't verify as a session token and vice versa.
    assert verify_session_token(state) is None
    session = make_session_token("slack-aaa")
    assert not verify_state_token(session)
