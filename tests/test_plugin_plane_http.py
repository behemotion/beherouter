"""`plane-http` — the same Plane surface, attached over HTTP so it can be per-user.

The stdio `plane` plugin can never carry a per-request identity (a subprocess
environment is fixed at spawn and `keep_alive=True` reuses it across callers),
which is the whole reason this second plugin exists. Everything else — the pin
list, the probe, the Community-Edition gaps — is deliberately shared with it, so
an operator moving a surface from one to the other changes the attach and
nothing an agent can see.
"""

import pytest

from beherouter.errors import UsageError
from beherouter.identity import validate_identity
from beherouter.plugins import get
from beherouter.plugins.spec import PluginContext
from beherouter.plugins.validate import validate_config

PLUGIN = "plane-http"


def test_registered_as_http():
    assert get(PLUGIN).spec.backing == "http"


def test_advertises_the_same_pins_as_the_stdio_plugin():
    """One vocabulary, two attachments: the pin list is shared, not copied."""
    assert get(PLUGIN).spec.pinned == get("plane").spec.pinned


def test_probe_authenticates_rather_than_lists():
    spec = get(PLUGIN).spec
    assert spec.probe == "member"
    assert spec.probe_args == {"action": "me"}


def test_declares_bearer_identity_landing_in_a_header():
    support = get(PLUGIN).spec.identity
    assert support.modes == ("bearer",)
    assert support.target == "header"


def test_a_bearer_surface_on_this_plugin_validates():
    """The refusal the stdio plugin earns, this one must not."""
    validate_identity("plane", get(PLUGIN).spec, {"mode": "bearer"})


def test_the_stdio_plugin_still_refuses_the_same_table():
    """`plane` declares no IdentitySupport at all, so it is refused one rule
    earlier than the stdio rule — which is the fail-closed order: a plugin
    nobody audited for per-user use cannot be configured for it."""
    with pytest.raises(UsageError, match="declares no identity support"):
        validate_identity("plane", get("plane").spec, {"mode": "bearer"})


def test_claims_mode_is_refused_because_plane_verifies_the_token_itself():
    """Plane's `/http` mount validates the bearer against Plane's own API, so an
    asserted identity it cannot verify would simply 401. Declaring only `bearer`
    is what turns that into an offline lint error."""
    with pytest.raises(UsageError, match="supports identity mode"):
        validate_identity(
            "plane", get(PLUGIN).spec, {"mode": "claims", "map": {"x-user": "sub"}}
        )


def test_base_url_is_required_because_upstream_has_no_bearer_mount():
    """The old default pointed at upstream's OAuth proxy, which 401s a forwarded
    token; a surface built from it attached green and 401'd every user call."""
    with pytest.raises(UsageError, match="base_url"):
        validate_config("plane", get(PLUGIN).spec, {})


def test_upstreams_oauth_proxy_mount_is_refused():
    with pytest.raises(UsageError, match="OAuth proxy"):
        get(PLUGIN).validate({"base_url": "http://plane-mcp:8211/http/mcp"})


def test_no_workspace_slug_is_configured():
    """⚠️ Unlike the stdio plugin. The `/http` mount reads the workspace from the
    token's own app installation (`client.py` takes `workspace_slug` from the
    AccessToken claims), so configuring one here would be a value nothing reads."""
    assert "workspace_slug" not in {f.name for f in get(PLUGIN).spec.config}


def test_the_api_key_mount_is_refused():
    """`/http/api-key` is a per-request PLANE PAT — a second shared-secret
    scheme, not an identity. Pointing a per-user surface at it would forward a
    bearer the mount ignores, and attribute every write to the PAT."""
    with pytest.raises(UsageError, match="api-key"):
        get(PLUGIN).validate({"base_url": "http://plane-mcp:8211/http/api-key/mcp"})


def test_a_url_that_is_not_an_mcp_endpoint_is_refused():
    with pytest.raises(UsageError, match="/mcp"):
        get(PLUGIN).validate({"base_url": "http://plane-mcp:8211/http"})


def test_a_bearer_forwarding_mount_passes_the_validator():
    get(PLUGIN).validate({"base_url": "http://plane-mcp-bearer:8211/bearer/mcp"})


async def test_the_deployment_token_attaches_as_a_bearer_header(monkeypatch):
    """Attach and the catalogue use the DEPLOYMENT credential — per-request
    identity overrides this header per call, it does not replace it."""
    seen = {}

    async def fake_load(backing, headers=None):
        seen["backing"] = backing
        return object()

    monkeypatch.setattr("beherouter.plugins.plane_http.load_mcp_backend", fake_load)
    ctx = PluginContext(
        surface="plane",
        config={"base_url": "http://plane-mcp-bearer:8211/bearer/mcp"},
        env={"access_token": "deployment-bearer"},
        pinned=["workitem"],
    )
    await get(PLUGIN).build(ctx)
    backing = seen["backing"]
    assert backing.transport == "http"
    assert backing.url == "http://plane-mcp-bearer:8211/bearer/mcp"
    assert backing.env["authorization"] == "Bearer deployment-bearer"


def test_the_credential_is_not_named_token():
    """`token` would resolve to BEHEROUTER_<SURFACE>_TOKEN — the variable a
    client config uses for the caller's GATEWAY bearer. Two secrets, one name."""
    assert {e.name for e in get(PLUGIN).spec.env} == {"access_token"}
