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
    with pytest.raises(UsageError, match=r"plane.*api_key"):
        expand("plane", {"api_key": "${MISSING}"})


def test_error_never_contains_the_value(monkeypatch):
    monkeypatch.setenv("TOK", "supersecret")
    # a resolved value must not leak even when a LATER key fails
    monkeypatch.delenv("MISSING", raising=False)
    with pytest.raises(UsageError) as e:
        expand("s", {"good": "${TOK}", "bad": "${MISSING}"})
    assert "supersecret" not in str(e.value)


def test_file_placeholder_reads_the_file(tmp_path):
    f = tmp_path / "key"
    f.write_text("s3cret\n")
    assert expand("s", {"a": f"${{file:{f}}}"}) == {"a": "s3cret"}


def test_file_placeholder_strips_exactly_one_trailing_newline(tmp_path):
    f = tmp_path / "key"
    f.write_text("s3cret\n\n")
    assert expand("s", {"a": f"${{file:{f}}}"}) == {"a": "s3cret\n"}


@pytest.mark.parametrize("content", ["", "\n"])
def test_empty_file_is_refused(tmp_path, content):
    f = tmp_path / "key"
    f.write_text(content)
    with pytest.raises(UsageError, match="empty"):
        expand("s", {"a": f"${{file:{f}}}"})


def test_missing_file_is_refused_by_path(tmp_path):
    f = tmp_path / "nope"
    with pytest.raises(UsageError, match=str(f)):
        expand("s", {"a": f"${{file:{f}}}"})


def test_relative_file_path_is_refused():
    with pytest.raises(UsageError, match="absolute"):
        expand("s", {"a": "${file:run/secrets/k}"})


def test_unreadable_file_names_the_error_type_not_the_content(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(UsageError) as e:
        expand("s", {"a": f"${{file:{d}}}"})
    assert "IsADirectoryError" in str(e.value)


def test_file_error_never_contains_the_content(tmp_path):
    f = tmp_path / "key"
    f.write_text("supersecret")
    out = expand("s", {"a": f"${{file:{f}}}"})
    assert out["a"] == "supersecret"
    f.write_text("")
    with pytest.raises(UsageError) as e:
        expand("s", {"a": f"${{file:{f}}}"})
    assert "supersecret" not in str(e.value)


def test_file_refs_lists_only_file_placeholders():
    from beherouter.envexpand import file_refs

    assert file_refs({"a": "${file:/x}", "b": "${VAR}", "c": "lit", "d": "${file:/y}"}) == [
        "/x",
        "/y",
    ]
    assert file_refs(None) == []


def test_non_utf8_file_is_refused_without_quoting_its_bytes(tmp_path):
    f = tmp_path / "key"
    f.write_bytes(b"\xff\xfe")
    with pytest.raises(UsageError) as e:
        expand("s", {"a": f"${{file:{f}}}"})
    assert "UnicodeDecodeError" in str(e.value)
    assert "0xff" not in str(e.value)
    assert e.value.__cause__ is None
    import traceback

    assert "0xff" not in "".join(traceback.format_exception(e.value))
