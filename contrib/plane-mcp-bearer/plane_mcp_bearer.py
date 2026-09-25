"""plane-mcp-bearer — upstream plane-mcp-server, plus a mount that forwards the
caller's bearer to Plane.

WHY THIS EXISTS. beherouter's `plane-http` plugin with identity mode `bearer`
forwards each caller's own IdP token. Neither of plane-mcp-server 0.3.2's HTTP
mounts can carry one: `/http` is a FastMCP OAuth proxy that accepts only tokens
it minted itself, and `/http/api-key` treats the credential as a Plane PAT. So a
per-user surface pointed at either attaches green and 401s every user call.

WHAT IT DOES. It builds the SAME server upstream builds — the same tools, the
same middleware, via upstream's own `_configured()` — behind a `TokenVerifier`
that does two things:

  - A token shaped like a Plane PAT (`PAT_PATTERN`) is routed to Plane as
    `X-Api-Key`. That is the gateway's DEPLOYMENT credential: it attaches, lists
    the catalogue and answers `health --deep`, and an IdP token would expire
    under a long-lived gateway.
  - Anything else is forwarded to Plane as `Authorization: Bearer`, unchanged.
    Plane must therefore verify your IdP's tokens itself (an authentication
    class checking your realm's JWKS). This wrapper verifies NOTHING about the
    token beyond "Plane accepted it on /api/v1/users/me/": Plane is the control.

⚠️ IT RELIES ON UPSTREAM'S PRIVATE ROUTING, and only an exact pin protects it.
`plane_mcp/client.py` sends the two `auth_method`s ("api_key_env",
"api_key_header") to `PlaneClient(api_key=...)` and every other value to
`PlaneClient(access_token=...)`, which the Plane SDK sends as
`Authorization: Bearer`. `_configured()` is private too. So the wrapper refuses
to start against any plane-mcp-server other than `UPSTREAM_VERSION` unless
`PLANE_MCP_BEARER_ALLOW_UNPINNED=1` — re-verify, then bump the pin.

Environment:
  PLANE_BASE_URL / PLANE_INTERNAL_BASE_URL   Plane's API, exactly as upstream reads them
  PLANE_WORKSPACE_SLUG                       REQUIRED: an IdP token carries no workspace
  PLANE_MCP_BEARER_PATH                      mount prefix, default "/bearer" (-> /bearer/mcp)
  PLANE_MCP_BEARER_PORT                      default 8211
  PLANE_MCP_BEARER_PAT_PATTERN               default ^plane_api_[0-9a-f]{32}\\Z
  PLANE_MCP_BEARER_CACHE_SECONDS             verdict cache per token, default 60
"""

import contextlib
import hashlib
import logging
import os
import re
import time
from importlib.metadata import version

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier

UPSTREAM_VERSION = "0.3.2"

# The two auth_methods upstream routes to `api_key=`; see the module docstring.
PAT_METHOD = "api_key_header"
# Any value OUTSIDE upstream's api-key pair reaches `access_token=`.
BEARER_METHOD = "forwarded_bearer"

DEFAULT_PAT_PATTERN = r"^plane_api_[0-9a-f]{32}\Z"
_CACHE_LIMIT = 4096

logger = logging.getLogger("plane_mcp_bearer")


def plane_base_url() -> str:
    """Resolved exactly as upstream's client resolves it, so the verdict and the
    call hit the same Plane."""
    return (
        os.environ.get("PLANE_INTERNAL_BASE_URL")
        or os.environ.get("PLANE_BASE_URL")
        or "https://api.plane.so"
    ).rstrip("/")


