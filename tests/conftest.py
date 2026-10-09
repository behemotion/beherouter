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


_CATALOGUES = Path(__file__).parent / "search_eval" / "catalogues"


@pytest.fixture
def catalogue_descriptors():
    """A recorded `tools/list` snapshot as the descriptors an MCP backend builds.

    Mirrors `backends/mcp.py`'s descriptor construction (name == verb, the
    description as summary, the inputSchema as schema) so a test sees what the
    gateway would index — without a subprocess or network.
    """
    import json

    from beherouter.backends.mcp import _mutating
    from beherouter.models import ToolDescriptor

    def load(name: str, pinned: tuple[str, ...] = ()) -> list[ToolDescriptor]:
        rows = json.loads((_CATALOGUES / f"{name}.json").read_text())
        return [
            ToolDescriptor(
                name=r["name"],
                verb=r["name"],
                summary=r["description"],
                schema=r["inputSchema"],
                pinned=r["name"] in pinned,
                mutating=_mutating(r["annotations"]),
                annotations=r["annotations"],
            )
            for r in rows
        ]

    return load


class _Exec:
    """A backend executor that answers every verb and counts its closes."""

    def __init__(self):
        self.closed = 0

    async def run(self, verb, args, *, identity=None):
        return {"result": "ok"}

    async def aclose(self):
        self.closed += 1


@pytest.fixture
def gateway_plugin():
    """Register throwaway `inproc` plugins by name; unregistered at teardown."""
    from beherouter.plugins import PLUGINS, register
    from beherouter.plugins.spec import PluginSpec

    names = []

    def _register(name, build):
        register(PluginSpec(name=name, summary="t", backing="inproc"), build)
        names.append(name)

    yield _register
    for n in names:
        PLUGINS.pop(n, None)


@pytest.fixture
def exec_builder():
    """`exec_builder(execs)` is a plugin build that appends each executor it
    makes to `execs`, so a test can see which backends were closed."""
    from beherouter.models import Backend

    def builder(execs):
        async def build(ctx):
            ex = _Exec()
            execs.append(ex)
            return Backend(name=ctx.surface, kind="mcp", descriptors=[], executor=ex)

        return build

    return builder
