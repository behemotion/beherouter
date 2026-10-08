"""Per-user calendar consent: one person's OAuth grant -> their identity-map entry.

`beherouter calendar-consent <surface> --subject <claim value>` runs the
provider's authorization-code flow with PKCE against a LOOPBACK redirect on the
machine it runs on, exchanges the code, and writes the resulting refresh token
into the identity map under the logical name the surface's `lookup` mode reads.
`--revoke` removes it again. Design and the rejected alternative (an HTTP
consent route on the gateway): docs/superpowers/specs/2026-10-07-calendar-consent-design.md.

What this module deliberately is NOT:

- **Not on the gateway.** Nothing here is imported by `serve`; the gateway's
  network posture (bearer-auth behind a proxy, no login UI, no session state)
  is unchanged, and attach still does no identity work. The gateway sees a
  grant only by re-reading the map on the call path, which it already does
  by stat.
- **Not a token printer.** A refresh token is a long-lived credential; it goes
  from the token endpoint straight into a 0600 file and is never emitted,
  logged or interpolated into an error. Neither is the authorization code: the
  loopback handler's access log is silenced because it would print `?code=`.

The provider traps are inherited, not re-derived:

- Google issues a refresh token only with `access_type=offline` AND
  `prompt=consent`; both are always sent. A consent screen left in *Testing*
  expires the token after 7 days — undetectable from here, so it is a warning
  on every Google grant.
- Microsoft's authority is `consumers`, never `common`: both URLs come from
  `providers/microsoft.py`, where `test_token_url_uses_the_consumers_authority`
  holds them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import html
import os
import secrets
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import tomlkit

from ...errors import AuthError, NotFound, Unavailable, UsageError
from .providers import google, microsoft

DEFAULT_PORT = 8765  # the redirect URI docs/CALENDAR-BOOTSTRAP.md registers
DEFAULT_TIMEOUT_S = 300


@dataclass(frozen=True)
class ConsentProvider:
    """Everything provider-specific about the consent flow, as inert data."""

    name: str
    authorize_url: str
    token_url: str
    scope: str
    # Sent on the authorize request. Google's two are the refresh-token switch.
    authorize_params: tuple[tuple[str, str], ...] = ()
    needs_client_secret: bool = False
    # Microsoft wants the scope repeated on the code exchange; Google does not.
    scope_on_exchange: bool = False
    revoke_url: str | None = None
    revoke_hint: str = ""
    no_refresh_hint: str = ""
    warnings: tuple[str, ...] = ()


GOOGLE = ConsentProvider(
    name="google",
    authorize_url=google.AUTHORIZE_URL,
    token_url=google.TOKEN_URL,
    scope=google.SCOPE,
    # ⚠️ BOTH are required. Without them Google returns an access token and
    # NO refresh token, and the omission is silent.
    authorize_params=(("access_type", "offline"), ("prompt", "consent")),
    needs_client_secret=True,
    revoke_url=google.REVOKE_URL,
    no_refresh_hint=(
        "the request carried access_type=offline and prompt=consent, so check "
        "that the OAuth client is a Desktop-app client"
    ),
    warnings=(
        (
            "Google expires this refresh token after 7 days unless the OAuth consent "
            "screen is published to Production; nothing here can detect which "
            "(docs/CALENDAR-BOOTSTRAP.md)."
        ),
    ),
)

MICROSOFT = ConsentProvider(
    name="microsoft",
    # ⚠️ `consumers`, never `common` — both derived in providers/microsoft.py.
    authorize_url=microsoft.AUTHORIZE_URL,
    token_url=microsoft.TOKEN_URL,
    scope=microsoft.SCOPE,
    scope_on_exchange=True,
    revoke_hint=(
        "Microsoft personal accounts have no token-revocation endpoint; the "
        "user removes the app's access at https://account.live.com/consent/Manage"
    ),
    no_refresh_hint="the scope must include offline_access",
    warnings=(
        (
            "An hour of working calls proves nothing for Microsoft: re-run "
            "`health --deep --bearer-file` as this user after the first token refresh."
        ),
    ),
)

# Plugin name -> its consent flow. A plugin absent here has no consent flow.
PROVIDERS: dict[str, ConsentProvider] = {"gcal": GOOGLE, "m365": MICROSOFT}

# The logical credential names a calendar grant can populate, in write order.
_GRANT_NAMES = ("refresh_token", "client_id", "client_secret")


# --- what a surface reads -----------------------------------------------------


@dataclass(frozen=True)
class ConsentTarget:
    surface: str
    plugin: str
    provider: ConsentProvider
    map_path: Path
    key_claim: str
    # Logical credential (refresh_token, client_id, ...) -> the name it has in
    # the identity map, exactly as the entry's [surface.identity.map] cites it.
    names: dict[str, str]


def resolve_target(entry, identity_map: str = "") -> ConsentTarget:
    """Read, from the registry entry alone, where a grant must land.

    Refuses a surface that is not per-user: writing a token that no `lookup`
    reads would be a grant that silently does nothing — or worse, one an
    operator believes is in use while every call goes out as the deployment.
    """
    from ...identity import DEFAULT_MAP_VAR

    provider = PROVIDERS.get(entry.plugin)
    if provider is None:
        raise UsageError(
            f"surface '{entry.name}' uses plugin '{entry.plugin}'; calendar-consent "
            f"serves plugins {sorted(PROVIDERS)}"
        )
    identity = dict(entry.identity or {})
    if identity.get("mode") != "lookup":
        raise UsageError(
            f"surface '{entry.name}' is not per-user: it needs [{entry.name}.identity] "
            f'mode = "lookup", or a grant written here is read by nothing'
        )
    mapping = dict(identity.get("map") or {})
    if "refresh_token" not in mapping:
        raise UsageError(
            f"surface '{entry.name}': [{entry.name}.identity.map] does not map "
            f"'refresh_token', so a consent grant would never reach the provider"
        )
    raw_path = identity_map or identity.get("path") or os.environ.get(DEFAULT_MAP_VAR) or ""
    if not raw_path:
        raise UsageError(
            f"surface '{entry.name}': no identity map to write; pass --identity-map, "
            f"or set 'path' in [{entry.name}.identity] or ${DEFAULT_MAP_VAR}"
        )
    return ConsentTarget(
        surface=entry.name,
        plugin=entry.plugin,
        provider=provider,
        map_path=Path(raw_path),
        key_claim=identity.get("key", "sub"),
        names={k: mapping[k] for k in _GRANT_NAMES if k in mapping},
    )


# --- PKCE and the authorize URL -----------------------------------------------


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) per RFC 7636: 86 unreserved characters."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(
    provider: ConsentProvider,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    challenge: str,
    login_hint: str | None = None,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider.scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        **dict(provider.authorize_params),
    }
    if login_hint:
        # Steers the browser to the right account. Not a control — the user can
        # still pick another — but it makes the wrong-account mistake harder.
        params["login_hint"] = login_hint
    return f"{provider.authorize_url}?{urlencode(params)}"


# --- the loopback redirect ----------------------------------------------------


class LoopbackReceiver:
    """A one-shot HTTP listener on 127.0.0.1 for the provider's redirect.

    Bound in the constructor, BEFORE the browser is sent anywhere, so the
    redirect cannot race the listener. Serves `/` only; anything else (a
    browser's favicon fetch) is a 404 and is not mistaken for the redirect.
    """

    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
        received: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                if parts.path != "/" or not ({"code", "error"} & set(query)):
                    self.send_error(404)
                    return
                received.update(query)
                ok = "code" in query
                body = (
                    "Consent received. You can close this tab."
                    if ok
                    else f"Consent was not granted: {html.escape(query.get('error', ''))}"
                )
                payload = f"<!doctype html><title>beherouter</title><p>{body}</p>".encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                # ⚠️ The default access log prints the request line — which is
                # `GET /?code=...` — to stderr. The code is a credential until
                # it is exchanged.
                return

        try:
            self._server = HTTPServer((host, port), Handler)
        except OSError as e:
            raise UsageError(
                f"cannot listen on {host}:{port} for the consent redirect "
                f"({type(e).__name__}); pass --port with a free port your OAuth "
                f"client allows"
            ) from e
        self._received = received

    @property
    def redirect_uri(self) -> str:
        # `localhost`, not 127.0.0.1: it is what the bootstrap registers with
        # Microsoft, and Google accepts either for a Desktop-app client.
        return f"http://localhost:{self._server.server_port}"

    def wait(self, timeout: float) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        try:
            while not self._received:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Unavailable(
                        f"no consent redirect arrived within {int(timeout)} s; "
                        f"re-run, or raise --timeout"
                    )
                self._server.timeout = min(remaining, 1.0)
                self._server.handle_request()
            return dict(self._received)
        finally:
            self.close()

    def close(self) -> None:
        """Idempotent: `wait` closes on the way out, `grant` again on failure."""
        self._server.server_close()


# --- the code exchange --------------------------------------------------------


async def exchange_code(
    provider: ConsentProvider,
    *,
    code: str,
    verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str | None,
    http: httpx.AsyncClient,
) -> str:
    """Authorization code -> refresh token. Never echoes the request or response."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret
    if provider.scope_on_exchange:
        form["scope"] = provider.scope
    try:
        resp = await http.post(provider.token_url, data=form)
    except httpx.HTTPError as e:
        # The exception carries its request, whose form holds the code and the
        # client secret.
        raise Unavailable(f"token endpoint unreachable: {type(e).__name__}") from e
    if resp.status_code in (400, 401, 403):
        try:
            err = resp.json().get("error", "unknown_error")
        except (ValueError, AttributeError):
            err = "unknown_error"
        raise AuthError(f"code exchange rejected ({resp.status_code}): {err}")
    if resp.status_code >= 400:
        raise Unavailable(f"token endpoint returned {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise Unavailable("token endpoint did not return JSON") from e
    token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not token or not isinstance(token, str):
        raise AuthError(f"{provider.name} issued no refresh token: {provider.no_refresh_hint}")
    return token


async def obtain_refresh_token(
    provider: ConsentProvider,
    *,
    client_id: str,
    client_secret: str | None,
    receiver,
    open_url: Callable[[str], None],
    http: httpx.AsyncClient,
    # The operator's --timeout for a human in a browser, enforced by the
    # blocking receiver thread; asyncio.timeout would not interrupt that wait.
    timeout: float = DEFAULT_TIMEOUT_S,  # noqa: ASYNC109
    login_hint: str | None = None,
) -> str:
    """The whole browser round-trip. `receiver` is a bound LoopbackReceiver."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    redirect_uri = receiver.redirect_uri
    open_url(
        authorize_url(
            provider,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            challenge=challenge,
            login_hint=login_hint,
        )
    )
    params = await asyncio.to_thread(receiver.wait, timeout)
    if not secrets.compare_digest(params.get("state", ""), state):
        # A redirect we did not start: a stale tab, or someone else's request.
        raise AuthError("the consent redirect carried the wrong state; re-run")
    if "error" in params:
        raise AuthError(f"consent was not granted: {params['error']}")
    return await exchange_code(
        provider,
        code=params["code"],
        verifier=verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
        http=http,
    )


# --- the identity map writer --------------------------------------------------


@contextlib.contextmanager
def _locked(path: Path):
    """Serialise read-modify-write of one map across concurrent consent runs.

    A sidecar lock file, because the map itself is REPLACED (a new inode) by
    every write, so a lock on it would guard a file that no longer exists.
    """
    import fcntl

    lock = path.parent / f".{path.name}.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _check_writable(path: Path) -> None:
    if not path.parent.is_dir():
        raise UsageError(f"identity map directory '{path.parent}' does not exist")
    if path.is_symlink():
        # A Kubernetes Secret or ConfigMap projection is a symlink into a
        # read-only `..data` directory; replacing it would sever the projection
        # (and fail anyway). The source of truth is elsewhere.
        raise UsageError(
            f"identity map '{path}' is a symlink, likely a projected Secret; write "
            f"the grant to a working copy with --identity-map and ship it through "
            f"the Secret's own source"
        )


def _read_doc(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text())
    except Exception as e:  # tomlkit raises its own ParseError family
        raise UsageError(
            f"identity map '{path}' is not valid TOML ({type(e).__name__}); refusing to rewrite it"
        ) from e


def _atomic_write(path: Path, text: str) -> None:
    """Write-then-rename, so the gateway's stat-reload never sees half a file.

    The temporary file is created 0600 (mkstemp) and stays so: the map holds
    every user's refresh token. When root rewrites a map another user owns —
    the gateway's UID — ownership is carried over, or the gateway could no
    longer read its own map.
    """
    previous = path.stat() if path.exists() else None
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        if previous is not None and os.geteuid() == 0:
            os.fchown(fd, previous.st_uid, previous.st_gid)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def upsert_subject(path: Path, subject: str, values: Mapping[str, str]) -> None:
    """Set `values` in the subject's entry, keeping every other name and entry.

    Other names matter: one person's entry can carry a Google and a Microsoft
    token side by side, under the names two surfaces' maps cite.
    """
    _check_writable(path)
    with _locked(path):
        doc = _read_doc(path)
        existing = doc.get(subject)
        table: MutableMapping
        if existing is None:
            table = tomlkit.table()
            doc[subject] = table
        elif isinstance(existing, MutableMapping):
            table = existing
        else:
            raise UsageError(f"identity map '{path}': the entry for this subject is not a table")
        for name, value in values.items():
            table[name] = value
        _atomic_write(path, tomlkit.dumps(doc))


def read_subject(path: Path, subject: str) -> dict[str, str]:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise UsageError(f"identity map '{path}' is unreadable ({type(e).__name__})") from e
    entry = data.get(subject)
    return {k: str(v) for k, v in entry.items()} if isinstance(entry, dict) else {}


def remove_names(path: Path, subject: str, names: list[str]) -> list[str]:
    """Drop `names` from the subject's entry; drop the entry once it is empty."""
    _check_writable(path)
    with _locked(path):
        doc = _read_doc(path)
        table = doc.get(subject)
        if not isinstance(table, MutableMapping):
            raise NotFound(f"identity map '{path}' has no entry for this subject")
        removed = [n for n in names if n in table]
        for name in removed:
            del table[name]
        if not table:
            del doc[subject]
        if removed:
            _atomic_write(path, tomlkit.dumps(doc))
        return removed


# --- client credentials -------------------------------------------------------


def _read_secret_file(path: str) -> str:
    """A secret from a file (`-` = stdin), never from argv, which `ps` shows."""
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    value = raw.strip()
    if not value:
        raise UsageError(f"--client-secret-file {path!r} is empty")
    return value


def _from_entry(entry, name: str, flag: str) -> str:
    """The entry's own `env` value for `name`, resolving its ${VAR}."""
    from ...envexpand import expand

    raw = (entry.env or {}).get(name)
    if not raw:
        raise UsageError(f"surface '{entry.name}' has no '{name}' in its env; pass {flag}")
    try:
        return expand(entry.name, {name: raw})[name]
    except UsageError as e:
        raise UsageError(f"{e}; or pass {flag}") from None


def client_credentials(
    entry, provider: ConsentProvider, client_id: str = "", client_secret_file: str = ""
) -> tuple[str, str | None]:
    """The OAuth client the grant is issued to — the one the surface refreshes with.

    A refresh token is bound to its client. Consenting with any other client id
    yields a token the surface's refresh grant rejects as `invalid_grant`.
    """
    cid = client_id or _from_entry(entry, "client_id", "--client-id")
    secret = None
    if provider.needs_client_secret:
        secret = (
            _read_secret_file(client_secret_file)
            if client_secret_file
            else _from_entry(entry, "client_secret", "--client-secret-file")
        )
    elif client_secret_file:
        raise UsageError(
            f"plugin '{entry.plugin}' is a public client with no client secret; "
            f"drop --client-secret-file"
        )
    return cid, secret


# --- the two operations -------------------------------------------------------


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _opener(no_browser: bool, say: Callable[[str], None]) -> Callable[[str], None]:
    def open_url(url: str) -> None:
        # stderr, never stdout: stdout carries the command's JSON result.
        say("Open this URL in a browser on THIS machine and sign in as the subject:")
        say(url)
        if not no_browser:
            import webbrowser

            webbrowser.open(url)

    return open_url


async def grant(
    entry,
    *,
    subject: str,
    identity_map: str = "",
    client_id: str = "",
    client_secret_file: str = "",
    port: int = DEFAULT_PORT,
    no_browser: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,  # noqa: ASYNC109  forwarded to the receiver thread
    http: httpx.AsyncClient | None = None,
    receiver=None,
    open_url: Callable[[str], None] | None = None,
    say: Callable[[str], None] = _stderr,
) -> dict:
    if not subject:
        raise UsageError("--subject is required: the caller's identity-map key")
    target = resolve_target(entry, identity_map)
    _check_writable(target.map_path)  # before the browser, not after it
    cid, secret = client_credentials(entry, target.provider, client_id, client_secret_file)
    receiver = receiver or LoopbackReceiver(port)
    open_url = open_url or _opener(no_browser, say)
    owned = http is None
    http = http or httpx.AsyncClient(timeout=30.0)
    try:
        refresh_token = await obtain_refresh_token(
            target.provider,
            client_id=cid,
            client_secret=secret,
            receiver=receiver,
            open_url=open_url,
            http=http,
            timeout=timeout,
            login_hint=subject if "@" in subject else None,
        )
    finally:
        close = getattr(receiver, "close", None)
        if close is not None:
            close()
        if owned:
            await http.aclose()
    # The client the token is bound to travels with it whenever the surface
    # reads the client per user too; otherwise the deployment's is used.
    granted = {"refresh_token": refresh_token, "client_id": cid}
    if secret:
        granted["client_secret"] = secret
    values = {target.names[name]: granted[name] for name in target.names if name in granted}
    upsert_subject(target.map_path, subject, values)
    return {
        "granted": True,
        "surface": target.surface,
        "plugin": target.plugin,
        "subject": subject,
        "key_claim": target.key_claim,
        "identity_map": str(target.map_path),
        # NAMES only — never a value.
        "written": sorted(values),
        "warnings": list(target.provider.warnings),
        "verify": (
            f"beherouter health --deep --surface {target.surface} --bearer-file - "
            f"(with this subject's token on stdin)"
        ),
    }


async def revoke(
    entry,
    *,
    subject: str,
    identity_map: str = "",
    http: httpx.AsyncClient | None = None,
) -> dict:
    """Remove the subject's grant from the map, and revoke it upstream where possible."""
    if not subject:
        raise UsageError("--subject is required: the caller's identity-map key")
    target = resolve_target(entry, identity_map)
    _check_writable(target.map_path)
    current = read_subject(target.map_path, subject)
    refresh_name = target.names["refresh_token"]
    if refresh_name not in current:
        raise NotFound(
            f"identity map '{target.map_path}' holds no '{refresh_name}' for this subject"
        )
    upstream = await _revoke_upstream(target.provider, current[refresh_name], http)
    # Only the refresh token: a client id/secret under this subject may be
    # shared with another surface's grant, and is useless without a token.
    removed = remove_names(target.map_path, subject, [refresh_name])
    out = {
        "revoked": True,
        "surface": target.surface,
        "subject": subject,
        "identity_map": str(target.map_path),
        "removed": removed,
        "upstream": upstream,
    }
    if target.provider.revoke_hint:
        out["hint"] = target.provider.revoke_hint
    return out


async def _revoke_upstream(
    provider: ConsentProvider, token: str, http: httpx.AsyncClient | None
) -> str:
    """Best effort: the local removal happens whatever this returns."""
    if provider.revoke_url is None:
        return "not_supported"
    owned = http is None
    http = http or httpx.AsyncClient(timeout=30.0)
    try:
        resp = await http.post(provider.revoke_url, data={"token": token})
    except httpx.HTTPError as e:
        return f"unreachable: {type(e).__name__}"
    finally:
        if owned:
            await http.aclose()
    # Google answers 400 invalid_token for a grant that is already gone, which
    # is the outcome revoke wants.
    if resp.status_code == 200:
        return "revoked"
    if resp.status_code == 400:
        return "already_invalid"
    return f"failed: {resp.status_code}"


def run(
    registry: Mapping,
    surface: str,
    *,
    subject: str,
    identity_map: str = "",
    client_id: str = "",
    client_secret_file: str = "",
    port: int = DEFAULT_PORT,
    no_browser: bool = False,
    revoke_grant: bool = False,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> dict:
    """The `calendar-consent` verb, minus argument parsing."""
    entry = registry.get(surface)
    if entry is None:
        raise NotFound(f"no attached tool '{surface}'")
    if revoke_grant:
        if client_id or client_secret_file:
            raise UsageError("--revoke takes no client credentials")
        return asyncio.run(revoke(entry, subject=subject, identity_map=identity_map))
    if timeout <= 0:
        raise UsageError("--timeout must be a positive number of seconds")
    return asyncio.run(
        grant(
            entry,
            subject=subject,
            identity_map=identity_map,
            client_id=client_id,
            client_secret_file=client_secret_file,
            port=port,
            no_browser=no_browser,
            timeout=timeout,
        )
    )
