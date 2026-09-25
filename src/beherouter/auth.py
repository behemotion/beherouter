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

⚠️ A REJECTED TOKEN SAYS WHY — to the operator and to the client. An expired
token is THE failure of a per-user rollout (a client forwarding a stale access
token), and FastMCP both logs it at INFO and answers it with the same generic
401 as a forged one. Here it is logged at WARNING, counted by reason
(`/metrics`), and answered with RFC 6750's
`WWW-Authenticate: Bearer error="invalid_token", error_description="token
expired"` so a client can tell "refresh" from "re-authenticate". The reason is
only ever "expired" for a token whose SIGNATURE verified: FastMCP checks the
signature before `exp`, so a forged token with an old `exp` is "invalid".
"""

import collections
import contextvars
import hmac
import json
import os
import re

from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier

from .errors import UsageError

ENV_VAR = "BEHEROUTER_GATEWAY_TOKEN"

AUTH_MODE_VAR = "BEHEROUTER_AUTH_MODE"
OIDC_ISSUER_VAR = "BEHEROUTER_OIDC_ISSUER"
OIDC_AUDIENCE_VAR = "BEHEROUTER_OIDC_AUDIENCE"
OIDC_JWKS_VAR = "BEHEROUTER_OIDC_JWKS_URI"
OIDC_SCOPES_VAR = "BEHEROUTER_OIDC_REQUIRED_SCOPES"
# Dotted path to the claim carrying a caller's roles. Unset means roles are
# unsupported on this gateway, which is what makes a role gate fail closed:
# there is no default, because every IdP puts them somewhere else
# (realm_access.roles on Keycloak, roles on Entra, groups elsewhere) and
# guessing would special-case one vendor.
OIDC_ROLES_CLAIM_VAR = "BEHEROUTER_OIDC_ROLES_CLAIM"

AUTH_MODES = ("shared", "oidc", "both")

# --- rejection reasons --------------------------------------------------------
#
# Why the last token on THIS request was refused. The slot is a dict set per
# request by `RejectionMiddleware`, so a verifier deep in a mounted sub-app can
# report into it however many tasks sit in between (a copied context still
# holds the same dict).
REASONS = ("expired", "invalid", "issuer", "audience", "scope")
REJECTIONS: collections.Counter[str] = collections.Counter()
_SLOT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "beherouter_auth_rejection", default=None
)

# FastMCP's JWTVerifier rejection log lines -> our reason. Matched on the
# FORMAT string, which is a constant in FastMCP's source; if a future FastMCP
# rewords one, that reason degrades to "invalid" and its level to FastMCP's
# own, never to a crash. test_auth pins the mapping against the real verifier.
_LOG_REASONS = (
    ("token expired", "expired"),
    ("issuer mismatch", "issuer"),
    ("audience mismatch", "audience"),
    ("missing required scopes", "scope"),
)


def _note(reason: str) -> None:
    slot = _SLOT.get()
    if slot is not None:
        slot["reason"] = reason


class _RejectionLog:
    """Stands in for `JWTVerifier.logger`: forwards every call, and turns
    FastMCP's rejection lines into a reason — raising expiry to WARNING."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @staticmethod
    def _reason(msg) -> str | None:
        text = str(msg)
        return next((r for needle, r in _LOG_REASONS if needle in text), None)

    def info(self, msg, *args, **kwargs):
        reason = self._reason(msg)
        if reason:
            _note(reason)
        if reason == "expired":
            # FastMCP files expiry as rotation noise. On a gateway forwarding
            # user tokens it is the first incident of every rollout.
            return self._inner.warning(msg, *args, **kwargs)
        return self._inner.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        reason = self._reason(msg)
        if reason:
            _note(reason)
        return self._inner.warning(msg, *args, **kwargs)


class GatewayJWTVerifier(JWTVerifier):
    """FastMCP's JWKS verifier, reporting WHY it refused a token."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.logger = _RejectionLog(self.logger)


class ObservedVerifier(TokenVerifier):
    """Count every refusal by reason, once per presented token.

    Wraps the whole verifier (shared, JWT or both) rather than living inside
    one, so a `both` gateway counts a token once — not once per verifier it
    fell through.
    """

    def __init__(self, inner: TokenVerifier) -> None:
        super().__init__(required_scopes=inner.required_scopes)
        self._inner = inner

    async def verify_token(self, token: str) -> AccessToken | None:
        slot = _SLOT.get()
        reset = None
        if slot is None:  # outside RejectionMiddleware (tests, in-process use)
            slot = {}
            reset = _SLOT.set(slot)
        slot.pop("reason", None)
        try:
            found = await self._inner.verify_token(token)
        finally:
            if reset is not None:
                _SLOT.reset(reset)
        if found is None:
            reason = slot.setdefault("reason", "invalid")
            REJECTIONS[reason] += 1
        return found


_EXPIRED_BODY = json.dumps(
    {"error": "invalid_token", "error_description": "token expired"}
).encode()
_RESOURCE_METADATA = re.compile(rb'resource_metadata="[^"]*"')


class RejectionMiddleware:
    """Pure ASGI. Opens the per-request reason slot, and rewrites the 401 for
    an EXPIRED token to say so (RFC 6750 §3.1 `error_description`).

    Only the expired case is rewritten: it is the one a client can fix on its
    own, by refreshing. Every other refusal keeps FastMCP's generic answer —
    telling a caller WHICH check its forged token failed helps nobody else.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        slot: dict = {}
        token = _SLOT.set(slot)
        replaced = False

        async def send_wrapper(message) -> None:
            nonlocal replaced
            if replaced:
                if message["type"] == "http.response.body":
                    return  # the generic body we already replaced
            elif (
                message["type"] == "http.response.start"
                and message.get("status") == 401
                and slot.get("reason") == "expired"
            ):
                challenge = b'Bearer error="invalid_token", error_description="token expired"'
                headers = []
                for name, value in message.get("headers", []):
                    if name.lower() == b"www-authenticate":
                        found = _RESOURCE_METADATA.search(value)
                        if found:
                            challenge += b", " + found.group(0)
                        continue
                    if name.lower() in (b"content-length", b"content-type"):
                        continue
                    headers.append((name, value))
                headers += [
                    (b"www-authenticate", challenge),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_EXPIRED_BODY)).encode()),
                ]
                await send({**message, "headers": headers})
                await send({"type": "http.response.body", "body": _EXPIRED_BODY})
                replaced = True
                return
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _SLOT.reset(token)


def metrics_text() -> str:
    """Prometheus text exposition of the rejection counter. Every known reason
    is emitted, zero included, so a dashboard has a series before an incident."""
    lines = [
        (
            "# HELP beherouter_auth_rejections_total Bearer tokens the gateway "
            "refused, by reason."
        ),
        "# TYPE beherouter_auth_rejections_total counter",
    ]
    for reason in sorted(set(REASONS) | set(REJECTIONS)):
        lines.append(
            f'beherouter_auth_rejections_total{{reason="{reason}"}} {REJECTIONS[reason]}'
        )
    return "\n".join(lines) + "\n"

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
    # Comma-separated means ANY of them, which is JWTVerifier's list rule.
    # Several surfaces owned by several teams can then each keep an audience
    # of their own and narrow to it with [surface.authz] audience, instead of
    # all sharing one gateway-wide name.
    audiences = [a.strip() for a in audience.split(",") if a.strip()]
    return GatewayJWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        audience=audiences if len(audiences) > 1 else audiences[0],
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


def roles_claim() -> str:
    """The configured dotted claim path for roles; empty when unset."""
    return (os.environ.get(OIDC_ROLES_CLAIM_VAR) or "").strip()
