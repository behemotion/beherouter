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
    # Role gating, from [surface.authz]. Deliberately NOT part of a mode: a
    # surface may gate without forwarding anything, and gating needs no
    # IdentitySupport from the plugin — which is why it works on stdio.
    require_roles: tuple[str, ...] = ()
    roles_claim: str = ""  # dotted path; from $BEHEROUTER_OIDC_ROLES_CLAIM

    @property
    def enabled(self) -> bool:
        """Whether this surface needs a verified caller at all.

        A role gate alone is enough: an authz-only surface still refuses a
        shared-token caller, it simply forwards nothing afterwards.
        """
        return bool(self.mode) or bool(self.require_roles)

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

    def authorise(self, req: RequestIdentity) -> CallIdentity | None:
        """Gate the caller, then materialise if this surface forwards anything.

        Returns None for an authz-only surface — which the dispatch in
        `surface.py` already handles as "no identity to apply".
        """
        if req.shared or not req.subject:
            raise AuthError(
                f"surface '{self.surface}' requires a verified user; this "
                f"caller presented the shared gateway token or none at all"
            )
        if self.require_roles:
            self._check_roles(req)
        return self.materialise(req) if self.mode else None

    def resolve(self) -> CallIdentity | None:
        """Authorise the live request. The ONLY reader of FastMCP context."""
        if not self.enabled:
            return None
        return self.authorise(request_identity(self.wanted_headers))

    def _check_roles(self, req: RequestIdentity) -> None:
        """Every role in `require_roles` must be held. ALL, not any.

        ⚠️ Carries no security weight: the backend's own verification is the
        control. This exists so a caller without access gets a sentence naming
        the surface instead of an opaque backend 401.
        """
        if not self.roles_claim:
            raise UsageError(
                f"surface '{self.surface}' requires role(s) "
                f"{list(self.require_roles)} but no roles claim is configured; "
                f"set $BEHEROUTER_OIDC_ROLES_CLAIM to the dotted path of the "
                f"claim your IdP puts them in (e.g. 'realm_access.roles')"
            )
        raw = claim_at(req.claims, self.roles_claim)
        if raw is None:
            raise AuthError(
                f"surface '{self.surface}': the caller's token carries no "
                f"{self.roles_claim!r} claim, which this surface gates on"
            )
        # A space-delimited string is what Entra and several proxies emit; a
        # list is what Keycloak emits. Anything else is a misconfigured path.
        if isinstance(raw, str):
            held = set(raw.split())
        elif isinstance(raw, (list, tuple)):
            held = {str(v) for v in raw}
        else:
            raise AuthError(
                f"surface '{self.surface}': claim {self.roles_claim!r} is "
                f"{type(raw).__name__}, not a list or a space-delimited string"
            )
        missing = [role for role in self.require_roles if role not in held]
        if missing:
            # The MISSING role names, never the held ones: what a caller needs
            # to be granted is actionable, what they already have is not, and
            # echoing a token's full role list into an error is gratuitous.
            raise AuthError(
                f"you do not have access to surface '{self.surface}': it "
                f"requires role(s) {missing}"
            )

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


def claim_at(claims: Mapping[str, Any], path: str):
    """Read a dotted claim path out of a JWT payload, or None if absent.

    Roles are not scopes: they arrive in a provider-specific claim rather than
    in `scope`, so the path is configuration rather than a constant. A
    non-mapping half way down is treated as absent, not as an error — a
    misconfigured path should refuse the caller, not crash the surface.
    """
    node: Any = claims
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node


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


