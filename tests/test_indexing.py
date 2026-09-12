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
