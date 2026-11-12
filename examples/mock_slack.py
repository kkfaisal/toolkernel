"""A Slack-like MCP server. post_message is marked destructive, so default policies can require approval."""

from mcp.server import MCPServer
from mcp_types import ToolAnnotations

mcp = MCPServer("slack", instructions="Mock Slack. Channels: #eng, #general, #alerts.")
POSTED: list[dict] = []


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_channels() -> list[dict]:
    """List channels the bot can post to."""
    return [{"id": "C1", "name": "#eng", "members": 42}, {"id": "C2", "name": "#general", "members": 310},
            {"id": "C3", "name": "#alerts", "members": 12}]


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True))
def post_message(channel: str, text: str) -> dict:
    """Post a message to a channel."""
    POSTED.append({"channel": channel, "text": text})
    return {"ok": True, "channel": channel, "ts": f"1731300000.{len(POSTED):06d}"}


if __name__ == "__main__":
    mcp.run()