class SecretMap:
    """A mounted TOML file of identity key -> {logical credential: secret}.

        ["alice@example.test"]     # the value of the surface's `key` claim
        api_key = "…"              # logical names, as the entry's map cites them

    ⚠️ READ ON THE CALL PATH, NEVER AT ATTACH. A typo'd path or a malformed file
    must not crash-loop the gateway and take /healthz with it; it degrades to
    this one surface's calls failing. `registry-lint` and `health --deep` are
    where an operator finds out early.

    Hot-reloaded by stat: one `os.stat` per resolve, and the parsed content is
    cached against (mtime_ns, size). That is the difference between rotating a
    user's credential and redeploying the gateway.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._stamp: tuple[int, int] | None = None
        self._data: dict = {}

    def _load(self) -> dict:
        try:
            st = self._path.stat()
        except OSError as e:
            # type(e).__name__, never {e}: an OSError carries the path and this
            # message reaches an agent.
            raise Unavailable(
                f"identity map '{self._path}' is unreadable: {type(e).__name__}"
            ) from e
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            try:
                data = tomllib.loads(self._path.read_text())
            except (OSError, tomllib.TOMLDecodeError) as e:
                raise Unavailable(
                    f"identity map '{self._path}' could not be parsed: "
                    f"{type(e).__name__}"
                ) from e
            self._data, self._stamp = data, stamp
        return self._data

    def credentials_for(self, surface: str, key: str, key_claim: str) -> dict[str, str]:
        entry = self._load().get(key)
        if not isinstance(entry, dict) or not entry:
            # The CLAIM NAME, not its value: this string reaches logs, and the
            # value is the caller's own identifier.
            raise AuthError(
                f"surface '{surface}': the identity map has no entry for this "
                f"caller (matched on the {key_claim!r} claim)"
            )
        return {name: str(value) for name, value in entry.items()}

    def status(self) -> dict:
        """A verdict for `health --deep`: never a key, never a value."""
        try:
            data = self._load()
        except Unavailable as e:
            state = "missing" if not self._path.exists() else "unparsable"
            return {"state": state, "error": str(e)}
        return {"state": "ok", "entries": len(data)}


# One SecretMap per path, so the stat-cache is shared by every surface reading
# the same mount rather than re-parsed per policy.
_MAPS: dict[str, SecretMap] = {}


def secret_map(path: str | Path) -> SecretMap:
    key = str(path)
    if key not in _MAPS:
        _MAPS[key] = SecretMap(Path(path))
    return _MAPS[key]


_IDENTITY_KEYS = ("mode", "header", "prefix", "key", "path", "map")


def validate_identity(surface: str, spec, raw: dict | None) -> None:
    """Validate a `[surface.identity]` table offline. No I/O, no attach.

    ⚠️ Deliberately does NOT check the gateway's auth mode. This runs at boot AND
    from `registry-lint`, and lint runs on a workstation or in an init container
    where `$BEHEROUTER_AUTH_MODE` may be absent — a default-driven refusal there
    would fail a perfectly good registry. `gateway.build_gateway_app` owns that
    check, and `registry_lint` repeats it only when the variable is set.
    """
    if not raw:
        return
    if not isinstance(raw, dict):
        raise UsageError(
            f"'{surface}': [identity] must be a table, got {type(raw).__name__}"
        )
    unknown = sorted(set(raw) - set(_IDENTITY_KEYS))
    if unknown:
        raise UsageError(
            f"'{surface}': unknown identity key(s) {unknown}; "
            f"allowed: {sorted(_IDENTITY_KEYS)}"
        )
    mode = raw.get("mode")
    if mode in (None, "", "none"):
        others = sorted(set(raw) - {"mode"})
        if others:
            raise UsageError(
                f"'{surface}': [identity] names no mode, so {others} would be "
                f"silently ignored"
            )
        return
    if mode not in MODES:
        raise UsageError(f"'{surface}': unknown identity mode {mode!r}; known: {MODES}")

    support = spec.identity
    if not support.modes:
        raise UsageError(
            f"'{surface}': plugin '{spec.name}' declares no identity support, so "
            f"it cannot be configured with mode {mode!r}. A plugin must declare "
            f"IdentitySupport before a surface can be per-user."
        )
    if spec.backing == "stdio":
        raise UsageError(
            f"'{surface}': a stdio backing cannot carry a per-request identity "
            f"— the subprocess environment is fixed at spawn and is reused "
            f"across callers (keep_alive=True). Attach it over http instead."
        )
    if support.target not in TARGETS:
        raise UsageError(
            f"plugin '{spec.name}': IdentitySupport.target must be one of "
            f"{TARGETS}, got {support.target!r}"
        )
    if support.target == "credential" and not support.accepts:
        raise UsageError(
            f"plugin '{spec.name}': target 'credential' requires 'accepts' — a "
            f"native plugin's credentials are a closed set it already declares"
        )
    if mode not in support.modes:
        raise UsageError(
            f"'{surface}': plugin '{spec.name}' supports identity mode(s) "
            f"{list(support.modes)}, not {mode!r}"
        )

    if mode == "bearer":
        if "map" in raw:
            raise UsageError(
                f"'{surface}': mode 'bearer' takes 'header' and 'prefix', not a map"
            )
        for name in ("header", "prefix"):
            if name in raw and not isinstance(raw[name], str):
                raise UsageError(f"'{surface}': identity '{name}' must be a string")
    else:
        mapping = raw.get("map")
        if not isinstance(mapping, dict) or not mapping:
            raise UsageError(
                f"'{surface}': mode {mode!r} requires a non-empty "
                f"[{surface}.identity.map] table of target key -> source name"
            )
        for key, source in mapping.items():
            if not isinstance(source, str) or not source:
                raise UsageError(
                    f"'{surface}': identity map '{key}' must name a non-empty source"
                )
            if support.accepts and key not in support.accepts:
                raise UsageError(
                    f"'{surface}': {key!r} is not an identity target of plugin "
                    f"'{spec.name}'; accepted: {sorted(support.accepts)}"
                )

    if mode == "lookup":
        key_claim = raw.get("key", "sub")
        if not isinstance(key_claim, str) or not key_claim:
            raise UsageError(f"'{surface}': identity 'key' must name a claim")
        if not (raw.get("path") or os.environ.get(DEFAULT_MAP_VAR)):
            raise UsageError(
                f"'{surface}': mode 'lookup' needs 'path' in [{surface}.identity] "
                f"or ${DEFAULT_MAP_VAR} in the gateway's environment"
            )


_AUTHZ_KEYS = ("require_roles",)


def validate_authz(surface: str, raw: dict | None) -> None:
    """Validate a `[surface.authz]` table offline. No I/O, no attach.

    Like `validate_identity`, this does NOT check the gateway's auth mode or
    whether a roles claim is configured: lint runs where the gateway's own
    environment is absent. `gateway.build_gateway_app` owns both checks and
    `registry_lint` repeats them where the variables are visible.
    """
    if not raw:
        return
    if not isinstance(raw, dict):
        raise UsageError(
            f"'{surface}': [authz] must be a table, got {type(raw).__name__}"
        )
    unknown = sorted(set(raw) - set(_AUTHZ_KEYS))
    if unknown:
        raise UsageError(
            f"'{surface}': unknown authz key(s) {unknown}; "
            f"allowed: {sorted(_AUTHZ_KEYS)}"
        )
    roles = raw.get("require_roles")
    if roles is None:
        return
    if (
        isinstance(roles, str)
        or not isinstance(roles, (list, tuple))
        or not roles
        or not all(isinstance(r, str) and r for r in roles)
    ):
        raise UsageError(
            f"'{surface}': authz require_roles must be a non-empty array of "
            f"role names, got {roles!r}"
        )


def policy_from_entry(entry, spec) -> IdentityPolicy:
    """The inert policy for one registry entry. Validate first."""
    from .auth import roles_claim

    raw = dict(entry.identity or {})
    mode = raw.get("mode") or ""
    if mode == "none":
        mode = ""
    gate = tuple((entry.authz or {}).get("require_roles") or ())
    # Read once, at attach: os.environ is not I/O, and an inert policy is what
    # lets health and lint describe a surface without a request in hand.
    claim = roles_claim()
    if not mode:
        return IdentityPolicy(
            surface=entry.name, require_roles=gate, roles_claim=claim
        )
    return IdentityPolicy(
        surface=entry.name,
        mode=mode,
        target=spec.identity.target,
        map=dict(raw.get("map") or {}),
        header=raw.get("header", "authorization"),
        prefix=raw.get("prefix", "Bearer "),
        key=raw.get("key", "sub"),
        path=raw.get("path"),
        require_roles=gate,
        roles_claim=claim,
    )


def identity_report(policy: IdentityPolicy) -> dict:
    """What `health --deep` says about a surface's identity configuration.

    ⚠️ Says nothing about whether any USER's credential works. The probe still
    authenticates with the deployment credential, so a green probe proves the
    bootstrap credential and nothing more. Only a real per-user call proves a
    per-user credential.
    """
    if not policy.enabled:
        return {"mode": "none"}
    if not policy.mode:
        # An authz-only surface: gated, but forwarding nothing.
        return {"mode": "none", "require_roles": list(policy.require_roles)}
    # `probe_scope` is the machine-readable half of the warning above: an
    # operator reading `health --deep --json` sees what the green probe covers
    # without having to have read the docs.
    report = {"mode": policy.mode, "probe_scope": "deployment-credential"}
    if policy.require_roles:
        report["require_roles"] = list(policy.require_roles)
    if policy.mode == "lookup":
        try:
            report["map"] = secret_map(policy._map_path()).status()
        except Unavailable as e:
            report["map"] = {"state": "missing", "error": str(e)}
    return report
