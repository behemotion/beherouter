"""Gateway shared-token auth.

The gateway is the harness's only network MCP surface (CONVENTIONS: bearer auth,
localhost-bound backends), so the check here is deliberately simple: one shared
static token in `$BEHEROUTER_GATEWAY_TOKEN`, compared in constant time.

Per-user *backend* credentials are a separate concern — those are forwarded
per-session as headers by `backends.mcp.build_transport`.
"""

import hmac
import os

from fastmcp.server.auth import AccessToken, TokenVerifier

from .errors import UsageError

ENV_VAR = "BEHEROUTER_GATEWAY_TOKEN"


def configured_token() -> str | None:
    """The gateway's shared token, or None if unset/empty."""
    return os.environ.get(ENV_VAR) or None


def token_ok(expected: str | None, presented: str | None) -> bool:
    """Constant-time compare. An unconfigured gateway (expected=None) denies all."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


class SharedTokenVerifier(TokenVerifier):
    """FastMCP token verifier backed by the single shared gateway secret."""

    def __init__(self, expected: str | None, required_scopes: list[str] | None = None) -> None:
        super().__init__(required_scopes=required_scopes)
        self._expected = expected

    @classmethod
    def from_env(cls, strict: bool = True) -> "SharedTokenVerifier":
        """Build from `$BEHEROUTER_GATEWAY_TOKEN`.

        `strict=True` refuses to start an unauthenticated gateway — failing at
        boot is far better than silently serving every backend to the network.
        """
        token = configured_token()
        if strict and not token:
            raise UsageError(
                f"{ENV_VAR} is not set; refusing to start an unauthenticated gateway"
            )
        return cls(token)

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token_ok(self._expected, token):
            return None
        return AccessToken(
            token=token,
            client_id="beherouter-shared",
            scopes=list(self.required_scopes or []),
            expires_at=None,
        )
