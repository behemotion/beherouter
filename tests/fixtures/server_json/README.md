# server.json fixtures: provenance

Inputs for `tests/test_catalog.py` (`catalog-import` / `catalog-export`).

## Recorded from the official MCP Registry, 2026-10-07

Each is the unedited reply of
`GET https://registry.modelcontextprotocol.io/v0/servers/<name>/versions/latest`,
pretty-printed. They keep the API's `{"server": ..., "_meta": ...}` wrapper, so
the importer's unwrapping is exercised too.

| File | Server | Shape |
|---|---|---|
| `brave-npm.json` | `io.github.brave/brave-search-mcp-server` 2.1.3 | npm package, stdio, one required secret env var |
| `serena-pypi.json` | `io.github.oraios/serena` 1.5.3 | pypi package, stdio, server arguments published as `runtimeArguments` |
| `github-remote-oci.json` | `io.github.github/github-mcp-server` 2.0.1 | streamable-http remote with an optional secret `Authorization` header and no value template, plus an `oci` package |
| `notion-remote.json` | `com.notion/mcp` 1.0.1 | streamable-http and sse remotes, no headers; schema `2025-09-29` |

## Written for the test

No published server carries the `io.beherouter/plugin` block yet, and none
found declares two secrets on one package, so these two are hand-written to
the 2025-12-11 schema:

- `wiki-remote-meta.json` — the publisher block from
  `docs/superpowers/specs/2026-09-25-plugin-sources-design.md`, plus a `lookup`
  identity mode `mcp-http` does not honour.
- `multi-secret-pypi.json` — one required secret, one optional secret, one
  non-secret variable with a default.
