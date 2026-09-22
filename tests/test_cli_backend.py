import pytest

from beherouter.backends.backing import CliBacking
from beherouter.backends.cli import CLIExecutor, build_argv, load_cli_backend
from beherouter.errors import AuthError, Conflict, NotFound, Unavailable, UsageError

SCHEMA = {
    "name": {"name": "name", "type": "string", "required": True},
    "--limit": {"name": "--limit", "type": "integer", "required": False},
    "--force": {"name": "--force", "type": "boolean", "required": False},
}


def test_load_cli_backend_builds_descriptors(fake_cli_cmd):
    e = CliBacking(name="faketool", cmd=fake_cli_cmd)
    b = load_cli_backend(e)
    names = {d.name for d in b.descriptors}
    assert "faketool_search" in names
    assert "faketool_shelf_create" in names
    assert [d.name for d in b.pinned] == ["faketool_search"]


def test_descriptors_carry_schema_and_flags(fake_cli_cmd):
    b = load_cli_backend(CliBacking(name="faketool", cmd=fake_cli_cmd))
    by_name = {d.name: d for d in b.descriptors}
    create = by_name["faketool_shelf_create"]
    assert create.verb == "shelf-create"
    assert create.mutating is True
    assert create.schema["name"]["required"] is True
    assert create.schema["limit"]["type"] == "integer"


def test_pinned_override_from_registry(fake_cli_cmd):
    """An explicit `pinned` list overrides the manifest's own flags."""
    e = CliBacking(name="faketool", cmd=fake_cli_cmd, pinned=["shelf-create"])
    b = load_cli_backend(e)
    assert [d.name for d in b.pinned] == ["faketool_shelf_create"]


def test_load_cli_backend_bad_cmd_raises_unavailable():
    e = CliBacking(name="nope", cmd="this-binary-does-not-exist-xyz")
    with pytest.raises(Unavailable):
        load_cli_backend(e)


def test_load_cli_backend_non_json_raises_unavailable(tmp_path):
    """A tool that exits 0 but prints garbage is Unavailable, not a crash."""
    script = tmp_path / "garbage.py"
    script.write_text("print('not json')\n")
    import sys

    e = CliBacking(name="garbage", cmd=f"{sys.executable} {script}")
    with pytest.raises(Unavailable):
        load_cli_backend(e)


def _executor(fake_cli_cmd, **kw):
    b = load_cli_backend(CliBacking(name="faketool", cmd=fake_cli_cmd))
    return (
        b.executor
        if not kw
        else CLIExecutor("faketool", fake_cli_cmd, b.executor.schemas, **kw)
    )


async def test_cli_executor_runs_a_verb(fake_cli_cmd):
    out = await _executor(fake_cli_cmd).run("search", {"query": "hello"})
    assert out == {"hits": ["hello"]}


async def test_cli_executor_requests_json_output(fake_cli_cmd):
    """beheaxi CLIs print a HUMAN dashboard by default and JSON only under
    `--json` (CONVENTIONS.md, beheaxi CLI profile). Without this the gateway
    parses a Python-repr dashboard and every real cli call fails Unavailable —
    which the fake_tool fixture cannot show, since it always prints JSON."""
    ex = _executor(fake_cli_cmd)
    ex.schemas["echo-argv"] = {}
    out = await ex.run("echo-argv", {})
    assert out["argv"][-1] == "--json"


async def test_cli_executor_does_not_duplicate_an_explicit_json_flag(fake_cli_cmd):
    ex = _executor(fake_cli_cmd)
    ex.schemas["echo-argv"] = {
        "--json": {"name": "--json", "type": "boolean", "required": False}
    }
    out = await ex.run("echo-argv", {"--json": True})
    assert out["argv"].count("--json") == 1


async def test_cli_executor_sends_flags(fake_cli_cmd):
    out = await _executor(fake_cli_cmd).run("shelf-create", {"name": "b", "limit": 5})
    assert out == {"created": ["b", "--limit", "5"]}


