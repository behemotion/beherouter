"""Dev-only: record a stdio MCP server's `tools/list` as a search-eval fixture.

NOT shipped. Runs the server as a subprocess over stdio, so it needs no network
and no real credentials — the dummy env below is enough to LIST tools, never to
call one. Re-run after every upgrade of a backend the eval set covers (AGENTS.md:
re-probe after an upgrade), then re-run `uv run pytest tests/test_search_eval.py`.

    uv run python scripts/record_catalogue.py plane-mcp-server==0.3.2 \\
        tests/search_eval/catalogues/plane-0.3.2.json \\
        PLANE_API_KEY=x PLANE_WORKSPACE_SLUG=w PLANE_BASE_URL=http://127.0.0.1:9
"""

import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def record(package: str, out: Path, env: dict[str, str]) -> int:
    transport = StdioTransport(command="uvx", args=[package, "stdio"], env=env)
    async with Client(transport) as c:
        tools = await c.list_tools()
    rows = [
        {
            "name": t.name,
            "description": t.description or "",
            "inputSchema": t.inputSchema or {},
            "annotations": (
                t.annotations.model_dump(exclude_none=True) if t.annotations else None
            ),
        }
        for t in tools
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n")
    return len(rows)


if __name__ == "__main__":
    package, out, *pairs = sys.argv[1:]
    env = dict(p.split("=", 1) for p in pairs)
    print(asyncio.run(record(package, Path(out), env)), "tools recorded")
