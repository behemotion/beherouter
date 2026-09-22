"""Minimal beheaxi-shaped tool: `fake_tool describe --json` emits a valid manifest."""

import json
import sys

MANIFEST = {
    "tool": "faketool",
    "version": "0.1.0",
    "summary": "a fake tool",
    "verbs": [
        {
            "name": "search",
            "summary": "search things",
            "args": [{"name": "query", "type": "string", "required": True}],
            "pinned": True,
            "mutating": False,
        },
        {
            # hyphenated, as beheaxi v0.1.0 emits
            "name": "shelf-create",
            "summary": "create a shelf",
            "args": [
                {"name": "name", "type": "string", "required": True},
                {"name": "limit", "type": "integer", "required": False},
            ],
            "pinned": False,
            "mutating": True,
        },
    ],
}

if __name__ == "__main__":
    raw = sys.argv[1:]
    # `echo-argv` reports the vector VERBATIM; every other verb sees --json
    # stripped, the way a real beheaxi CLI consumes it as an output-format flag
    # rather than passing it through as a positional.
    if raw[:1] == ["echo-argv"]:
        print(json.dumps({"argv": raw}))
        sys.exit(0)
    argv = [a for a in raw if a != "--json"]
    if argv[:1] == ["describe"]:
        print(json.dumps(MANIFEST))
        sys.exit(0)
    if argv[:1] == ["search"]:
        print(json.dumps({"hits": argv[1:]}))
        sys.exit(0)
    if argv[:1] == ["shelf-create"]:
        print(json.dumps({"created": argv[1:]}))
        sys.exit(0)
    if argv[:1] == ["whoami"]:
        import os

        print(json.dumps({"user": os.environ.get("REMOTE_USER")}))
        sys.exit(0)
    if argv[:1] == ["slow"]:
        import time

        time.sleep(30)
        sys.exit(0)
    if argv[:1] == ["garbage"]:
        print("not json")
        sys.exit(0)
    if argv[:1] == ["boom"]:
        print("stack trace", file=sys.stderr)
        sys.exit(int(argv[1]) if len(argv) > 1 else 1)
    sys.exit(2)
