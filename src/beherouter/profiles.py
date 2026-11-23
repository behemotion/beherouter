"""Route one surface's requests to its full or lean app (spec 2026-11-22).

A request carrying an `mcp-session-id` goes to the app that issued that id,
whatever its URL says, so a client that drops `?profile=` mid-session stays
on its profile. Everything else (an `initialize`, every request on a
stateless surface, an id this process never issued) is routed by `?profile=`.
"""

from collections import OrderedDict
from collections.abc import Iterable
from urllib.parse import parse_qs

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PROFILES = ("full", "lean")
# Clients that never DELETE their session would otherwise grow the map forever.
MAX_SESSIONS = 10_000
_SID = b"mcp-session-id"


def _header(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> str | None:
    for k, v in headers:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


class ProfileDispatcher:
    def __init__(self, full: ASGIApp, lean: ASGIApp, *, max_sessions: int = MAX_SESSIONS) -> None:
        self.apps = {"full": full, "lean": lean}
        self.sessions: OrderedDict[str, str] = OrderedDict()
        self.max_sessions = max_sessions

    def _remember(self, sid: str, profile: str) -> None:
        self.sessions[sid] = profile
        self.sessions.move_to_end(sid)
        while len(self.sessions) > self.max_sessions:
            self.sessions.popitem(last=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.apps["full"](scope, receive, send)
            return
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        requested = query.get("profile", [None])[-1]
        if requested is not None and requested not in PROFILES:
            # The caller's value is never echoed back.
            response = JSONResponse(
                {
                    "type": "about:blank",
                    "title": "Unknown profile",
                    "status": 400,
                    "detail": f"profile must be one of: {', '.join(PROFILES)}",
                },
                status_code=400,
                media_type="application/problem+json",
            )
            await response(scope, receive, send)
            return
        sid = _header(scope.get("headers", []), _SID)
        profile = (self.sessions.get(sid) if sid else None) or requested or "full"
        if sid and sid in self.sessions:
            self.sessions.move_to_end(sid)  # LRU: a used session is not evicted first
        app = self.apps[profile]
        if sid and scope.get("method") == "DELETE" and sid in self.sessions:
            sessions = self.sessions

            async def forget(message: Message) -> None:
                # Forget the id only once the issuing app has accepted the DELETE.
                if message["type"] == "http.response.start" and 200 <= message["status"] < 300:
                    sessions.pop(sid, None)
                await send(message)

            await app(scope, receive, forget)
            return
        if sid:
            await app(scope, receive, send)
            return

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                issued = _header(message.get("headers", []), _SID)
                if issued:
                    self._remember(issued, profile)
            await send(message)

        await app(scope, receive, capture)
