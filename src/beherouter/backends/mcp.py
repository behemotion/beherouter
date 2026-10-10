"""mcp backend: proxy an existing MCP server; expose pinned-few + search over all its tools.

Unlike `cli` backends there is no beheaxi manifest here — the tool catalogue comes
from an MCP `list_tools()` call, and there is no `pinned` flag to read, so the
backing decides what gets pinned.

Tool names are used verbatim (no `flatten()`): they are already flat MCP names
chosen by the upstream server, and rewriting them would break `call_tool`.
"""

import json
import os
import shlex
import shutil

import httpx
from fastmcp import Client
from fastmcp.client.transports import ClientTransport, StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.client.stdio import get_default_environment

from ..errors import Unavailable, UsageError
from ..identity import settle
from ..models import Backend, ToolDescriptor
from .backing import McpBacking

# Trust-store locations: not credentials, and without them a stdio child talking
# to a backend behind a private CA fails TLS while the gateway itself succeeds.
# `requests` (the Plane SDK) reads REQUESTS_CA_BUNDLE and ignores SSL_CERT_FILE.
_TRUST_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")


def build_transport(
    backing: McpBacking, headers: dict[str, str] | None = None
) -> ClientTransport:
    """Construct the FastMCP client transport described by a backing.

    `backing.env` is already resolved. `headers` stays as an override for
    per-session credentials forwarded from the caller, including backends that
    need two or more such headers.
    """
    # ⚠️ A stdio child's environment is fixed at spawn and `keep_alive=True`
    # reuses the subprocess across sessions, so per-request headers cannot reach
    # it. This USED TO BE SILENTLY DISCARDED: the stdio branch simply never read
    # `headers`, so a per-user configuration attached green and forwarded
    # nothing — the "believed per-user, actually shared" state. Refuse instead.
    if headers and backing.transport == "stdio":
        raise UsageError(
            f"'{backing.name}': a stdio backing cannot carry per-request "
            f"headers; the subprocess environment is fixed at spawn and is "
            f"reused across callers. Attach it over http instead."
        )
    resolved = backing.env or None
    if backing.transport == "stdio":
        if not backing.cmd:
            raise UsageError(f"'{backing.name}': mcp/stdio requires 'cmd'")
        argv = shlex.split(backing.cmd)
        # The MCP SDK does NOT inherit the parent environment: it starts the
        # subprocess with a 6-var safe list (HOME/LOGNAME/PATH/SHELL/TERM/USER)
        # and drops everything else, so a backend's credentials arrive only if
        # they are passed explicitly here. Merge ON TOP of that safe list rather
        # than replacing it — a subprocess without PATH cannot find its own
        # helper binaries. Left as None when the backing declares no env, so the
        # SDK keeps applying its own default. The gateway's trust-store
        # variables ride along (see _TRUST_VARS); the backing's own env wins.
        trust = {k: os.environ[k] for k in _TRUST_VARS if os.environ.get(k)}
        env = (
            (get_default_environment() | trust | (resolved or {}))
            if (resolved or trust)
            else None
        )
        # keep_alive lets FastMCP reuse the subprocess across sessions, so the
        # reconnect-per-call executor below stays cheap for stdio backends.
        return StdioTransport(command=argv[0], args=argv[1:], env=env, keep_alive=True)
    if backing.transport == "http":
        if not backing.url:
            raise UsageError(f"'{backing.name}': mcp/http requires 'url'")
        merged = {**(resolved or {}), **(headers or {})}
        return StreamableHttpTransport(url=backing.url, headers=merged or None)
    raise UsageError(
        f"'{backing.name}': mcp requires transport in ('stdio', 'http'), "
        f"got {backing.transport!r}"
    )


def _payload(res):
    """Pull the useful value out of a fastmcp CallToolResult.

    `.data` is ONLY populated when the backend tool declares an output schema
    that fastmcp can deserialize against; for a tool that just returns a dict it
    is None while the value sits in `.structured_content`. Reading `.data`
    alone therefore forwarded `{"result": null}` for a call that had in fact
    succeeded — found live on 2026-08-04 against office-mcp's `discover`, where
    every call looked fine and returned nothing.

    `.content` is the last resort: the unstructured text blocks every MCP server
    sends, joined so a text-only backend still says something.
    """
    data = getattr(res, "data", None)
    if data is not None:
        return data
    structured = getattr(res, "structured_content", None)
    if structured is not None:
        return structured
    texts = [t for t in (getattr(b, "text", None) for b in getattr(res, "content", [])) if t]
    if texts:
        return "\n".join(texts)
    return None


