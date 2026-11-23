"""Record the FULL listing of representative surfaces. Run ONLY on unchanged
code: its output is the byte-identity reference (spec 2026-11-22, anti-goal 1)."""

import asyncio
import json
import sys
from pathlib import Path

from fastmcp import Client

from beherouter.gateway import build_surfaces
from beherouter.models import Backend, ToolDescriptor
from beherouter.registry import RegistryEntry
from beherouter.surface import build_surface

OUT = Path(__file__).parent / "full_listing_golden.json"


class _Exec:
    async def run(self, verb, args, *, identity=None):
        return {"result": "ok"}


def mcp_like_backend() -> Backend:
    return Backend(
        name="demo",
        kind="mcp",
        descriptors=[
            ToolDescriptor(
                name="ping", verb="ping", summary="Ping it.",
                schema={"type": "object", "properties": {"n": {"type": "integer"}}},
                pinned=True, mutating=False, annotations={"readOnlyHint": True},
            ),
            ToolDescriptor(
                name="pong", verb="pong", summary="Pong it.",
                schema={"type": "object", "properties": {}}, pinned=False, mutating=True,
            ),
        ],
        executor=_Exec(),
    )


async def listing(mcp) -> dict:
    async with Client(mcp) as c:
        tools = [t.model_dump(mode="json", exclude_none=True) for t in await c.list_tools()]
    return {"instructions": mcp.instructions, "tools": tools}


async def main(cli_cmd: str) -> None:
    out = {"mcp_like": await listing(build_surface(mcp_like_backend()))}
    entry = RegistryEntry(name="faketool", plugin="_test-cli", config={"cmd": cli_cmd})
    surfaces = await build_surfaces({"faketool": entry})
    out["cli_gateway"] = await listing(surfaces["faketool"])
    OUT.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
