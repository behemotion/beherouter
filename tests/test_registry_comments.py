"""`attach`/`detach` keep the operator's comments and layout in registry.toml.

The writer used to regenerate the whole file from the parsed entries, so the
first `attach` silently deleted every comment — including the ones explaining
why a pin or a probe was overridden, which is exactly the knowledge the plugin
model exists to keep out of people's heads.
"""

from beherouter.registry import RegistryEntry, load_registry, save_registry

COMMENTED = """\
# Live gateway registry. Keep entries alphabetical.

[office]  # the pass-through surface
plugin = "office-mcp"

# ⚠️ plane's PAT is the bot account, not per-user.
[plane]
plugin = "plane"
  [plane.config]
  workspace_slug = "homelab"   # underscore-free alias, see plane.py
  [plane.env]
  api_key = "${BEHEROUTER_PLANE_API_KEY}"
"""


def test_attach_keeps_every_existing_comment(tmp_path):
    p = tmp_path / "registry.toml"
    p.write_text(COMMENTED)
    reg = load_registry(p)
    reg["docs"] = RegistryEntry(name="docs", plugin="office-mcp", probe="job_status")
    save_registry(p, reg)
    text = p.read_text()
    assert text.startswith(COMMENTED.rstrip("\n"))  # untouched, byte for byte
    assert load_registry(p)["docs"].probe == "job_status"


def test_detach_keeps_the_comments_of_the_entries_that_remain(tmp_path):
    p = tmp_path / "registry.toml"
    p.write_text(COMMENTED)
    reg = load_registry(p)
    del reg["office"]
    save_registry(p, reg)
    text = p.read_text()
    assert "[office]" not in text
    assert "# Live gateway registry. Keep entries alphabetical." in text
    assert "# ⚠️ plane's PAT is the bot account, not per-user." in text
    assert "# underscore-free alias, see plane.py" in text
    assert set(load_registry(p)) == {"plane"}


def test_changing_one_value_keeps_the_comments_around_it(tmp_path):
    p = tmp_path / "registry.toml"
    p.write_text(COMMENTED)
    reg = load_registry(p)
    reg["plane"].config = {"workspace_slug": "acme"}
    save_registry(p, reg)
    text = p.read_text()
    assert "# ⚠️ plane's PAT is the bot account, not per-user." in text
    assert load_registry(p)["plane"].config == {"workspace_slug": "acme"}


def test_a_removed_key_is_removed_from_an_existing_entry(tmp_path):
    p = tmp_path / "registry.toml"
    p.write_text('[x]\nplugin = "office-mcp"\nprobe = "job_status"  # override\n')
    reg = load_registry(p)
    reg["x"].probe = None
    save_registry(p, reg)
    assert load_registry(p)["x"].probe is None
    assert "probe" not in p.read_text()


def test_an_int_replacing_an_equal_bool_is_written_as_an_int(tmp_path):
    """True == 1 in Python; an equality short-cut would keep `true` on disk."""
    p = tmp_path / "registry.toml"
    p.write_text('[x]\nplugin = "office-mcp"\n  [x.config]\n  n = true\n')
    reg = load_registry(p)
    reg["x"].config = {"n": 1}
    save_registry(p, reg)
    assert load_registry(p)["x"].config["n"] is not True
    assert load_registry(p)["x"].config == {"n": 1}


def test_a_new_entry_with_sub_tables_round_trips(tmp_path):
    p = tmp_path / "registry.toml"
    p.write_text(COMMENTED)
    reg = load_registry(p)
    reg["wiki"] = RegistryEntry(
        name="wiki",
        plugin="office-mcp",
        identity={"mode": "claims", "map": {"x-remote-user": "email"}},
        search_aliases={"github.search": ["find code"]},
    )
    save_registry(p, reg)
    loaded = load_registry(p)
    assert loaded["wiki"].identity == {"mode": "claims", "map": {"x-remote-user": "email"}}
    assert loaded["wiki"].search_aliases == {"github.search": ["find code"]}
    assert loaded["plane"].env == {"api_key": "${BEHEROUTER_PLANE_API_KEY}"}


def test_the_cli_attach_then_detach_round_trip_keeps_comments(tmp_path, fake_cli_cmd):
    """The same guarantee through the verbs an operator actually runs."""
    import os
    import subprocess
    import sys

    p = tmp_path / "registry.toml"
    p.write_text(COMMENTED)
    env = {**os.environ, "BEHEROUTER_TEST_PLUGINS": "1", "BEHEROUTER_REGISTRY": str(p)}

    def run(*args):
        r = subprocess.run(
            [sys.executable, "-m", "beherouter.cli.app", *args],
            capture_output=True, text=True, env=env, check=False,
        )
        assert r.returncode == 0, r.stderr

    run("attach", "faketool", "_test-cli", "--config", f"cmd={fake_cli_cmd}")
    assert "faketool" in load_registry(p)
    run("detach", "faketool")
    assert p.read_text().rstrip("\n") == COMMENTED.rstrip("\n")
