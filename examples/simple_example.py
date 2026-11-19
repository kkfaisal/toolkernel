"""The smallest ToolKernel example: send Python the way an agent would.

Start the gateway first (`toolkernel serve`), then run:  python examples/simple_example.py
Uses only the standard library.
"""
import json
import urllib.request

GATEWAY = "http://localhost:7300"


def post(path, body):
    req = urllib.request.Request(GATEWAY + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


# 1. Open a session. It gets its own Python runtime, and variables persist inside it.
sid = post("/v1/sessions", {"client": "simple example"})["session_id"]


def run(code):
    """What an agent does: call the execute_code tool with some Python."""
    out = post(f"/v1/sessions/{sid}/call", {"tool": "execute_code", "arguments": {"code": code}})
    print(">>> sent:\n" + code.strip() + "\n<<< the model sees:\n" + out["content"] + "\n")


# 2. Load a 24,000-row spreadsheet through the files MCP server.
#    The rows stay in the runtime; only the last line's value comes back.
run("""
from servers.files import read_table
orders = read_table(path="sales/sales_2025.xlsx", sheet="Orders")["rows"]
len(orders)
""")

# 3. A second step reuses `orders` without fetching the file again.
run("""
q3 = [o for o in orders if o["order_date"][5:7] in ("07", "08", "09")]
round(sum(o["revenue"] for o in q3 if o["region"] == "EMEA"), 2)
""")
