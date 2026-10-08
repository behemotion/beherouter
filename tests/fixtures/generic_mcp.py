"""A real MCP server for the generic plugins, on stdio or streamable HTTP.

    python generic_mcp.py stdio
    python generic_mcp.py http <port>

`whoami` reports the credential the call arrived with — the Authorization
header over HTTP, `GENERIC_MCP_KEY` from the environment over stdio — so a
test can assert what the plugin actually sent, not what it was configured with.
"""

import os
import sys

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

mcp = FastMCP("generic")


@mcp.tool(description="Report the credential this call arrived with.")
def whoami() -> dict:
    headers = get_http_headers(include={"authorization", "x-api-key"})
    return {
        "authorization": headers.get("authorization", ""),
        "x_api_key": headers.get("x-api-key", ""),
        "env_key": os.environ.get("GENERIC_MCP_KEY", ""),
    }


@mcp.tool(description="Search the notes for a query.")
def search_notes(query: str) -> dict:
    return {"hits": [f"note about {query}"]}


@mcp.tool(description="Delete a note by id.")
def delete_note(note_id: str) -> dict:
    return {"deleted": note_id}


@mcp.tool(description="Sleep for a number of seconds, then answer.")
async def sleep(seconds: float) -> dict:
    import asyncio

    await asyncio.sleep(seconds)
    return {"slept": seconds}


if __name__ == "__main__":
    if sys.argv[1] == "http":
        mcp.run(transport="http", host="127.0.0.1", port=int(sys.argv[2]), path="/mcp",
                show_banner=False)
    else:
        mcp.run(transport="stdio", show_banner=False)
