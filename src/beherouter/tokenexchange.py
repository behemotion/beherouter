"""OAuth 2.0 Token Exchange (RFC 8693) for identity mode `exchange`.

The gateway, authenticated as an OAuth client, trades the caller's VERIFIED
inbound token at the IdP for a token addressed to the backend, and forwards
that. Design record: docs/superpowers/specs/2026-10-07-token-exchange-design.md.

⚠️ NOTHING HERE FALLS BACK. Every path returns an issued token or raises; a
surface configured for exchange is never served with the deployment credential
or with the caller's un-exchanged token.

⚠️ NOTHING HERE LOGS OR ECHOES A TOKEN. Not the subject token, not the issued
one, not the client secret, and not the IdP's `error_description` (free text an
IdP may fill with anything). Cache keys are digests.

Imports nothing from `identity.py`, which imports this module.
"""

import asyncio
import base64
import functools
import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

import httpx

from .envexpand import PLACEHOLDER
from .errors import AuthError, AxiError, Unavailable, UsageError, tag

logger = logging.getLogger(__name__)

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPES = {
    "jwt": "urn:ietf:params:oauth:token-type:jwt",
    "access_token": "urn:ietf:params:oauth:token-type:access_token",
}
CLIENT_AUTH = ("client_secret_basic", "client_secret_post")

# The [surface.identity] keys only this mode reads.
KEYS = (
    "token_url",
    "audience",
    "resource",
    "scope",
    "subject_token_type",
    "client_auth",
    "client_id",
    "client_secret",
)

# Refused-subject codes: the CALLER's token cannot be exchanged for this target.
# Every other 4xx code describes the gateway's own client configuration.
_CALLER_CODES = ("invalid_grant", "invalid_target")
# An OAuth `error` is echoed only when it is a plain token; anything else could
# be an IdP stuffing arbitrary text (or the token) into the field.
_CODE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")

SKEW_S = 30  # an issued token is dropped this long before the IdP says it expires
MAX_ENTRIES = 1024  # per surface
TIMEOUT_S = 10.0
# Tests route the token endpoint through an httpx.MockTransport here.
TRANSPORT: httpx.AsyncBaseTransport | None = None


def _now() -> float:
    return time.monotonic()


def _strings(value) -> tuple[str, ...] | None:
    """`"x"` or `["x", "y"]` -> a tuple; None when malformed or empty."""
    if isinstance(value, str):
        return (value,) if value else None
    if (
        isinstance(value, (list, tuple))
        and value
        and all(isinstance(v, str) and v for v in value)
    ):
        return tuple(value)
    return None


@dataclass(frozen=True)
class ExchangeConfig:
    """The inert half: what to send where. Secrets stay `${VAR}` placeholders
    and are resolved per exchange, so building this does no I/O and needs no
    secret to be set."""

    token_url: str
    client_id: str
    client_secret: str  # always a ${VAR} placeholder; validated
    audience: tuple[str, ...] = ()
    resource: tuple[str, ...] = ()
    scope: str = ""
    subject_token_type: str = TOKEN_TYPES["jwt"]
    client_auth: str = "client_secret_basic"

    @classmethod
    def from_table(cls, raw: dict) -> "ExchangeConfig":
        scope = _strings(raw.get("scope")) or ()
        return cls(
            token_url=raw["token_url"],
            client_id=raw["client_id"],
            client_secret=raw["client_secret"],
            audience=_strings(raw.get("audience")) or (),
            resource=_strings(raw.get("resource")) or (),
            scope=" ".join(scope),
            subject_token_type=TOKEN_TYPES[raw.get("subject_token_type", "jwt")],
            client_auth=raw.get("client_auth", "client_secret_basic"),
        )

    def secret_state(self) -> str:
        """`set` | `unset`, for `health --deep`. Never the value."""
        m = PLACEHOLDER.match(self.client_secret)
        return "set" if m and os.environ.get(m.group(1)) else "unset"


