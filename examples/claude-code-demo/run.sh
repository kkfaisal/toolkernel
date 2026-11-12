#!/bin/sh
# Run Claude Code headless with only ToolKernel connected, then render a readable transcript.
# Needs a running gateway (toolkernel serve) and a logged-in Claude Code. Uses your Claude Code usage.
# The previous run.jsonl / transcript.md are kept unless the new run actually reached ToolKernel.
cd "$(dirname "$0")"
URL=$(python3 -c "import json; print(json.load(open('mcp.json'))['mcpServers']['toolkernel']['url'])")
if ! curl -fsS -o /dev/null "${URL%/mcp}/api/overview"; then
  echo "ToolKernel isn't reachable at ${URL%/mcp}. Start it first:  toolkernel serve" >&2
  exit 1
fi
claude -p "$(cat task.md)" --mcp-config mcp.json --strict-mcp-config \
  --allowedTools "mcp__toolkernel__*" \
  --disallowedTools "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,Agent,NotebookEdit,TodoWrite" \
  --output-format stream-json --verbose --max-turns 40 > run.new.jsonl
python3 - <<'EOF' || { echo "kept the previous transcript; this run is in run.new.jsonl" >&2; exit 1; }
import json, sys
events = [json.loads(l) for l in open("run.new.jsonl") if l.strip()]
init = next((e for e in events if e.get("subtype") == "init"), {})
status = {s["name"]: s["status"] for s in init.get("mcp_servers", [])}
if status.get("toolkernel") != "connected":
    sys.exit(f"Claude Code couldn't connect to ToolKernel (status: {status.get('toolkernel', 'missing')})")
result = next((e for e in events if e.get("type") == "result"), {})
if result.get("is_error"):
    sys.exit(f"Claude Code stopped with an error: {result.get('result')}")
EOF
mv run.new.jsonl run.jsonl
python3 render.py
echo "The Slack post waits for approval: open ${URL%/mcp}/#/approvals"
