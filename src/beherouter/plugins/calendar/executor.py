"""Validate arguments, then dispatch a verb onto a provider adapter.

Validation lives HERE rather than in the adapters for one reason: a typo'd
argument reaching `provider.list_events(calender_id=...)` raises TypeError, and
TypeError is not an AxiError — it would escape `health.check_entry` and
crash-loop the whole gateway. Every argument is checked against the same schema
the surface advertises, so the error an agent gets names the tool it called.
"""

from ...errors import AxiError, Unavailable, UsageError
from .tools import SCHEMAS

# beheaxi manifest arg types -> Python types, mirroring surface._PY_TYPES. A
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
    """The `Executor` protocol from beherouter.models: async run(verb, args)."""

    def __init__(self, provider, *, max_results: int = 50) -> None:
        self._provider = provider
        self._max_results = max_results

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
        if identity is not None:
            # Threaded through by the seam in Task 5; this backing does not
            # apply it yet. Raising rather than ignoring keeps the "no silent
            # shared fallback" rule whole.
            raise UsageError(
                f"backend call '{verb}': this backing cannot yet apply a "
                f"per-request identity"
            )
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
        try:
            return await getattr(self._provider, verb)(**call)
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