def validate_table(surface: str, raw: dict) -> None:
    """Offline checks for a mode-`exchange` table. No I/O.

    Refusals name the KEY, never its value: the value of a misplaced
    `client_secret` is exactly what must not reach a lint log.
    """
    url = raw.get("token_url")
    parts = urlsplit(url) if isinstance(url, str) else None
    if (
        parts is None
        or parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise UsageError(
            f"'{surface}': mode 'exchange' needs 'token_url', an http(s) URL "
            f"with a host and no credentials or fragment"
        )
    for name in ("audience", "resource", "scope"):
        if name in raw and _strings(raw[name]) is None:
            raise UsageError(
                f"'{surface}': identity '{name}' must be a non-empty string or "
                f"a non-empty array of them"
            )
    if not (raw.get("audience") or raw.get("resource")):
        raise UsageError(
            f"'{surface}': mode 'exchange' needs 'audience' or 'resource' — an "
            f"exchange with no target narrows nothing (forwarding the caller's "
            f"own token is mode 'bearer')"
        )
    stt = raw.get("subject_token_type", "jwt")
    if stt not in TOKEN_TYPES:
        raise UsageError(
            f"'{surface}': identity 'subject_token_type' must be one of "
            f"{sorted(TOKEN_TYPES)}"
        )
    if raw.get("client_auth", "client_secret_basic") not in CLIENT_AUTH:
        raise UsageError(
            f"'{surface}': identity 'client_auth' must be one of {list(CLIENT_AUTH)}"
        )
    client_id = raw.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise UsageError(
            f"'{surface}': mode 'exchange' needs 'client_id', the gateway's own "
            f"OAuth client at the identity provider (a literal or a ${{VAR}})"
        )
    secret = raw.get("client_secret")
    if not isinstance(secret, str) or not PLACEHOLDER.match(secret):
        raise UsageError(
            f"'{surface}': identity 'client_secret' must be exactly one ${{VAR}} "
            f"placeholder naming a variable in the gateway's environment; an "
            f"inline secret would be committed with the registry"
        )


def _resolve(surface: str, key: str, value: str) -> str:
    """A `${VAR}` placeholder -> its value, on the CALL path.

    `Unavailable`, not `UsageError` at attach: an unset variable degrades this
    surface's calls, like an unreadable identity map, instead of crash-looping
    the gateway. The variable NAME is in the message, never a value.
    """
    m = PLACEHOLDER.match(value)
    if m is None:
        return value
    resolved = os.environ.get(m.group(1))
    if not resolved:
        raise Unavailable(
            f"surface '{surface}': token exchange '{key}' references "
            f"${{{m.group(1)}}}, which is unset or empty in the gateway's environment"
        )
    return resolved


def _code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "unrecognised"
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) and _CODE.fullmatch(code) else "unrecognised"


