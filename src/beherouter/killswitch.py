"""A11: the kill switch -- stop every surface, quarantine one, block one caller.

State lives in ONE JSON file ($BEHEROUTER_KILLSWITCH_PATH), not in memory, so it
survives a restart and replicas sharing the file agree:

    {"all": {...}, "surfaces": {"dwh": {...}}, "subjects": {"<sub>": {...}}}

each value `{"reason", "at", "by"}`, all optional when a human writes it.

Read like the identity map (identity.SecretMap): one stat per call, re-parsed
only when (mtime_ns, size, ino) changes -- a ConfigMap, a Secret, an RWX volume
or a hand edit takes effect on the next call with no reload.

⚠️ A malformed file keeps the LAST-GOOD state (ERROR once per stamp, `stale`
on /healthz). Failing open on a typo would silently lift every block; failing
closed would make a typo a total outage. With no last-good state -- at boot --
it is a UsageError, and boot is refused.

⚠️ The file holds caller `sub` values. They never leave it except through the
admin API: not into a log, a metric, /healthz or a refusal. (The one exception
is not ours: a blocked caller's OWN refused call writes its per-call `tool_call`
audit line with its `sub`, as every caller's call does.)
"""

import asyncio
import contextlib
import copy
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Conflict, UsageError
from .logsetup import timestamp

KILLSWITCH_VAR = "BEHEROUTER_KILLSWITCH_PATH"
logger = logging.getLogger("beherouter.killswitch")

_KINDS = ("all", "surfaces", "subjects")
_NONE_BAD = object()


@dataclass(frozen=True)
class KillState:
    all: dict | None = None
    surfaces: dict[str, dict] = field(default_factory=dict)
    subjects: dict[str, dict] = field(default_factory=dict)

    def disabled(self, surface: str) -> str | None:
        """Why `surface` is stopped -- "all" or "surface" -- or None."""
        if self.all is not None:
            return "all"
        if surface in self.surfaces:
            return "surface"
        return None

    def empty(self) -> bool:
        return self.all is None and not self.surfaces and not self.subjects

    def as_json(self) -> dict:
        """A deep copy: the state is cached and shared by every call."""
        out: dict = {"surfaces": self.surfaces, "subjects": self.subjects}
        if self.all is not None:
            out["all"] = self.all
        return copy.deepcopy(out)


def _table(data: dict, kind: str) -> dict[str, dict]:
    raw = data.get(kind, {})
    if not isinstance(raw, dict):
        raise ValueError(f"'{kind}' must be an object")
    for key, value in raw.items():
        if not isinstance(key, str) or not key or not isinstance(value, dict):
            raise ValueError(f"'{kind}' must map non-empty names to objects")
    return dict(raw)


def parse_state(data: object) -> KillState:
    """The file's content as a KillState. ValueError on any other shape; the
    message names keys, never values."""
    if not isinstance(data, dict):
        raise ValueError("the top level must be an object")
    unknown = sorted(set(data) - set(_KINDS))
    if unknown:
        raise ValueError(f"unknown key(s) {unknown}; allowed: {list(_KINDS)}")
    stop_all = data.get("all")
    if stop_all is not None and not isinstance(stop_all, dict):
        raise ValueError("'all' must be an object or absent")
    return KillState(stop_all, _table(data, "surfaces"), _table(data, "subjects"))


def entry(reason: str | None, actor: str) -> dict:
    """What the admin API writes for one stop or block."""
    out = {"at": timestamp(time.time()), "by": actor}
    if reason:
        out["reason"] = reason
    return out


