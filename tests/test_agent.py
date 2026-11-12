"""The agent's tool-use loop against a real gateway, with a stand-in for the Claude API."""

import asyncio
import io
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from toolkernel_gateway.agent import Agent
from toolkernel_gateway.app import serve
from toolkernel_gateway.bench.data import generate
from toolkernel_gateway.config import load_config
from toolkernel_gateway.gateway import Gateway

EXAMPLES = Path(__file__).parents[1] / "examples"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeClaude:
    """Plays Claude: first a multi-tool step, then (after seeing the result) a final answer."""

    def __init__(self):
        self.requests = []
        self.messages = NS(create=self.create)
        self.beta = NS(messages=NS(create=self.create))

    async def create(self, **kw):
        self.requests.append(kw)
        usage = NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
        last = kw["messages"][-1]["content"]
        if isinstance(last, str):  # a new question
            code = ("from servers.files import read_table, search_text\n"
                    "from servers.slack import list_channels\n"
                    "rows = read_table(path='sales/sales_2025.xlsx', sheet='Orders', where={'region': 'NA'})['rows']\n"
                    "hits = search_text(query='Lighthouse', path='reports')\n"
                    "len(rows), len(hits), [c['name'] for c in list_channels()]")
            return NS(content=[{"type": "text", "text": "Let me check three sources at once."},
                               {"type": "tool_use", "id": "tu_1", "name": "execute_code", "input": {"code": code}}],
                      stop_reason="tool_use", usage=usage)
        result = last[0]["content"]
        return NS(content=[{"type": "text", "text": f"Done. The runtime said:\n{result.splitlines()[1]}"}],
                  stop_reason="end_turn", usage=usage)


@pytest.fixture
async def gateway_url(tmp_path):
    data = tmp_path / "data"
    generate(data)
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {
        "files": {"command": sys.executable, "args": ["-m", "toolkernel_gateway.mcp_servers.files", "--root", str(data)]},
        "slack": {"command": sys.executable, "args": [str(EXAMPLES / "mock_slack.py")]},
    }}))
    port = free_port()
    gw = Gateway(load_config(None, {"server": {"port": port, "bridge_port": free_port()}, "data_dir": str(tmp_path / "gw"),
                                    "upstreams": {"config_file": str(mcp_json), "watch": False}}))
    task = asyncio.create_task(serve(gw))
    for _ in range(200):
        ups = list(gw.upstreams.upstreams.values())
        if len(ups) == 2 and all(u.status == "ready" for u in ups):
            break
        await asyncio.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"
    gw.request_shutdown()
    await asyncio.wait_for(task, 15)


async def test_agent_runs_a_multi_tool_step(gateway_url, tmp_path):
    fake, out = FakeClaude(), io.StringIO()
    async with Agent(gateway_url=gateway_url, client=fake, out=out, color=False) as agent:
        answer = await agent.ask("How many NA orders are there, and what do the reports say about Lighthouse?")
        agent.save_transcript(tmp_path / "t.md")
    # Claude got the gateway's tools and instructions, and the request used the current API shape.
    req = fake.requests[0]
    assert [t["name"] for t in req["tools"]][0] == "execute_code"
    assert "persistent Python environment" in req["system"]
    assert req["thinking"] == {"type": "adaptive"} and req["output_config"] == {"effort": "medium"}
    assert req["fallbacks"] == "default"  # opus 5.5 gets the server-side refusal fallback
    # One execute_code made three real upstream calls; the agent printed each step and the result.
    printed = out.getvalue()
    assert "▸ execute_code" in printed and "read_table(path='sales/sales_2025.xlsx'" in printed
    assert "(8704, 3, ['#eng', '#general', '#alerts'])" in answer
    assert "2 model calls · 1 tool calls" in printed
    assert "## Claude" in (tmp_path / "t.md").read_text()
