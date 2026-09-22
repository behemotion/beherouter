"""Microsoft 365 calendar (a PERSONAL outlook.com account) — a native plugin.

⚠️ THE `consumers` AUTHORITY, NEVER `common`. A refresh token issued via the
`common` authority is REJECTED AT THE FIRST REFRESH: the surface works for about
an hour and then dies, and nothing about the failure points at the authority.
The constant lives in providers/microsoft.py and a test holds it.

⚠️ A PERSONAL ACCOUNT HAS NO CLIENT SECRET. This plugin deliberately declares
only client_id and refresh_token: `registry.validate_entry` demands every
declared credential, so declaring a secret would make a working entry
un-attachable.

⚠️ EVERY WRITE THROUGH THIS SURFACE IS ATTRIBUTED TO ONE MICROSOFT IDENTITY —
the account that consented. Universal availability in LibreChat is not per-user
access.

Free/busy derives from calendarView, not from Graph's getSchedule, which is a
work/school feature — see providers/microsoft.py.

Attach performs no network I/O — see plugins/calendar/__init__.py.
"""

from . import register
from .calendar import build_backend
from .calendar.oauth import RefreshTokenAuth
from .calendar.providers.microsoft import SCOPE, TOKEN_URL, MicrosoftCalendar
from .spec import ConfigField, EnvVar, IdentitySupport, PluginContext, PluginSpec

SPEC = PluginSpec(
    name="m365",
    summary=(
        "Microsoft 365 calendar: list calendars and events, find free slots, "
        "book and adjust meetings."
    ),
    backing="native",
    pinned=(
        "list_calendars",
        "list_events",
        "get_freebusy",
        "create_event",
        "update_event",
        "delete_event",
    ),
    probe="list_calendars",
    config=(
        ConfigField(
            name="calendar_id",
            type=str,
            default=None,
            doc="Default calendar id for calls that omit one. Omit for the account's default.",
        ),
        ConfigField(
            name="max_results",
            type=int,
            default=50,
            doc="Default cap on events returned by list_events when the agent does not ask.",
        ),
    ),
    env=(
        EnvVar(
            name="client_id",
            doc="OAuth client id registered for personal accounts only.",
        ),
        EnvVar(
            name="refresh_token",
            doc="Long-lived refresh token from docs/CALENDAR-BOOTSTRAP.md.",
        ),
    ),
    identity=IdentitySupport(
        modes=("lookup",),
        target="credential",
        accepts=("client_id", "refresh_token"),
        doc="A per-user Microsoft grant from the identity map.",
    ),
)


async def build(ctx: PluginContext):
    def provider(credentials: dict):
        """Build a provider from the deployment's credentials, overridden per user."""
        merged = {**ctx.env, **credentials}
        auth = RefreshTokenAuth(
            # ⚠️ `consumers`, never `common` — see providers/microsoft.py.
            token_url=TOKEN_URL,
            client_id=merged["client_id"],
            refresh_token=merged["refresh_token"],
            scope=SCOPE,
        )
        return MicrosoftCalendar(auth=auth, calendar_id=ctx.config["calendar_id"])

    return build_backend(
        surface=ctx.surface,
        provider=provider({}),
        pinned=ctx.pinned,
        max_results=ctx.config["max_results"],
        provider_factory=provider,
    )


register(SPEC, build)
