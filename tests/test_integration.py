"""Integration tests against real backends (beheaxi CLI, gitea-mcp)."""

import json
import shutil
import subprocess

import pytest
from fastmcp import Client

from beherouter.backends.backing import CliBacking, McpBacking
from beherouter.backends.cli import load_cli_backend
from beherouter.naming import flatten
from beherouter.surface import build_surface

# --- cli kind: the real beheaxi CLI -----------------------------------------


async def test_attach_real_beheaxi():
    b = load_cli_backend(CliBacking(name="beheaxi", cmd="beheaxi"))
    assert any(d.name == "beheaxi_conformance" for d in b.descriptors)
    surface = build_surface(b)
    async with Client(surface) as c:
        res = await c.call_tool("search_tools", {"query": "conformance"})
    assert "beheaxi_conformance" in str(res.data)


def test_real_beheaxi_verb_name_format():
    """Pin what beheaxi actually emits: flat verb names, no spaces."""
    out = subprocess.run(
        ["beheaxi", "describe", "--json"], capture_output=True, text=True, check=False
    )
    manifest = json.loads(out.stdout)
    for v in manifest["verbs"]:
        assert " " not in v["name"], f"unexpected space in verb name: {v['name']!r}"
    for v in manifest["verbs"]:
        fn = flatten("beheaxi", v["name"])
        assert "-" not in fn and " " not in fn


async def test_real_beheaxi_surface_builds_with_flag_args():
    """beheaxi renders optional args as `--flag`; the surface must still build."""
    b = load_cli_backend(CliBacking(name="beheaxi", cmd="beheaxi"))
    surface = build_surface(b)
    async with Client(surface) as c:
        names = {t.name for t in await c.list_tools()}
    assert {"search_tools", "describe_tool", "run_tool"} <= names


async def test_real_beheaxi_verb_executes():
    """The whole cli path against a REAL beheaxi CLI: manifest -> argv ->
    subprocess -> parsed JSON. The fake_tool fixture cannot prove the manifest
    convention (required=positional) matches what beheaxi actually accepts."""
    b = load_cli_backend(CliBacking(name="beheaxi", cmd="beheaxi"))
    out = await b.executor.run("conformance", {"target": "beheaxi"})
    assert out["ok"] is True
    assert out["target"] == "beheaxi"


async def test_real_beheaxi_missing_required_arg_is_usage():
    """Against the real manifest, not a hand-written schema."""
    from beherouter.errors import UsageError

    b = load_cli_backend(CliBacking(name="beheaxi", cmd="beheaxi"))
    with pytest.raises(UsageError):
        await b.executor.run("conformance", {})


async def test_real_beheaxi_nonzero_exit_maps_to_an_error(fake_cli_cmd):
    """A real beheaxi CLI exiting 1 (INTERNAL) must raise, not return junk."""
    from beherouter.errors import AxiError

    b = load_cli_backend(CliBacking(name="beheaxi", cmd="beheaxi"))
    with pytest.raises(AxiError):
        # fake_tool is deliberately non-conformant, so conformance exits 1.
        await b.executor.run("conformance", {"target": fake_cli_cmd})


# --- mcp kind: the real gitea-mcp server ------------------------------------

gitea_required = pytest.mark.skipif(
    shutil.which("gitea-mcp") is None, reason="gitea-mcp not installed locally"
)


@gitea_required
async def test_attach_gitea_lists_and_searches():
    from beherouter.backends.mcp import load_mcp_backend

    b = await load_mcp_backend(
        McpBacking(
            name="gitea",
            transport="stdio",
            cmd="gitea-mcp --transport stdio --read-only",
        )
    )
    names = {d.name for d in b.descriptors}
    assert len(names) > 20, f"expected ~100 tools, got {len(names)}"


@gitea_required
async def test_gitea_surface_pins_few_and_searches_the_rest():
    """The whole point: ~100 tools cost a handful of definitions in context."""
    from beherouter.backends.mcp import load_mcp_backend

    b = await load_mcp_backend(
        McpBacking(
            name="gitea",
            transport="stdio",
            cmd="gitea-mcp --transport stdio --read-only",
            pinned=["get_my_user_info"],
        )
    )
    total = len(b.descriptors)
    surface = build_surface(b)
    async with Client(surface) as c:
        listed = {t.name for t in await c.list_tools()}
        res = await c.call_tool("search_tools", {"query": "list repository issues"})
    # pinned + exactly the four meta-tools — not the whole catalogue
    assert len(listed) <= 6, f"surface leaked {len(listed)} tools into context"
    assert {"search_tools", "describe_tool", "run_tool", "context_cost"} <= listed
    assert total > 20
    # the long tail is reachable by search even though it is not listed
    assert res.data, "search returned no hits for a plainly-present capability"
