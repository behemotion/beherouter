"""End-to-end assertions against the running local stack.

Everything here goes through the published gateway port as a real MCP client
over HTTP — no in-process shortcuts, no monkeypatching. What each check proves
is named in its own docstring, because a green suite that proves the wrong thing
is the failure mode this file exists to avoid.
"""

import asyncio
import json
import pathlib
import subprocess
import sys

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

HERE = pathlib.Path(__file__).parent
MATERIAL = json.loads((HERE / "material.json").read_text())
TOKENS = MATERIAL["tokens"]
GATEWAY = "http://localhost:47100"
FIXTURES = "http://localhost:18000"
SHARED_TOKEN = "e2e-shared-gateway-token"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))


def client(surface: str, *, jwt: str | None = None, token: str | None = None, extra=None):
    headers = {}
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(extra or {})
    return Client(StreamableHttpTransport(url=f"{GATEWAY}/{surface}/mcp", headers=headers))


async def call(c, tool, args):
    async with c:
        return await c.call_tool(tool, args)


async def main() -> int:
    # --- 1. the gateway is up, with both surfaces -------------------------
    health = httpx.get(f"{GATEWAY}/healthz", timeout=10).json()
    check(
        health == {"status": "ok", "surfaces": ["echo", "plane"]},
        "healthz reports both surfaces attached",
        json.dumps(health),
    )

    # --- 2. no credential is refused --------------------------------------
    r = httpx.post(
        f"{GATEWAY}/plane/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
        timeout=10,
    )
    check(r.status_code == 401, "an uncredentialed request is refused", f"HTTP {r.status_code}")

    # --- 3. the shared gateway token cannot reach a per-user surface ------
    """The fail-closed rule: a surface configured per-user is never served with
    the deployment credential, not even to a caller holding the gateway token."""
    try:
        await call(client("plane", token=SHARED_TOKEN), "search_tools", {"query": "work"})
        check(False, "a shared-token caller is refused by the per-user surface", "it was served")
    except Exception as e:
        check(
            "requires a verified user" in str(e),
            "a shared-token caller is refused by the per-user surface",
            str(e)[:160],
        )

    # --- 4 & 5. two callers, two Plane identities, one gateway ------------
    """THE ASK. Each caller's own PAT travels from their request, through the
    gateway, through real plane-mcp-server, to a real Plane REST call."""
    httpx.post(f"{FIXTURES}/_calls/reset", timeout=10)
    seen = {}
    for user in ("alice", "bob"):
        res = await call(
            client(
                "plane",
                jwt=TOKENS[user],
                extra={"x-plane-pat": f"Bearer pat-{user}"},
            ),
            "run_tool",
            {"name": "member", "args": {"action": "me"}},
        )
        seen[user] = res.structured_content["result"]
    check(
        seen["alice"]["id"] == "alice" and seen["bob"]["id"] == "bob",
        "two callers act as themselves in Plane (per-user identity)",
        f"alice -> {seen['alice']['id']}, bob -> {seen['bob']['id']}",
    )
    check(
        seen["alice"]["email"] == "alice@bank.invalid"
        and seen["bob"]["email"] == "bob@bank.invalid",
        "the identity Plane returns is the caller's own, not the deployment's",
        f"{seen['alice']['email']} / {seen['bob']['email']}",
    )

    calls = httpx.get(f"{FIXTURES}/_calls", timeout=10).json()["calls"]
    resolved = [c["resolved"] for c in calls if c["path"].endswith("/users/me/")]
    check(
        "alice" in resolved and "bob" in resolved and "svc-beherouter" not in resolved,
        "Plane itself saw each caller's credential, never the deployment one",
        f"resolved at the Plane API: {resolved}",
    )

    # --- 6. the role gate refuses, by name --------------------------------
    try:
        await call(
            client("plane", jwt=TOKENS["carol"], extra={"x-plane-pat": "Bearer pat-carol"}),
            "run_tool",
            {"name": "member", "args": {"action": "me"}},
        )
        check(False, "a caller without the role is refused", "it was served")
    except Exception as e:
        check(
            "do not have access to surface 'plane'" in str(e),
            "a caller without the role is refused, naming the surface",
            str(e)[:160],
        )

    # --- 7. a missing client header refuses, and sends nothing partial ----
    try:
        await call(client("plane", jwt=TOKENS["alice"]), "run_tool",
                   {"name": "member", "args": {"action": "me"}})
        check(False, "a caller missing their PAT header is refused", "it was served")
    except Exception as e:
        check(
            "x-plane-pat" in str(e),
            "a caller missing their PAT header is refused, naming the header",
            str(e)[:160],
        )

    # --- 8. mode `bearer`: the caller's own token continues downstream ----
    # A published pinned tool wraps its payload under `result`; read the
    # structured content rather than the deserialized model so the assertion
    # does not depend on FastMCP's generated output type.
    res = await call(client("echo", jwt=TOKENS["alice"]), "whoami", {})
    forwarded = res.structured_content["result"]["token"]
    check(
        forwarded == TOKENS["alice"],
        "mode `bearer` forwards the caller's own verified token, byte-identical",
        f"backend saw {forwarded[:28]}… (len {len(forwarded)})",
    )
    res_b = await call(client("echo", jwt=TOKENS["bob"]), "whoami", {})
    forwarded_b = res_b.structured_content["result"]["token"]
    check(
        forwarded_b == TOKENS["bob"] and forwarded_b != forwarded,
        "a second caller reaches the same backend as themselves",
        f"bob's token differs from alice's: {forwarded_b != forwarded}",
    )

    # --- 9. the gateway's own verdict on the deployment --------------------
    deep = json.loads(
        subprocess.run(
            ["podman", "exec", "beherouter", "beherouter", "health", "--deep", "--json"],
            capture_output=True, text=True, check=False,
        ).stdout
    )
    # The records live under `backends`. Asserting the COUNT first, because an
    # `all()` over an empty list is the green-check-that-proves-nothing this
    # file exists to avoid — and it did exactly that on the first run.
    by_name = {s["name"]: s for s in deep.get("backends", [])}
    check(
        len(by_name) == 2
        and all(s.get("attach") == "ok" and s.get("probe") == "ok" for s in by_name.values()),
        "health --deep: both surfaces attach and probe with the deployment credential",
        json.dumps({n: {"attach": s.get("attach"), "probe": s.get("probe")} for n, s in by_name.items()}),
    )
    check(
        by_name["plane"]["identity"]["mode"] == "client"
        and by_name["plane"]["identity"]["probe_scope"] == "deployment-credential"
        and by_name["plane"]["identity"]["require_roles"] == ["ai-plane-access"]
        and by_name["echo"]["identity"]["mode"] == "bearer",
        "health --deep reports each surface's identity mode and probe scope",
        json.dumps({n: s.get("identity") for n, s in by_name.items()}),
    )

    # --- 10. the pre-deploy gate agrees with the running gateway ----------
    lint = subprocess.run(
        ["podman", "exec", "beherouter", "beherouter", "registry-lint", "--json"],
        capture_output=True, text=True, check=False,
    )
    payload = json.loads(lint.stdout or "{}")
    check(
        lint.returncode == 0 and payload.get("ok") is True and payload.get("warnings") == [],
        "registry-lint passes the live registry inside the serving image",
        json.dumps(payload),
    )

    # --- 11. the same registry under the default auth mode fails the gate --
    """Fix A's regression check, run for real: with BEHEROUTER_AUTH_MODE=shared
    the hook must REFUSE this registry rather than let the gateway die at boot."""
    shared_lint = subprocess.run(
        ["podman", "exec", "-e", "BEHEROUTER_AUTH_MODE=shared", "beherouter",
         "beherouter", "registry-lint", "--json"],
        capture_output=True, text=True, check=False,
    )
    # beheaxi prints an error record on STDERR; the first run of this check
    # looked only at stdout and reported a failure the gateway did not have.
    shared_out = shared_lint.stdout + shared_lint.stderr
    check(
        shared_lint.returncode != 0 and "requires a verified user" in shared_out,
        "registry-lint refuses a per-user registry on a shared-mode gateway",
        shared_out.strip()[:200],
    )

    failed = [name for ok, name, _ in results if not ok]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
