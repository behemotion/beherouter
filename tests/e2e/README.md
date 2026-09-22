# Local end-to-end stack

> Four containers, one network, and a set of assertions that a per-user identity
> reaches a **real backend** — not a mock of one.
>
> Not collected by pytest (no `test_*.py`): it needs podman and a few minutes.
> The unit suite proves the rules; this proves the deployment.

```bash
uv run python tests/e2e/mint.py                     # key set + three users' JWTs
podman build -t localhost/beherouter:e2e -f Containerfile .
podman build -t localhost/plane-mcp:e2e -f tests/e2e/Containerfile.plane-mcp tests/e2e
bash tests/e2e/up.sh                                # → {"status":"ok","surfaces":[...]}
uv run python tests/e2e/e2e.py                      # → 14/14 checks passed
podman rm -f beherouter plane-mcp echo-mcp e2e-fixtures
```

## What is in the stack

| Container | What it is | Why it is here |
|---|---|---|
| `e2e-fixtures` | stdlib HTTP: a fake Plane REST API + the IdP's JWKS | The two things a laptop lacks. It records **which credential arrived**, which is the whole evidence base |
| `plane-mcp` | **real** `plane-mcp-server==0.3.2`, HTTP mode | A mock of the backend would prove nothing about the backend's auth |
| `echo-mcp` | an MCP server reporting its own `Authorization` | mode `bearer` needs a backend that ACCEPTS a forwarded bearer — see below |
| `beherouter` | the image built from this repo | the thing under test |

Two surfaces, two identity modes: `plane` (`client` — the caller's own Plane
PAT, plus a role gate) and `echo` (`bearer` — the caller's own verified JWT).

## ⚠️ The finding that shaped the plugins

plane-mcp-server 0.3.2 serves two HTTP mounts and **only one can carry a
credential the gateway forwards**:

- **`/http`** is an OAuth *proxy*. It verifies a token **it minted itself** (a
  FastMCP-issued JWT whose `jti` it resolves in its own store) and returns 401
  for anything else — **before Plane is consulted**. Measured here: attaching
  yields `could not attach mcp backend 'plane': Client error '401 Unauthorized'`
  and the Plane API records **no request at all**. A forwarded IdP token cannot
  reach Plane through this mount.
- **`/http/api-key`** reads a per-request Plane PAT plus `x-workspace-slug`,
  validates it against Plane's own `/api/v1/users/me/`, and builds the client
  with it. Measured here: `pat-alice` → Plane resolves **alice**.

So `plane-http-apikey` is per-user Plane against the **published** server today,
and `plane-http` is for a backend that accepts a forwarded bearer (a fork with
its own JWT authentication, which is what the requesting team is building). The
`echo-mcp` container exists so that second path is proven rather than asserted.

## What the 14 checks establish

1. `/healthz` reports both surfaces attached · 2. an uncredentialed request is
401 · 3. a **shared-token** caller is refused by a per-user surface, on
`search_tools` — enumeration is gated, not just calls · **4–6. two callers act
as themselves in Plane**, the identity Plane returns is each caller's own, and
Plane never saw the deployment credential on those calls · 7. a caller without
the gated role is refused, naming the surface · 8. a caller missing their
credential header is refused, naming the header, with nothing partial sent ·
9–10. mode `bearer` forwards the caller's token **byte-identical**, and a second
caller arrives as themselves · 11–12. `health --deep` reports `attach`/`probe`
ok and each surface's identity mode with `probe_scope` ·
13. `registry-lint` passes the live registry **inside the serving image** ·
14. the same registry under `BEHEROUTER_AUTH_MODE=shared` is **refused by the
lint**, not by the gateway at boot.

⚠️ Check 11 once passed vacuously — an `all()` over an empty list, because the
records live under `backends` and the assertion read `surfaces`. It now asserts
the count first. A green check that proves nothing is the failure mode this
directory is most exposed to.
