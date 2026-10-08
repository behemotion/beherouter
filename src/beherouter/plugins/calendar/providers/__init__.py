"""Provider adapters. Only these modules know a specific provider exists.

Every adapter returns the SAME normalised shapes so an agent's vocabulary does
not change with the backing provider. That is the whole point of two plugins
over one core: the credential, the surface and the Caddy token fork; the
agent-facing vocabulary must not.
"""

from urllib.parse import quote

import httpx

from ....errors import AuthError, NotFound, Unavailable, UsageError


def raise_for_status(resp: httpx.Response, context: str) -> None:
    """Map an HTTP status onto the AxiError the gateway understands.

    Anything that is not an AxiError escapes health.check_entry's except clause
    and crash-loops the gateway, so every provider call funnels through here.
    """
    if resp.status_code < 400:
        return

    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                detail = err.get("message", "")
            elif isinstance(err, str):
                detail = err
    except ValueError:
        detail = resp.text[:200]

    if resp.status_code in (401, 403):
        raise AuthError(f"{context}: {resp.status_code} {detail}")
    if resp.status_code == 404:
        raise NotFound(f"{context}: {detail or 'not found'}")
    if resp.status_code in (400, 422):
        raise UsageError(f"{context}: {detail or 'rejected by the provider'}")
    raise Unavailable(f"{context}: {resp.status_code} {detail}")


class HttpCalendarProvider:
    """The HTTP plumbing both adapters share: one client, one error funnel,
    one path-escaping rule.

    Everything provider-specific is a class attribute or an override: `BASE`,
    `DEFAULT_CALENDAR`, and `_headers` (Microsoft adds a `Prefer`). State is
    per INSTANCE, never per class — each provider holds its own credential and
    its own clients, which is what keeps a dead Google grant from touching the
    Microsoft surface.
    """

    BASE: str
    # What an absent `calendar_id` means; None = the provider's own default.
    DEFAULT_CALENDAR: str | None = None

    def __init__(
        self,
        *,
        auth,
        calendar_id: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._auth = auth
        self._default_calendar = calendar_id or self.DEFAULT_CALENDAR
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def _headers(self) -> dict:
        return {"Authorization": f"Bearer {await self._auth.access_token()}"}

    async def _request(self, method: str, path: str, *, context: str, **kw) -> dict:
        try:
            resp = await self._client.request(
                method, f"{self.BASE}{path}", headers=await self._headers(), **kw
            )
        except httpx.HTTPError as e:
            raise Unavailable(f"{context}: {type(e).__name__}") from e
        raise_for_status(resp, context)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    @staticmethod
    def _segment(value: str) -> str:
        """Percent-encode a user-supplied URL path segment.

        ⚠️ An id is whatever the agent passes, and each grant is broad: a
        '../' segment escapes the six-verb surface — the surface's whole
        boundary — into the rest of the provider's API. And real ids need it:

        - Google's secondary calendar ids genuinely contain '#' and '@'
          (en.usa#holiday@group.v.calendar.google.com), and list_calendars
          hands exactly those ids to the agent. Unquoted, the '#' truncates the
          path at the fragment; the grant is the broad calendar scope, so a
          traversal reaches the whole Calendar API.
        - Graph calendar and event ids are long base64-ish strings that can
          carry '/' and '='; a traversal reaches the rest of Graph under the
          same Calendars.ReadWrite grant.
        """
        return quote(value, safe="")

    async def aclose(self) -> None:
        """Close this provider's clients: its own and its token refresher's."""
        await self._client.aclose()
        await self._auth.aclose()
