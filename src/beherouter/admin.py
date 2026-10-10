"""The admin API (spec §4): reload the registry, drive the kill switch.

⚠️ This widens the gateway's only network surface, so it is OFF unless an
admin credential is configured: no $BEHEROUTER_ADMIN_TOKEN and no
$BEHEROUTER_ADMIN_ROLE means no /admin route at all (404). Either credential
passes: the admin token (never the clients' shared token -- boot refuses the
same value) or a verified JWT carrying the admin role.

A caller `sub` travels in a request BODY, never the path, so it cannot land in
a reverse proxy's access log; the audit line carries only its digest. Neither
the body nor its `reason` reaches a log line: `reason` is stored in the state
file and nowhere else.

Every refusal counts in `beherouter_auth_rejections_total{surface=""}`, exactly
once: a JWT the verifier refuses is counted by the verifier itself
(`auth.ObservedVerifier`), every other refusal here.
"""

import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import metrics
from .errors import AuthError, Conflict, UsageError
from .runtime import problem

ADMIN_TOKEN_VAR = "BEHEROUTER_ADMIN_TOKEN"
ADMIN_ROLE_VAR = "BEHEROUTER_ADMIN_ROLE"
TOKEN_ACTOR = "<admin-token>"
REFUSED_ACTOR = "<refused>"
_REASON_MAX = 200
_SUB_MAX = 512
_CHALLENGE = {"WWW-Authenticate": "Bearer"}
# The rejection reasons only the admin API counts. Seeded as zero series when
# the API is configured, like auth.seed_rejections, so a dashboard has them
# before the first refusal; an unconfigured gateway gains no series.
REJECTION_REASONS = ("missing", "missing_role")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdminAuth:
    token: str | None
    role: str | None

    @classmethod
    def from_env(cls) -> "AdminAuth | None":
        token = os.environ.get(ADMIN_TOKEN_VAR) or None
        role = (os.environ.get(ADMIN_ROLE_VAR) or "").strip() or None
        return cls(token, role) if token or role else None


def check_admin_config(admin: AdminAuth | None) -> None:
    """Boot-time refusals: one secret must not do two jobs, and a role with
    nowhere to read roles from could only refuse every admin. Worded like the
    surface role-gate rules in `gateway.check_registry`."""
    from .auth import AUTH_MODE_VAR, OIDC_ROLES_CLAIM_VAR, auth_mode, configured_token, roles_claim

    if admin is None:
        return
    shared = configured_token()
    if admin.token and shared and admin.token == shared:
        raise UsageError(
            f"{ADMIN_TOKEN_VAR} must differ from the clients' gateway token; "
            f"one secret must not do two jobs"
        )
    if admin.role:
        if auth_mode() == "shared":
            raise UsageError(
                f"{AUTH_MODE_VAR} is 'shared' but {ADMIN_ROLE_VAR} requires a "
                f"verified user; set it to 'oidc' or 'both'"
            )
        if not roles_claim():
            raise UsageError(
                f"{ADMIN_ROLE_VAR} gates on a role but {OIDC_ROLES_CLAIM_VAR} is "
                f"unset; set it to the dotted path of the claim your IdP puts roles "
                f"in (e.g. 'realm_access.roles')"
            )


def subject_digest(sub: str) -> str:
    """The only form a `sub` takes in an admin audit line: enough to match a
    block to its unblock, never the value."""
    return "sha256:" + hashlib.sha256(sub.encode()).hexdigest()[:8]


def seed_rejections() -> None:
    for reason in REJECTION_REASONS:
        metrics.AUTH_REJECTIONS.labels(reason=reason, surface="")


def _reject(reason: str) -> None:
    metrics.AUTH_REJECTIONS.labels(reason=reason, surface="").inc()


async def _actor(request: Request, admin: AdminAuth, verifier) -> str | Response:
    """The admin's actor string, or the 401/403 to answer with."""
    from .auth import SHARED_CLIENT_ID, roles_claim, token_ok
    from .identity import check_roles, identity_from_token

    scheme, _, cred = request.headers.get("authorization", "").partition(" ")
    cred = cred.strip()
    if scheme.lower() != "bearer" or not cred:
        _reject("missing")
        return problem(401, "Unauthorized", "an admin bearer token is required", _CHALLENGE)
    if admin.token and token_ok(admin.token, cred):
        return TOKEN_ACTOR
    if not admin.role:
        _reject("invalid")
        return problem(401, "Unauthorized", "not an admin credential", _CHALLENGE)
    found = await verifier.verify_token(cred)  # counts its own refusal
    if found is None:
        return problem(401, "Unauthorized", "not an admin credential", _CHALLENGE)
    if found.client_id == SHARED_CLIENT_ID:
        # the clients' shared token verified, but it is never an admin credential
        _reject("invalid")
        return problem(401, "Unauthorized", "not an admin credential", _CHALLENGE)
    req = identity_from_token(found)
    try:
        check_roles("the admin API", (admin.role,), roles_claim(), req)
    except (AuthError, UsageError):
        _reject("missing_role")
        return problem(403, "Forbidden", "this token lacks the admin role")
    return req.subject or "<unknown-subject>"


def _bad(detail: str) -> Response:
    return problem(400, "Bad request", detail)


async def _body(request: Request) -> dict | Response:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return _bad("the body must be a JSON object")
    if not isinstance(data, dict):
        return _bad("the body must be a JSON object")
    reason = data.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > _REASON_MAX):
        return _bad(f"reason must be a string of at most {_REASON_MAX} characters")
    return data


# A change is a function of the raw state file's dict (KillSwitch.update).
Change = Callable[[dict], None]


