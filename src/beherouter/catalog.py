"""`catalog-import` / `catalog-export`: the MCP Registry's `server.json` path.

`server.json` (schema 2025-12-11) says how to REACH or LAUNCH a server and
which secrets it needs: `remotes[]` (URL, headers) and `packages[]`
(`registryType`, `transport`, `environmentVariables`). It carries no tool list,
no pins and no probe, so it is an import SOURCE for the generic plugins
(`mcp-http`, `mcp-stdio`), never a plugin of its own.

Import emits fragments, like `plugin-config` — never a credential, only the
`${VAR}` placeholder and the vault line:

- ⚠️ `pinned` and `probe` come out as TODO placeholders that FAIL
  `registry-lint` (`requires_entry` refuses an empty value), so an imported
  entry cannot be committed until someone chose them. That is the
  `gitea-home` rule — a surface that only lists tools proves nothing —
  surviving the import path.
- A server that publishes our `_meta` block (below) gets those filled in
  instead. The block is ADVISORY: it becomes registry-entry overrides an
  operator reviews, never trusted spec data, and its `identity` modes are
  honoured only where the target plugin already declares them.
- ⚠️ A package is pinned to its EXACT version, never `latest`: an unpinned
  `npx` is a different server on every restart, behind a probe that was
  chosen for the old one.
- ⚠️ A generic plugin carries ONE credential (`api_key`). A server declaring
  more is imported with the first required one and a warning naming every
  other — never a silent drop, because a missing second secret attaches green
  and fails on the first call that needs it.

The publisher block, under the registry's reserved publisher key:

    "_meta": {"io.modelcontextprotocol.registry/publisher-provided": {
        "io.beherouter/plugin": {"v": 1, "pinned": [...], "probe": "...",
            "probe_args": {...}, "search_aliases": {...},
            "identity": {"modes": [...], "target": "header"}}}}

Export writes the reverse for a curated plugin, so its pins and probe can be
listed in a private subregistry intact.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .errors import NotFound, Unavailable, UsageError

SCHEMA_VERSION = "2025-12-11"
SCHEMA_URL = f"https://static.modelcontextprotocol.io/schemas/{SCHEMA_VERSION}/server.schema.json"
PUBLISHER_KEY = "io.modelcontextprotocol.registry/publisher-provided"
PLUGIN_KEY = "io.beherouter/plugin"
META_VERSION = 1
DEFAULT_REGISTRY = "https://registry.modelcontextprotocol.io"

# The registry's server name: reverse-DNS namespace, a slash, a name. An
# optional `@version` selects one release instead of the latest.
_REGISTRY_NAME = re.compile(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+(@[A-Za-z0-9._+-]+)?$")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
# server.json caps `description` at 100 characters.
_DESCRIPTION_MAX = 100
_RUNTIMES = {"npm": "npx", "pypi": "uvx"}


# --- reading a source -------------------------------------------------------


def _fetch(url: str, transport=None) -> dict:
    import httpx

    try:
        with httpx.Client(transport=transport, timeout=30, follow_redirects=True) as client:
            response = client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise Unavailable(f"could not fetch {url}: {type(e).__name__}") from None
    if response.status_code == 404:
        raise NotFound(f"{url} answered 404")
    if response.status_code >= 400:
        raise Unavailable(f"{url} answered HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError:
        raise UsageError(f"{url} did not return JSON") from None


def load_source(source: str, registry: str = DEFAULT_REGISTRY, transport=None) -> tuple[dict, str]:
    """(server.json object, where it came from) for a file, URL or registry name.

    `transport` is an httpx transport, injectable so tests never touch a
    network. Accepts a bare server.json or the registry API's
    `{"server": ..., "_meta": ...}` wrapper (whose outer `_meta` is the
    registry's own bookkeeping, not the publisher's, and is ignored).
    """
    if source.startswith(("http://", "https://")):
        data, origin = _fetch(source, transport), source
    elif Path(source).is_file():
        try:
            data = json.loads(Path(source).read_text())
        except ValueError as e:  # JSONDecodeError, or UnicodeDecodeError on read
            why = e.msg if isinstance(e, json.JSONDecodeError) else str(e)
            raise UsageError(f"{source}: not JSON ({why})") from None
        origin = str(source)
    elif _REGISTRY_NAME.match(source):
        name, _, version = source.partition("@")
        url = (
            f"{registry.rstrip('/')}/v0/servers/{quote(name, safe='')}"
            f"/versions/{quote(version or 'latest', safe='')}"
        )
        try:
            data = _fetch(url, transport)
        except NotFound:
            raise NotFound(
                f"no server '{source}' in the registry at {registry}"
            ) from None
        origin = f"{name}@{version or 'latest'} from {registry}"
    else:
        raise NotFound(
            f"'{source}' is not a file, an http(s) URL or a registry name "
            f"(namespace/name, e.g. io.github.owner/server)"
        )
    if isinstance(data, dict) and isinstance(data.get("server"), dict):
        data = data["server"]
    if not isinstance(data, dict) or not data.get("name"):
        raise UsageError(f"{origin}: not a server.json (no `name`)")
    return data, origin


# --- TOML rendering ---------------------------------------------------------


def _toml(value) -> str:
    """One TOML value, inline. JSON string escapes are valid TOML basic strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_toml(v)}" for k, v in value.items()) + " }"
    raise UsageError(f"cannot render {type(value).__name__} as TOML")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k)


