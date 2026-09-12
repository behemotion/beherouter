# Set BEFORE beherouter is imported anywhere: plugins/__init__ reads it at import
# time to decide whether to register the test-only `_test-cli` plugin, which is
# the `cli` backing's one plugin-level caller.
import os

os.environ["BEHEROUTER_TEST_PLUGINS"] = "1"

import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fake_cli_cmd() -> str:
    """A beheaxi-shaped stub CLI: `<cmd> describe --json` emits a valid manifest."""
    return f"{sys.executable} {FIXTURES / 'fake_tool.py'}"
