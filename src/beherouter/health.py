"""Deep backend health: does the backend's CREDENTIAL still work?

`/healthz` answers "is the gateway up and did its registry load" and deliberately
stops there (see `gateway.healthz`). That is the right contract for an external
monitor but it cannot see the failure that actually took the service down on
2026-07-30: a revoked Gitea PAT. `tools/list`, `search_tools` and `describe_tool`
are all served from the catalogue cached at attach time, so they stayed perfectly
green while every `tools/call` failed with "invalid username, password or token".

Only calling a tool touches the credential. Which tool is cheap AND
authenticating is backend-specific, so the PLUGIN names it (`probe`) and a
registry entry may override it; this module calls it through the same executor
the gateway itself uses.

Each check reports rather than raises: one dead backend must not hide the state
of the others.
"""

import json
import logging

from .costing import surface_cost
from .errors import AxiError
from .gateway import load_backend
from .plugins import PLUGINS, resolve_pinned
from .registry import RegistryEntry

logger = logging.getLogger(__name__)

ATTACH_OK = "ok"
ATTACH_FAILED = "failed"

# probe verdicts
PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_NONE = "none"  # no probe configured -> UNKNOWN, never green
PROBE_SKIPPED = "skipped"  # attach failed, so there was nothing to call

# catalogue verdicts
CATALOGUE_OK = "ok"
CATALOGUE_PINNED_MISSING = "pinned_missing"

# A pinned tool the backend no longer serves is a BROKEN PUBLISHED TOOL: the
# gateway advertises it and every call fails. That is a failure, unlike `drift`
# (a backend gaining tools is normal) and unlike PROBE_NONE (absence of
# evidence). Note that `drift` and `stale` are states of a LIVE Catalogue and
# are not observable here: check_entry attaches fresh, in a separate process,
# with no baseline to diff against. They ride on the context_cost payload
# instead.
_FAILURES = (ATTACH_FAILED, PROBE_FAILED, CATALOGUE_PINNED_MISSING)

# user-probe verdicts (`health --deep --bearer-file`)
USER_OK = "ok"
USER_NOT_APPLICABLE = "not_applicable"  # no identity mode: nothing per-user to prove
USER_SKIPPED = "skipped"  # no probe, or attach failed
USER_REJECTED = "rejected"  # the gateway would not accept this token at all
USER_REFUSED = "refused"  # accepted, but this surface refuses the caller
USER_FAILED = "failed"  # the backend call failed with the caller's identity
USER_MISMATCH = "mismatch"  # the backend answered as someone else
_USER_FAILURES = (USER_REJECTED, USER_REFUSED, USER_FAILED, USER_MISMATCH)

# Fields a backend's "who am I" answer is likely to carry, and the token claims
# a caller is likely to be recognised by. Compared case-insensitively.
_BACKEND_IDENTITY_KEYS = (
    "id", "email", "username", "preferred_username", "display_name", "login", "sub",
)
_CALLER_CLAIMS = ("email", "preferred_username", "sub", "upn")


def _backend_identity(result) -> dict:
    """The identity fields of a probe's answer, or {} when there are none."""
    payload = result.get("result") if isinstance(result, dict) else result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    # FastMCP wraps a non-object return as {"result": ...}; one level only.
    if isinstance(payload, dict) and set(payload) == {"result"}:
        payload = payload["result"]
    if not isinstance(payload, dict):
        return {}
    return {
        k: payload[k]
        for k in _BACKEND_IDENTITY_KEYS
        if isinstance(payload.get(k), (str, int)) and payload.get(k) != ""
    }


