"""Per-request identity: who is calling, and what the backend is told about it.

TWO HALVES, DELIBERATELY SEPARATE. `IdentityPolicy` is inert configuration — it
is built from a registry entry beside the `PluginContext` and performs no I/O —
while `materialise` turns a caller into the credential material a backing
applies. Everything an executor receives is already materialised, so no backend
code parses configuration and every mode is testable without an HTTP request.

⚠️ THERE IS NO FALLBACK IN THIS MODULE. Every path either produces complete
material or raises. A surface configured per-user must never be served with the
deployment credential, and a PARTIAL header set is worse than a refusal: it
asserts an identity the caller does not have.
"""

import hashlib
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import AuthError, Unavailable, UsageError

MODES = ("bearer", "claims", "client", "lookup")
TARGETS = ("header", "env", "credential")

# Where a `lookup` surface finds its map when the entry names no path.
DEFAULT_MAP_VAR = "BEHEROUTER_IDENTITY_MAP"

# The claims that may name a caller, in order of preference. `sub` is the only
# one OIDC guarantees; the other two are what a human recognises in a log.
_SUBJECT_CLAIMS = ("sub", "preferred_username", "email")

# Which CallIdentity slot a target fills.
_SLOTS = {"header": "headers", "env": "env", "credential": "credentials"}


@dataclass(frozen=True)
class RequestIdentity:
    """What the verifier and the transport know about this caller."""

    shared: bool
    subject: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict)
    raw_token: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CallIdentity:
    """Materialised identity, ready for a backing to apply.

    There is deliberately no `kind` field: a shared-token caller never produces
    a CallIdentity at all (it is refused in `materialise`), so a "shared"
    variant would be a state no code path can reach and every consumer would
    have to pretend to handle.
    """

    subject: str
    headers: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    credentials: Mapping[str, str] = field(default_factory=dict)
    cache_key: str = ""


def material_key(subject: str, material: Mapping[str, str]) -> str:
    """A stable digest of a caller plus their material.

    Used as a cache key for per-identity objects (a calendar provider holding a
    refreshed access token). A digest rather than the material itself so that
    not even a cache key holds a credential.
    """
    h = hashlib.sha256()
    h.update(subject.encode())
    for name in sorted(material):
        h.update(b"\x00")
        h.update(name.encode())
        h.update(b"\x00")
        h.update(str(material[name]).encode())
    return h.hexdigest()


@dataclass(frozen=True)
class IdentityPolicy:
    """One surface's identity rule. Inert: built at attach, applied per call."""

    surface: str
    mode: str = ""  # "" == no per-user identity on this surface
    target: str = ""  # "header" | "env" | "credential"
    map: Mapping[str, str] = field(default_factory=dict)  # target key -> source
    header: str = "authorization"  # bearer only
    prefix: str = "Bearer "  # bearer only
    key: str = "sub"  # lookup only: the claim identifying the caller
    path: str | None = None  # lookup only: the identity map

    @property
    def enabled(self) -> bool:
        return bool(self.mode)

    @property
    def wanted_headers(self) -> tuple[str, ...]:
        """The client headers this policy reads — an ALLOW-LIST, and the only
        headers that may reach a backend. A gateway forwarding whatever a client
        sent would be a header-smuggling hole in front of every backend.
        """
        return tuple(sorted(self.map.values())) if self.mode == "client" else ()

    def materialise(self, req: RequestIdentity) -> CallIdentity:
        if req.shared or not req.subject:
            raise AuthError(
                f"surface '{self.surface}' requires a per-user identity; this "
                f"caller presented the shared gateway token or none at all"
            )
        material = self._material(req)
        slot = _SLOTS.get(self.target)
        if slot is None:
            raise UsageError(
                f"surface '{self.surface}': identity target must be one of "
                f"{TARGETS}, got {self.target!r}"
            )
        ident = CallIdentity(
            subject=req.subject, cache_key=material_key(req.subject, material)
        )
        return replace(ident, **{slot: material})

    def resolve(self) -> CallIdentity | None:
        """Materialise from the live request. The ONLY reader of FastMCP context."""
        if not self.enabled:
            return None
        return self.materialise(request_identity(self.wanted_headers))

    # --- modes ------------------------------------------------------------

    def _material(self, req: RequestIdentity) -> dict[str, str]:
        if self.mode == "bearer":
            if not req.raw_token:
                raise AuthError(
                    f"surface '{self.surface}': mode 'bearer' forwards the "
                    f"caller's own token, and this request carries none"
                )
            return {self.header: f"{self.prefix}{req.raw_token}"}
        if self.mode == "claims":
            return {key: self._claim(req, src) for key, src in self.map.items()}
        if self.mode == "client":
            return {key: self._header(req, src) for key, src in self.map.items()}
        if self.mode == "lookup":
            found = secret_map(self._map_path()).credentials_for(
                self.surface, self._claim(req, self.key), self.key
            )
            out: dict[str, str] = {}
            for key, src in self.map.items():
                if src not in found:
                    raise AuthError(
                        f"surface '{self.surface}': the identity map has no "
                        f"credential '{src}' for this caller"
                    )
                out[key] = found[src]
            return out
        raise UsageError(
            f"surface '{self.surface}': unknown identity mode {self.mode!r}"
        )

    def _claim(self, req: RequestIdentity, name: str) -> str:
        value = req.claims.get(name)
        if value is None or value == "":
            raise AuthError(
                f"surface '{self.surface}': the caller's token carries no "
                f"{name!r} claim, which this surface maps"
            )
        return str(value)

    def _header(self, req: RequestIdentity, name: str) -> str:
        value = req.headers.get(name.lower())
        if not value:
            raise AuthError(
                f"surface '{self.surface}': this call needs the client header "
                f"{name!r}, which the request does not carry"
            )
        return value

    def _map_path(self) -> str:
        path = self.path or os.environ.get(DEFAULT_MAP_VAR) or ""
        if not path:
            raise Unavailable(
                f"surface '{self.surface}': mode 'lookup' has no identity map "
                f"(set 'path' in [{self.surface}.identity] or ${DEFAULT_MAP_VAR})"
            )
        return path


def request_identity(wanted: tuple[str, ...] = ()) -> RequestIdentity:
    """Read the live request: the verified token, plus allow-listed headers.

    ⚠️ `get_http_headers(include=...)` does NOT allow-list — `include` only
    un-excludes names from FastMCP's own deny list (which contains
    `authorization`), and everything else the client sent is returned anyway. The
    filter below is therefore what makes `wanted` an allow-list.
    """
    from fastmcp.server.dependencies import get_access_token, get_http_headers

    from .auth import SHARED_CLIENT_ID

    lowered = {name.lower() for name in wanted}
    headers = (
        {k: v for k, v in get_http_headers(include=lowered).items() if k in lowered}
        if lowered
        else {}
    )
    token = get_access_token()
    if token is None:
        return RequestIdentity(shared=False, subject=None, headers=headers)
    claims = dict(getattr(token, "claims", None) or {})
    subject = next(
        (str(claims[name]) for name in _SUBJECT_CLAIMS if claims.get(name)), None
    )
    return RequestIdentity(
        shared=token.client_id == SHARED_CLIENT_ID,
        subject=subject,
        claims=claims,
        raw_token=token.token,
        headers=headers,
    )
