"""Drive a running gateway over MCP. Usage: python tests/e2e_client.py [url]"""
import asyncio, sys
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7300/mcp"

STEPS = [
    ("list_servers", {}),
    ("search_tools", {"query": "github issues"}),
    ("describe_tool", {"name": "github.list_issues"}),
    ("execute_code", {"code": '''from datetime import datetime, timedelta, timezone
from servers.github import list_issues
issues = list_issues(owner="acme", repo="api", state="open", labels=["bug"], per_page=100)
cutoff = datetime(2026, 11, 11, tzinfo=timezone.utc) - timedelta(days=30)
stale = [i for i in issues if datetime.fromisoformat(i["updated_at"].replace("Z", "+00:00")) < cutoff]
len(issues), len(stale)'''}),
    ("execute_code", {"code": "issues"}),
    ("execute_code", {"code": "print(issues)"}),
    ("execute_code", {"code": "from servers.crm import list_accounts\nimport pandas as pd\ndf = pd.DataFrame(list_accounts())\ndf.groupby('region').arr.sum()"}),
    ("execute_code", {"code": "from servers.github import list_isues"}),
    ("execute_code", {"code": "list_issues(owner='acme', repo='api', status='open')"}),
    ("execute_code", {"code": "for i in range(5):\n    print('same line')\n1/0"}),
    ("call_tool", {"name": "github.get_issue", "arguments": {"owner": "acme", "repo": "api", "number": 3}}),
    ("call_tool", {"name": "crm.list_accounts", "arguments": {}}),
    ("execute_code", {"code": "save(stale, 'stale'); stash['n'] = len(stale); preview(df)"}),
    ("reset_runtime", {}),
    ("execute_code", {"code": "stale = load('stale'); len(stale), stash['n']"}),
]

async def main():
    async with streamable_http_client(URL) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            init = await s.initialize()
            print("INSTRUCTIONS:", init.instructions[:200], "...\n")
            tools = await s.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])
            print("execute_code desc:", tools.tools[0].description, "\n")
            for name, args in STEPS:
                res = await s.call_tool(name, args)
                print(f"===== {name}({str(args)[:70]}) error={res.is_error}")
                print(res.content[0].text)
                print()
            # print a spill handle page if any
asyncio.run(main())
