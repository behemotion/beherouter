"""Provider adapters. Only these modules know a specific provider exists.

Every adapter returns the SAME normalised shapes so an agent's vocabulary does
not change with the backing provider. That is the whole point of two plugins
over one core: the credential, the surface and the Caddy token fork; the
agent-facing vocabulary must not.
"""

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
