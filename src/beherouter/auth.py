"""Gateway auth: a shared static token, an OIDC JWT, or both at once.

The gateway is the harness's only network MCP surface (CONVENTIONS: bearer
auth, localhost-bound backends). The default is still one shared static token in
`$BEHEROUTER_GATEWAY_TOKEN`, compared in constant time — a homelab gateway needs
nothing else, and four of the five wired consumers cannot mint anything else.

`BEHEROUTER_AUTH_MODE=both` adds a JWKS-verified JWT beside it, which is what
makes a per-user identity possible at all: a shared secret cannot tell callers
apart. Per-surface identity forwarding is a separate concern — see
`identity.py`, which reads the AccessToken this module produces.

⚠️ THE JWKS URI IS EXPLICIT, NEVER DISCOVERED. Resolving it from the issuer's
`.well-known/openid-configuration` would put a network call before the routes
exist, and a boot-time failure here takes /healthz down for every surface.
`JWTVerifier` fetches and caches the key set lazily, on the first token it sees,
which is the correct side of the attach boundary. A convention-derived path is
rejected too: it would be an IdP-vendor assumption, and nothing else here makes
one.
"""

import hmac
import os

from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier

from .errors import UsageError

ENV_VAR = "BEHEROUTER_GATEWAY_TOKEN"

AUTH_MODE_VAR = "BEHEROUTER_AUTH_MODE"
OIDC_ISSUER_VAR = "BEHEROUTER_OIDC_ISSUER"
OIDC_AUDIENCE_VAR = "BEHEROUTER_OIDC_AUDIENCE"
OIDC_JWKS_VAR = "BEHEROUTER_OIDC_JWKS_URI"
OIDC_SCOPES_VAR = "BEHEROUTER_OIDC_REQUIRED_SCOPES"

AUTH_MODES = ("shared", "oidc", "both")

# The shared token's AccessToken.client_id, and the ONE discriminator between a
# shared-secret caller and a user. `identity.py` refuses per-user surfaces on
# this value, so it is load-bearing rather than cosmetic — asserted by
# test_shared_verifier_stamps_the_discriminating_client_id.
SHARED_CLIENT_ID = "beherouter-shared"


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
            client_id=SHARED_CLIENT_ID,
            scopes=list(self.required_scopes or []),
            expires_at=None,
        )


def auth_mode() -> str:
    """The configured gateway auth mode; `shared` when unset."""
    mode = (os.environ.get(AUTH_MODE_VAR) or "shared").strip().lower()
    if mode not in AUTH_MODES:
        raise UsageError(f"{AUTH_MODE_VAR}={mode!r} is not one of {AUTH_MODES}")
    return mode


def oidc_verifier() -> JWTVerifier:
    """A JWKS-backed JWT verifier from the environment.

    Every setting is required and checked as a string: a mode that cannot
    verify anything must fail at boot, not on the first user's call.
    """
    issuer = os.environ.get(OIDC_ISSUER_VAR) or None
    audience = os.environ.get(OIDC_AUDIENCE_VAR) or None
    jwks_uri = os.environ.get(OIDC_JWKS_VAR) or None
    missing = [
        name
        for name, value in (
            (OIDC_ISSUER_VAR, issuer),
            (OIDC_AUDIENCE_VAR, audience),
            (OIDC_JWKS_VAR, jwks_uri),
        )
        if not value
    ]
    if missing:
        raise UsageError(
            f"{AUTH_MODE_VAR} includes OIDC but {', '.join(missing)} is unset "
            f"or empty; the JWKS URI is never discovered from the issuer"
        )
    scopes = [
        s.strip()
        for s in (os.environ.get(OIDC_SCOPES_VAR) or "").split(",")
        if s.strip()
    ]
    return JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        audience=audience,
        required_scopes=scopes or None,
    )


class CompositeVerifier(TokenVerifier):
    """Try each verifier in order; the first AccessToken wins.

    Order is cheapest-first: a constant-time compare before a JWT parse. A
    rejection stays indistinguishable from a missing credential (a bare 401),
    which is deliberate — see SharedTokenVerifier.
    """

    def __init__(
        self, verifiers: list[TokenVerifier], required_scopes: list[str] | None = None
    ) -> None:
        super().__init__(required_scopes=required_scopes)
        self._verifiers = list(verifiers)

    async def verify_token(self, token: str) -> AccessToken | None:
        for verifier in self._verifiers:
            found = await verifier.verify_token(token)
            if found is not None:
                return found
        return None


def build_verifier(strict: bool = True) -> TokenVerifier:
    """The verifier this deployment's configuration asks for.

    `strict` still refuses to start a gateway whose SHARED token is missing —
    but only in the modes that use one. A pure-`oidc` gateway has no shared
    secret to demand.
    """
    mode = auth_mode()
    if mode == "shared":
        return SharedTokenVerifier.from_env(strict=strict)
    if mode == "oidc":
        return oidc_verifier()
    return CompositeVerifier(
        [SharedTokenVerifier.from_env(strict=strict), oidc_verifier()]
    )
