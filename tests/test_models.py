from beherouter.models import Backend, ToolDescriptor


def _d(**over):
    base = {
        "name": "t",
        "verb": "t",
        "summary": "s",
        "schema": {},
        "pinned": False,
        "mutating": None,
    }
    return ToolDescriptor(**{**base, **over})


def test_mutating_is_tri_state():
    """None is 'the backend did not say', distinct from False.

    Defaulting an unannotated tool to False is the bug this fixes (every Plane
    delete advertised read-only); defaulting to True would make every read tool
    on an unannotated backend look destructive. Absence of evidence gets its own
    value, exactly as health.PROBE_NONE does.
    """
    assert _d(mutating=None).mutating is None
    assert _d(mutating=False).mutating is False
    assert _d(mutating=True).mutating is True


def test_annotations_and_output_schema_default_to_none():
    d = _d()
    assert d.annotations is None
    assert d.output_schema is None


def test_annotations_and_output_schema_round_trip():
    d = _d(
        annotations={"readOnlyHint": True, "destructiveHint": False},
        output_schema={"type": "object", "properties": {"entries": {"type": "array"}}},
    )
    assert d.annotations["readOnlyHint"] is True
    assert d.output_schema["properties"]["entries"]["type"] == "array"


def test_backend_pinned_still_filters_descriptors():
    b = Backend(
        name="b",
        kind="mcp",
        descriptors=[_d(name="a", pinned=True), _d(name="b", pinned=False)],
        executor=object(),
    )
    assert [d.name for d in b.pinned] == ["a"]
