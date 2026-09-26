"""openapi: a customer's REST API from its OpenAPI document — no code, no sidecar.

A source on the `inproc` seam: `build()` turns the document into an in-process
FastMCP server (`backends.inproc.openapi_server`) and hands it to
`load_inproc_backend`, so the result has every MCP backend's pipeline. This
module imports NOTHING from FastMCP (the 4.x port is `backends/inproc.py`).

Rules, all offline at lint unless stated (spec § `openapi`, § Measured
2026-09-26):

- `include` is required. `["*"]` publishes everything, with a warning naming the
  count: auto-converted catalogues are the documented failure of OpenAPI→MCP.
- Each included `operationId` must exist, and must be a name FastMCP's slug
  leaves UNCHANGED (`^[A-Za-z0-9]+(_[A-Za-z0-9]+)*$`, at most 56 characters).
  Otherwise the published tool would silently be called something else. An
  operation with no `operationId` is refused by method and path.
- `probe` and `pinned` are required (`requires_entry`): there is no tested
  default. Every pin must be in `include`.
- `spec` may be a URL. It is fetched in build(), never at lint, and lint says so
  as a warning.
- Every schema is closed, so a misspelled argument is refused with a
  did-you-mean rather than dropped by the upstream.
"""

import json
import re
from pathlib import Path

import httpx
import yaml
from rapidfuzz import fuzz, process

from ..backends.backing import McpBacking
from ..backends.inproc import identity_client, load_inproc_backend, openapi_server
from ..errors import Unavailable, UsageError
from . import register
from .spec import ConfigField, EnvVar, IdentitySupport, PluginContext, PluginSpec

_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
_NAME = re.compile(r"^[A-Za-z0-9]+(_[A-Za-z0-9]+)*$")
_MAX_NAME = 56

SPEC = PluginSpec(
    name="openapi",
    summary=(
        "A REST API from its OpenAPI document: the listed operations become "
        "tools, with no MCP server to run. Needs `include`, `probe` and `pinned`."
    ),
    backing="inproc",
    requires_entry=("probe", "pinned"),
    config=(
        ConfigField("spec", str, required=True,
                    doc="OpenAPI document: a local .json/.yaml path, or an http(s) URL fetched at attach"),
        ConfigField("base_url", str, required=True, doc="the REST API's base URL"),
        ConfigField("include", list, required=True,
                    doc='operationIds to publish; ["*"] for every operation (warned)'),
        ConfigField("auth_header", str, default="authorization",
                    doc="header carrying the deployment token"),
        ConfigField("auth_prefix", str, default="Bearer ", doc="prefix before the token"),
    ),
    env=(EnvVar("token", doc="deployment credential for attach, the probe and shared "
                             "callers; required until Phase 2 adds optional credentials"),),
    identity=IdentitySupport(
        modes=("bearer", "claims", "client", "lookup"),
        target="header",
        doc="the caller's material lands on the upstream REST request, over the deployment header",
    ),
)


def _is_url(ref: str) -> bool:
    return ref.startswith(("http://", "https://"))


def _parse(text: str, where: str) -> dict:
    try:
        doc = json.loads(text) if where.endswith(".json") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise UsageError(f"openapi: cannot parse '{where}': {e}") from e
    if not isinstance(doc, dict) or "paths" not in doc:
        raise UsageError(f"openapi: '{where}' is not an OpenAPI document (no `paths`)")
    return doc


def load_document(ref: str) -> dict:
    """A LOCAL document. No network — this is what lint calls."""
    path = Path(ref)
    if not path.is_file():
        raise UsageError(f"openapi: spec file '{ref}' does not exist")
    return _parse(path.read_text(), ref)


async def fetch_document(ref: str) -> dict:
    """A local file, or a URL — build() only."""
    if not _is_url(ref):
        return load_document(ref)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(ref)
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise Unavailable(f"openapi: could not fetch spec '{ref}': {e}") from e
    return _parse(r.text, ref if ref.endswith(".json") else ref + ".yaml")


def operation_ids(doc: dict) -> dict[str, str | None]:
    """'METHOD /path' -> operationId (None when absent)."""
    out: dict[str, str | None] = {}
    for path, item in (doc.get("paths") or {}).items():
        for method in _METHODS:
            op = (item or {}).get(method)
            if isinstance(op, dict):
                out[f"{method.upper()} {path}"] = op.get("operationId")
    return out


def _check_name(where: str, op_id: str) -> None:
    if not _NAME.match(op_id) or len(op_id) > _MAX_NAME:
        slug = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "", re.sub(r"[\s\-.]+", "_", op_id)))
        raise UsageError(
            f"{where} operationId '{op_id}' would be published as "
            f"'{slug[:_MAX_NAME]}'; rename it in the document to a name made of "
            f"letters, digits and single underscores, at most {_MAX_NAME} characters"
        )


def check_document(where: str, doc: dict, include: list, pinned: list | None) -> None:
    """`where` prefixes every refusal: `'<surface>':` from build(), and
    `openapi:` from lint, where the plugin's validator never sees the surface."""
    ops = operation_ids(doc)
    known = {i for i in ops.values() if i}
    if include == ["*"]:
        unnamed = sorted(k for k, v in ops.items() if not v)
        if unnamed:
            raise UsageError(
                f"{where} include = [\"*\"] needs an operationId on every "
                f"operation; missing on {unnamed}. Add one, or list the operations"
            )
        selected = sorted(known)
    else:
        for op_id in include:
            if op_id not in known:
                close = process.extractOne(op_id, sorted(known), scorer=fuzz.ratio, score_cutoff=60)
                hint = f"; did you mean '{close[0]}'?" if close else ""
                raise UsageError(f"{where} include names '{op_id}', not an operationId in the spec{hint}")
        selected = list(include)
    for op_id in selected:
        _check_name(where, op_id)
    outside = sorted(set(pinned or []) - set(selected))
    if outside:
        raise UsageError(f"{where} pinned names {outside}, which include does not publish")


def validate(config: dict) -> None:
    """Offline: reads a local document, never a URL."""
    include = config.get("include")
    if not isinstance(include, list) or not include or not all(isinstance(i, str) for i in include):
        raise UsageError('openapi: include must be a non-empty list of operationIds, or ["*"]')
    if not _is_url(config["spec"]):
        # The entry's pins are not visible here: `validate_entry` checks them
        # against `include`, and build() re-checks them against the document.
        check_document("openapi:", load_document(config["spec"]), include, None)


def warn(config: dict) -> list[str]:
    out = []
    if _is_url(config["spec"]):
        out.append("spec is a URL, fetched at attach; lint cannot check `include` "
                   "against it — use a local file to catch a bad name before a deploy")
    elif config.get("include") == ["*"]:
        n = sum(1 for v in operation_ids(load_document(config["spec"])).values() if v)
        out.append(f'include = ["*"] publishes {n} tool(s); list the operations agents need')
    return out


async def build(ctx: PluginContext):
    cfg = ctx.config
    doc = await fetch_document(cfg["spec"])
    check_document(f"'{ctx.surface}':", doc, cfg["include"], ctx.pinned)
    client = identity_client(
        base_url=cfg["base_url"],
        headers={cfg["auth_header"]: f"{cfg['auth_prefix']}{ctx.env['token']}"},
        timeout=30,
    )
    server = await openapi_server(
        doc,
        client=client,
        include=None if cfg["include"] == ["*"] else cfg["include"],
        name=ctx.surface,
    )
    return await load_inproc_backend(
        McpBacking(name=ctx.surface, transport="inproc", server=server, pinned=ctx.pinned)
    )


register(SPEC, build, validate=validate, warn=warn)
