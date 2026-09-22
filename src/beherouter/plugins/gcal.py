"""Google Calendar — a native plugin over the shared calendar core.

⚠️ THE OAUTH CONSENT SCREEN MUST BE PUBLISHED TO PRODUCTION. Left in "Testing",
Google expires refresh tokens after SEVEN DAYS and this surface dies weekly.
The setup step passes either way; only the seventh day tells you. See
docs/CALENDAR-BOOTSTRAP.md.

⚠️ THE OAUTH CLIENT MUST BE A "DESKTOP APP" CLIENT. A Web-application client
uses a different redirect model and cannot complete the loopback bootstrap.

⚠️ EVERY WRITE THROUGH THIS SURFACE IS ATTRIBUTED TO ONE GOOGLE IDENTITY — the
account that consented. Making the surface available to every LibreChat user did
not make it per-user, exactly as with the Plane PAT. A second user reads and
writes the consenting account's calendar.

All six tools are pinned: a six-tool catalogue is small enough that adding a
search tier would cost more context than it saves.

Attach performs no network I/O — see plugins/calendar/__init__.py.
"""

from . import register
from .calendar import build_backend
from .calendar.oauth import RefreshTokenAuth
from .calendar.providers.google import DEFAULT_CALENDAR, TOKEN_URL, GoogleCalendar
from .spec import ConfigField, EnvVar, IdentitySupport, PluginContext, PluginSpec

SPEC = PluginSpec(
    name="gcal",
    summary=(
        "Google Calendar: list calendars and events, find free slots, book "
        "and adjust meetings."
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
    # list_calendars authenticates against Google with the real refresh token
    # and takes no arguments, so `health --deep` proves the CREDENTIAL rather
    # than the catalogue. A native plugin's catalogue is static, so nothing else
    # here would ever notice a revoked grant.
    probe="list_calendars",
    config=(
        ConfigField(
            name="calendar_id",
            type=str,
            default=DEFAULT_CALENDAR,
            doc="Default calendar for calls that omit calendar_id. 'primary' is the account's own.",
        ),
        ConfigField(
            name="max_results",
            type=int,
            default=50,
            doc="Default cap on events returned by list_events when the agent does not ask.",
        ),
    ),
    env=(
        EnvVar(name="client_id", doc="OAuth client id of the Desktop-app client."),
        EnvVar(
            name="client_secret",
            doc="OAuth client secret; Google's refresh grant requires it.",
        ),
        EnvVar(
            name="refresh_token",
            doc="Long-lived refresh token from docs/CALENDAR-BOOTSTRAP.md.",
        ),
    ),
    identity=IdentitySupport(
        modes=("lookup",),
        target="credential",
        accepts=("client_id", "client_secret", "refresh_token"),
        doc=(
            "A per-user Google grant from the identity map. Each user's own "
            "refresh token replaces the deployment's; the OAuth client may be "
            "shared, so mapping refresh_token alone is the common case."
        ),
    ),
)


async def build(ctx: PluginContext):
    def provider(credentials: dict):
        """Build a provider from the deployment's credentials, overridden per user."""
        merged = {**ctx.env, **credentials}
        auth = RefreshTokenAuth(
            token_url=TOKEN_URL,
            client_id=merged["client_id"],
            client_secret=merged["client_secret"],
            refresh_token=merged["refresh_token"],
        )
        return GoogleCalendar(auth=auth, calendar_id=ctx.config["calendar_id"])

    return build_backend(
        surface=ctx.surface,
        provider=provider({}),
        pinned=ctx.pinned,
        max_results=ctx.config["max_results"],
        provider_factory=provider,
    )


register(SPEC, build)
