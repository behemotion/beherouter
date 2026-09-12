"""Common types: a Backend is descriptors + a pinned subset + an executor.

Both backend kinds (`cli` and `mcp`) reduce to this one shape, which is what lets
`surface.py` build an MCP surface without caring where the tools came from.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ToolDescriptor:
    name: str  # flat MCP tool name (post-flatten)
    verb: str  # original verb (as the backend knows it)
    summary: str
    schema: dict  # {arg_name: {"name","type","required",...}}
    pinned: bool
    mutating: bool | None  # None == the backend did not say; see health.PROBE_NONE
    # Raw MCP tool annotations as the backend sent them (readOnlyHint,
    # destructiveHint, idempotentHint, openWorldHint). None when absent —
    # beherouter never invents an annotation a backend did not make.
    annotations: dict | None = None
    # The backend's own outputSchema, unwrapped. surface.py wraps it to match
    # the {"result": ...} envelope before republishing.
    output_schema: dict | None = None


class Executor(Protocol):
    async def run(self, verb: str, args: dict) -> dict: ...


Relister = Callable[[], Awaitable[list[ToolDescriptor]]]


@dataclass
class Backend:
    name: str
    kind: str
    descriptors: list[ToolDescriptor]
    executor: "Executor"
    # Re-fetch this backend's catalogue. None for backings that cannot (`cli`,
    # `native`), which is what makes Phase 3 a no-op for them.
    relist: "Relister | None" = None
    # Milliseconds the cached catalogue stays fresh. None or 0 disables refresh.
    ttl_ms: int | None = None

    @property
    def pinned(self) -> list[ToolDescriptor]:
        return [d for d in self.descriptors if d.pinned]
