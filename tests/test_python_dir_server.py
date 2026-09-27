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
