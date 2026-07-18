#!/usr/bin/env python3
"""Create matching local env files without replacing user-owned files."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

SECRET_KEY = "BRIDGE_OAUTH_STATE_SECRET"
TARGETS = (
    (Path("bridge/.env.example"), Path("bridge/.env.local")),
    (Path("onboarding/.env.example"), Path("onboarding/.env.local")),
)


def _read_secret(path: Path) -> str | None:
    value: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == SECRET_KEY:
            value = candidate.strip().strip('"').strip("'")
    return value


def _render_example(example: Path, secret: str) -> str:
    lines = example.read_text(encoding="utf-8").splitlines(keepends=True)
    replaced = False
    rendered: list[str] = []
    for line in lines:
        if line.startswith(f"{SECRET_KEY}="):
            rendered.append(f"{SECRET_KEY}={secret}\n")
            replaced = True
        else:
            rendered.append(line)
    if not replaced:
        raise RuntimeError(f"{example} does not define {SECRET_KEY}")
    return "".join(rendered)


def _create_exclusive(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def bootstrap(root: Path) -> list[str]:
    resolved = [(root / example, root / target) for example, target in TARGETS]
    existing = [(example, target) for example, target in resolved if target.exists()]

    values: dict[Path, str | None] = {}
    for _example, target in existing:
        if target.is_symlink() or not target.is_file():
            raise RuntimeError(f"{target} exists but is not a regular file")
        values[target] = _read_secret(target)

    nonempty = {value for value in values.values() if value}
    if len(nonempty) > 1:
        paths = ", ".join(str(path.relative_to(root)) for path in values)
        raise RuntimeError(
            f"existing {SECRET_KEY} values disagree in {paths}; "
            "files were preserved. Make the values match, then rerun setup"
        )
    if values and any(not value for value in values.values()):
        paths = ", ".join(
            str(path.relative_to(root)) for path, value in values.items() if not value
        )
        raise RuntimeError(
            f"{paths} already exists without {SECRET_KEY}; files were preserved. "
            "Add one shared value of at least 32 characters, then rerun setup"
        )

    shared_secret = next(iter(nonempty), secrets.token_hex(32))
    if len(shared_secret) < 32:
        raise RuntimeError(
            f"existing {SECRET_KEY} is shorter than 32 characters; files were preserved. "
            "Replace it with one shared value of at least 32 characters"
        )

    rendered_examples: dict[Path, str] = {}
    for example, target in resolved:
        if example.is_symlink() or not example.is_file():
            raise RuntimeError(f"tracked template is missing or unsafe: {example}")
        rendered_examples[target] = _render_example(example, shared_secret)

    messages: list[str] = []
    for _example, target in resolved:
        if target.exists():
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
            messages.append(f"preserved {target.relative_to(root)} (mode 0600)")
            continue
        _create_exclusive(target, rendered_examples[target])
        messages.append(f"created {target.relative_to(root)} from its example (mode 0600)")

    final_values = {_read_secret(target) for _example, target in resolved}
    if final_values != {shared_secret}:
        raise RuntimeError("local env files do not share the same session secret")
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create bridge and onboarding .env.local files without overwriting them."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: inferred from this script)",
    )
    args = parser.parse_args(argv)
    try:
        messages = bootstrap(args.root.resolve())
    except (OSError, RuntimeError) as exc:
        print(f"local env setup failed: {exc}", file=sys.stderr)
        return 1
    for message in messages:
        print(message)
    print("local session secret is shared; its value was not printed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