class TokenExchanger:
    """One surface's exchanges: a bounded LRU of issued tokens plus the
    in-flight exchanges, both keyed by a digest of the subject token."""

    def __init__(self, surface: str, config: ExchangeConfig) -> None:
        self.surface = surface
        self.config = config
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}

    def _key(self, subject_token: str) -> str:
        h = hashlib.sha256()
        h.update(self.surface.encode())
        h.update(b"\x00")
        h.update(subject_token.encode())
        return h.hexdigest()

    async def token(self, subject_token: str) -> tuple[str, bool]:
        """The issued access token for this subject token, and whether it
        came from the cache."""
        key = self._key(subject_token)
        hit = self._cache.get(key)
        if hit is not None:
            if hit[1] > _now():
                self._cache.move_to_end(key)
                return hit[0], True
            del self._cache[key]
        task = self._inflight.get(key)
        if task is None or task.get_loop() is not asyncio.get_running_loop():
            task = asyncio.ensure_future(self._exchange(subject_token, key))
            self._inflight[key] = task
            task.add_done_callback(functools.partial(self._settled, key))
        # Shielded: one waiter's cancellation must not cancel the exchange the
        # other waiters for the same caller are sharing.
        return await asyncio.shield(task), False

    def _settled(self, key: str, task: asyncio.Task) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]
        if not task.cancelled():
            task.exception()  # retrieved, so an all-cancelled wait is not a warning

    async def _exchange(self, subject_token: str, key: str) -> str:
        cfg, surface = self.config, self.surface
        client_id = _resolve(surface, "client_id", cfg.client_id)
        secret = _resolve(surface, "client_secret", cfg.client_secret)
        form: dict[str, str | list[str]] = {
            "grant_type": GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": cfg.subject_token_type,
        }
        if cfg.audience:
            form["audience"] = list(cfg.audience)
        if cfg.resource:
            form["resource"] = list(cfg.resource)
        if cfg.scope:
            form["scope"] = cfg.scope
        headers = {"accept": "application/json"}
        if cfg.client_auth == "client_secret_post":
            form["client_id"] = client_id
            form["client_secret"] = secret
        else:
            # RFC 6749 §2.3.1: each half form-urlencoded BEFORE base64. httpx's
            # BasicAuth skips that step, which breaks any secret with a colon.
            pair = f"{quote_plus(client_id)}:{quote_plus(secret)}".encode()
            headers["authorization"] = "Basic " + base64.b64encode(pair).decode()
        try:
            async with httpx.AsyncClient(transport=TRANSPORT, timeout=TIMEOUT_S) as http:
                response = await http.post(cfg.token_url, data=form, headers=headers)
        except httpx.HTTPError as e:
            # The type only: an httpx message can carry the request URL and,
            # for some errors, the request itself.
            raise Unavailable(
                f"surface '{surface}': token exchange failed: {type(e).__name__}"
            ) from None
        status = response.status_code
        if status == 429 or status >= 500:
            raise Unavailable(
                f"surface '{surface}': the token endpoint answered {status}"
            )
        if 400 <= status < 500:
            code = _code(response)
            if code in _CALLER_CODES:
                raise AuthError(
                    f"surface '{surface}': the identity provider refused to "
                    f"exchange this caller's token ({code})"
                )
            raise UsageError(
                f"surface '{surface}': the identity provider refused the token "
                f"exchange ({code}); check the surface's [identity] client and "
                f"target configuration"
            )
        if status != 200:
            raise Unavailable(
                f"surface '{surface}': the token endpoint answered {status}"
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise Unavailable(
                f"surface '{surface}': the token endpoint answered 200 without "
                f"an access_token"
            )
        token_type = body.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.lower() not in ("bearer", "n_a"):
            shown = (
                token_type
                if isinstance(token_type, str) and _CODE.fullmatch(token_type)
                else "unrecognised"
            )
            raise UsageError(
                f"surface '{surface}': the identity provider issued a "
                f"{shown!r} token, which cannot be forwarded as a bearer"
            )
        lifetime = body.get("expires_in")
        if isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool):
            ttl = lifetime - SKEW_S
            if ttl > 0:
                self._cache[key] = (token, _now() + ttl)
                self._cache.move_to_end(key)
                while len(self._cache) > MAX_ENTRIES:
                    self._cache.popitem(last=False)
        return token

    def stats(self) -> dict:
        return {"cached": len(self._cache), "in_flight": len(self._inflight)}


_EXCHANGERS: dict[str, TokenExchanger] = {}


def exchanger_for(surface: str, config: ExchangeConfig) -> TokenExchanger:
    """One exchanger per surface, replaced when its configuration changes —
    a token issued for another audience must not outlive the edit."""
    current = _EXCHANGERS.get(surface)
    if current is None or current.config != config:
        current = _EXCHANGERS[surface] = TokenExchanger(surface, config)
    return current


def reset() -> None:
    """Drop every exchanger and cached token. Tests, and nothing else."""
    _EXCHANGERS.clear()


async def exchanged_headers(
    surface: str, config: ExchangeConfig, subject_token: str, header: str, prefix: str,
    subject: str = "",
) -> dict[str, str]:
    """The header set an exchanged call carries. Logs names only."""
    try:
        token, cached = await exchanger_for(surface, config).token(subject_token)
    except AuthError as e:
        raise tag(e, "unauthenticated") from None
    except AxiError as e:
        raise tag(e, "identity_unavailable") from None
    logger.info(
        "token exchange surface=%s subject=%s header=%s source=%s",
        surface,
        subject,
        header,
        "cache" if cached else "idp",
    )
    return {header: f"{prefix}{token}"}
