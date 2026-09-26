"""What a loader needs, as opposed to what an operator writes.

The public registry schema and the loaders' input used to be the same type
(`RegistryEntry`), which coupled the TOML format to the loaders. A plugin's
build() constructs one of these instead, so the two can change independently.

`env` here is ALREADY RESOLVED — `${VAR}` expansion happens once, in the load
path, so every backing gets identical semantics.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

# A pre-call check: (verb, args) -> None, raising UsageError to refuse. Runs
# before any transport is opened, so a refused call costs no backend round trip.
Guard = Callable[[str, dict], None]


@dataclass(frozen=True)
class McpBacking:
    name: str
    transport: str  # "stdio" | "http" | "inproc"
    cmd: str | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    pinned: list[str] | None = None
    # False for a backend whose replies violate its OWN outputSchema. The
    # surface validates every pinned tool's reply against the schema it
    # republishes, so a backend declaring `string` and answering `null` fails
    # every call through the gateway while a direct call succeeds. Declining
    # the schema costs a code-mode host its typed result, nothing else; opt-in
    # per plugin so no other surface's declared shape changes.
    republish_output_schema: bool = True
    # Refuse a call this deployment is known not to serve, with a sentence the
    # model can act on, instead of forwarding it for an opaque backend 404.
    guard: Guard | None = None
    # Tool name -> a sentence appended to that tool's description, on attach
    # AND on every re-list, so the published array and the searchable catalogue
    # say the same thing. For what a model must know before it calls the tool.
    notes: dict[str, str] = field(default_factory=dict)
    # inproc only: the in-process FastMCP server a plugin's build() constructed.
    # Typed loosely so this module stays free of FastMCP imports.
    server: object | None = None


@dataclass(frozen=True)
class CliBacking:
    name: str
    cmd: str
    pinned: list[str] | None = None
