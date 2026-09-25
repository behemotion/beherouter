from beherouter.indexing import build_index
from beherouter.models import ToolDescriptor


def _d(name, summary, schema=None):
    return ToolDescriptor(
        name=name,
        verb=name.split("_", 1)[-1],
        summary=summary,
        schema=schema or {},
        pinned=False,
        mutating=False,
    )


def test_build_index_finds_by_summary():
    idx = build_index([_d("t_search", "search things"), _d("t_create", "make a shelf")])
    assert "t_search" in idx.search("search")


def test_build_index_is_empty_for_no_descriptors():
    assert build_index([]).search("anything") == []


def test_index_covers_argument_names():
    d = _d("t_render", "produce output", {"--format": {"name": "--format", "type": "string"}})
    assert "t_render" in build_index([d]).search("format")


def test_index_covers_enum_values():
    d = _d(
        "t_render",
        "produce output",
        {"--format": {"name": "--format", "type": "string", "enum": ["png", "pdf"]}},
    )
    assert "t_render" in build_index([d]).search("pdf")


def test_index_strips_dashes_from_flag_names():
    d = _d("t_x", "nothing useful", {"--dry-run": {"name": "--dry-run", "type": "boolean"}})
    assert "t_x" in build_index([d]).search("dry run")


from beherouter.indexing import _arg_tokens, search_hits

PLANE_LIKE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["list", "create", "archive"]},
        "project_id": {"anyOf": [{"type": "string"}, {"type": "null"}],
                       "description": "UUID of the owning project"},
        "priority": {"anyOf": [{"enum": ["urgent", "low"]}, {"type": "null"}]},
    },
}


def test_json_schema_args_are_indexed_by_name_enum_and_description():
    tokens = " ".join(_arg_tokens(PLANE_LIKE))
    for word in ("action", "archive", "project_id", "owning", "urgent"):
        assert word in tokens


def test_json_schema_keywords_are_never_indexed():
    """Regression: the MCP dialect used to index its own schema keywords."""
    tokens = _arg_tokens(PLANE_LIKE)
    for keyword in ("properties", "required", "additionalProperties", "type"):
        assert keyword not in tokens


def test_mcp_tool_is_found_by_an_enum_value():
    d = _d("workitem", "Work items.", PLANE_LIKE)
    assert build_index([d, _d("label", "Labels.")]).search("archive") == ["workitem"]


def test_build_index_applies_aliases():
    ds = [_d("cycle", "Cycles in a project."), _d("module", "Modules.")]
    assert build_index(ds, {"cycle": ("sprint",)}).search("sprint") == ["cycle"]


def test_search_hits_shape():
    ds = [
        _d("t_search", "Search things. Long detail follows here."),
        ToolDescriptor(name="t_mystery", verb="mystery", summary="Mystery search.",
                       schema={}, pinned=False, mutating=None),
    ]
    by_name = {d.name: d for d in ds}
    hits = search_hits(build_index(ds), by_name, "search", 5, {"t_search"})
    assert hits[0] == {"name": "t_search", "brief": "Search things.",
                       "mutating": False, "pinned": True}
    assert hits[1] == {"name": "t_mystery", "brief": "Mystery search."}
