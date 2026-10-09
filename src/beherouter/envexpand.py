"""Resolve whole-value `${VAR}` placeholders against the gateway's environment.

Shared by every backing so the rule cannot drift. Only a value that is EXACTLY
one placeholder is substituted — no partial or recursive interpolation. That
keeps the rule easy to audit and means a literal value containing a brace can
never be mistaken for a secret reference.

An unset OR EMPTY variable is a UsageError rather than an empty string: an empty
credential yields a surface that attaches cleanly and then fails every call,
which is precisely the silent breakage this path exists to prevent (observed
2026-07-30 with a revoked Gitea PAT).

`${file:/absolute/path}` is the second whole-value form: the file's content,
read when the value is resolved (preflight, attach, reload) and never per call.
It is how a rotated secret reaches a running gateway: a process's environment
never changes, a mounted Secret does. Exactly one trailing newline is stripped
(Kubernetes Secrets and `echo >` add one). Missing, unreadable, empty or
relative is a UsageError naming the PATH, never the content.
"""

import os
import re
from pathlib import Path

from .errors import UsageError

PLACEHOLDER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
FILE_PLACEHOLDER = re.compile(r"^\$\{file:([^{}]+)\}$")


def _read_file(context: str, key: str, raw: str) -> str:
    path = Path(raw)
    if not path.is_absolute():
        raise UsageError(
            f"'{context}': '{key}' references ${{file:{raw}}}; the path must be absolute"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        # type(e).__name__, never {e}, and `from None`: the original's message
        # (and a chained traceback) can quote a byte of the file.
        raise UsageError(
            f"'{context}': '{key}' references ${{file:{raw}}}, which is unreadable "
            f"({type(e).__name__})"
        ) from None
    if text.endswith("\n"):
        text = text[:-1]
    if text == "":
        raise UsageError(f"'{context}': '{key}' references ${{file:{raw}}}, which is empty")
    return text


def expand(context: str, values: dict[str, str]) -> dict[str, str]:
    """Resolve placeholders in `values`. `context` names the caller for errors."""
    out: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(value, str):
            out[key] = value
            continue
        f = FILE_PLACEHOLDER.match(value)
        if f is not None:
            out[key] = _read_file(context, key, f.group(1))
            continue
        m = PLACEHOLDER.match(value)
        if m is None:
            out[key] = value
            continue
        var = m.group(1)
        resolved = os.environ.get(var)
        if resolved is None or resolved == "":
            raise UsageError(
                f"'{context}': '{key}' references ${{{var}}}, which is "
                f"unset or empty in the gateway's environment"
            )
        out[key] = resolved
    return out


def file_refs(values: dict | None) -> list[str]:
    """The paths of every `${file:...}` value, in order: what a watch must stat."""
    out: list[str] = []
    for value in (values or {}).values():
        m = FILE_PLACEHOLDER.match(value) if isinstance(value, str) else None
        if m is not None:
            out.append(m.group(1))
    return out
