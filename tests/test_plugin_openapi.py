"""The `openapi` plugin: a REST API from its OpenAPI document, no code."""

import json

import httpx
import pytest

from beherouter.errors import UsageError
from beherouter.plugins import get
from beherouter.registry import RegistryEntry, validate_entry

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
        "/customers": {
            "get": {
                "operationId": "search_customers",
                "parameters": [{"name": "q", "in": "query", "required": True,
                                "schema": {"type": "string"}}],
                "responses": {"200": {"description": "ok"}},
            },
            "post": {"responses": {"200": {"description": "ok"}}},  # no operationId
        },
        "/bad": {"get": {"operationId": "get-thing", "responses": {"200": {"description": "ok"}}}},
    },
}


def _entry(tmp_path, monkeypatch, *, doc=DOC, suffix=".json", **over) -> RegistryEntry:
    path = tmp_path / f"crm{suffix}"
    if suffix == ".json":
        path.write_text(json.dumps(doc))
    else:
        import yaml

        path.write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("BEHEROUTER_CRM_TOKEN", "deploy")
    config = {"spec": str(path), "base_url": "https://crm.internal",
              "include": ["get_customer", "search_customers"]}
    config.update(over.pop("config", {}))
    fields = {"name": "crm", "plugin": "openapi", "config": config,
              "env": {"token": "${BEHEROUTER_CRM_TOKEN}"},
              "pinned": ["get_customer"], "probe": "get_customer", "probe_args": {"id": 1}}
    fields.update(over)
    return RegistryEntry(**fields)


def test_a_good_entry_validates(tmp_path, monkeypatch):
    validate_entry(_entry(tmp_path, monkeypatch))


def test_yaml_documents_are_read(tmp_path, monkeypatch):
    validate_entry(_entry(tmp_path, monkeypatch, suffix=".yaml"))


def test_include_is_required(tmp_path, monkeypatch):
    e = _entry(tmp_path, monkeypatch)
    e.config.pop("include")
    with pytest.raises(UsageError, match="include"):
        validate_entry(e)


def test_an_unknown_operation_id_is_refused_with_a_suggestion(tmp_path, monkeypatch):
    with pytest.raises(UsageError, match=r"get_custmer.*did you mean 'get_customer'"):
        validate_entry(_entry(tmp_path, monkeypatch, config={"include": ["get_custmer"]}))


def test_an_operation_id_the_slug_would_rewrite_is_refused(tmp_path, monkeypatch):
    with pytest.raises(UsageError, match=r"get-thing.*get_thing"):
        validate_entry(_entry(tmp_path, monkeypatch, config={"include": ["get-thing"]}))


def test_star_refuses_an_operation_without_an_id_by_method_and_path(tmp_path, monkeypatch):
    with pytest.raises(UsageError, match=r"operationId.*POST /customers"):
        validate_entry(_entry(tmp_path, monkeypatch, config={"include": ["*"]}))


def test_a_pin_outside_include_is_refused(tmp_path, monkeypatch):
    with pytest.raises(UsageError, match=r"pinned.*create_note.*include"):
        validate_entry(_entry(tmp_path, monkeypatch, pinned=["create_note"]))


def test_probe_and_pinned_are_required(tmp_path, monkeypatch):
    with pytest.raises(UsageError, match="probe"):
        validate_entry(_entry(tmp_path, monkeypatch, probe=None, probe_args=None))


def test_a_missing_spec_file_is_refused_by_path(tmp_path, monkeypatch):
    with pytest.raises(UsageError, match="nope.json"):
        validate_entry(_entry(tmp_path, monkeypatch, config={"spec": str(tmp_path / "nope.json")}))


def test_warnings_star_and_url(tmp_path, monkeypatch):
    warn = get("openapi").warn
    ok_doc = {**DOC, "paths": {k: v for k, v in DOC["paths"].items() if k == "/customers/{id}"}}
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(ok_doc))
    base = {"base_url": "https://x", "auth_header": "authorization", "auth_prefix": "Bearer "}
    assert any("1 tool" in w for w in warn({**base, "spec": str(path), "include": ["*"]}))
    assert any("URL" in w for w in warn({**base, "spec": "https://crm/openapi.json",
                                         "include": ["get_customer"]}))
    assert warn({**base, "spec": str(path), "include": ["get_customer"]}) == []


def test_lint_is_offline_for_a_url_spec(tmp_path, monkeypatch):
    """A URL is fetched only at build(); lint must not touch the network."""
    def no_network(*a, **k):
        raise AssertionError("lint touched the network")

    monkeypatch.setattr(httpx, "get", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    validate_entry(_entry(tmp_path, monkeypatch, config={"spec": "https://crm/openapi.json"}))


async def test_build_attaches_and_calls_with_identity(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import beherouter.plugins.openapi as plugin
    from beherouter.gateway import load_backend

    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"id": 7, "auth": req.headers.get("authorization")})

    # Route the plugin's upstream client through a mock transport.
    real = plugin.identity_client
    monkeypatch.setattr(plugin, "identity_client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    b = await load_backend(_entry(tmp_path, monkeypatch))
    assert b.kind == "inproc"
    assert sorted(d.name for d in b.descriptors) == ["get_customer", "search_customers"]
    assert b.executor.identity_aware is True
    out = await b.executor.run("get_customer", {"id": 7}, identity=SimpleNamespace(
        headers={"authorization": "Bearer alice"}))
    assert out["result"]["auth"] == "Bearer alice"
    assert str(seen[-1].url) == "https://crm.internal/customers/7"
    with pytest.raises(UsageError):
        await b.executor.run("get_customer", {"id": "x"})
    assert len(seen) == 1