# --- the publisher block ----------------------------------------------------


def _publisher_block(server: dict, warnings: list[str]) -> dict:
    """The validated `io.beherouter/plugin` block, or {} — each bad key dropped
    WITH a warning, so a typo in a publisher's pins degrades to a TODO rather
    than to a pin list nobody chose."""
    block = ((server.get("_meta") or {}).get(PUBLISHER_KEY) or {}).get(PLUGIN_KEY)
    if block is None:
        return {}
    if not isinstance(block, dict):
        warnings.append(f"_meta '{PLUGIN_KEY}' is not an object; ignored")
        return {}
    if block.get("v") != META_VERSION:
        warnings.append(
            f"_meta '{PLUGIN_KEY}' is version {block.get('v')!r}; this importer "
            f"reads v{META_VERSION}, so the block is ignored"
        )
        return {}
    out: dict = {}
    checks = {
        "pinned": lambda v: isinstance(v, list) and v and all(isinstance(x, str) for x in v),
        "probe": lambda v: isinstance(v, str) and v,
        "probe_args": lambda v: isinstance(v, dict),
        "search_aliases": lambda v: isinstance(v, dict) and all(
            isinstance(w, list) and all(isinstance(x, str) for x in w) for w in v.values()
        ),
        "identity": lambda v: isinstance(v, dict) and isinstance(v.get("modes", []), list),
    }
    for key, ok in checks.items():
        if key not in block:
            continue
        if ok(block[key]):
            out[key] = block[key]
        else:
            warnings.append(f"_meta '{PLUGIN_KEY}': `{key}` is malformed; ignored")
    if "probe_args" in out and "probe" not in out:
        warnings.append(f"_meta '{PLUGIN_KEY}': `probe_args` without `probe`; ignored")
        del out["probe_args"]
    return out


# --- choosing what to import ------------------------------------------------


