"""Shared AWS region resolution for the standalone infrastructure CLIs."""

from __future__ import annotations

import os
from collections.abc import Mapping


FALLBACK_REGION = "us-west-2"


def _configured_profile_region() -> str | None:
    """Read the active profile region without resolving credentials or networking."""
    from botocore.session import Session

    value = Session().get_config_variable("region")
    return value if isinstance(value, str) and value else None


def resolve_default_region(environ: Mapping[str, str] | None = None) -> str:
    """Return the standard CLI region using AWS SDK configuration precedence."""
    selected = os.environ if environ is None else environ
    environment_region = selected.get("AWS_REGION") or selected.get(
        "AWS_DEFAULT_REGION"
    )
    if environment_region:
        return environment_region
    return _configured_profile_region() or FALLBACK_REGION
