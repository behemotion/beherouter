"""Shared by the reload, trigger and admin tests."""

import contextlib
import os

import httpx

from beherouter.gateway import build_gateway_app


def write(path, body: str) -> None:
    """Write and bump mtime past the filesystem clock's granularity."""
    path.write_text(body)
    st = path.stat()
    os.utime(path, ns=(st.st_mtime_ns + 1_000_000, st.st_mtime_ns + 1_000_000))


@contextlib.asynccontextmanager
async def running(path, **kw):
    app = await build_gateway_app(path, retry_initial_s=0.01, retry_max_s=0.05, **kw)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        yield app.state.runtime, c