class KillSwitch:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._stamp: tuple[int, int, int] | None = None
        self._good: KillState | None = None
        # The stamp last found malformed; _NONE_BAD = none yet (a None stamp is
        # itself a value: the file could not even be stat'ed).
        self._bad_stamp: object = _NONE_BAD
        self.stale = False
        self._lock = asyncio.Lock()

    def _stamp_now(self) -> tuple[int, int, int] | None:
        """(mtime_ns, size, ino), or None for a missing file. OSError otherwise."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def state(self) -> KillState:
        """One stat per call; a read and a parse only when the stamp changed.
        A missing file is empty state. A file already found malformed is not
        re-parsed until it changes again."""
        stamp: tuple[int, int, int] | None = None
        try:
            stamp = self._stamp_now()
            if stamp is None:
                state = KillState()
            elif stamp == self._stamp and self._good is not None:
                state = self._good
            elif stamp == self._bad_stamp and self._good is not None:
                self.stale = True
                return self._good
            else:
                state = parse_state(json.loads(self.path.read_text()))
        except (OSError, ValueError) as e:
            # The exception TYPE, plus a ValueError's message (parse_state names
            # keys only; a JSONDecodeError gives a position): never file content.
            why = f"{type(e).__name__}: {e}" if isinstance(e, ValueError) else type(e).__name__
            if self._good is None:
                raise UsageError(
                    f"kill-switch file '{self.path}' is malformed or unreadable ({why})"
                ) from e
            if stamp != self._bad_stamp:
                logger.error(
                    "kill-switch file %r is malformed (%s); keeping the last good state",
                    str(self.path),
                    type(e).__name__,
                )
            self._bad_stamp = stamp
            self.stale = True
            return self._good
        if stamp is None and self._good is not None and not self._good.empty():
            # Missing = empty is the contract, but a file that VANISHES under a
            # live stop or block lifts all of it silently -- say so, once per
            # transition (the empty state that follows does not re-trigger it).
            # Counts only, never a name or a sub.
            logger.warning(
                "kill-switch file %r is gone; every stop and block it held is lifted "
                "(all=%s, %d surface(s), %d subject(s)). Restore the file to re-apply them",
                str(self.path),
                self._good.all is not None,
                len(self._good.surfaces),
                len(self._good.subjects),
            )
        self._stamp, self._good, self.stale, self._bad_stamp = stamp, state, False, _NONE_BAD
        return state

    def _load_raw(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def _write_raw(self, raw: dict) -> None:
        """Atomically: a temp file in the same directory, fsync'd, mode 0600,
        then renamed over the old one."""
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".killswitch.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(raw, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    async def update(self, change: Callable[[dict], None], *, actor: str) -> KillState:
        """Read-modify-write the file under a process lock, atomically.

        ⚠️ Two writers in DIFFERENT processes sharing one file can race; the last
        writer wins. Re-reading first is what keeps a hand edit made between two
        API writes."""
        async with self._lock:
            try:
                raw = await asyncio.to_thread(self._load_raw)
                parse_state(raw)
            except (OSError, ValueError) as e:
                raise Conflict(
                    f"kill-switch file '{self.path}' is malformed or unreadable "
                    f"({type(e).__name__}); fix it by hand before writing through the API"
                ) from e
            try:
                change(raw)
                parse_state(raw)  # our own change must leave a valid file
            except ValueError as e:
                raise UsageError(
                    f"that change would leave an invalid kill-switch file ({e})"
                ) from e
            try:
                # fsync and replace off the loop: a slow volume must not stall calls
                await asyncio.to_thread(self._write_raw, raw)
            except OSError as e:
                raise Conflict(
                    f"kill-switch file '{self.path}' cannot be written "
                    f"({type(e).__name__}); it is read-only here -- edit it where it "
                    f"is managed"
                ) from e
            logger.info("kill-switch file updated by an administrator")
            return self.state()


_SWITCHES: dict[str, KillSwitch] = {}


def configured() -> KillSwitch | None:
    """The gateway's kill switch, or None when $BEHEROUTER_KILLSWITCH_PATH is
    unset. One instance per path, so every surface shares one stat-cache."""
    path = os.environ.get(KILLSWITCH_VAR, "").strip()
    if not path:
        return None
    if path not in _SWITCHES:
        _SWITCHES[path] = KillSwitch(path)
    return _SWITCHES[path]


def stage_for(surface: str):
    """The surface-wide pipeline stage (pipeline.Stage), or None when the kill
    switch is not configured -- an unconfigured gateway's call path is unchanged.

    Order is a contract: a stopped surface answers before a blocked caller is
    looked at, so a blocked caller on a stopped surface learns only that.
    Refusals are tool-level errors, never HTTP 401/403 (LibreChat reads a 401
    as "OAuth required"), and name the category -- never the stored reason,
    never the caller's sub.
    """
    ks = configured()
    if ks is None:
        return None
    from .errors import AuthError, tag

    async def stage(scope) -> None:
        state = ks.state()
        which = state.disabled(scope.surface)
        if which is not None:
            raise tag(
                AuthError(f"surface '{scope.surface}' is stopped by an administrator"),
                "surface_disabled",
                scope=which,
            )
        caller = scope.caller
        if caller.auth == "oidc" and caller.sub and caller.sub in state.subjects:
            raise tag(
                AuthError("your access to this gateway is suspended by an administrator"),
                "caller_blocked",
            )

    return stage