def _choose(server: dict, warnings: list[str]) -> tuple[str, Any]:
    """("remote", remote) or ("package", package). A streamable-http remote
    wins: it needs nothing in the image, where a package needs its runtime."""
    remotes = server.get("remotes") or []
    packages = server.get("packages") or []
    remote = next((r for r in remotes if r.get("type") == "streamable-http"), None)
    package = next(
        (
            p for p in packages
            if p.get("registryType") in _RUNTIMES
            and (p.get("transport") or {}).get("type") == "stdio"
        ),
        None,
    )
    if remote is None and package is None:
        raise UsageError(
            f"'{server.get('name')}' declares no streamable-http remote and no "
            f"npm/pypi stdio package; nothing here can be attached"
        )
    chosen = ("remote", remote) if remote is not None else ("package", package)
    warnings.extend(
        f"remote {r.get('type')} {r.get('url')} not imported: "
        + ("only one source becomes the entry" if r.get("type") == "streamable-http"
           else "mcp-http speaks streamable HTTP only")
        for r in remotes
        if r is not remote
    )
    for p in packages:
        if p is chosen[1]:
            continue
        kind = p.get("registryType")
        transport = (p.get("transport") or {}).get("type")
        if kind not in _RUNTIMES or transport != "stdio":
            why = (f"registryType '{kind}' over '{transport}' is not importable "
                   f"(npm or pypi over stdio are)")
        elif chosen[0] == "remote":
            why = "the streamable-http remote was preferred"
        else:
            why = "only one source becomes the entry"
        warnings.append(f"package {kind}:{p.get('identifier')} not imported: {why}")
    return chosen


def _secret_choice(items: list[dict], what: str, plugin: str, warnings: list[str]) -> dict | None:
    """The one secret a generic plugin can carry: the first required, else the
    first. Every other secret is NAMED in a warning."""
    secrets = [i for i in items if i.get("isSecret")]
    if not secrets:
        return None
    chosen = next((s for s in secrets if s.get("isRequired")), secrets[0])
    warnings.extend(
        f"{what} '{s.get('name')}' is a secret {plugin} cannot carry: it "
        f"takes one credential (`api_key`), given to '{chosen.get('name')}'. "
        f"Write a curated plugin, or front the server with something that "
        f"supplies '{s.get('name')}'"
        + ("" if s.get("isRequired") else " (the server marks it optional)")
        for s in secrets
        if s is not chosen
    )
    return chosen


# --- import -----------------------------------------------------------------


def _from_remote(surface: str, remote: dict, warnings: list[str]):
    url = remote.get("url") or ""
    variables = remote.get("variables") or {}

    def fill(m):
        default = (variables.get(m.group(1)) or {}).get("default")
        if default is None:
            warnings.append(
                f"url variable '{{{m.group(1)}}}' has no default; replace it in "
                f"[{surface}.config] url"
            )
            return m.group(0)
        return str(default)

    url = _PLACEHOLDER.sub(fill, url)
    headers = remote.get("headers") or []
    secret = _secret_choice(headers, "header", "mcp-http", warnings)
    warnings.extend(
        f"header '{h.get('name')}' is not carried: mcp-http sends only "
        f"its credential header"
        + (f" (the server wants it set to {h['value']!r})" if h.get("value") else "")
        for h in headers
        if not h.get("isSecret")
    )
    config: dict = {"url": url}
    if secret is not None:
        name = secret.get("name") or "authorization"
        value = secret.get("value") or ""
        found = _PLACEHOLDER.search(value)
        if found:
            prefix = value[: found.start()]
            if value[found.end():]:
                warnings.append(
                    f"header '{name}' value {value!r} has text after its "
                    f"placeholder; mcp-http sends only `auth_prefix` + api_key"
                )
        elif value:
            prefix = ""
            warnings.append(
                f"header '{name}' has a fixed value and no placeholder; check "
                f"auth_prefix before attaching"
            )
        else:
            # No template: an Authorization header conventionally carries a
            # bearer (GitHub's remote publishes exactly this shape); any
            # other header carries the bare key.
            prefix = "Bearer " if name.lower() == "authorization" else ""
        config["auth_header"] = name.lower()
        config["auth_prefix"] = prefix
    return "mcp-http", config, secret, None


