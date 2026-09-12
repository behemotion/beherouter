import pytest

from beherouter.envexpand import expand
from beherouter.errors import UsageError


def test_literal_values_pass_through():
    assert expand("s", {"a": "plain"}) == {"a": "plain"}


def test_whole_value_placeholder_is_resolved(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    assert expand("s", {"a": "${TOK}"}) == {"a": "secret"}


def test_partial_placeholder_is_not_interpolated(monkeypatch):
    monkeypatch.setenv("TOK", "secret")
    assert expand("s", {"a": "Bearer ${TOK}"}) == {"a": "Bearer ${TOK}"}


def test_unset_variable_raises_usage_error(monkeypatch):
    monkeypatch.delenv("MISSING", raising=False)
    with pytest.raises(UsageError, match="MISSING"):
        expand("s", {"a": "${MISSING}"})


def test_empty_variable_raises_usage_error(monkeypatch):
    monkeypatch.setenv("EMPTY", "")
    with pytest.raises(UsageError, match="EMPTY"):
        expand("s", {"a": "${EMPTY}"})


def test_error_names_the_context_and_key(monkeypatch):
    monkeypatch.delenv("MISSING", raising=False)
    with pytest.raises(UsageError, match="plane.*api_key"):
        expand("plane", {"api_key": "${MISSING}"})


def test_error_never_contains_the_value(monkeypatch):
    monkeypatch.setenv("TOK", "supersecret")
    # a resolved value must not leak even when a LATER key fails
    monkeypatch.delenv("MISSING", raising=False)
    with pytest.raises(UsageError) as e:
        expand("s", {"good": "${TOK}", "bad": "${MISSING}"})
    assert "supersecret" not in str(e.value)