async def probe_as_user(entry, backend, probe, probe_args, token: str, verifier) -> dict:
    """Run the surface's probe AS the holder of `token`, through the same gate
    and materialisation a real call goes through.

    ⚠️ This is what a green deployment-credential probe cannot tell you: that
    a USER's identity reaches the backend and the backend answers as them.
    The token is verified by the gateway's own verifier first, so an expired or
    mis-addressed token is reported as the gateway would treat it.
    """
    from . import auth
    from .errors import AxiError
    from .identity import identity_from_token, policy_from_entry

    plugin = PLUGINS.get(entry.plugin)
    policy = policy_from_entry(entry, plugin.spec) if plugin else None
    if policy is None or not policy.mode:
        return {
            "state": USER_NOT_APPLICABLE,
            "reason": "the surface has no identity mode, so every call uses the "
            "deployment credential; there is no per-user path to prove",
        }
    if not probe:
        return {"state": USER_SKIPPED, "reason": "no probe configured"}
    slot: dict = {}
    reset = auth._SLOT.set(slot)
    try:
        access = await auth.ObservedVerifier(verifier).verify_token(token)
    finally:
        auth._SLOT.reset(reset)
    if access is None:
        return {"state": USER_REJECTED, "reason": slot.get("reason", "invalid")}
    req = identity_from_token(access)
    try:
        ident = policy.authorise(req)
    except AxiError as e:
        return {"state": USER_REFUSED, "subject": req.subject, "error": str(e)}
    try:
        result = await backend.executor.run(probe, probe_args or {}, identity=ident)
    except AxiError as e:
        return {"state": USER_FAILED, "subject": req.subject, "error": str(e)}
    seen = _backend_identity(result)
    caller = {
        str(req.claims[c]).lower() for c in _CALLER_CLAIMS if req.claims.get(c)
    }
    matches = (
        bool(caller & {str(v).lower() for v in seen.values()}) if seen else None
    )
    return {
        "state": USER_MISMATCH if matches is False else USER_OK,
        "subject": req.subject,
        "backend_identity": seen,
        # None when the probe's answer carries no recognisable identity field:
        # absence of evidence, never a failure.
        "matches_caller": matches,
    }