def _argument_tokens(args: list[dict] | None, where: str, warnings: list[str]) -> list[str]:
    tokens: list[str] = []
    for arg in args or []:
        label = arg.get("name") or arg.get("valueHint") or arg.get("value") or "?"
        value = arg.get("value", arg.get("default"))
        if isinstance(value, str) and _PLACEHOLDER.search(value):
            variables = arg.get("variables") or {}
            unresolved: list[tuple[str, bool]] = []

            def fill(m, variables=variables, unresolved=unresolved):
                var = variables.get(m.group(1)) or {}
                if var.get("isSecret") or var.get("default") is None:
                    unresolved.append((m.group(1), bool(var.get("isSecret"))))
                    return m.group(0)
                return str(var["default"])

            value = _PLACEHOLDER.sub(fill, value)
            if unresolved:
                secret = any(s for _, s in unresolved)
                warnings.append(
                    f"{where} argument '{label}' needs "
                    + ", ".join(f"{{{v}}}" for v, _ in unresolved)
                    + (" — a SECRET in argv, which mcp-stdio cannot supply (argv is "
                       "visible to every process on the host); not imported"
                       if secret else " and has no default; not imported, add it to `cmd`")
                )
                continue
        if arg.get("type") == "named":
            tokens.append(arg.get("name") or "")
            if value is not None:
                tokens.append(str(value))
        elif value is not None:
            tokens.append(str(value))
        else:
            warnings.append(
                f"{where} argument '{label}' has no value or default; not imported, "
                f"add it to `cmd`"
            )
    return [t for t in tokens if t]


def _from_package(surface: str, package: dict, warnings: list[str]):
    kind, ident = package.get("registryType"), package.get("identifier") or ""
    version = str(package.get("version") or "")
    # Exact versions only. `latest`, an empty one or a range would make the
    # cmd a different server on every restart, behind a probe chosen for this one.
    if not version or version == "latest" or re.search(r"[\^~<>=*| ]", version):
        raise UsageError(
            f"package {kind}:{ident} has version {version!r}; import needs an "
            f"exact version, never `latest` or a range"
        )
    runtime = _RUNTIMES[str(kind)]  # `_choose` admits only _RUNTIMES kinds
    hint = package.get("runtimeHint")
    if hint and hint != runtime:
        warnings.append(f"package runtimeHint is '{hint}'; the cmd uses '{runtime}'")
    if package.get("registryBaseUrl") and package["registryBaseUrl"] not in (
        "https://registry.npmjs.org", "https://pypi.org",
    ):
        warnings.append(
            f"package comes from {package['registryBaseUrl']}; point {runtime} at "
            f"that index in the image"
        )
    runtime_args = _argument_tokens(package.get("runtimeArguments"), "runtime", warnings)
    package_args = _argument_tokens(package.get("packageArguments"), "package", warnings)
    pinned_id = f"{ident}@{version}" if kind == "npm" else f"{ident}=={version}"
    head = ["npx", "-y"] if kind == "npm" else ["uvx"]
    env_vars = package.get("environmentVariables") or []
    secret = _secret_choice(env_vars, "environment variable", "mcp-stdio", warnings)
    prefix: list[str] = []
    for var in env_vars:
        if var.get("isSecret"):
            continue
        value = var.get("value", var.get("default"))
        if value is not None and not _PLACEHOLDER.search(str(value)):
            prefix.append(f"{var['name']}={value}")
        else:
            warnings.append(
                f"environment variable '{var.get('name')}' "
                + ("is required and " if var.get("isRequired") else "")
                + "has no value; not imported — add `env "
                + f"{var.get('name')}=...` to the front of `cmd`"
            )
    argv = (["env", *prefix] if prefix else []) + head + runtime_args + [pinned_id] + package_args
    if runtime_args:
        warnings.append(
            f"runtime arguments {runtime_args} were placed before the package, as "
            f"server.json defines them; publishers often put package arguments "
            f"there, so check `cmd` runs the server"
        )
    warnings.append(
        f"the image must ship `{runtime}`: "
        + ("the published image has no Node, so `npx` is absent"
           if runtime == "npx" else
           "the published image ships `uv` but not the `uvx` alias (`uv tool run` "
           "is the same command)")
        + f", and it fetches {ident} at attach, so the gateway needs that index "
        f"reachable and a writable cache"
    )
    config: dict = {"cmd": shlex.join(argv)}
    if secret is not None:
        config["api_key_env"] = secret["name"]
    return "mcp-stdio", config, secret, None


