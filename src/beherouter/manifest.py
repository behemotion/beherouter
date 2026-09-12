"""Load beheaxi's manifest_schema.json and validate attached tools' describe output.

The contract is deliberately CLOSED (`additionalProperties: false` at every level).
A sibling that emits an extra key fails attach with Unavailable (exit 6). Layer
authors who need a new field extend the schema *in beheaxi*, never locally.
"""

import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

import jsonschema

from .errors import Unavailable


@lru_cache(maxsize=1)
def load_schema() -> dict:
    """The beheaxi manifest schema — packaged resource, with a source-tree fallback."""
    try:
        return json.loads((files("beheaxi") / "manifest_schema.json").read_text())
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        import beheaxi

        path = Path(beheaxi.__file__).parent / "manifest_schema.json"
        return json.loads(path.read_text())


def validate_manifest(obj: dict) -> None:
    """Raise Unavailable if `obj` does not conform to the beheaxi manifest schema."""
    try:
        jsonschema.validate(obj, load_schema())
    except jsonschema.ValidationError as e:
        raise Unavailable(f"describe --json is not a valid manifest: {e.message}") from e
