"""Which of the two argument-schema dialects a descriptor carries.

`cli` backends: `{arg_name: {"name","type","required","enum"}}` (a beheaxi
manifest). `mcp` backends: a JSON Schema object. A zero-argument MCP tool
publishes a bare `{"type": "object"}` with no `properties` (4 of gitea-mcp's 50
tools do), so the dialect is detected by its marker keys, not by `properties`.
ONE test, used by the indexer and by argument preparation alike.
"""


def is_json_schema(schema: dict) -> bool:
    return bool(schema) and (
        "properties" in schema or "$schema" in schema or schema.get("type") == "object"
    )
