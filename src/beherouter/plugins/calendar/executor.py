"""Validate arguments, then dispatch a verb onto a provider adapter.

Validation lives HERE rather than in the adapters for one reason: a typo'd
argument reaching `provider.list_events(calender_id=...)` raises TypeError, and
TypeError is not an AxiError — it would escape `health.check_entry` and
crash-loop the whole gateway. Every argument is checked against the same schema
the surface advertises, so the error an agent gets names the tool it called.
"""

import logging
from collections import OrderedDict

from ...errors import AxiError, Unavailable, UsageError
from .tools import SCHEMAS

logger = logging.getLogger(__name__)

# beheaxi manifest arg types -> Python types, mirroring args.PY_TYPES. A
# schema type absent from this map (or a property with no "type" at all) means
# "don't check" rather than a guess.
_JSON_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class CalendarExecutor:
    """The `Executor` protocol from beherouter.models: async run(verb, args).

    `provider_factory` is what makes a native surface per-user: given one
    caller's credentials it returns a provider for that caller, cached under
    their identity's digest. The cache is BOUNDED because a provider holds a
    refreshed access token — worth keeping between a user's calls, and not
    worth keeping for every user who ever called.

    ⚠️ AN EVICTED PROVIDER IS CLOSED, BUT NEVER MID-CALL. Each provider holds
    two httpx clients (its own and its token refresher's), so dropping one
    without `aclose()` leaks both. Closing it at eviction would be worse: a
    slow call for the evicted user is still using those clients. So eviction
    RETIRES a provider, every `run` counts the calls in flight per provider,
    and a retired provider is closed once its count reaches zero — at the
    evicting call if it is idle, or when its own last call finishes.
    """

    def __init__(
        self,
        provider,
        *,
        max_results: int = 50,
        provider_factory=None,
        cache_size: int = 128,
    ) -> None:
        self._provider = provider
        self._max_results = max_results
        self._factory = provider_factory
        self._cache_size = cache_size
        self._providers: OrderedDict[str, object] = OrderedDict()
        # id(provider) -> calls in flight. Keyed by id() because a provider is
        # not required to be hashable; every provider counted here is also
        # referenced from the cache, `_retired` or the call holding it, so its
        # id cannot be reused while it is a key.
        self._inflight: dict[int, int] = {}
        # Evicted providers not yet closed: waiting for their last call.
        self._retired: list[object] = []

    def _provider_for(self, identity):
        if identity is None or not identity.credentials:
            if self._provider is None:
                raise UsageError(
                    "this surface requires a per-user identity, and this call "
                    "carries none"
                )
            return self._provider
        if self._factory is None:
            raise UsageError(
                "this surface cannot apply a per-request identity: its plugin "
                "supplied no provider factory"
            )
        key = identity.cache_key
        cached = self._providers.get(key)
        if cached is not None:
            self._providers.move_to_end(key)
            return cached
        built = self._factory(dict(identity.credentials))
        self._providers[key] = built
        if len(self._providers) > self._cache_size:
            _, evicted = self._providers.popitem(last=False)
            self._retired.append(evicted)
        return built

    def _acquire(self, identity):
        """Resolve and count a provider in ONE synchronous step.

        No await between the lookup and the increment: otherwise another task
        could evict and close the provider in the gap.
        """
        provider = self._provider_for(identity)
        self._inflight[id(provider)] = self._inflight.get(id(provider), 0) + 1
        return provider

    def _release(self, provider) -> None:
        left = self._inflight[id(provider)] - 1
        if left:
            self._inflight[id(provider)] = left
        else:
            del self._inflight[id(provider)]

    async def _close_idle_retired(self) -> None:
        idle = [p for p in self._retired if id(p) not in self._inflight]
        if not idle:
            return
        # Detach the list before awaiting, so a concurrent call cannot close
        # the same provider twice.
        self._retired = [p for p in self._retired if id(p) in self._inflight]
        for provider in idle:
            await _close_quietly(provider)

    async def aclose(self) -> None:
        """Close the deployment provider and every cached per-user one.

        The same `executor.aclose()` contract as `InprocExecutor`, so whoever
        drops a backend can release it without knowing its backing.
        """
        providers = [self._provider, *self._providers.values(), *self._retired]
        self._providers.clear()
        self._retired.clear()
        for provider in providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    def _validate(self, verb: str, args: dict) -> None:
        schema = SCHEMAS[verb]
        properties = schema["properties"]
        unknown = sorted(set(args) - set(properties))
        if unknown:
            raise UsageError(
                f"'{verb}': unknown argument(s) {unknown}; expected any of {sorted(properties)}"
            )
        missing = sorted(set(schema["required"]) - set(args))
        if missing:
            raise UsageError(f"'{verb}': missing required argument(s) {missing}")
        for name, value in args.items():
            expected = _JSON_TYPES.get(properties[name].get("type"))
            if expected is None:
                continue
            # bool is a subclass of int in Python; accepting True for an
            # integer field would silently turn a typo into the value 1.
            wrong = not isinstance(value, expected) or (
                expected is int and isinstance(value, bool)
            )
            if wrong:
                raise UsageError(
                    f"'{verb}': argument '{name}' must be {expected.__name__}, "
                    f"got {type(value).__name__}"
                )

    async def run(self, verb: str, args: dict, *, identity=None) -> dict:
        if verb not in SCHEMAS:
            raise UsageError(
                f"unknown verb '{verb}'; this surface exposes {sorted(SCHEMAS)}"
            )
        self._validate(verb, args)
        call = dict(args)
        # The surface drops unset optionals before they reach here, so an absent
        # max_results means "the operator's configured default", not "none".
        if verb == "list_events" and "max_results" not in call:
            call["max_results"] = self._max_results
        provider = self._acquire(identity)
        try:
            # Whatever this call's acquire just evicted is closed now if idle,
            # rather than waiting for some later call to notice it.
            await self._close_idle_retired()
            return await self._call(provider, verb, call)
        finally:
            self._release(provider)
            await self._close_idle_retired()

    async def _call(self, provider, verb: str, call: dict) -> dict:
        try:
            return await getattr(provider, verb)(**call)
        except AxiError:
            raise
        except Exception as e:
            # The last hole in the AxiError funnel: a 2xx with a non-JSON body
            # raises a bare ValueError out of resp.json(), and anything that is
            # not an AxiError escapes health.check_entry — losing the state of
            # every OTHER backend in the same `health --deep` run.
            #
            # ⚠️ type(e).__name__, never {e}: a blanket handler that
            # interpolated the exception would re-open the secret-leak path
            # oauth.py closes, since an httpx error carries its request and the
            # refresh grant's form body with it.
            raise Unavailable(f"'{verb}' failed: {type(e).__name__}") from e


async def _close_quietly(provider) -> None:
    """Close an evicted provider without letting its failure reach a caller.

    The call that triggers the close belongs to a DIFFERENT user; a socket
    error tearing down someone else's client must not fail it. Only the type
    is logged, for the same secret-leak reason as `run`'s funnel.
    """
    close = getattr(provider, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception as e:  # noqa: BLE001 - see docstring
        logger.warning("closing an evicted calendar provider failed: %s", type(e).__name__)