async def check_entry(
    entry: RegistryEntry, load=load_backend, user_token: str | None = None, verifier=None
) -> dict:
    """Attach one backend and, where a probe is configured, call it.

    Attaching fresh makes this a black-box check: it exercises the same load path
    the gateway uses at boot, independently of the running gateway process.
    """
    try:
        backend = await load(entry)
    except AxiError as e:
        failed_record = {
            "name": entry.name,
            "attach": ATTACH_FAILED,
            "probe": PROBE_SKIPPED,
            "error": str(e),
        }
        if user_token is not None:
            failed_record["user_probe"] = {"state": USER_SKIPPED, "reason": "attach failed"}
        return failed_record

    from .surface import build_surface

    record = {"name": entry.name, "attach": ATTACH_OK}
    # Reported, never failed on: an identity map is read on the call path, and a
    # health sweep must describe the configuration rather than deny it. The
    # verdict list (_FAILURES) is deliberately unchanged.
    from .identity import identity_report, policy_from_entry

    plugin_for_identity = PLUGINS.get(entry.plugin)
    record["identity"] = (
        identity_report(policy_from_entry(entry, plugin_for_identity.spec))
        if plugin_for_identity
        else {"mode": "none"}
    )
    # Costing is not the credential probe below and must not be confused with
    # it: a schema edge case in `surface_cost` degrades this record to having
    # no cost fields (the same "report, don't take the whole sweep down"
    # instinct as `gateway.build_surfaces`), rather than raising or being
    # reported as PROBE_FAILED, which would misattribute a costing bug to a
    # dead credential.
    try:
        cost = await surface_cost(build_surface(backend), backend)
    except Exception:
        logger.warning(
            "context-cost computation failed for backend %r; reporting health "
            "without cost fields",
            entry.name,
            exc_info=True,
        )
    else:
        record["advertised"] = cost.advertised
        record["tokens_published"] = cost.tokens_published
    # A pinned tool the backend no longer serves is a broken published tool
    # (AGENTS.md's manual "re-probe the whole pin list" rule, mechanized for
    # the existence half). `cli` descriptors pin by BARE VERB while their
    # published name is `flatten(surface, verb)` (see `load_cli_backend`), so
    # comparing a `cli` pin list against `d.name` would report every healthy
    # `cli` surface as `pinned_missing` — compare against `d.verb` instead.
    #
    # Wrapped for the same reason as the costing block above: it cannot raise
    # today (it holds only because `RegistryEntry.pinned` and
    # `PluginSpec.pinned` are typed as lists/tuples of names, not because
    # anything here structurally guards it), and relying on that incidental
    # fact would be fragile. Degrading leaves `catalogue` unset rather than
    # misreporting a broken check as PROBE_FAILED or as a clean catalogue.
    try:
        served = {
            d.verb if backend.kind == "cli" else d.name for d in backend.descriptors
        }
        expected = resolve_pinned(entry, PLUGINS.get(entry.plugin))
        missing = sorted(n for n in expected if n not in served)
        # Vocabulary for a tool the backend does not serve is dead weight, not
        # an outage: reported, never in _FAILURES. Keyed by published name for
        # every backing (unlike pins, which a cli backend keys by verb).
        served_names = {d.name for d in backend.descriptors}
        unknown_aliases = sorted(
            t for t in backend.search_aliases if t not in served_names
        )
    except Exception:
        logger.warning(
            "pin-check failed for backend %r; reporting health without a "
            "catalogue verdict",
            entry.name,
            exc_info=True,
        )
    else:
        record["catalogue"] = CATALOGUE_PINNED_MISSING if missing else CATALOGUE_OK
        if missing:
            record["pinned_missing"] = missing
        if unknown_aliases:
            record["aliases_unknown"] = unknown_aliases
    # No backing special-case: cli backends execute since 2026-08-04, so an
    # unprobed one is UNKNOWN for exactly the same reason an unprobed mcp one
    # is. Reporting them "unsupported" outlived the deferred-execution
    # iteration and would have quietly excused every cli surface from the one
    # check that catches a dead credential.
    #
    # The entry's probe is an OVERRIDE of the plugin's tested default, so the
    # default has to be consulted or every surface that (correctly) omits
    # `probe` would report UNKNOWN. Looked up without raising: this function's
    # contract is to report rather than raise, and it accepts an injected
    # loader that need not have gone through plugin dispatch at all.
    #
    # A probe and its arguments are ONE unit. Falling back field-by-field would
    # call an entry's overriding probe with the PLUGIN's arguments, which belong
    # to a different tool — `get_me` invoked with {"query": "pdf"} fails on an
    # unexpected keyword and reads like a dead credential.
    plugin = PLUGINS.get(entry.plugin)
    if entry.probe:
        probe, probe_args = entry.probe, entry.probe_args
    elif plugin:
        probe, probe_args = plugin.spec.probe, plugin.spec.probe_args
    else:
        probe, probe_args = None, None
    if not probe:
        record["probe"] = PROBE_NONE
    else:
        try:
            await backend.executor.run(probe, probe_args or {})
        except AxiError as e:
            record.update(probe=PROBE_FAILED, error=str(e))
        else:
            record["probe"] = PROBE_OK
    if user_token is not None:
        record["user_probe"] = await probe_as_user(
            entry, backend, probe, probe_args, user_token, verifier
        )
    return record


async def deep_health(
    registry: dict[str, RegistryEntry],
    load=load_backend,
    user_token: str | None = None,
    verifier=None,
) -> list[dict]:
    """One record per registry entry, in registry order.

    With `user_token`, each record also carries `user_probe`: the probe run as
    that token's holder (see `probe_as_user`). `verifier` defaults to the
    gateway's own, built from this environment.
    """
    if user_token is not None and verifier is None:
        from .auth import build_verifier

        verifier = build_verifier(strict=False)
    return [
        await check_entry(entry, load=load, user_token=user_token, verifier=verifier)
        for entry in registry.values()
    ]


def failed(records: list[dict]) -> list[str]:
    """Names that failed to attach, failed their probe, or lost a pinned tool.

    `none` and `unsupported` are NOT failures — they are absences of evidence,
    and conflating the two would make an unprobed gateway alarm forever.
    """
    return [
        r["name"]
        for r in records
        if r.get("attach") in _FAILURES
        or r.get("probe") in _FAILURES
        or r.get("catalogue") in _FAILURES
        or (r.get("user_probe") or {}).get("state") in _USER_FAILURES
    ]
