"""Every full listing stays byte-identical (spec 2026-11-22, anti-goal 1)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

import record_full_listing as rec

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "full_listing_golden.json").read_text())


async def test_mcp_like_full_listing_is_byte_identical():
    assert await rec.listing(rec.build_surface(rec.mcp_like_backend())) == GOLDEN["mcp_like"]


async def test_gateway_full_listing_is_byte_identical(fake_cli_cmd):
    entry = rec.RegistryEntry(name="faketool", plugin="_test-cli", config={"cmd": fake_cli_cmd})
    surfaces = await rec.build_surfaces({"faketool": entry})
    assert await rec.listing(surfaces["faketool"]) == GOLDEN["cli_gateway"]