def _notes(res) -> list[str]:
    """The text blocks a structured result would otherwise drop, in order.

    Once `.data` or `.structured_content` is the payload, `.content` is not
    forwarded, and FastMCP's `content[0]` is only that payload serialized. But
    a backend may append a block the structured value does not carry — found
    2026-10-08 by a deployment whose Plane middleware appends "NOT ASSIGNED:
    …" when Plane silently drops an assignee; the model never saw it and told
    the user the assignment was done. So every text block that is NOT a
    serialization of the structured value travels beside the result.

    A block is redundant when it equals the structured value, as text or once
    parsed as JSON, or equals the `result` of FastMCP's `{"result": v}`
    envelope for a non-object return. Non-text blocks (images, resources) are
    still dropped. A text-only result has no notes: its text IS the payload.
    """
    structured = getattr(res, "structured_content", None)
    if structured is None:
        structured = getattr(res, "data", None)
    if structured is None:
        return []
    same = [structured]
    if isinstance(structured, dict) and set(structured) == {"result"}:
        same.append(structured["result"])
    notes = []
    for block in getattr(res, "content", None) or []:
        text = getattr(block, "text", None)
        if not text or text in same:
            continue
        try:
            if json.loads(text) in same:
                continue
        except ValueError:
            pass
        notes.append(text)
    return notes


def envelope(res, value) -> dict:
    """What an MCP-backed executor returns: `{"result": value}`, plus `notes`
    (see `_notes`) only when there are any, so a result with nothing extra is
    byte-identical to the pre-notes shape. `wrapped_output_schema` declares
    both keys."""
    notes = _notes(res)
    return {"result": value, "notes": notes} if notes else {"result": value}


def http_status(e: BaseException) -> int | None:
    """The upstream HTTP status in an exception's cause chain, if any.

    What the audit line's `status` reports. Read only from a real
    `httpx.HTTPStatusError`, never from message text: an MCP backend's
    isError result carries no status, and sniffing one would misclassify.
    """
    cur: BaseException | None = e
    for _ in range(16):  # a cycle-safe bound on the chain
        if cur is None:
            return None
        if isinstance(cur, httpx.HTTPStatusError):
            return cur.response.status_code
        cur = cur.__cause__ or (None if cur.__suppress_context__ else cur.__context__)
    return None


def call_failed(verb: str, e: Exception) -> UsageError | Unavailable:
    """A backend call that did not produce a result, classified by its HTTP
    status when it has one: a 4xx is the backend refusing the call (the
    deployment's or the caller's credential, say), anything else an outage."""
    status = http_status(e)
    context = {"status": status} if status is not None else None
    if status is not None and 400 <= status < 500:
        return UsageError(f"backend rejected '{verb}': {e}", context=context)
    return Unavailable(f"backend call '{verb}' failed: {e}", context=context)


