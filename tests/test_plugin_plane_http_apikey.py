"""`plane-http-apikey` — per-user Plane that works against UPSTREAM today.

⚠️ WHY THIS EXISTS BESIDE `plane-http`. plane-mcp-server 0.3.2 serves two HTTP
mounts, and only one of them can carry a credential the gateway forwards:

  - `/http` (bearer) is an OAuth PROXY. It verifies a token it minted itself —
    a FastMCP-issued JWT whose `jti` it looks up in its own store — and returns
    401 for anything else BEFORE Plane is consulted. A forwarded IdP token
    therefore cannot reach Plane through it. Verified 2026-09-22 against
    0.3.2: attach → `401 Unauthorized`, and the Plane API saw no request at all.
  - `/http/api-key` takes a per-request Plane PAT plus `x-workspace-slug`, and
    calls Plane with it. Verified the same day: a PAT presented per request
    reaches Plane's `/api/v1/users/me/` as `x-api-key` and resolves to THAT
    user.

So this plugin is the one that makes a Plane surface per-user against the
published server, and `plane-http` is for a deployment whose backend accepts a
forwarded bearer. Each declares exactly the identity mode its mount can honour,
so `registry-lint` refuses the wrong pairing offline.
"""

import pytest

from beherouter.errors import UsageError
from beherouter.identity import validate_identity
from beherouter.plugins import get
from beherouter.plugins.spec import PluginContext
from beherouter.plugins.validate import validate_config

PLUGIN = "plane-http-apikey"


def test_registered_as_http():
    assert get(PLUGIN).spec.backing == "http"


def test_shares_the_pin_list_with_every_other_plane_attachment():
    assert get(PLUGIN).spec.pinned == get("plane").spec.pinned


def test_declares_client_identity_only():
    """The mount authenticates a PAT the CLIENT holds; there is no token for the
    gateway to mint, assert or look up, so `client` is the only honest mode."""
    support = get(PLUGIN).spec.identity
    assert support.modes == ("client",)
    assert support.target == "header"


def test_bearer_mode_is_refused_on_this_mount():
    with pytest.raises(UsageError, match="supports identity mode"):
        validate_identity("plane", get(PLUGIN).spec, {"mode": "bearer"})


def test_client_mode_validates_with_a_map():
    validate_identity(
        "plane",
        get(PLUGIN).spec,
        {"mode": "client", "map": {"authorization": "x-plane-pat"}},
    )


def test_a_target_header_outside_the_accepted_set_is_refused():
    """The mount reads exactly two headers; anything else would be forwarded
    into a backend that ignores it, which reads as 'configured, not applied'."""
    with pytest.raises(UsageError, match="not an identity target"):
        validate_identity(
            "plane",
            get(PLUGIN).spec,
            {"mode": "client", "map": {"x-made-up": "x-plane-pat"}},
        )


def test_the_workspace_may_also_be_per_caller():
    """Two headers per request is the founding multi-header requirement — the
    axis the surveyed aggregators were rejected on."""
    validate_identity(
        "plane",
        get(PLUGIN).spec,
        {
            "mode": "client",
            "map": {
                "authorization": "x-plane-pat",
                "x-workspace-slug": "x-plane-workspace",
            },
        },
    )


def test_base_url_defaults_to_the_api_key_mount():
    cfg = validate_config("plane", get(PLUGIN).spec, {"workspace_slug": "homelab"})
    assert cfg["base_url"].endswith("/http/api-key/mcp")


def test_the_bearer_mount_is_refused_here():
    """The mirror of `plane-http`'s rule: this plugin's credentials mean
    nothing on the OAuth mount, which would 401 every call."""
    with pytest.raises(UsageError, match="api-key"):
        get(PLUGIN).validate(
            {"base_url": "http://plane-mcp:8211/http/mcp", "workspace_slug": "h"}
        )


def test_workspace_slug_is_required():
    """⚠️ Unlike `plane-http`: this mount refuses any request without
    `x-workspace-slug`, and the attach must carry one."""
    with pytest.raises(UsageError, match="workspace_slug"):
        validate_config("plane", get(PLUGIN).spec, {})


async def test_attach_sends_the_deployment_pat_and_the_workspace(monkeypatch):
    seen = {}

    async def fake_load(backing, headers=None):
        seen["backing"] = backing
        return object()

    monkeypatch.setattr(
        "beherouter.plugins.plane_http_apikey.load_mcp_backend", fake_load
    )
    ctx = PluginContext(
        surface="plane",
        config={
            "base_url": "http://plane-mcp:8211/http/api-key/mcp",
            "workspace_slug": "homelab",
        },
        env={"api_key": "deployment-pat"},
        pinned=["workitem"],
    )
    await get(PLUGIN).build(ctx)
    env = seen["backing"].env
    assert env["authorization"] == "Bearer deployment-pat"
    assert env["x-workspace-slug"] == "homelab"
