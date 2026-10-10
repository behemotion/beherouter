"""Registry hot reload (spec §2): fingerprints, the diff, the triggers."""

import asyncio
import hashlib
import json
import logging
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .envexpand import expand, file_refs
from .errors import UsageError

logger = logging.getLogger("beherouter.gateway")

DRAIN_VAR = "BEHEROUTER_RELOAD_DRAIN_S"
WATCH_VAR = "BEHEROUTER_REGISTRY_WATCH_S"


def drain_s() -> float:
    raw = os.environ.get(DRAIN_VAR, "").strip()
    if not raw:
        return 30.0
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not (math.isfinite(value) and value >= 0):
        raise UsageError(f"${DRAIN_VAR} must be a number of seconds >= 0, got {raw!r}")
    return value


def fingerprint(entry) -> str:
    """sha256 over the entry AND the resolved value of every placeholder, so a
    rotated ${file:} secret changes its own surface's fingerprint. Only the
    digest is kept, never a value."""
    resolved = expand(entry.name, entry.env) if entry.env else {}
    fields = asdict(entry)
    # An explicit `stateless = false` is the default spelled out; it must not
    # read as a change and re-attach the surface.
    fields["stateless"] = bool(fields.get("stateless"))
    doc = {"entry": fields, "resolved": resolved}
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, default=str).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Diff:
    added: list[str]
    removed: list[str]
    changed: list[str]
    unchanged: list[str]


def diff(old: dict[str, str], new: dict[str, str]) -> Diff:
    return Diff(
        added=sorted(set(new) - set(old)),
        removed=sorted(set(old) - set(new)),
        changed=sorted(n for n in new if n in old and new[n] != old[n]),
        unchanged=sorted(n for n in new if n in old and new[n] == old[n]),
    )


def watched_paths(path: Path, registry: dict) -> list[Path]:
    """The registry file plus every ${file:} it references: what a watch stats."""
    out = [Path(path)]
    for entry in registry.values():
        out += [Path(p) for p in file_refs(entry.env)]
    return out


def watch_stamp(paths: list[Path]) -> tuple:
    """(mtime_ns, size, ino) per path, symlinks FOLLOWED -- the atomic symlink swap
    of a Kubernetes ConfigMap or Secret volume changes the target's inode."""
    out: list[tuple[int, int, int] | None] = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            out.append(None)
            continue
        out.append((st.st_mtime_ns, st.st_size, st.st_ino))
    return tuple(out)


def watch_s() -> float | None:
    raw = os.environ.get(WATCH_VAR, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not (math.isfinite(value) and value >= 0):
        raise UsageError(f"${WATCH_VAR} must be a number of seconds >= 0, got {raw!r}")
    return value or None


def _fail(fut: asyncio.Future, exc: BaseException) -> None:
    """Hand `exc` to whoever still awaits `fut`. Marked retrieved at once: every
    requester may already have given up; the caller has logged the failure,
    so asyncio must not add a 'Future exception was never retrieved'."""
    fut.set_exception(exc)
    fut.exception()


class Reloader:
    """Serializes reloads and COALESCES the requests that arrive during one:
    however many come in, exactly one follow-up runs, and each of them gets its
    result (or its exception). A queue would replay the same registry N times.

    Each reload runs in a task the Reloader owns, never in a requester's: every
    requester awaits it through `asyncio.shield`, so a requester that is
    cancelled (an admin client disconnecting) cancels only its own wait, not a
    reload half-way through. Only `cancel()` stops a reload; shutdown calls it,
    so none outlives the lifespan."""

    def __init__(self, apply: Callable[[str], Awaitable[dict]]) -> None:
        self._apply = apply
        self._task: asyncio.Task | None = None
        self._next: asyncio.Future | None = None
        self._next_trigger = ""
        self._closed = False

    @property
    def idle(self) -> bool:
        return self._task is None or self._task.done()

    @property
    def closed(self) -> bool:
        """True once `cancel()` ran: the runtime is shutting down, and every
        request answers `failed` without reloading."""
        return self._closed

    async def request(self, trigger: str) -> dict:
        if self._closed:
            # after cancel(): the runtime is being torn down, nothing to reload into
            return {
                "trigger": trigger, "outcome": "failed", "added": [], "changed": [],
                "removed": [], "unchanged": [], "failed": [],
                "error": "the gateway is shutting down; reload refused",
            }
        loop = asyncio.get_running_loop()
        if not self.idle:
            if self._next is None:
                self._next = loop.create_future()
                self._next_trigger = trigger
            return await asyncio.shield(self._next)
        first = loop.create_future()
        self._task = asyncio.create_task(self._run(trigger, first), name="beherouter-reload")
        return await asyncio.shield(first)

    async def _run(self, trigger: str, first: asyncio.Future) -> None:
        pending: asyncio.Future | None = first
        try:
            try:
                first.set_result(await self._apply(trigger))
            except Exception as e:  # handed to its requester, logged
                logger.error("registry reload (%s) raised", trigger, exc_info=e)
                _fail(first, e)
            # the follow-up runs even when the first apply failed, or its
            # waiters would hang
            while self._next is not None:
                pending, self._next = self._next, None
                follow_up = self._next_trigger
                try:
                    pending.set_result(await self._apply(follow_up))
                except Exception as e:  # handed to every waiter, logged
                    logger.error("registry reload (%s) raised", follow_up, exc_info=e)
                    _fail(pending, e)
        finally:
            # a cancellation cannot finish the reload or run the follow-up:
            # fail their waiters instead of leaving them to hang
            for fut in (pending, self._next):
                if fut is not None and not fut.done():
                    _fail(fut, RuntimeError("reload cancelled"))
            self._next = None

    async def cancel(self) -> None:
        """Cancel the reload in flight, if any, and wait for it to stop. Terminal:
        every later request() answers `failed` instead of starting a reload."""
        self._closed = True
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the CALLER was cancelled, not just the reload
