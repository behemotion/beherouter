"""A minimal MCP HTTP backend that reports the credential it was called with.

Stands in for a backend that ACCEPTS a forwarded bearer — which upstream
plane-mcp-server's `/http` mount does not (it is an OAuth proxy and only honours
tokens it minted itself). Without this, mode `bearer` could not be proven
end-to-end on a laptop; with it, the assertion is exact: the token the gateway
sent downstream is byte-identical to the one the caller presented.

Runs inside the beherouter image, which already has FastMCP.
"""

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

mcp = FastMCP("echo")


@mcp.tool(description="Report the Authorization this call arrived with.")
def whoami() -> dict:
    # ⚠️ `include` un-excludes: FastMCP drops `authorization` from its default
    # header view, and naming it here is what makes it visible.
    headers = get_http_headers(include={"authorization"})
    auth = headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    return {"scheme": auth.split(" ")[0] if auth else "", "token": token}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8300, path="/mcp")
