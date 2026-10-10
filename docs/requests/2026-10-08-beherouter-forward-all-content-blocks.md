# beherouter: forward every MCP content block, not just `structured_content`

**Date:** 2026-10-08
**From:** Unibank infra (`ai` namespace, beherouter 0.2.5, chart 0.1.6, revision 14)
**To:** beherouter upstream (`https://github.com/behemotion/beherouter`)
**Severity:** correctness. A backend's warning to the model is silently dropped, so the model tells the user an action succeeded when it partly failed.

## Problem

`beherouter/backends/mcp.py::_payload` picks **one** of three sources from a
backend's `CallToolResult`:

1. `.data`, if it is not None
2. else `.structured_content`, if it is not None
3. else every `text` block in `.content`, joined with `"\n"`

So once a backend returns structured output, **every `content` block is
discarded**, including text blocks that carry information that is not in the
structured payload. This applies to both `run_tool` and pinned tools: both go
through `dispatch` → `MCPClientExecutor` / `ReconnectingMCPExecutor` → `{"result": _payload(res)}`.
`backends/inproc.py` reuses the same `_payload`.

## Why it matters to us

Our plane-mcp middleware (`ReportDroppedAssignees`) appends a second text block
to `workitem` `create` / `update` / `manage_assignee` results when Plane
silently drops assignees who are not project members:

```
NOT ASSIGNED: <uuid>, … -- not a member of this project. … Tell the user who
was not assigned -- do not report the assignment as done, and do not retry it.
```

The work-item tool has an output schema, so `.data` is set, and the note never
leaves beherouter. LibreChat would show the model every text block (it joins
them), but it only receives beherouter's `{"result": …}`. The model therefore
reports the assignment as done.

There is no clean workaround on the backend side. We measured both options:
- **Fold the note into the first text block:** lost. Content is ignored whenever
  `.data` or `.structured_content` is present.
- **Add an extra key to `structured_content`:** lost too. `.data` is deserialized
  against the declared output schema, which drops undeclared keys, and `_payload`
  prefers `.data`.

## Reproduction (in `ghcr.io/behemotion/beherouter:0.2.5`, no cluster needed)

```python
import asyncio
from fastmcp import FastMCP, Client
from fastmcp.server.middleware import Middleware
from mcp.types import TextContent
from pydantic import BaseModel
from beherouter.backends.mcp import _payload

class WorkItem(BaseModel):
    id: str
    assignees: list[str]

srv = FastMCP("plane-like")

@srv.tool
def create_work_item(name: str) -> WorkItem:
    return WorkItem(id="wi-1", assignees=["a"])

class AddNote(Middleware):
    async def on_call_tool(self, context, call_next):
        r = await call_next(context)
        r.content = [*r.content, TextContent(type="text", text="NOT ASSIGNED: b -- not a member")]
        return r
srv.add_middleware(AddNote())

async def main():
    async with Client(srv) as c:
        res = await c.call_tool("create_work_item", {"name": "x"})
        print("upstream content blocks:", len(res.content))
        print("beherouter forwards:", {"result": _payload(res)})
asyncio.run(main())
```

```
$ podman run --rm -v ./probe:/probe:ro,Z --entrypoint python ghcr.io/behemotion/beherouter:0.2.5 /probe/probe.py
upstream content blocks: 2
beherouter forwards: {'result': Root(id='wi-1', assignees=['a'])}
```

The second block is gone.

## What we need

A result from beherouter has to carry every **text** block the backend sent,
in order, when that block is not just the JSON serialization of the structured
payload (FastMCP always emits that serialization as `content[0]`).

One possible shape, which upstream may change:

- In `_payload` (or the executors), collect the text blocks that do not
  round-trip to `structured_content`, and return them beside the result, for
  example `{"result": <payload>, "notes": ["NOT ASSIGNED: …"]}`. Include the
  key only when it is non-empty.
- Declare `notes` (an optional array of strings) in `wrapped_output_schema`, so
  output validation still passes and code-mode hosts see it.
- Tests: a structured result plus an extra text block keeps the block; a
  structured result alone gives the same output as today; a text-only result
  is unchanged.

Non-text blocks (images, resources) are dropped the same way. They are out of
scope for us, but worth a note in the docs if they stay dropped.

## Answers we already have (no action needed)

- No size cap or summarisation in the 0.2.5 result path was found. The loss
  comes from `_payload`'s source selection, not from truncation.
- `isError: true` results are unaffected. They become `UsageError("backend
  rejected …: <message>")`, which keeps the message.

## Acceptance

Through the gateway, `run_tool` for a Plane `workitem` `create` whose assignee is
not a project member returns a result containing `NOT ASSIGNED:`. End to end,
LibreChat's agent tells the user who was not assigned.