def import_server(server: dict, surface: str, origin: str = "") -> dict:
    """server.json → {'plugin','registry','caddy','env','warnings'}."""
    from .pluginconfig import ENV_HEADER, SURFACE_PATTERN, caddy_fragment, env_line, token_var
    from .plugins import get

    if not SURFACE_PATTERN.match(surface):
        raise UsageError(
            f"surface '{surface}' must be lowercase alphanumeric with dashes: "
            f"it becomes a URL path and a Caddy matcher name"
        )
    warnings: list[str] = []
    schema = server.get("$schema") or ""
    if SCHEMA_VERSION not in schema:
        warnings.append(
            f"server.json declares schema {schema or '(none)'}; this importer "
            f"reads {SCHEMA_VERSION}, so check the result"
        )
    meta = _publisher_block(server, warnings)
    kind, source = _choose(server, warnings)
    builder = _from_remote if kind == "remote" else _from_package
    plugin, config, secret, _ = builder(surface, source, warnings)
    spec = get(plugin).spec

    var = token_var(surface, "api_key")
    lines = [
        f"# imported from {server.get('name')} {server.get('version', '')}"
        + (f" ({origin})" if origin else ""),
        f"[{surface}]",
        f'plugin = "{plugin}"',
    ]
    if "pinned" in meta:
        lines.append(f"pinned = {_toml(meta['pinned'])}    # publisher-provided: review")
    else:
        lines.append("pinned = []    # TODO: choose the pinned tools; lint fails until you do")
    if "probe" in meta:
        lines.append(f"probe = {_toml(meta['probe'])}    # publisher-provided: review")
        if "probe_args" in meta:
            lines.append(f"probe_args = {_toml(meta['probe_args'])}")
    else:
        lines.append(
            'probe = ""    # TODO: a tool call that proves the credential; lint fails until set'
        )
    lines.append(f"  [{surface}.config]")
    for k, v in config.items():
        lines.append(f"  {k} = {_toml(v)}")
    optional = secret is not None and not secret.get("isRequired")
    if secret is not None:
        lines.append(f"  [{surface}.env]")
        line = f'api_key = "${{{var}}}"'
        what = secret.get("description") or secret.get("name")
        lines.append(f"  # {line}    # optional: {what}" if optional else f"  {line}    # {what}")
        if optional and plugin == "mcp-stdio":
            # api_key_env without api_key is refused at build: keep the pair together.
            idx = next(i for i, ln in enumerate(lines) if ln.startswith("  api_key_env"))
            lines[idx] = "  # " + lines[idx].lstrip() + "    # uncomment with api_key"
    aliases = meta.get("search_aliases")
    if aliases:
        lines.append(f"  [{surface}.search_aliases]    # publisher-provided: review")
        for tool, words in aliases.items():
            lines.append(f"  {_key(tool)} = {_toml(words)}")
    identity = meta.get("identity")
    if identity and identity.get("modes"):
        modes = [m for m in identity["modes"] if isinstance(m, str)]
        honoured = [m for m in modes if m in spec.identity.modes]
        dropped = [m for m in modes if m not in spec.identity.modes]
        if dropped:
            warnings.append(
                f"publisher suggests identity mode(s) {dropped}, which {plugin} "
                f"does not honour"
                + ("; a stdio server can never be per-user" if plugin == "mcp-stdio" else "")
            )
        if honoured:
            lines.append(
                f"  # [{surface}.identity]    # publisher suggests {honoured}; "
                f"see docs/IDENTITY.md before enabling"
            )
            lines.append(f'  # mode = "{honoured[0]}"')
            warnings.append(
                f"publisher suggests identity mode(s) {honoured}; emitted commented "
                f"out, because a per-user surface needs gateway auth and a review"
            )

    env = ""
    if secret is not None:
        env = ENV_HEADER + "\n" + env_line(var, required=not optional)
    return {
        "plugin": plugin,
        "server": server.get("name"),
        "version": server.get("version"),
        "registry": "# --- registry.toml ---\n" + "\n".join(lines),
        "caddy": caddy_fragment(surface),
        "env": env,
        "warnings": warnings,
    }


# --- export -----------------------------------------------------------------


