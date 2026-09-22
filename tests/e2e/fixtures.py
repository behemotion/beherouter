"""E2E fixtures: a fake Plane REST API and a JWKS endpoint, stdlib only.

Stands in for the two things a real deployment brings that a laptop does not:
an IdP (its public keys) and Plane itself. It is deliberately dumb — it does
NOT verify signatures — because what the e2e proves is which credential ARRIVED,
not whether Plane's own auth works.

Routes:
  GET /jwks.json                 the gateway's OIDC key set
  GET /api/v1/users/me/          the identity the presented bearer resolves to
  GET /auth/o/app-installation/  what plane-mcp-server's OAuth provider needs
  GET /_calls                    every request seen, with its Authorization
  POST /_calls/reset             clear the record
"""

import base64
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JWKS = json.loads(os.environ["E2E_JWKS"])
# The opaque token the gateway attaches and probes with. Resolves to a service
# account, exactly like the deployment credential it stands for.
DEPLOYMENT_TOKEN = os.environ["E2E_DEPLOYMENT_TOKEN"]

CALLS: list[dict] = []


def _identity(token: str) -> dict | None:
    """Resolve a bearer to a Plane user, or None for 401.

    A JWT resolves to the user it names — the payload is read WITHOUT verifying
    the signature, which is the whole point: this fixture must report who the
    gateway forwarded, not re-run the gateway's verification.
    """
    if token == DEPLOYMENT_TOKEN:
        return {
            "id": "svc-beherouter",
            "email": "beherouter@service.invalid",
            "display_name": "beherouter (deployment credential)",
            "first_name": "beherouter",
            "last_name": "service",
        }
    if token.startswith("pat-"):
        sub = token[4:]
        return {
            "id": sub,
            "email": f"{sub}@bank.invalid",
            "display_name": sub,
            "first_name": sub,
            "last_name": "user",
        }
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        pad = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return None
    sub = claims.get("sub")
    if not sub:
        return None
    return {
        "id": sub,
        "email": claims.get("email", f"{sub}@example.invalid"),
        "display_name": claims.get("preferred_username", sub),
        "first_name": sub,
        "last_name": "user",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the container log readable
        pass

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _credential(self) -> tuple[str, str]:
        """(scheme, token) — Plane accepts either, and WHICH one arrived is
        half of what this fixture exists to record."""
        api_key = self.headers.get("x-api-key", "")
        if api_key:
            return "x-api-key", api_key
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return "bearer", auth[7:]
        return "", ""

    def do_POST(self):
        if self.path.rstrip("/") == "/_calls/reset":
            CALLS.clear()
            return self._send(200, {"ok": True})
        self._send(404, {"error": self.path})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/jwks.json":
            return self._send(200, JWKS)
        if path.rstrip("/") == "/_calls":
            return self._send(200, {"calls": CALLS})

        scheme, token = self._credential()
        who = _identity(token)
        CALLS.append(
            {
                "path": path,
                "scheme": scheme,
                "credential_prefix": token[:24],
                "workspace_slug": self.headers.get("x-workspace-slug", ""),
                "resolved": (who or {}).get("id"),
            }
        )
        if path.rstrip("/") == "/api/v1/users/me":
            if who is None:
                return self._send(401, {"detail": "Authentication credentials were not provided."})
            return self._send(200, who)
        if path.rstrip("/") == "/auth/o/app-installation":
            if who is None:
                return self._send(401, {"detail": "unauthorized"})
            return self._send(
                200,
                [
                    {
                        "id": "inst-1",
                        "workspace_detail": {"name": "E2E", "slug": "e2e", "id": "ws-1"},
                        "created_at": "2026-09-22T00:00:00Z",
                        "updated_at": "2026-09-22T00:00:00Z",
                        "status": "active",
                        "workspace": "ws-1",
                        "application": "app-1",
                        "installed_by": "svc",
                        "app_bot": "bot-1",
                    }
                ],
            )
        self._send(404, {"error": path})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
