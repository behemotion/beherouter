"""The calendar core, shared by the `gcal` and `m365` plugins.

TWO PLUGINS, ONE CORE. The credential, the surface path and the Caddy token fork
— so a dead Google refresh token cannot take the Microsoft surface down with it
— while the agent-facing vocabulary does not fork at all. `build_backend` is the
single place a calendar Backend is assembled, and
`test_gcal_and_m365_expose_identical_tool_schemas` holds that as an invariant.

A native plugin needs no loader under `backends/`: `models.Backend` is only
`descriptors + executor`, and `surface.build_surface` does not care where the
descriptors came from.

⚠️ ATTACH PERFORMS NO NETWORK I/O. Nothing here opens a socket; the first token
fetch happens on the first `run()`. An attach failure crash-loops the whole
gateway and takes /healthz with it, so a revoked refresh token must fail the
PROBE instead.
"""

from ...models import Backend
from .executor import CalendarExecutor
from .tools import descriptors_for

__all__ = ["CalendarExecutor", "build_backend", "descriptors_for"]


def build_backend(
    *,
    surface: str,
    provider,
    pinned: list[str],
    max_results: int = 50,
    provider_factory=None,
) -> Backend:
    return Backend(
        name=surface,
        kind="native",
        descriptors=descriptors_for(pinned),
        executor=CalendarExecutor(
            provider, max_results=max_results, provider_factory=provider_factory
        ),
    )