async def test_cli_executor_non_json_stdout_is_unavailable(fake_cli_cmd):
    ex = _executor(fake_cli_cmd)
    ex.schemas["garbage"] = {}
    with pytest.raises(Unavailable):
        await ex.run("garbage", {})


async def test_cli_executor_timeout_is_unavailable(fake_cli_cmd):
    ex = _executor(fake_cli_cmd, timeout=0.5)
    ex.schemas["slow"] = {}
    with pytest.raises(Unavailable):
        await ex.run("slow", {})


async def test_cli_executor_missing_binary_is_unavailable():
    ex = CLIExecutor("nope", "this-binary-does-not-exist-xyz", {"v": {}})
    with pytest.raises(Unavailable):
        await ex.run("v", {})


async def test_cli_executor_unknown_verb_raises_usage(fake_cli_cmd):
    with pytest.raises(UsageError):
        await _executor(fake_cli_cmd).run("no-such-verb", {})


async def test_cli_executor_does_not_block_the_event_loop(fake_cli_cmd):
    """Two calls must overlap. A blocking subprocess would serialize them and
    stall every other surface on the gateway, which is the failure this guards."""
    import asyncio
    import time

    ex = _executor(fake_cli_cmd, timeout=10)
    ex.schemas["slow"] = {}
    start = time.monotonic()
    task = asyncio.create_task(ex.run("slow", {}))
    await asyncio.sleep(0.2)
    quick = await ex.run("search", {"query": "x"})
    elapsed = time.monotonic() - start
    task.cancel()
    assert quick == {"hits": ["x"]}
    assert elapsed < 5  # would be ~30 if `slow` blocked the loop


async def test_exit_2_is_usage(fake_cli_cmd):
    ex = _executor(fake_cli_cmd)
    ex.schemas["boom"] = {"code": {"name": "code", "required": True}}
    with pytest.raises(UsageError):
        await ex.run("boom", {"code": 2})


async def test_exit_3_is_not_found(fake_cli_cmd):
    ex = _executor(fake_cli_cmd)
    ex.schemas["boom"] = {"code": {"name": "code", "required": True}}
    with pytest.raises(NotFound):
        await ex.run("boom", {"code": 3})


async def test_exit_4_is_auth(fake_cli_cmd):
    ex = _executor(fake_cli_cmd)
    ex.schemas["boom"] = {"code": {"name": "code", "required": True}}
    with pytest.raises(AuthError):
        await ex.run("boom", {"code": 4})


async def test_exit_5_is_conflict(fake_cli_cmd):
    ex = _executor(fake_cli_cmd)
    ex.schemas["boom"] = {"code": {"name": "code", "required": True}}
    with pytest.raises(Conflict):
        await ex.run("boom", {"code": 5})


async def test_domain_exit_code_is_returned_not_raised(fake_cli_cmd):
    """>=10 is domain-specific: information for the agent, not a transport fault."""
    ex = _executor(fake_cli_cmd)
    ex.schemas["boom"] = {"code": {"name": "code", "required": True}}
    out = await ex.run("boom", {"code": 12})
    assert out["exit_code"] == 12
    assert "stack trace" in out["stderr"]


def test_build_argv_required_is_positional():
    argv = build_argv("mytool", "shelf-create", SCHEMA, {"name": "books"})
    assert argv == ["mytool", "shelf-create", "books"]


def test_build_argv_optional_is_a_flag():
    argv = build_argv("mytool", "shelf-create", SCHEMA, {"name": "b", "--limit": 5})
    assert argv == ["mytool", "shelf-create", "b", "--limit", "5"]


def test_build_argv_accepts_stripped_key_for_flag():
    """Agents see `--limit` in describe_tool, but tolerate the bare name too."""
    argv = build_argv("mytool", "shelf-create", SCHEMA, {"name": "b", "limit": 5})
    assert argv == ["mytool", "shelf-create", "b", "--limit", "5"]


