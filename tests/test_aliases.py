from beherouter.plugins import get, resolve_aliases
from beherouter.registry import RegistryEntry


def test_registry_aliases_add_to_the_plugins_never_replace():
    plugin = get("plane")
    entry = RegistryEntry(name="p", plugin="plane",
                          search_aliases={"cycle": ["PI"], "label": ["tag"]})
    merged = resolve_aliases(entry, plugin)
    assert set(plugin.spec.search_aliases.get("cycle", ())) <= set(merged["cycle"])
    assert "PI" in merged["cycle"]
    assert merged["label"][-1] == "tag"


def test_no_registry_aliases_yields_the_plugins():
    plugin = get("plane")
    merged = resolve_aliases(RegistryEntry(name="p", plugin="plane"), plugin)
    assert merged == {k: tuple(v) for k, v in plugin.spec.search_aliases.items()}
