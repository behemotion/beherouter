import pytest

from beherouter.errors import Conflict
from beherouter.naming import detect_collisions, flatten


def test_flatten_simple():
    assert flatten("behelib", "search") == "behelib_search"


def test_flatten_hyphenated_verb():
    """beheaxi v0.1.0 reality: fn name -> hyphenated verb."""
    assert flatten("behelib", "shelf-create") == "behelib_shelf_create"


def test_flatten_space_joined_too():
    """Forward-compat with any future nested-group form."""
    assert flatten("behelib", "shelf create") == "behelib_shelf_create"


def test_detect_collisions_passes_when_unique():
    detect_collisions("behelib", ["search", "shelf-create"])  # no raise


def test_detect_collisions_raises_conflict():
    # "read-multi" and an explicitly-named "read_multi" both normalize to
    # behemem_read_multi.
    with pytest.raises(Conflict) as e:
        detect_collisions("behemem", ["read-multi", "read_multi"])
    msg = str(e.value)
    assert "read-multi" in msg and "read_multi" in msg


def test_detect_collisions_allows_repeated_identical_verb():
    """The same verb listed twice is not a collision — only distinct verbs are."""
    detect_collisions("behelib", ["search", "search"])  # no raise
