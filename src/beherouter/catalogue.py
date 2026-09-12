"""A backend's tool catalogue, with freshness — the searchable half only.

WHY THIS EXISTS. The catalogue was read once at attach and never again, so a
backend that gained or lost tools stayed invisible until the gateway restarted.
The MCP client-best-practices doc asks clients to "re-index the search catalog
when a server sends notifications/tools/list_changed".

WHY IT REFRESHES ONLY THE SEARCHABLE HALF. The same doc warns that "adding or
removing tool definitions mid-conversation invalidates that cache, and the
resulting miss can cost more tokens than the definitions you removed". Those two
guidelines pull opposite ways — unless you notice that the searchable catalogue
(this module's index, describe_tool's lookup, run_tool's routing) lives entirely
behind meta-tools and is INVISIBLE to the published `tools` array. Only the
pinned set is array-visible.

So: the index refreshes freely, the pinned set stays frozen for the process
lifetime, and divergence between them is REPORTED as drift rather than silently
reconciled. Both guidelines are satisfied at once.

WHY NO NOTIFICATION LISTENER. Receiving `list_changed` needs a long-lived
session, which reintroduces exactly the stale-socket and dead-subprocess
fragility `ReconnectingMCPExecutor` was written to avoid — and a dead listener
fails silently: no error, just a gateway that quietly stops refreshing. A TTL
fails loudly and predictably instead.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .indexing import build_index
from .models import ToolDescriptor
from .search import ToolIndex

logger = logging.getLogger(__name__)

STATUS_OK = "ok"  # refreshed, catalogue unchanged
STATUS_DRIFT = "drift"  # refreshed, tools appeared or vanished
STATUS_STALE = "stale"  # refresh failed; serving the last good catalogue
STATUS_NONE = "none"  # this backing cannot re-list; nothing is claimed

Relister = Callable[[], Awaitable[list[ToolDescriptor]]]


@dataclass(frozen=True)
class Drift:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    # A tool that was PINNED and is no longer served. Not ordinary drift: the
    # gateway is still publishing it, so it is a broken published tool.
    pinned_missing: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.pinned_missing)


class Catalogue:
    """Descriptors + BM25 index + freshness, for one backend.

    `relist=None` (every `cli` and `native` backing) makes this inert: status is
    permanently `none` and `ensure_fresh` is a no-op, so those backings see no
    behavioural change at all.
    """

    def __init__(
        self,
        descriptors: list[ToolDescriptor],
        *,
        relist: Relister | None = None,
        ttl_ms: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        name: str = "",
    ) -> None:
        self._descriptors = list(descriptors)
        self._relist = relist
        self._ttl_ms = ttl_ms
        self._clock = clock
        # For the drift log line only -- identifies which surface drifted when
        # several are running in the same process. Purely cosmetic: nothing
        # else in this class reads it.
        self._name = name
        # The pin list is captured ONCE, from the descriptors as attached. It is
        # the baseline `pinned_missing` is measured against, and it must not
        # follow the backend — a backend that stops serving a pinned tool has
        # not thereby un-pinned it.
        self._pinned_names = tuple(d.name for d in descriptors if d.pinned)
        self._fetched_at = clock()
        # True while a re-list is in flight. See `ensure_fresh`.
        self._refreshing = False
        self._index: ToolIndex | None = None
        self._status = STATUS_NONE if not self._enabled else STATUS_OK
        self._drift = Drift()

    @property
    def _enabled(self) -> bool:
        return self._relist is not None and bool(self._ttl_ms)

    @property
    def descriptors(self) -> list[ToolDescriptor]:
        return self._descriptors

    @property
    def by_name(self) -> dict[str, ToolDescriptor]:
        return {d.name: d for d in self._descriptors}

    @property
    def index(self) -> ToolIndex:
        if self._index is None:
            self._index = build_index(self._descriptors)
        return self._index

    @property
    def status(self) -> str:
        return self._status

    @property
    def drift(self) -> Drift:
        return self._drift

    async def ensure_fresh(self) -> None:
        """Re-list if the cached catalogue has outlived its TTL.

        A refresh FAILURE is not an outage: the last good catalogue keeps being
        served and the status becomes `stale`. Letting a transient re-list error
        take down `search_tools` would trade a slightly old index for a dead
        surface — the wrong direction, and the same reasoning behind /healthz
        deliberately not fanning out to the backends.

        The clock is reset on failure too, so a dead backend is retried once per
        TTL rather than on every single call.

        CONCURRENCY. `_fetched_at` only moves AFTER the re-list returns, so the
        TTL check is not a critical section: two meta-tool calls arriving past
        the TTL would both fire. The duplicated round trip is the cheap half.
        The sharp half is that the second caller's `_diff` would run against the
        list the first one already installed, see no change, and overwrite a
        genuine `drift` with `ok` before anyone read it. Four meta-tools sit on
        this path, so the window is wider than when this was written.

        A caller that finds a refresh already in flight RETURNS rather than
        waiting for it. Serving the last-good catalogue one generation stale is
        the same trade this method already makes on a failed re-list, and it
        keeps a backend round trip off the request path -- the instinct behind
        /healthz not fanning out. A lock would be the other choice: the second
        caller would get the fresh list, at the cost of blocking on someone
        else's network call.
        """
        if not self._enabled:
            return
        if (self._clock() - self._fetched_at) * 1000 < self._ttl_ms:
            return
        if self._refreshing:
            return
        self._refreshing = True
        try:
            fresh = await self._relist()
        except Exception:
            logger.warning(
                "re-list failed; serving the last good catalogue as stale",
                exc_info=True,
            )
            self._fetched_at = self._clock()
            self._status = STATUS_STALE
            return
        finally:
            self._refreshing = False
        self._drift = self._diff(fresh)
        self._descriptors = list(fresh)
        self._index = None  # rebuilt lazily on next access
        self._fetched_at = self._clock()
        self._status = STATUS_DRIFT if self._drift.changed else STATUS_OK
        if self._drift.changed:
            # A trace even when nobody is polling context_cost during THIS
            # particular TTL window: `added`/`removed` are measured against
            # the previous refresh (see `_diff`), not the attach-time
            # baseline, so an unobserved drift reverts to `ok` on the next
            # refresh and leaves no other record.
            logger.warning(
                "catalogue drift on %r: added=%s removed=%s pinned_missing=%s",
                self._name,
                self._drift.added,
                self._drift.removed,
                self._drift.pinned_missing,
            )

    def _diff(self, fresh: list[ToolDescriptor]) -> Drift:
        before = {d.name for d in self._descriptors}
        after = {d.name for d in fresh}
        return Drift(
            added=tuple(sorted(after - before)),
            removed=tuple(sorted(before - after)),
            pinned_missing=tuple(sorted(n for n in self._pinned_names if n not in after)),
        )