def _set(kind: str, name: str, reason: str | None, actor: str) -> Change:
    from .killswitch import entry

    def change(raw: dict) -> None:
        raw.setdefault(kind, {})[name] = entry(reason, actor)

    return change


def _drop(kind: str, name: str) -> Change:
    def change(raw: dict) -> None:
        raw.setdefault(kind, {}).pop(name, None)

    return change


def _stop_all(reason: str | None, actor: str) -> Change:
    from .killswitch import entry

    def change(raw: dict) -> None:
        raw["all"] = entry(reason, actor)

    return change


def _resume_all(raw: dict) -> None:
    raw.pop("all", None)


def admin_routes(runtime, admin: AdminAuth, verifier, audit) -> list[Route]:
    """The /admin routes for `build_gateway_app`'s fixed route list. The
    kill-switch routes exist only when the kill switch is configured."""
    seed_rejections()

    def audited(action: str, target_kind: str, target: str, actor: str, outcome: str) -> None:
        audit.emit_admin(
            action=action, target_kind=target_kind, target=target, actor=actor, outcome=outcome
        )

    async def reload(request: Request) -> Response:
        actor = await _actor(request, admin, verifier)
        if not isinstance(actor, str):
            audited("reload", "registry", "-", REFUSED_ACTOR, "refused")
            return actor
        reloader = runtime.reloader
        try:
            result = await reloader.request("admin")
        except Exception:  # Reloader hands every apply error over
            audited("reload", "registry", "-", actor, "failed")
            if not reloader.closed:
                # a bug in the reload itself, not shutdown: it must leave a trace
                logger.exception("admin-requested registry reload raised")
            return problem(503, "Reload unavailable", "the reload did not complete")
        audited("reload", "registry", "-", actor, result["outcome"])
        if result["outcome"] == "failed":
            if reloader.closed:
                # shutting down, not a lint failure: 422 is reserved for that (§4.1)
                return problem(503, "Reload unavailable", result["error"])
            return problem(422, "Registry refused", result["error"])
        return JSONResponse(result)

    routes = [Route("/admin/reload", reload, methods=["POST"])]
    ks = runtime.killswitch
    if ks is None:
        return routes

    async def write(
        request: Request,
        action: str,
        target_kind: str,
        target_of: Callable[[Request, dict], str | Response],
        change_of: Callable[[str, dict, str], Change],
    ) -> Response:
        actor = await _actor(request, admin, verifier)
        if not isinstance(actor, str):
            audited(action, target_kind, "-", REFUSED_ACTOR, "refused")
            return actor
        data = await _body(request)
        if not isinstance(data, dict):
            audited(action, target_kind, "-", actor, "bad_request")
            return data
        target = target_of(request, data)
        if not isinstance(target, str):
            audited(action, target_kind, "-", actor, "bad_request")
            return target
        shown = subject_digest(target) if target_kind == "subject" else target
        try:
            state = await ks.update(change_of(target, data, actor), actor=actor)
        except Conflict as e:
            audited(action, target_kind, shown, actor, "conflict")
            return problem(409, "Conflict", str(e))
        except UsageError as e:  # the change itself would leave an invalid file
            audited(action, target_kind, shown, actor, "bad_request")
            return problem(400, "Bad request", str(e))
        audited(action, target_kind, shown, actor, "ok")
        body: dict = {"state": state.as_json()}
        if target_kind == "surface" and target not in runtime.table:
            body["warning"] = "no such surface"
        return JSONResponse(body)

    def all_target(_request: Request, _data: dict) -> str:
        return "*"

    def path_name(request: Request, _data: dict) -> str:
        return request.path_params["name"]

    def body_sub(_request: Request, data: dict) -> str | Response:
        sub = data.get("sub")
        if not isinstance(sub, str) or not sub or len(sub) > _SUB_MAX:
            return _bad(f"sub must be a non-empty string of at most {_SUB_MAX} characters")
        return sub

    async def get_state(request: Request) -> Response:
        actor = await _actor(request, admin, verifier)
        if not isinstance(actor, str):
            audited("read_killswitch", "all", "-", REFUSED_ACTOR, "refused")
            return actor
        audited("read_killswitch", "all", "-", actor, "ok")
        return JSONResponse(ks.state().as_json())

    # One Route per path, dispatching on the method: Starlette answers 405 on
    # the first path match rather than falling through to a second Route.
    async def everything(request: Request) -> Response:
        if request.method == "PUT":
            return await write(
                request, "stop_all", "all", all_target,
                lambda _t, d, a: _stop_all(d.get("reason"), a),
            )
        return await write(
            request, "resume_all", "all", all_target, lambda _t, _d, _a: _resume_all
        )

    async def surface(request: Request) -> Response:
        if request.method == "PUT":
            return await write(
                request, "disable_surface", "surface", path_name,
                lambda t, d, a: _set("surfaces", t, d.get("reason"), a),
            )
        return await write(
            request, "enable_surface", "surface", path_name,
            lambda t, _d, _a: _drop("surfaces", t),
        )

    async def block(request: Request) -> Response:
        return await write(
            request, "block_subject", "subject", body_sub,
            lambda t, d, a: _set("subjects", t, d.get("reason"), a),
        )

    async def unblock(request: Request) -> Response:
        return await write(
            request, "unblock_subject", "subject", body_sub,
            lambda t, _d, _a: _drop("subjects", t),
        )

    return [
        *routes,
        Route("/admin/killswitch", get_state, methods=["GET"]),
        Route("/admin/killswitch/all", everything, methods=["PUT", "DELETE"]),
        Route("/admin/killswitch/surfaces/{name}", surface, methods=["PUT", "DELETE"]),
        Route("/admin/killswitch/subjects/block", block, methods=["POST"]),
        Route("/admin/killswitch/subjects/unblock", unblock, methods=["POST"]),
    ]