def test_build_argv_true_boolean_is_bare_flag():
    argv = build_argv("mytool", "shelf-create", SCHEMA, {"name": "b", "--force": True})
    assert argv == ["mytool", "shelf-create", "b", "--force"]


def test_build_argv_false_boolean_is_omitted():
    argv = build_argv("mytool", "shelf-create", SCHEMA, {"name": "b", "--force": False})
    assert argv == ["mytool", "shelf-create", "b"]


def test_build_argv_splits_multiword_cmd():
    argv = build_argv(
        "python /x/t.py", "search", {"q": {"name": "q", "required": True}}, {"q": "hi"}
    )
    assert argv == ["python", "/x/t.py", "search", "hi"]


def test_build_argv_missing_required_raises_usage():
    with pytest.raises(UsageError):
        build_argv("mytool", "shelf-create", SCHEMA, {"--limit": 5})


def test_build_argv_unknown_arg_raises_usage():
    """A typo'd arg must fail loudly, not be silently dropped."""
    with pytest.raises(UsageError):
        build_argv("mytool", "shelf-create", SCHEMA, {"name": "b", "colour": "red"})


def test_build_argv_preserves_positional_order():
    schema = {
        "first": {"name": "first", "required": True},
        "second": {"name": "second", "required": True},
    }
    argv = build_argv("t", "v", schema, {"second": "2", "first": "1"})
    assert argv == ["t", "v", "1", "2"]


async def test_cli_backing_is_reachable_through_a_plugin(fake_cli_cmd):
    """The `cli` backing keeps a plugin-level caller.

    Nothing live uses it, but without one entry-to-backend path that reaches
    CLIExecutor the way the gateway does, the whole load path becomes
    unreachable code that only its own unit tests touch.
    """
    from beherouter.gateway import load_backend
    from beherouter.registry import RegistryEntry

    backend = await load_backend(
        RegistryEntry(name="s", plugin="_test-cli", config={"cmd": fake_cli_cmd})
    )
    assert backend.descriptors


def test_cli_backend_has_no_relister(fake_cli_cmd):
    from beherouter.backends.backing import CliBacking
    from beherouter.backends.cli import load_cli_backend

    b = load_cli_backend(CliBacking(name="faketool", cmd=fake_cli_cmd))
    assert b.relist is None
    assert b.ttl_ms is None


async def test_identity_env_reaches_the_subprocess(fake_cli_cmd):
    """A cli backend learns who is calling through its environment."""
    from beherouter.identity import CallIdentity

    executor = CLIExecutor(tool="demo", cmd=fake_cli_cmd, schemas={"whoami": {}})
    out = await executor.run(
        "whoami",
        {},
        identity=CallIdentity(subject="alice", env={"REMOTE_USER": "alice"}),
    )
    assert out == {"user": "alice"}


async def test_without_an_identity_the_variable_is_absent(fake_cli_cmd, monkeypatch):
    monkeypatch.delenv("REMOTE_USER", raising=False)
    executor = CLIExecutor(tool="demo", cmd=fake_cli_cmd, schemas={"whoami": {}})
    assert await executor.run("whoami", {}) == {"user": None}


async def test_identity_env_is_merged_on_top_of_the_gateways_own(
    fake_cli_cmd, monkeypatch
):
    """Replacing the environment outright would take PATH with it."""
    from beherouter.identity import CallIdentity

    monkeypatch.setenv("REMOTE_USER", "deployment")
    executor = CLIExecutor(tool="demo", cmd=fake_cli_cmd, schemas={"whoami": {}})
    out = await executor.run(
        "whoami",
        {},
        identity=CallIdentity(subject="alice", env={"REMOTE_USER": "alice"}),
    )
    assert out == {"user": "alice"}
