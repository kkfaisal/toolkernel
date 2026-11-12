"""A CRM-like MCP server that returns large, unstructured JSON text (no output schema)."""

import json
import random

from mcp.server import MCPServer

mcp = MCPServer("crm")
_rng = random.Random(11)
REGIONS = ["EMEA", "NA", "APAC", "LATAM"]
ACCOUNTS = [
    {"id": f"A{n:05d}", "name": f"Account {n}", "region": _rng.choice(REGIONS),
     "arr": _rng.randint(1_000, 900_000), "health": _rng.choice(["green", "yellow", "red"]),
     "owner": _rng.choice(["ana", "bo", "chen"]), "notes": "x" * _rng.randint(20, 120)}
    for n in range(10_000)
]


@mcp.tool(structured_output=False)
def list_accounts(region: str | None = None) -> str:
    """Export all CRM accounts as JSON (about 10,000 rows). Optionally filter by region (EMEA, NA, APAC, LATAM)."""
    rows = [a for a in ACCOUNTS if region is None or a["region"] == region]
    return json.dumps(rows)


@mcp.tool(structured_output=False)
def get_account(id: str) -> str:
    """Get one account by id, e.g. A00042."""
    for a in ACCOUNTS:
        if a["id"] == id:
            return json.dumps(a)
    raise ValueError(f"no account {id}")


if __name__ == "__main__":
    mcp.run()
