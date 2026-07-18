"""Strict public-origin validation for the Slack OAuth cookie handoff.

The bridge sets a host-scoped onboarding session cookie and then redirects to
Next.js. That is safe only when both services are intentionally published at
the same origin. Treat these URLs as security configuration, not display
strings: reject ambiguous URL forms and fail closed before sending a user to
Slack.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


@dataclass(frozen=True)
class OAuthPublicConfig:
    """Canonical, same-origin URLs used by the Slack install flow."""

    onboarding_origin: str
    bridge_origin: str
    slack_redirect_uri: str


@dataclass(frozen=True)
class _ParsedPublicUrl:
    origin: str
    origin_key: tuple[str, str, int]
    path: str


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _canonical_host(host: str, *, name: str) -> str:
    if host.endswith("."):
        raise RuntimeError(f"{name} must not use a trailing-dot hostname")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            canonical = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise RuntimeError(f"{name} has an invalid hostname") from exc
        labels = canonical.split(".")
        if (
            not canonical
            or len(canonical) > 253
            or any(not _DNS_LABEL.fullmatch(label) for label in labels)
        ):
            raise RuntimeError(f"{name} has an invalid hostname")
        return canonical
    return ip.compressed


def _parse_public_url(
    name: str,
    raw: str | None,
    *,
    local_dev: bool,
    required_path: str | None,
) -> _ParsedPublicUrl:
    if not raw:
        raise RuntimeError(f"{name} is required")
    if raw != raw.strip() or "\\" in raw or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in raw
    ):
        raise RuntimeError(f"{name} contains unsafe URL characters")

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"{name} is not a valid absolute URL") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise RuntimeError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError(f"{name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} must not contain a query string or fragment")

    if required_path is None:
        if parsed.path not in {"", "/"}:
            raise RuntimeError(f"{name} must be an origin without a path")
        path = ""
    else:
        if parsed.path != required_path:
            raise RuntimeError(f"{name} must end with {required_path}")
        path = required_path

    host = _canonical_host(parsed.hostname.lower(), name=name)
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    if effective_port <= 0:
        raise RuntimeError(f"{name} has an invalid port")

    if scheme == "http" and not (local_dev and _is_loopback_host(host)):
        raise RuntimeError(
            f"{name} must use HTTPS; loopback HTTP is allowed only with LOCAL_DEV=1"
        )

    display_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    port_suffix = "" if effective_port == default_port else f":{effective_port}"
    origin = f"{scheme}://{display_host}{port_suffix}"
    return _ParsedPublicUrl(
        origin=origin,
        origin_key=(scheme, host, effective_port),
        path=path,
    )


def load_oauth_public_config() -> OAuthPublicConfig:
    """Load and validate the three URLs that participate in OAuth.

    Production requires one identical HTTPS origin. ``LOCAL_DEV=1`` permits
    HTTP and different ports only on one exact loopback hostname/address;
    cookies are host-scoped rather than port-scoped.
    """

    local_dev = os.getenv("LOCAL_DEV") == "1"
    onboarding = _parse_public_url(
        "ONBOARDING_BASE_URL",
        os.getenv("ONBOARDING_BASE_URL"),
        local_dev=local_dev,
        required_path=None,
    )
    bridge = _parse_public_url(
        "BRIDGE_PUBLIC_URL",
        os.getenv("BRIDGE_PUBLIC_URL"),
        local_dev=local_dev,
        required_path=None,
    )
    callback = _parse_public_url(
        "SLACK_REDIRECT_URI",
        os.getenv("SLACK_REDIRECT_URI"),
        local_dev=local_dev,
        required_path="/slack/oauth/callback",
    )

    # Cookies are host-scoped, not port-scoped. The documented local setup
    # serves Next.js on :3000 and the bridge on :8000, so permit that one
    # loopback-only development shape. Production remains strict, and the
    # bridge origin must always match its callback exactly.
    loopback_dev = (
        local_dev
        and onboarding.origin_key[0] == bridge.origin_key[0] == callback.origin_key[0]
        and onboarding.origin_key[0] == "http"
        and onboarding.origin_key[1] == bridge.origin_key[1] == callback.origin_key[1]
        and _is_loopback_host(onboarding.origin_key[1])
        and bridge.origin_key == callback.origin_key
    )
    if not loopback_dev and not (
        onboarding.origin_key == bridge.origin_key == callback.origin_key
    ):
        raise RuntimeError(
            "ONBOARDING_BASE_URL, BRIDGE_PUBLIC_URL, and SLACK_REDIRECT_URI "
            "must use the same scheme, host, and effective port "
            "(LOCAL_DEV permits different ports on one loopback hostname)"
        )

    return OAuthPublicConfig(
        onboarding_origin=onboarding.origin,
        bridge_origin=bridge.origin,
        slack_redirect_uri=f"{callback.origin}{callback.path}",
    )
