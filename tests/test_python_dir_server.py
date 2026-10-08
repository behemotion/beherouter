"""python_dir_server: FastMCP's file discovery, with every fail-open path closed."""

import textwrap

import pytest

from beherouter.backends.inproc import python_dir_server
from beherouter.errors import UsageError


def _write(root, name, body):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))
    return path


PING = """
    from fastmcp.tools import tool

    @tool
    def ping() -> str:
        \"\"\"Ping.\"\"\"
        return "pong"
"""


async def test_decorated_functions_become_tools(tmp_path):
    _write(tmp_path, "pd_basic_ping.py", PING)
    _write(tmp_path, "sub/pd_basic_workers.py", """
        from fastmcp.tools import tool

        @tool(name="restart")
        def restart_worker(name: str) -> str:
            \"\"\"Restart one worker.\"\"\"
            return f"restarted {name}"
    """)
    server = python_dir_server(tmp_path, name="ops")
    assert sorted(t.name for t in await server.list_tools()) == ["ping", "restart"]
    out = await server.call_tool("restart", {"name": "w1"})
    assert out.structured_content == {"result": "restarted w1"}


def test_a_missing_directory_is_refused(tmp_path):
    with pytest.raises(UsageError, match=r"'ops'.*nope.*not a directory"):
        python_dir_server(tmp_path / "nope", name="ops")


def test_a_file_is_not_a_directory(tmp_path):
    f = _write(tmp_path, "pd_one.py", PING)
    with pytest.raises(UsageError, match="not a directory"):
        python_dir_server(f, name="ops")


def test_an_import_failure_names_the_file(tmp_path):
    _write(tmp_path, "pd_good.py", PING)
    _write(tmp_path, "pd_broken.py", "import no_such_module_for_beherouter\n")
    with pytest.raises(UsageError, match=r"'ops'.*pd_broken\.py.*no_such_module_for_beherouter"):
        python_dir_server(tmp_path, name="ops")


def test_zero_tools_is_refused(tmp_path):
    _write(tmp_path, "pd_plain.py", "def helper():\n    return 1\n")
    with pytest.raises(UsageError, match=r"'ops'.*no tools.*@tool"):
        python_dir_server(tmp_path, name="ops")


def test_two_functions_under_one_name_are_refused_naming_both_files(tmp_path):
    _write(tmp_path, "pd_dup_a.py", PING)
    _write(tmp_path, "pd_dup_b.py", PING)
    with pytest.raises(UsageError, match=r"'ping'.*pd_dup_a\.py.*pd_dup_b\.py"):
        python_dir_server(tmp_path, name="ops")


async def test_a_tool_imported_by_a_sibling_is_not_a_duplicate(tmp_path):
    _write(tmp_path, "pd_sib_helpers.py", PING)
    _write(tmp_path, "pd_sib_workers.py", """
        from fastmcp.tools import tool
        from pd_sib_helpers import ping  # re-exported: same function, not a duplicate

        @tool
        def restart(name: str) -> str:
            \"\"\"Restart.\"\"\"
            return name
    """)
    server = python_dir_server(tmp_path, name="ops")
    assert sorted(t.name for t in await server.list_tools()) == ["ping", "restart"]


async def test_prompts_are_dropped_with_a_warning(tmp_path, caplog):
    _write(tmp_path, "pd_mixed.py", PING + """

    from fastmcp.prompts import prompt

    @prompt
    def greet() -> str:
        \"\"\"Greet.\"\"\"
        return "hi"
    """)
    server = python_dir_server(tmp_path, name="ops")
    assert [t.name for t in await server.list_tools()] == ["ping"]
    assert "greet" in caplog.text


HELPERS_TOOL = """
    from __future__ import annotations

    import helpers
    from fastmcp.tools import tool

    @tool
    def which(n: int) -> str:
        \"\"\"Which directory's helpers answered.\"\"\"
        return f"{helpers.VALUE}{n}"
"""


async def test_same_named_files_in_two_directories_do_not_collide(tmp_path):
    """Two surfaces that each ship a sibling-imported `helpers.py` used to share
    whichever was imported first: FastMCP keeps the bare name `helpers` in
    sys.modules, so the second directory's `import helpers` found the first's."""
    import sys

    a, b = tmp_path / "a", tmp_path / "b"
    for root, value in ((a, "a"), (b, "b")):
        _write(root, "helpers.py", f"VALUE = {value!r}\n")
        _write(root, "tools.py", HELPERS_TOOL)
    before = set(sys.modules)
    server_a = python_dir_server(a, name="one")
    server_b = python_dir_server(b, name="two")
    assert (await server_a.call_tool("which", {"n": 1})).structured_content == {"result": "a1"}
    assert (await server_b.call_tool("which", {"n": "2"})).structured_content == {"result": "b2"}
    leaked = {m for m in set(sys.modules) - before if m in ("helpers", "tools")}
    assert leaked == set(), "an operator module stayed importable under its bare name"


async def test_same_named_packages_in_two_directories_do_not_collide(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root, value in ((a, "a"), (b, "b")):
        _write(root, "lib/__init__.py", "")
        _write(root, "lib/util.py", f"VALUE = {value!r}\n")
        _write(root, "tools.py", HELPERS_TOOL.replace(
            "import helpers", "from lib import util as helpers"))
    server_a = python_dir_server(a, name="one")
    server_b = python_dir_server(b, name="two")
    assert (await server_a.call_tool("which", {"n": 1})).structured_content == {"result": "a1"}
    assert (await server_b.call_tool("which", {"n": 1})).structured_content == {"result": "b1"}


async def test_a_reimport_sees_the_file_as_it_is_now(tmp_path):
    """A failed attach is retried; fixing the file on disk must heal it."""
    _write(tmp_path, "helpers.py", "VALUE = 'old'\n")
    _write(tmp_path, "tools.py", HELPERS_TOOL)
    python_dir_server(tmp_path, name="ops")
    _write(
        tmp_path,
        "helpers.py",
        "VALUE = 'fixed'  # a different size: pyc staleness is mtime+size\n",
    )
    server = python_dir_server(tmp_path, name="ops")
    assert (await server.call_tool("which", {"n": 1})).structured_content == {"result": "fixed1"}


def test_an_operator_file_never_shadows_an_installed_module(tmp_path):
    """A file named like a stdlib module is imported privately, and the stdlib
    module stays what everyone else gets."""
    import json
    import sys

    _write(tmp_path, "json.py", PING)
    python_dir_server(tmp_path, name="ops")
    assert sys.modules["json"] is json