class ForwardingVerifier(TokenVerifier):
    """Accept a token Plane accepts, and label it so upstream forwards it as-is."""

    def __init__(
        self,
        workspace_slug: str,
        pat_pattern: str = DEFAULT_PAT_PATTERN,
        cache_seconds: float = 60.0,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__()
        self._workspace_slug = workspace_slug
        self._pat = re.compile(pat_pattern)
        self._cache_seconds = cache_seconds
        self._timeout = timeout_seconds
        self._transport = transport  # injectable for tests
        # sha256(token) -> (expires_at_monotonic, plane user id). A digest, so
        # not even this cache holds a credential.
        self._cache: dict[str, tuple[float, str]] = {}

    def is_pat(self, token: str) -> bool:
        return bool(self._pat.match(token))

    async def _plane_user(self, token: str, pat: bool) -> str | None:
        """Plane's own verdict: the user id /users/me/ resolves to, or None."""
        headers = {"X-Api-Key": token} if pat else {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(
                    f"{plane_base_url()}/api/v1/users/me/", headers=headers
                )
        except httpx.RequestError as e:
            logger.warning("Plane unreachable while verifying a token: %s", type(e).__name__)
            return None
        if resp.status_code != 200:
            logger.info("Plane refused a %s: HTTP %d", "PAT" if pat else "bearer", resp.status_code)
            return None
        try:
            return str(resp.json().get("id") or "")
        except ValueError:
            return None

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        pat = self.is_pat(token)
        key = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            user_id = hit[1]
        else:
            user_id = await self._plane_user(token, pat)
            if user_id is None:
                return None
            if len(self._cache) >= _CACHE_LIMIT:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[key] = (now + self._cache_seconds, user_id)
        return AccessToken(
            token=token,
            client_id="plane-mcp-bearer",
            # upstream's own FastMCP instances require these two
            scopes=["read", "write"],
            expires_at=None,
            claims={
                "auth_method": PAT_METHOD if pat else BEARER_METHOD,
                "workspace_slug": self._workspace_slug,
                # upstream's log filter reads `sub` as the opaque user id
                "sub": user_id,
            },
        )


def check_upstream(found: str | None = None) -> None:
    found = found or version("plane-mcp-server")
    if found != UPSTREAM_VERSION and os.environ.get("PLANE_MCP_BEARER_ALLOW_UNPINNED") != "1":
        raise SystemExit(
            f"plane-mcp-bearer: built against plane-mcp-server {UPSTREAM_VERSION}, "
            f"found {found}. It relies on upstream's private auth_method routing; "
            f"re-verify it against {found}, then bump UPSTREAM_VERSION "
            f"(or set PLANE_MCP_BEARER_ALLOW_UNPINNED=1 to start anyway)."
        )


def build_app(verifier: TokenVerifier | None = None, path: str | None = None):
    """The ASGI app: `<path>/mcp` plus an unauthenticated `/healthz`."""
    from plane_mcp.instructions import SERVER_INSTRUCTIONS
    from plane_mcp.server import _configured
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    workspace = os.environ.get("PLANE_WORKSPACE_SLUG", "")
    if verifier is None:
        if not workspace:
            raise SystemExit("plane-mcp-bearer: PLANE_WORKSPACE_SLUG is required")
        verifier = ForwardingVerifier(
            workspace_slug=workspace,
            pat_pattern=os.environ.get("PLANE_MCP_BEARER_PAT_PATTERN") or DEFAULT_PAT_PATTERN,
            cache_seconds=float(os.environ.get("PLANE_MCP_BEARER_CACHE_SECONDS") or 60),
        )
    prefix = "/" + (path or os.environ.get("PLANE_MCP_BEARER_PATH") or "/bearer").strip("/")
    mcp = _configured(
        FastMCP(
            "Plane MCP Server (bearer-forwarding)",
            instructions=SERVER_INSTRUCTIONS,
            auth=verifier,
        )
    )
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True)

    async def healthz(_request):
        return JSONResponse({"status": "ok", "upstream": UPSTREAM_VERSION})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    return Starlette(
        routes=[Route("/healthz", healthz), Mount(prefix, app=mcp_app)],
        lifespan=lifespan,
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    check_upstream()
    uvicorn.run(
        build_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PLANE_MCP_BEARER_PORT") or 8211),
        access_log=False,
    )


if __name__ == "__main__":
    main()
