"""Plane Community Edition: refuse the `workitem` shapes it cannot serve.

Measured on Plane CE v1.4.1 by a production deployment (2026-09-24): `workitem`
action `list` without `project_id` 404s, and any `pql` 400s — which the server
rewrites as "fix your PQL", so a model keeps retrying. One conversation made
twelve such calls and the model told its user the service was temporarily down.
The refusal must be actionable and must say it is NOT transient.
"""

import pytest
from fastmcp import Client, FastMCP

from beherouter.backends.backing import McpBacking
from beherouter.backends.mcp import ReconnectingMCPExecutor, backend_from_client
from beherouter.errors import UsageError
from beherouter.plugins import get
from beherouter.plugins.plane import (
    COMMUNITY_NOTES,
    community_edition_guard,
    edition_backing_options,
)
from beherouter.plugins.spec import PluginContext
from beherouter.plugins.validate import validate_config

PLANE_PLUGINS = ("plane", "plane-http", "plane-http-apikey")


def test_workspace_wide_list_is_refused_with_the_way_out():
    with pytest.raises(UsageError, match="project_id") as e:
        community_edition_guard("workitem", {"action": "list"})
    assert "not a temporary error" in str(e.value)
    assert "`project`" in str(e.value)


def test_any_pql_is_refused():
    with pytest.raises(UsageError, match="pql"):
        community_edition_guard(
            "workitem", {"action": "list", "project_id": "p1", "pql": 'state = "x"'}
        )


def test_count_scoped_to_a_project_is_refused_because_it_becomes_pql():
    with pytest.raises(UsageError, match="count"):
        community_edition_guard("workitem", {"action": "count", "project_id": "p1"})


@pytest.mark.parametrize(
    ("verb", "args"),
    [
        ("workitem", {"action": "list", "project_id": "p1"}),
        ("workitem", {"action": "create", "project_id": "p1", "name": "x"}),
        ("workitem", {"action": "list", "project_id": "p1", "pql": ""}),
        ("project", {"action": "list"}),
    ],
)
def test_served_calls_pass(verb, args):
    community_edition_guard(verb, args)


@pytest.mark.parametrize("plugin", PLANE_PLUGINS)
def test_every_plane_plugin_defaults_to_community(plugin):
    fields = {f.name: f for f in get(plugin).spec.config}
    assert fields["edition"].default == "community"


@pytest.mark.parametrize("plugin", PLANE_PLUGINS)
def test_an_unknown_edition_is_refused_offline(plugin):
    with pytest.raises(UsageError, match="edition"):
        get(plugin).validate({"edition": "enterprise"})


def test_commercial_turns_the_guard_and_the_notes_off():
    assert edition_backing_options({"edition": "commercial"}) == {}
    opts = edition_backing_options({"edition": "community"})
    assert opts["guard"] is community_edition_guard
    assert opts["notes"] == COMMUNITY_NOTES


@pytest.mark.parametrize(
    ("plugin", "config", "env"),
    [
        ("plane", {"workspace_slug": "w"}, {"api_key": "k"}),
        (
            "plane-http",
            {"base_url": "http://plane-mcp-bearer:8211/bearer/mcp"},
            {"access_token": "t"},
        ),
        ("plane-http-apikey", {"workspace_slug": "w"}, {"api_key": "k"}),
    ],
)
async def test_every_plane_plugin_attaches_with_the_guard(monkeypatch, plugin, config, env):
    """One vocabulary, three attachments: the edition rules ride on all of them."""
    seen = {}

    async def fake_load(backing, headers=None):
        seen["backing"] = backing
        return object()

    module = get(plugin).build.__module__
    monkeypatch.setattr(f"{module}.load_mcp_backend", fake_load)
    spec = get(plugin).spec
    ctx = PluginContext(
        surface="plane", config=validate_config("plane", spec, config), env=env
    )
    await get(plugin).build(ctx)
    assert seen["backing"].guard is community_edition_guard
    assert "workitem" in seen["backing"].notes


@pytest.fixture
def workitem_server():
    s = FastMCP("plane")

    @s.tool
    def workitem(action: str, project_id: str = "") -> str:
        """Work items."""
        return f"{action}:{project_id}"

    return s


async def test_the_note_is_in_the_description_a_model_reads(workitem_server):
    async with Client(workitem_server) as c:
        b = await backend_from_client("plane", c, notes=COMMUNITY_NOTES)
    summary = {d.name: d.summary for d in b.descriptors}["workitem"]
    assert summary.startswith("Work items.")
    assert "requires `project_id`" in summary


async def test_a_refused_call_never_reaches_the_backend(monkeypatch):
    """The guard runs before a transport is built, so it costs no round trip."""
    from beherouter.backends import mcp as mcp_backend

    def boom(*a, **kw):
        raise AssertionError("a refused call must not open a client")

    monkeypatch.setattr(mcp_backend, "Client", boom)
    backing = McpBacking(
        name="plane",
        transport="http",
        url="http://plane.test/mcp",
        guard=community_edition_guard,
    )
    executor = ReconnectingMCPExecutor(mcp_backend.build_transport(backing), backing)
    with pytest.raises(UsageError, match="project_id"):
        await executor.run("workitem", {"action": "list"})
