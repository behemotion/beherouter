"""The `python-dir` plugin: drop a file, get tools. Lint never runs the code."""

import textwrap

import pytest

from beherouter.errors import UsageError
from beherouter.plugins import get
from beherouter.registry import RegistryEntry, validate_entry

TOOLS = """
    from fastmcp.tools import tool
    import fastmcp.tools

    @tool
    def ping() -> str:
        \"\"\"Ping.\"\"\"
        return "pong"

    @tool(name="restart")
    def restart_worker(name: str) -> str:
        \"\"\"Restart one worker.\"\"\"
        return f"restarted {name}"

    @fastmcp.tools.tool("status")
    async def worker_status(name: str) -> dict:
        \"\"\"One worker's status.\"\"\"
        return {"name": name, "up": True}

    @tool
    def _private() -> str:
        return "never published"

    def helper():
        return 1
"""


def _dir(tmp_path, body=TOOLS, fname="ops_tools.py"):
    d = tmp_path / "ops"
    d.mkdir(exist_ok=True)
    (d / fname).write_text(textwrap.dedent(body))
    return d


def _entry(path, **over) -> RegistryEntry:
    fields = {"name": "ops", "plugin": "python-dir", "config": {"path": str(path)},
              "pinned": ["restart"], "probe": "ping", "probe_args": {}}
    fields.update(over)
    return RegistryEntry(**fields)


def test_registered_inproc_without_identity():
    spec = get("python-dir").spec
    assert spec.backing == "inproc"
    assert not spec.identity.modes  # the fail-closed default: no identity
    assert set(spec.requires_entry) == {"probe", "pinned"}
    assert "gateway process" in spec.summary


def test_scan_reads_decorator_names_statically(tmp_path):
    from beherouter.plugins.python_dir import scan_tools

    assert sorted(scan_tools(_dir(tmp_path))) == ["ping", "restart", "status"]


def test_a_good_entry_validates(tmp_path):
    validate_entry(_entry(_dir(tmp_path)))


def test_the_path_must_be_absolute(tmp_path):
    with pytest.raises(UsageError, match="absolute"):
        validate_entry(_entry("relative/ops"))


def test_a_syntax_error_names_file_and_line(tmp_path):
    d = _dir(tmp_path)
    (d / "bad.py").write_text("def broken(:\n    pass\n")
    with pytest.raises(UsageError, match=r"bad\.py:1"):
        validate_entry(_entry(d))


def test_a_pin_that_is_not_a_tool_is_refused(tmp_path):
    with pytest.raises(UsageError, match=r"pinned.*'reboot'.*does not publish"):
        validate_entry(_entry(_dir(tmp_path), pinned=["reboot"]))


def test_a_probe_that_is_not_a_tool_is_refused(tmp_path):
    with pytest.raises(UsageError, match=r"probe 'restart_worker'.*does not publish"):
        validate_entry(_entry(_dir(tmp_path), probe="restart_worker"))


def test_probe_and_pinned_are_required(tmp_path):
    with pytest.raises(UsageError, match="probe"):
        validate_entry(_entry(_dir(tmp_path), probe=None, probe_args=None))


def test_a_path_absent_here_only_warns(tmp_path):
    missing = tmp_path / "not-on-this-machine"
    validate_entry(_entry(missing))
    warnings = get("python-dir").warn({"path": str(missing)})
    assert len(warnings) == 1 and "not-on-this-machine" in warnings[0]
    assert get("python-dir").warn({"path": str(_dir(tmp_path))}) == []


def test_identity_is_refused(tmp_path):
    with pytest.raises(UsageError, match="declares no identity support"):
        validate_entry(_entry(_dir(tmp_path), identity={"mode": "bearer"}))


def test_lint_never_imports_the_code(tmp_path):
    """A module that would blow up on import passes lint: it was only parsed."""
    d = _dir(
        tmp_path, textwrap.dedent(TOOLS) + "\nraise SystemExit('lint imported operator code')\n"
    )
    validate_entry(_entry(d))
    get("python-dir").warn({"path": str(d)})


async def test_build_attaches_and_calls(tmp_path):
    from beherouter.gateway import load_backend

    b = await load_backend(_entry(_dir(tmp_path, fname="ops_build.py")))
    assert b.kind == "inproc"
    assert sorted(d.name for d in b.descriptors) == ["ping", "restart", "status"]
    assert b.executor.identity_aware is False
    out = await b.executor.run("restart", {"name": "w1"})
    assert out["result"] == "restarted w1"


async def test_build_refuses_an_import_failure_by_file(tmp_path):
    from beherouter.gateway import load_backend

    d = _dir(tmp_path, fname="ops_fail.py")
    (d / "needs_dep.py").write_text("import no_such_dependency_for_beherouter\n")
    with pytest.raises(UsageError, match=r"needs_dep\.py"):
        await load_backend(_entry(d))


def test_scan_reads_every_decorator_shape(tmp_path):
    from beherouter.plugins.python_dir import scan_tools

    d = _dir(tmp_path, """
        import functools
        from fastmcp.tools import tool

        @functools.cache
        @tool(description="named by its function")
        def by_function() -> str:
            return "x"

        @tool()
        def bare_call() -> str:
            return "y"

        @functools.cache
        def not_a_tool() -> str:
            return "z"
    """)
    assert set(scan_tools(d)) == {"by_function", "bare_call"}


def test_a_path_that_is_a_file_is_refused(tmp_path):
    f = tmp_path / "tools.py"
    f.write_text("")
    with pytest.raises(UsageError, match="is not a directory"):
        get("python-dir").validate({"path": str(f)})
