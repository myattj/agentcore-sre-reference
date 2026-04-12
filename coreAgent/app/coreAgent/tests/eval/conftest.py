"""Eval-specific pytest configuration."""
from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-evals",
        action="store_true",
        default=False,
        help="Run investigation eval scenarios (requires Bedrock credentials)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "eval: marks tests as LLM eval tests (requires --run-evals)")


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    if config.getoption("--run-evals"):
        return
    skip_eval = pytest.mark.skip(reason="need --run-evals to run")
    for item in items:
        # Only skip tests explicitly marked with @pytest.mark.eval,
        # not all tests in the eval/ directory.
        if item.get_closest_marker("eval") is not None:
            item.add_marker(skip_eval)