def _parse_package(text: str) -> tuple[str, str, str]:
    kind, sep, rest = text.partition(":")
    ident, at, version = rest.rpartition("@")
    if not sep or kind not in _RUNTIMES or not at or not ident or not version:
        raise UsageError(
            f"--package must be npm:<identifier>@<version> or "
            f"pypi:<identifier>@<version>, got {text!r}"
        )
    return kind, ident, version


def export_plugin(
    plugin_name: str,
    *,
    name: str = "",
    version: str = "",
    url: str = "",
    package: str = "",
) -> dict:
    """A curated plugin → {'server': server.json, 'warnings': [...]}."""
    from .plugins import get

    spec = get(plugin_name).spec
    warnings: list[str] = []
    if spec.requires_entry:
        raise UsageError(
            f"'{plugin_name}' is generic: it has no tested pins or probe to "
            f"export. Export a curated plugin."
        )
    name = name or f"io.beherouter/{plugin_name}"
    if not _REGISTRY_NAME.match(name) or "@" in name:
        raise UsageError(f"--name must be namespace/name (reverse DNS), got {name!r}")
    if not version:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as dist_version

        try:
            version = dist_version("beherouter")
        except PackageNotFoundError:
            version = "0.0.0"
        warnings.append(
            f"version defaults to beherouter's ({version}); pass --server-version "
            f"for the backend's own"
        )
    description = spec.summary
    if len(description) > _DESCRIPTION_MAX:
        description = description[: _DESCRIPTION_MAX - 1].rsplit(" ", 1)[0] + "…"
        warnings.append("summary shortened to server.json's 100-character description")
    server: dict = {
        "$schema": SCHEMA_URL,
        "name": name,
        "description": description,
        "version": version,
    }
    defaults = {f.name: f.default for f in spec.config}
    if spec.backing == "http":
        url = url or str(defaults.get("base_url") or defaults.get("url") or "")
        if not url:
            raise UsageError(f"'{plugin_name}' has no default URL; pass --url")
        server["remotes"] = [{"type": "streamable-http", "url": url}]
        if not url.startswith("https://"):
            warnings.append(
                f"the URL {url} is the plugin's in-network default; pass --url "
                f"with the address a subregistry's readers can reach"
            )
    elif spec.backing == "stdio":
        if not package:
            raise UsageError(
                f"'{plugin_name}' launches `cmd`, and beherouter does not know "
                f"which package ships it; pass --package npm:<id>@<version> or "
                f"pypi:<id>@<version>"
            )
        kind, ident, pkg_version = _parse_package(package)
        server["packages"] = [{
            "registryType": kind,
            "identifier": ident,
            "version": pkg_version,
            "transport": {"type": "stdio"},
        }]
    else:
        raise UsageError(
            f"'{plugin_name}' is a `{spec.backing}` plugin: it runs inside the "
            f"gateway, so there is no server to reach or launch for server.json "
            f"to describe"
        )
    if spec.env:
        warnings.append(
            f"credential(s) {[v.name for v in spec.env]} are not described: how "
            f"'{plugin_name}' sends them is in its build(). Add the "
            + ("remotes[].headers" if spec.backing == "http" else
               "packages[].environmentVariables")
            + " the server expects, marked isSecret, before publishing"
        )
    required = [f.name for f in spec.config if f.required]
    if required:
        warnings.append(
            f"config {required} has no server.json equivalent; an importer must "
            f"supply it to the server itself"
        )
    block: dict = {"v": META_VERSION, "pinned": list(spec.pinned)}
    if spec.probe:
        block["probe"] = spec.probe
        if spec.probe_args is not None:
            block["probe_args"] = dict(spec.probe_args)
    if spec.search_aliases:
        block["search_aliases"] = {k: list(v) for k, v in spec.search_aliases.items()}
    if spec.identity.modes:
        block["identity"] = {"modes": list(spec.identity.modes), "target": spec.identity.target}
    server["_meta"] = {PUBLISHER_KEY: {PLUGIN_KEY: block}}
    return {"server": server, "warnings": warnings}