class MCPClientExecutor:
    """Forward calls over an already-connected client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def run(self, verb: str, args: dict, *, identity=None) -> dict:
        if identity is not None and (
            identity.headers or getattr(identity, "pending", None) is not None
        ):
            # This executor is the bound-client one that `load_mcp_backend`
            # replaces; it holds an OPEN session whose headers were fixed at
            # connect. Raising keeps the "no silent shared fallback" rule
            # whole rather than relying on the swap having happened.
            raise UsageError(
                f"backend call '{verb}': a bound-client executor cannot apply "
                f"a per-request identity"
            )
        try:
            res = await self._client.call_tool(verb, args)
        except ToolError as e:
            # The backend answered isError:true. Per the MCP client-best-practices
            # doc this is a SUCCESSFUL response carrying a tool-level error --
            # bad arguments, not found, refused -- that the model should
            # self-correct against. Unavailable would file it alongside a dead
            # subprocess and read to every monitor as an outage.
            #
            # No message sniffing to pick AuthError or NotFound: backend error
            # text is unstructured and a heuristic here would misclassify
            # silently, which is the failure mode this file has already been
            # bitten by twice (_payload's {"result": null}, the
            # action-parameterized default forwarding in _make_pinned_tool).
            raise UsageError(f"backend rejected '{verb}': {e}") from e
        except Exception as e:
            raise call_failed(verb, e) from e
        return envelope(res, _payload(res))


class ReconnectingMCPExecutor:
    """Open a short-lived session per call against a stored transport.

    The gateway is long-lived while backend sessions are not, so holding one
    connection open for the process lifetime invites stale-socket and
    dead-subprocess failures. Reconnecting per call trades a little latency for
    robustness; `keep_alive=True` on stdio keeps the subprocess warm.

    `backing` is kept so a per-request identity can be applied: its headers are
    built into a FRESH transport for that one call, layered over the attach-time
    `backing.env`. No transport cache — this executor already opens a session
    per call, so a cache would add eviction concerns to save a constructor.
    """

    def __init__(
        self, transport: ClientTransport, backing: McpBacking | None = None
    ) -> None:
        self._transport = transport
        self._backing = backing

    async def run(self, verb: str, args: dict, *, identity=None) -> dict:
        if self._backing is not None and self._backing.guard is not None:
            # Before any transport is built: a refused call must not cost a
            # round trip, and must not depend on the backend being up.
            self._backing.guard(verb, args)
        # After the guard: a refused call must not cost a token exchange either.
        identity = await settle(identity)
        transport = self._transport
        if identity is not None and identity.headers:
            if self._backing is None:
                raise UsageError(
                    f"backend call '{verb}': this executor has no backing to "
                    f"rebuild a transport from, so a per-request identity "
                    f"cannot be applied"
                )
            transport = build_transport(self._backing, dict(identity.headers))
        try:
            async with Client(transport) as client:
                res = await client.call_tool(verb, args)
        except ToolError as e:
            raise UsageError(f"backend rejected '{verb}': {e}") from e
        except Exception as e:
            raise call_failed(verb, e) from e
        return envelope(res, _payload(res))


def _with_note(description: str, note: str | None) -> str:
    if not note:
        return description
    return f"{description.rstrip()}\n\n{note}" if description else note


def _require_command(backing: McpBacking) -> None:
    """Refuse a stdio backing whose command is not installed, by name.

    Without this, a missing binary surfaces as an OSError from deep inside the
    stdio client, wrapped in "could not attach", which names neither the
    missing file nor the way out. A plugin's default `cmd` assumes an image
    that has the binary; the override is the operator's escape hatch.
    """
    argv = shlex.split(backing.cmd or "")
    if argv and shutil.which(argv[0]) is None:
        raise UsageError(
            f"'{backing.name}': stdio command '{argv[0]}' is not installed in "
            f"this image (not found, or not executable, on PATH). Use an image "
            f"that ships it, or override the plugin's `cmd` in "
            f"[{backing.name}.config] to point at one that exists."
        )


def _annotations(tool) -> dict | None:
    """The backend's tool annotations as a plain dict, or None if it sent none.

    `mcp.types.ToolAnnotations` is a pydantic model with every field defaulting
    to None, so a server that annotates nothing still yields an object whose
    fields are all None. `exclude_none` collapses that back to {} and we report
    None — otherwise "annotated with nothing" and "not annotated" would be
    indistinguishable, and the tri-state would silently become a two-state.
    """
    ann = getattr(tool, "annotations", None)
    if ann is None:
        return None
    dumped = ann.model_dump(exclude_none=True) if hasattr(ann, "model_dump") else dict(ann)
    return dumped or None


def _mutating(annotations: dict | None) -> bool | None:
    """readOnlyHint -> mutating, tri-state.

    None (the backend said nothing) stays None. It is NOT False: assuming a
    silent backend is read-only is what advertised every Plane delete as safe.
    """
    if annotations is None:
        return None
    return not annotations.get("readOnlyHint", False)


async def backend_from_client(
    name: str,
    client: Client,
    pinned: list[str] | None = None,
    republish_output_schema: bool = True,
    notes: dict[str, str] | None = None,
) -> Backend:
    """The testable core: build a Backend from a connected MCP client.

    `republish_output_schema=False` drops every tool's outputSchema — see
    `McpBacking.republish_output_schema` for when that is the right call.
    `notes` appends a sentence to named tools' descriptions — see
    `McpBacking.notes`.
    """
    notes = notes or {}
    pinned_set = set(pinned or [])
    tools = await client.list_tools()
    descriptors = []
    for t in tools:
        annotations = _annotations(t)
        descriptors.append(
            ToolDescriptor(
                name=t.name,
                verb=t.name,
                summary=_with_note(t.description or "", notes.get(t.name)),
                schema=getattr(t, "inputSchema", {}) or {},
                pinned=(t.name in pinned_set),
                mutating=_mutating(annotations),
                annotations=annotations,
                output_schema=(
                    getattr(t, "outputSchema", None)
                    if republish_output_schema
                    else None
                ),
            )
        )

    async def _relist() -> list[ToolDescriptor]:
        """Re-fetch over the SAME client — valid only while its session is open.

        `load_mcp_backend` replaces this with a reconnecting version once its
        own short-lived session closes; this one exists so `backend_from_client`
        is independently testable without a transport to reconnect through.
        """
        fresh = await backend_from_client(
            name, client, pinned, republish_output_schema, notes
        )
        return fresh.descriptors

    return Backend(
        name=name,
        kind="mcp",
        descriptors=descriptors,
        executor=MCPClientExecutor(client),
        relist=_relist,
    )


async def load_mcp_backend(
    backing: McpBacking, headers: dict[str, str] | None = None
) -> Backend:
    """Connect, list the upstream catalogue, and return a Backend for the gateway."""
    transport = build_transport(backing, headers)
    if backing.transport == "stdio":
        _require_command(backing)
    try:
        async with Client(transport) as client:
            backend = await backend_from_client(
                backing.name,
                client,
                backing.pinned,
                backing.republish_output_schema,
                backing.notes,
            )
    except UsageError:
        raise
    except Exception as e:
        raise Unavailable(f"could not attach mcp backend '{backing.name}': {e}") from e
    # Swap the bound-client executor for one that reconnects per call — the
    # session opened above is closed by the time the gateway serves traffic.
    backend.executor = ReconnectingMCPExecutor(transport, backing=backing)

    async def _relist() -> list[ToolDescriptor]:
        """Re-fetch the catalogue over a fresh short-lived session.

        Uses the same reconnect-per-use discipline as ReconnectingMCPExecutor:
        the gateway is long-lived while backend sessions are not.
        """
        async with Client(transport) as client:
            fresh = await backend_from_client(
                backing.name,
                client,
                backing.pinned,
                backing.republish_output_schema,
                backing.notes,
            )
        return fresh.descriptors

    backend.relist = _relist
    return backend
