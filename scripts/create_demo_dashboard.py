#!/usr/bin/env python3
"""Materialize the tracked dashboard example with a fresh local TTL."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

DEFAULT_TOKEN = "51c17c51c17c51c17c51c17c51c17c51"


def create_dashboard(source: Path, output_dir: Path, token: str = DEFAULT_TOKEN) -> Path:
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        raise ValueError("token must be exactly 32 lowercase hexadecimal characters")
    payload = json.loads(source.read_text(encoding="utf-8"))
    now = int(time.time())
    payload["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    payload["ttl"] = now + (7 * 24 * 60 * 60)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = output_dir / f"{token}.json"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    args = parser.parse_args()
    path = create_dashboard(args.source, args.output_dir, args.token)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
