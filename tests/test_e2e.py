"""End-to-end: a real gateway, real mock MCP servers over stdio, a real MCP client over Streamable HTTP."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from toolkernel_gateway.app import serve
from toolkernel_gateway.config import load_config
from toolkernel_gateway.gateway import Gateway

EXAMPLES = Path(__file__).parents[1] / "examples"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", "toolkernel/runtime:1"], capture_output=True).returncode == 0


DRIVERS = ["local"] + (["docker"] if os.environ.get("TOOLKERNEL_TEST_DOCKER") and docker_ready() else [])


@pytest.fixture(params=DRIVERS)
async def gateway(tmp_path, request):
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {
        name: {"command": sys.executable, "args": [str(EXAMPLES / f"mock_{name}.py")]} for name in ("github", "slack", "crm")
    }}))
    port, bridge = free_port(), free_port()
    cfg = load_config(None, {
        "server": {"port": port, "bridge_port": bridge},
        "data_dir": str(tmp_path / "data"),
        "upstreams": {"config_file": str(mcp_json), "watch": False},
        "runtime": {"idle_timeout_s": 2, "exec_timeout_s": 5, "driver": request.param},
        "output": {"budget_chars": 1500},
        "policies": {"rules": [{"match": "*", "when": {"annotation": "destructiveHint"}, "action": "approve"}],
                     "approval_timeout_s": 10},
    })
    gw = Gateway(cfg)
    task = asyncio.create_task(serve(gw))
    base = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(base_url=base) as http:
        for _ in range(150):
            try:
                servers = (await http.get("/api/servers")).json()
                if len(servers) == 3 and all(s["status"] == "ready" for s in servers):
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("upstreams did not become ready")
        yield gw, base, http
    task.cancel()
    with pytest.raises(BaseException):
        await task


async def call(session: ClientSession, tool: str, **args) -> str:
    res = await session.call_tool(tool, args)
    return res.content[0].text


async def test_full_flow(gateway):
    gw, base, http = gateway
    async with streamable_http_client(base + "/mcp") as streams, ClientSession(streams[0], streams[1]) as s:
        init = await s.initialize()
        assert "persistent Python environment" in init.instructions
        names = [t.name for t in (await s.list_tools()).tools]
        assert names == ["execute_code", "search_tools", "describe_tool", "list_servers", "call_tool", "read_output", "reset_runtime"]

        assert "github.list_issues" in await call(s, "search_tools", query="issues")

        out = await call(s, "execute_code", code=(
            "from servers.github import list_issues\n"
            "issues = list_issues(owner='acme', repo='api', state='all', per_page=100)\n"
            "len(issues)"))
        assert "[result]\n100" in out
        assert "[state] issues: list[100]" in out
        assert "tool calls 1 (github.list_issues ×1)" in out

        # Careless print: guarded to the budget, spilled, pageable.
        out = await call(s, "execute_code", code="print(issues)")
        assert len(out) < 1700 and "read_output(" in out
        handle = out.split('read_output("')[1].split('"')[0]
        page = await call(s, "read_output", handle=handle, offset=0, limit=300)
        assert page.startswith("[stdout]") and "next: read_output" in page

        # Static check catches typos before running.
        out = await call(s, "execute_code", code="list_issues(owner='acme', repo='api', status='open')")
        assert "Static check failed" in out and "no argument 'status'" in out

        # State persists; save() survives restarts; idle reaping produces a restart notice.
        await call(s, "execute_code", code="save(issues[:5], 'few'); stash['k'] = 3")
        await asyncio.sleep(2.5)
        for _ in range(60):  # reaper runs every idle/4 seconds
            sess = next(iter(gw.sessions.values()))
            if sess.runtime is None:
                break
            await asyncio.sleep(0.1)
        out = await call(s, "execute_code", code="few = load('few'); len(few), stash['k']")
        assert out.startswith("[runtime] Restarted since your last call (reason: idle timeout")
        assert "few.json" in out and "stash keys: k" in out
        assert "[result]\n(5, 3)" in out

        # Approval: a destructive call waits for a person.
        async def approve_when_asked():
            for _ in range(100):
                pending = [a for a in (await http.get("/api/approvals")).json() if a["status"] == "pending"]
                if pending:
                    await http.post(f"/api/approvals/{pending[0]['id']}", json={"approve": True})
                    return pending[0]
                await asyncio.sleep(0.05)

        approver = asyncio.create_task(approve_when_asked())
        out = await call(s, "execute_code", code=(
            "from servers.slack import post_message\npost_message(channel='#eng', text='hi')['ok']"))
        asked = await approver
        assert asked["tool"] == "post_message" and asked["arguments"]["text"] == "hi"
        assert "[result]\nTrue" in out

        # Hybrid direct call keeps big results in the runtime as _last.
        out = await call(s, "call_tool", name="crm.list_accounts", arguments={})
        assert "list[10000] of dict" in out and "_last" in out and len(out) < 1500

        # Timeout interrupts without losing state.
        out = await call(s, "execute_code", code="import time\nkept = 1\ntime.sleep(30)")
        assert "time limit" in out
        assert "[result]\n1" in await call(s, "execute_code", code="kept")

    runs = (await http.get("/api/runs?limit=100")).json()
    big = next(r for r in runs if r["tool"] == "call_tool")
    assert big["bytes_from_tools"] > 1_000_000 and big["bytes_returned"] < 1500
    detail = (await http.get(f"/api/runs/{big['id']}")).json()
    assert detail["tool_calls"][0]["tool"] == "crm.list_accounts"


async def test_rest_interface(gateway):
    gw, base, http = gateway
    tools = (await http.get("/v1/tools?format=openai")).json()
    assert tools[0]["type"] == "function" and tools[0]["function"]["name"] == "execute_code"
    sid = (await http.post("/v1/sessions", json={})).json()["session_id"]
    res = (await http.post(f"/v1/sessions/{sid}/call", json={"tool": "execute_code", "arguments": {"code": "6*7"}})).json()
    assert "[result]\n42" in res["content"] and not res["is_error"]


async def test_docker_runtime_is_isolated(gateway):
    gw, base, http = gateway
    if gw.cfg["runtime"]["driver"] != "docker":
        pytest.skip("isolation applies to the docker driver")
    code = (
        "import urllib.request, os\n"
        "try:\n"
        "    urllib.request.urlopen('http://example.com', timeout=3); net = 'open'\n"
        "except Exception as e:\n"
        "    net = 'blocked'\n"
        "from servers.github import get_issue\n"
        "(net, get_issue(owner='acme', repo='api', number=1)['number'], os.getuid(), "
        "any(k.startswith('TOOLKERNEL_SECRET') for k in os.environ))"
    )
    async with streamable_http_client(base + "/mcp") as streams, ClientSession(streams[0], streams[1]) as s:
        await s.initialize()
        out = await call(s, "execute_code", code=code)
    assert "[result]\n('blocked', 1, 1000, False)" in out, out


async def test_repeated_calls_get_a_note(gateway):
    gw, base, http = gateway
    sid = (await http.post("/v1/sessions", json={})).json()["session_id"]
    call = lambda: http.post(f"/v1/sessions/{sid}/call", json={"tool": "list_servers", "arguments": {}})
    texts = [(await call()).json()["content"] for _ in range(3)]
    assert "[note]" not in texts[1] and "same list_servers call as your last 2" in texts[2]
    # A large expression result says plainly that `_` is a variable, not a read_output handle.
    res = (await http.post(f"/v1/sessions/{sid}/call", json={"tool": "execute_code",
                                                            "arguments": {"code": "list(range(5000))"}})).json()
    assert "Python variable `_`" in res["content"] and "not a read_output handle" in res["content"]
