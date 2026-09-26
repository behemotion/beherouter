"""SonarQube — code quality and security findings for a self-hosted instance.

⚠️ `base_url` addresses the container over a SHARED podman network, NOT via
127.0.0.1 and NOT via the public vhost. A rootless bridged container cannot
reach a port published on its own host, so co-location makes a backend HARDER
to reach, not easier — sonarqube-mcp publishes no port at all and is reachable
only by container name on `behe-gateway`.

⚠️ THE ENDPOINT IS `/mcp`, WITH NO TRAILING SLASH, and the difference is not
cosmetic. Measured against sonarqube-mcp 1.27.0.4335 (2026-09-22): `/mcp`
answers 200, while `/mcp/` answers a JSON **404** that looks exactly like the
server being absent. `validate()` below refuses the trailing-slash form rather
than letting it fail at attach time, because a 404 on a URL you can see in the
registry reads as "the container is down" and sends you to the wrong host.

⚠️ THIS BACKEND AUTHENTICATES ITS OWN CALLERS, so the surface carries a
credential. An un-credentialed `tools/list` is refused outright with
`"SonarQube token required. Provide via Authorization: Bearer <token>"` — this
is the `plane` shape, not the `office-mcp` shape, and it was determined by
probing rather than assumed. Note the auth check runs BEFORE routing: without a
token every path returns that error, so a missing credential and a wrong path
are indistinguishable until you fix the credential.

⚠️ THE CREDENTIAL IS A SONARQUBE **USER** TOKEN (`squ_…`), and the kind matters.
SonarQube mints three kinds and they are not interchangeable: a
GLOBAL_ANALYSIS_TOKEN (`sqa_…`) may submit analyses but is **403 on every read
endpoint**, so it attaches and then fails every tool call. Verified on a live
instance: `sqa_` scores 403 against `/api/projects/search` where `squ_` scores
200. SonarSource document the same constraint for the MCP server itself.
"""

from urllib.parse import urlparse

from ..backends.backing import McpBacking
from ..backends.mcp import load_mcp_backend
from ..errors import UsageError
from . import register
from .spec import ConfigField, EnvVar, PluginContext, PluginSpec

# No `IdentitySupport` yet: every caller shares one SonarQube user token.
# Declare it when a consumer needs findings scoped to its own SonarQube user.

# The header the server reads the token from. A closed constant so a typo is a
# lint error rather than a header the backend silently ignores.
AUTH_HEADER = "authorization"

# ⚠️ THIS BACKEND'S OUTPUT SCHEMAS ARE WRONG, SO THEY ARE NOT REPUBLISHED
# (`republish_output_schema=False` in build() below) — measured 2026-09-22
# against sonarqube-mcp 1.27.0.4335, not reasoned about.
#
# All 19 of its tools declare an `outputSchema`, and several type a field as
# plain `string`/`object`/`boolean` while the server's own replies return `null`
# for it — `get_component_measures` answers `"description": null` inside a
# component whose schema permits no nulls. A pinned tool republished with that
# schema has every reply validated against it and rejected:
#
#     Output validation error: None is not of type 'string'
#
# With the schemas republished, four of these five pins never returned a single
# successful call:
#     search_my_sonarqube_projects     OK
#     show_rule                        FAIL  (None is not of type 'string')
#     get_component_measures           FAIL  (None is not of type 'string')
#     get_project_quality_gate_status  FAIL  (None is not of type 'boolean')
#     search_sonar_issues_in_projects  FAIL  (None is not of type 'object')
#
# ⚠️ A DIRECT CALL TO THE BACKEND SUCCEEDS, and that is the trap rather than the
# reassurance: plain curl validates nothing, so a test that bypasses the
# validating layer would certify a broken surface. Test through the gateway.
#
# RE-CHECK ON EVERY BACKEND BUMP: once upstream's schemas match its replies,
# set the flag back to True so code-mode hosts get typed results again.
#
# These stay unpinned by policy, reachable through `run_tool`:
#   - change_sonar_issue_status / change_security_hotspot_status: writes. An
#     agent triaging findings unprompted is not wanted.
#   - analyze_code_snippet: sends source to the server on every call.
#   - list_pull_requests / list_branches: Community Build stores the MAIN
#     branch only, so both are structurally empty on this deployment. Same
#     edition-boundary trap as Plane's Community Edition 404s.
PINNED = (
    "search_my_sonarqube_projects",
    "search_sonar_issues_in_projects",
    "get_project_quality_gate_status",
    "get_component_measures",
    "show_rule",
)

# Chosen because it takes NO required arguments and still round-trips to SonarQube itself — so it proves the token is live, not merely
# that the MCP process is up. `pageSize: 1` keeps the probe cheap on an instance
# with dozens of projects.
PROBE = "search_my_sonarqube_projects"
PROBE_ARGS = {"pageSize": 1}

SPEC = PluginSpec(
    name="sonarqube",
    summary=(
        "Code quality and security findings: projects, issues, quality gates, "
        "rule explanations and measures."
    ),
    backing="http",
    pinned=PINNED,
    probe=PROBE,
    probe_args=PROBE_ARGS,
    config=(
        ConfigField(
            name="base_url",
            type=str,
            default="http://sonarqube-mcp:8080/mcp",
            doc=(
                "MCP endpoint on the shared podman network. Must end in '/mcp' "
                "with no trailing slash — '/mcp/' returns 404."
            ),
        ),
    ),
    env=(
        EnvVar(
            name="api_key",
            doc=(
                "SonarQube USER token (squ_…) used for attach, probe and every "
                "call. A GLOBAL_ANALYSIS_TOKEN (sqa_…) attaches and then 403s "
                "every read — it can submit analyses, not answer questions."
            ),
        ),
    ),
)


def validate(config: dict) -> None:
    """Refuse a base_url the server does not serve. Offline, no I/O.

    The trailing-slash form is the whole reason this exists: `/mcp/` returns a
    JSON 404 that reads as an absent container, so catching it at lint time is
    the difference between a one-line fix and debugging the wrong host.
    """
    base_url = config.get("base_url")
    if not base_url:
        return
    path = urlparse(base_url).path
    if path.endswith("/mcp/"):
        raise UsageError(
            f"sonarqube: base_url '{base_url}' has a trailing slash. "
            f"sonarqube-mcp serves '/mcp' and returns 404 for '/mcp/' — which "
            f"looks like the container being down. Drop the trailing slash."
        )
    if not path.rstrip("/").endswith("/mcp"):
        raise UsageError(
            f"sonarqube: base_url '{base_url}' is not an MCP endpoint; "
            f"it must end in '/mcp'"
        )


async def build(ctx: PluginContext):
    return await load_mcp_backend(
        McpBacking(
            name=ctx.surface,
            transport="http",
            url=ctx.config["base_url"],
            # For an http backing, `env` IS the attach-time header set.
            # Per-call identity material is merged OVER these, key by key.
            env={AUTH_HEADER: f"Bearer {ctx.env['api_key']}"},
            pinned=ctx.pinned,
            republish_output_schema=False,
        )
    )


register(SPEC, build, validate=validate)
