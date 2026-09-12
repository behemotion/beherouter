"""Refresh token -> access token. Held in memory, never written to disk.

This is the design's single biggest simplification versus running the upstream
Node servers as sidecars: they cache tokens in a file, which would give the
gateway its first writable volume. Here the long-lived secret arrives through
the registry's `env` (a `${VAR}` from the deployment's vault) and the short-lived
one never outlives the process.

The cost is that a refresh token revoked upstream cannot be repaired locally —
which is correct, and is exactly what `probe` exists to make visible.
"""

import time

import httpx

from ...errors import AuthError, Unavailable

# Refresh this many seconds BEFORE nominal expiry. A token that is valid when we
# check and expired when the provider reads it produces a 401 that looks like a
# revoked grant; the skew makes that race impossible.
_EXPIRY_SKEW_SECONDS = 60

# Used when the provider omits `expires_in` or sends something unparseable. An
# access token is cheap to re-fetch; raising here would turn a cosmetic payload
# quirk into a dead surface.
_DEFAULT_LIFETIME_SECONDS = 3600.0


class RefreshTokenAuth:
    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        refresh_token: str,
        client_secret: str | None = None,
        scope: str | None = None,
        client: httpx.AsyncClient | None = None,
        clock=time.monotonic,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._scope = scope
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def access_token(self) -> str:
        if self._access_token is not None and self._clock() < self._expires_at:
            return self._access_token
        await self._refresh()
        return self._access_token  # type: ignore[return-value]

    async def _refresh(self) -> None:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            form["client_secret"] = self._client_secret
        if self._scope:
            form["scope"] = self._scope

        try:
            resp = await self._client.post(
                self._token_url,
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as e:
            # Deliberately does not interpolate the exception's request, which
            # carries the form body and therefore the refresh token.
            raise Unavailable(f"token endpoint unreachable: {type(e).__name__}") from e

        if resp.status_code in (400, 401, 403):
            # The provider's own error CODE is safe and is the useful part
            # ("invalid_grant" = revoked, expired, or wrong authority). The body
            # is not echoed wholesale and the request form is never echoed.
            try:
                code = resp.json().get("error", "unknown_error")
            except ValueError:
                code = "unknown_error"
            raise AuthError(
                f"refresh grant rejected ({resp.status_code}): {code}. "
                f"The refresh token is revoked, expired, or was issued by the "
                f"wrong authority."
            )
        if resp.status_code >= 400:
            raise Unavailable(f"token endpoint returned {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as e:
            raise Unavailable("token endpoint did not return JSON") from e
        if not isinstance(payload, dict):
            # A JSON array or scalar would make every .get() below an
            # AttributeError, which is not an AxiError and would escape the
            # gateway's error funnel.
            raise Unavailable("token endpoint returned a non-object JSON body")

        token = payload.get("access_token")
        if not token or not isinstance(token, str):
            raise Unavailable("token endpoint returned no access_token")

        self._access_token = token
        # ⚠️ Microsoft's identity platform ROTATES the refresh token on every
        # refresh and invalidates the old one; Google does not rotate. Discarding
        # a rotation makes m365 work for about an hour and then fail with the
        # exact symptom of the `common`-authority trap the module docstrings
        # teach the operator to look for — a diagnostic dead end.
        #
        # Kept in memory only, so the no-disk-state property holds: a restart
        # falls back to the vault's value, which is what the provider hands back
        # to a client that never used the rotation.
        rotated = payload.get("refresh_token")
        if rotated and isinstance(rotated, str):
            self._refresh_token = rotated
        self._expires_at = self._clock() + self._lifetime(payload) - _EXPIRY_SKEW_SECONDS

    @staticmethod
    def _lifetime(payload: dict) -> float:
        """`expires_in` is provider-supplied and therefore not trustworthy.

        A null, missing or non-numeric value must not raise: TypeError and
        ValueError are not AxiErrors, and `health --deep` would traceback
        instead of reporting this one backend as failed.
        """
        try:
            return float(payload.get("expires_in") or _DEFAULT_LIFETIME_SECONDS)
        except (TypeError, ValueError):
            return _DEFAULT_LIFETIME_SECONDS

    async def aclose(self) -> None:
        await self._client.aclose()
