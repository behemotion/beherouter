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


@dataclass(frozen=True)
class CliBacking:
    name: str
    cmd: str
    pinned: list[str] | None = None
