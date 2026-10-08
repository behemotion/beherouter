# Recorded catalogues: provenance

Each `*.json` here is a real `tools/list` reply, recorded from the server named
below. Nothing was written or edited by hand. Each row keeps `name`,
`description`, `inputSchema` and `annotations`. `tests/test_search_eval.py`
scores the queries in `../queries/<plugin>.toml` against these files.

## `plane-0.3.2.json`: 30 tools

`plane-mcp-server==0.3.2` over stdio, recorded with `scripts/record_catalogue.py`
and the command in that script's docstring. Listing tools needs no live Plane.

## `sonarqube-1.27.0.4335.json`: 18 tools

- **Server:** `docker.io/sonarsource/sonarqube-mcp:1.27.0.4335`, image digest
  `sha256:e178074f2cf49f57c524fee411275a7c141b7cf7586868d14c7b3c518cdccbcd`.
  This is the tag the homelab deployment ran
  (`$HOMELAB_REPO/ur/service/sonarqube/podman-compose.yml`), and the version
  the `sonarqube` plugin's measurements cite.
- **Recorded:** 2026-10-07, over stdio with `podman run -i --rm`, with a dummy
  `SONARQUBE_TOKEN`. `SONARQUBE_TOOLSETS` was set to the deployment's own list:
  `analysis,issues,projects,quality-gates,rules,measures,coverage,duplications,security-hotspots`.
  The server's default toolsets returned a byte-identical listing.
- **No SonarQube instance.** The server will not finish `initialize` without
  one. Before it answers, it calls `GET /api/features/list`,
  `/api/system/status`, `/api/users/current` and `/api/plugins/installed`.
  `SONARQUBE_URL` therefore pointed at a stub that answered those four calls as
  a Community Build would: features `[]`, status `UP` at version
  `26.9.0.129388` (the deployment's `sonarqube` tag), a logged-in user, and no
  plugins. The tool definitions are the server's own. The stub affected only
  which tools are enabled, as described next.
- **Edition matters.** When `/api/features/list` answered `["sca"]`, the server
  advertised a 19th tool, `search_dependency_risks`. That is the "19" in
  `plugins/sonarqube.py`. On Community Build, that tool answers "requires
  Enterprise" to every call, and the deployment had already removed its toolset.
  So the 18-tool listing is the surface agents actually saw.

To re-record, run a stub HTTP server on the host that returns those four
replies. Then point the container at it with
`-e SONARQUBE_URL=http://host.containers.internal:<port>` and list tools through
`fastmcp.Client(StdioTransport("podman", ["run", "-i", "--rm", ...]))`, writing
rows in the same shape as `scripts/record_catalogue.py`.

## `office-mcp-0.1.0.json`: 4 tools

- **Server:** office-mcp `0.1.0`, the homelab's own server. Its source is in
  the homelab repo at `ur/service/office-mcp/`, commit
  `e83200accd369ed7f7ba28f2f7b5c9251fcc8cc3` (2026-09-24), and the tree was
  clean. It ran on `mcp` 1.28.1, from its `uv.lock` (`uv run --frozen`).
- **Recorded:** 2026-10-07, in memory. `office_mcp.mcp_server.build_mcp()` was
  connected to an MCP client session (`mcp.shared.memory`), and the client
  called `list_tools`. `OFFICE_MCP_DATA` pointed at a scratch directory. No
  Gotenberg, worker or network was involved, and none is needed to list tools.
- **Only the four advertised entry points** (`discover`, `invoke`,
  `file_from_url`, `job_status`) are here. The other 32 tools in its 34-tool
  catalogue sit behind `discover`, and the gateway never sees them. This file is what the
  gateway indexes.
