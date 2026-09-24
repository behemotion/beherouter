"""What a loader needs, as opposed to what an operator writes.

The public registry schema and the loaders' input used to be the same type
(`RegistryEntry`), which coupled the TOML format to the loaders. A plugin's
build() constructs one of these instead, so the two can change independently.

`env` here is ALREADY RESOLVED — `${VAR}` expansion happens once, in the load
path, so every backing gets identical semantics.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class McpBacking:
    name: str
    transport: str  # "stdio" | "http"
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


@dataclass(frozen=True)
class CliBacking:
    name: str
    cmd: str
    pinned: list[str] | None = None
