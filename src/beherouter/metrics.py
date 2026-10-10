"""Prometheus series, on a private registry rendered at /metrics.

Label values come only from the catalogue, published names or configured
surface names — never from caller input — so cardinality is bounded by
configuration. Counters reset on restart, as every Prometheus counter does;
`rate()`/`increase()` handle that.
"""

import contextlib
from collections.abc import Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)

disable_created_metrics()

REGISTRY = CollectorRegistry()
UNKNOWN_TOOL = "<unknown>"

TOOL_CALLS = Counter(
    "beherouter_tool_calls",
    "Tool calls, by surface, tool (run_tool's inner tool) and outcome.",
    ("surface", "tool", "outcome"),
    registry=REGISTRY,
)
TOOL_DURATION = Histogram(
    "beherouter_tool_call_duration_seconds",
    "Tool call latency, gate to result.",
    ("surface", "tool"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
    registry=REGISTRY,
)
AUTH_REJECTIONS = Counter(
    "beherouter_auth_rejections",
    "Bearer tokens the gateway refused, by reason and surface.",
    ("reason", "surface"),
    registry=REGISTRY,
)
SURFACE_UP = Gauge(
    "beherouter_surface_up",
    "1 when the surface is attached, 0 while it answers 503.",
    ("surface",),
    registry=REGISTRY,
)
SURFACE_DISABLED = Gauge(
    "beherouter_surface_disabled",
    "1 while the kill switch stops the surface (directly or by 'all').",
    ("surface",),
    registry=REGISTRY,
)
BLOCKED_SUBJECTS = Gauge(
    "beherouter_blocked_subjects",
    "How many caller subjects the kill switch blocks. A count, never a name.",
    registry=REGISTRY,
)
ACTIVE_SESSIONS = Gauge(
    "beherouter_active_sessions",
    "Open MCP sessions on the surface.",
    ("surface",),
    registry=REGISTRY,
)

RELOADS = Counter(
    "beherouter_reloads",
    "Registry reloads, by what triggered them and how they ended.",
    ("trigger", "outcome"),
    registry=REGISTRY,
)
RELOAD_LAST_SUCCESS = Gauge(
    "beherouter_reload_last_success_timestamp_seconds",
    "Unix time of the last reload whose lint passed (ok or partial).",
    registry=REGISTRY,
)


def observe_call(surface: str, tool: str, kind: str, seconds: float) -> None:
    TOOL_CALLS.labels(surface=surface, tool=tool, outcome=kind).inc()
    TOOL_DURATION.labels(surface=surface, tool=tool).observe(seconds)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def untrack_sessions(surface: str) -> None:
    """A stateless surface has no session table: drop its series rather than
    report a constant 0 that reads as 'nobody connected'."""
    with contextlib.suppress(KeyError):
        ACTIVE_SESSIONS.remove(surface)


def forget_surface(surface: str) -> None:
    """A removed surface's GAUGES go; its counters stay -- rate() over history
    still means something, and a counter that vanishes mid-series is worse
    than one that stops rising."""
    for gauge in (SURFACE_UP, ACTIVE_SESSIONS, SURFACE_DISABLED):
        with contextlib.suppress(KeyError):
            gauge.remove(surface)


def track_surface_up(surface: str, up: Callable[[], bool]) -> None:
    SURFACE_UP.labels(surface=surface).set_function(lambda: 1.0 if up() else 0.0)


def track_killswitch(surface: str, ks) -> None:
    """Read at scrape time; the state's stat-cache makes that one stat."""
    SURFACE_DISABLED.labels(surface=surface).set_function(
        lambda: 1.0 if ks.state().disabled(surface) else 0.0
    )
    BLOCKED_SUBJECTS.set_function(lambda: float(len(ks.state().subjects)))


def _session_holder(app: object) -> object | None:
    """The object whose `session_manager` attribute FastMCP fills in: the
    `StreamableHTTPASGIApp` that is a route's endpoint on `http_app()`."""
    for route in getattr(app, "routes", ()):
        endpoint = getattr(route, "endpoint", None)
        # With auth configured FastMCP wraps the endpoint in
        # RequireAuthMiddleware, which keeps the real one at `.app`.
        for candidate in (endpoint, getattr(endpoint, "app", None)):
            if hasattr(candidate, "session_manager"):
                return candidate
    return None


def track_sessions(surface: str, app: object) -> bool:
    """Count `app`'s open sessions at scrape time. False, and no series, when
    this FastMCP does not expose its session table.

    ⚠️ Reads a PRIVATE attribute (`_server_instances` of the MCP SDK's
    StreamableHTTPSessionManager). FastMCP's `http_app()` creates that manager
    inside the app's lifespan and hangs it on the route endpoint
    (`StreamableHTTPASGIApp.session_manager`, None until the lifespan starts),
    so it is looked up at scrape time, never captured. There is no public
    count; test_metrics holds this against the pinned version.
    """
    holder = _session_holder(app)
    if holder is None:
        return False

    def count() -> float:
        instances = getattr(getattr(holder, "session_manager", None), "_server_instances", None)
        if not isinstance(instances, dict):
            return 0.0
        # A DELETE-terminated transport stays in the table, flagged.
        return float(sum(1 for t in instances.values() if not getattr(t, "is_terminated", False)))

    ACTIVE_SESSIONS.labels(surface=surface).set_function(count)
    return True
