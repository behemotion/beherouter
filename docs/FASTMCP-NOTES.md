# FastMCP API notes (plan Task 0)

Probed against **fastmcp 3.4.5** (mcp 1.29.0, pydantic 2.13.4) on 2026-07-28.

The plan (`docs/superpowers/plans/2026-06-21-beherouter.md`) was written against
`fastmcp>=2.0.0`. FastMCP has since gone to 3.x. This file records what the
probe actually found, so the plan's snippets can be read against reality.

## Symbols the plan depends on — all still present

| Plan assumption | 3.4.5 reality |
|---|---|
| `@mcp.tool(name=, description=)` | present; `tool(name_or_fn=None, *, name, description, ...)` |
| `mcp.http_app(path=...)` | present |
| `FastMCP.as_proxy` | present |
| `fastmcp.Client` + `list_tools()` / `call_tool()` | present |
| stdio + http client transports | `StdioTransport`, `StreamableHttpTransport` (also `SSETransport`, `PythonStdioTransport`, `UvxStdioTransport`, …) |

## Three real differences

### 1. `**kwargs` tool functions are rejected — affects Task 8

The plan's `build_surface` registers pinned tools as:

```python
async def _tool(**kwargs):
    return await backend.executor.run(desc.verb, kwargs)
```

FastMCP 3.x raises:

```
ValueError: Functions with **kwargs are not supported as tools
```

FastMCP derives each tool's MCP `inputSchema` by introspecting the function
signature; a `**kwargs` function has no introspectable parameters and would
advertise an empty schema, leaving the calling agent with no idea what to pass.

**Fix used here:** synthesize a real signature on the generated closure from the
descriptor's arg schema (`surface.py:_make_pinned_tool`). This is strictly better
than the original — each tool now publishes a proper typed schema:

```json
{"properties": {"query": {"type": "string"}},
 "required": ["query"], "additionalProperties": false, "type": "object"}
```

which matters because agents discover these tools through `search_tools` with no
other documentation.

### 2. `mount()` signature changed

2.x was `mount(prefix, server)`; 3.x is
`mount(server, namespace=None, as_proxy=None, tool_names=None, prefix=None)`.

Not load-bearing here — `gateway.py` composes surfaces with Starlette `Mount`
over `http_app()`, not `FastMCP.mount`.

### 3. Auth provider shape

`fastmcp.server.auth` exports `AuthProvider`, `TokenVerifier`, `AccessToken`,
`RemoteAuthProvider`, `OAuthProvider`, `MultiAuth`, `require_scopes`. There is a
`StaticTokenVerifier(tokens: dict[str, dict[str, Any]], required_scopes=None)` at
`fastmcp.server.auth.providers.jwt`.

We implement our own `TokenVerifier` subclass instead (`auth.py`) — the gateway's
contract is a single shared secret compared in constant time, and subclassing
keeps that explicit rather than encoding a one-entry token dict.

## Not used

`BM25SearchTransform` — the spec named it, the plan deliberately replaced it with
our own `rank-bm25` `ToolIndex` exposed as real MCP meta-tools (testable,
version-independent, and required by LibreChat which has no native tool search).
