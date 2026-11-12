"""`toolkernel connect URL`: a stdio ↔ Streamable HTTP adapter for clients that only speak stdio."""

from __future__ import annotations

from typing import Any

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared._httpx_utils import create_mcp_http_client


async def run_shim(url: str, key: str | None) -> None:
    url = url.rstrip("/")
    if not url.endswith("/mcp"):
        url += "/mcp"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with create_mcp_http_client(headers=headers) as http, streamable_http_client(url, http_client=http) as streams:
        async with ClientSession(streams[0], streams[1]) as remote:
            init = await remote.initialize()

            async def on_list_tools(ctx: Any, params: Any):
                return await remote.list_tools(params=params)

            async def on_call_tool(ctx: Any, params: Any):
                return await remote.call_tool(params.name, params.arguments or {})

            server = Server("toolkernel", version=init.server_info.version, instructions=init.instructions,
                            on_list_tools=on_list_tools, on_call_tool=on_call_tool)
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())
