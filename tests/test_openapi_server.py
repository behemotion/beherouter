"""openapi_server: FastMCP.from_openapi, filtered, closed, and checked."""

import httpx
import pytest

from beherouter.backends.inproc import IDENTITY_MARKER, identity_client, openapi_server
from beherouter.errors import UsageError

DOC = {
    "openapi": "3.0.3",
    "info": {"title": "crm", "version": "1"},
    "paths": {
        "/customers/{id}": {"get": {
            "operationId": "get_customer",
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "ok"}},
        }},
        "/customers": {"get": {
            "operationId": "search_customers",
            "parameters": [{"name": "q", "in": "query", "required": True,
                            "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok"}},
        }},
        "/notes": {"post": {
            "operationId": "create_note",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "required": ["text"],
                "properties": {"text": {"type": "string"}}}}}},
            "responses": {"200": {"description": "ok"}},
        }},
    },
}


def _client():
    return httpx.AsyncClient(
        base_url="https://crm", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )


async def test_include_publishes_exactly_the_listed_operations():
    server = await openapi_server(DOC, client=_client(), include=["get_customer", "create_note"])
    assert sorted(t.name for t in await server.list_tools()) == ["create_note", "get_customer"]


async def test_no_include_publishes_every_operation():
    server = await openapi_server(DOC, client=_client(), include=None)
    assert len(await server.list_tools()) == 3


async def test_every_published_schema_is_closed():
    server = await openapi_server(DOC, client=_client(), include=None)
    for t in await server.list_tools():
        assert t.parameters.get("additionalProperties") is False, t.name


async def test_an_include_name_that_publishes_nothing_is_refused():
    with pytest.raises(UsageError, match="get_custommer"):
        await openapi_server(DOC, client=_client(), include=["get_custommer"])


async def test_the_identity_marker_follows_the_client():
    aware = await openapi_server(DOC, client=identity_client(base_url="https://crm"), include=None)
    blind = await openapi_server(DOC, client=_client(), include=None)
    assert getattr(aware, IDENTITY_MARKER) is True
    assert getattr(blind, IDENTITY_MARKER, False) is False


async def test_a_failing_filter_cannot_publish_everything(monkeypatch):
    """route_map_fn fails OPEN in FastMCP 3.4.5: an exception is logged and the
    route falls back to TOOL. The post-construction check must catch it."""
    from beherouter.backends import inproc

    def boom(include):
        def fn(route, route_type):
            raise RuntimeError("filter bug")
        return fn

    monkeypatch.setattr(inproc, "_include_filter", boom)
    with pytest.raises(UsageError, match="search_customers"):
        await openapi_server(DOC, client=_client(), include=["get_customer"])


async def test_a_failing_schema_closer_is_caught(monkeypatch):
    """mcp_component_fn fails open too."""
    from beherouter.backends import inproc

    def boom(route, component):
        raise RuntimeError("closer bug")

    monkeypatch.setattr(inproc, "_close_schema", boom)
    with pytest.raises(UsageError, match="not closed"):
        await openapi_server(DOC, client=_client(), include=None)
