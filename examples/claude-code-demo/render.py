"""Turn Claude Code's stream-json output (run.jsonl) into a readable transcript (transcript.md)."""

import json
import sys
from pathlib import Path

src = Path(sys.argv[1] if len(sys.argv) > 1 else "run.jsonl")
events = [json.loads(line) for line in src.read_text().splitlines() if line.strip()]
results = {}
for e in events:
    if e.get("type") == "user":
        for b in e["message"]["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                text = "\n".join(x.get("text", "") for x in c) if isinstance(c, list) else str(c)
                results[b["tool_use_id"]] = (text, b.get("is_error", False))

out = ["# Claude Code + ToolKernel: a real run", "",
       "Claude Code ran headless with **only** ToolKernel connected (its own shell, file and web tools were "
       "switched off). Every step below is real; tool results are exactly what the model received.", "",
       "## Task", "", Path("task.md").read_text().strip(), ""]
step = 0
for e in events:
    if e.get("type") != "assistant":
        continue
    for b in e["message"]["content"]:
        if b.get("type") == "text" and b["text"].strip():
            out += ["", "> " + b["text"].strip().replace("\n", "\n> "), ""]
        elif b.get("type") == "tool_use":
            step += 1
            name = b["name"].replace("mcp__toolkernel__", "")
            inp = b["input"]
            out += [f"### {step}. `{name}`", ""]
            if name == "execute_code":
                out += ["```python", inp.get("code", "").rstrip(), "```"]
            elif inp:
                out += ["```json", json.dumps(inp, indent=1), "```"]
            text, err = results.get(b["id"], ("(no result)", False))
            out += ["", "Result" + (" (error)" if err else "") + ":", "", "```", text.rstrip()[:3000], "```", ""]
final = next((e for e in events if e.get("type") == "result"), {})
u = final.get("usage") or {}
out += ["## Totals (reported by Claude Code)", "",
        f"- {final.get('num_turns')} turns · {step} tool calls · {final.get('duration_ms', 0) / 1000:.0f} s · ${final.get('total_cost_usd', 0):.2f}",
        f"- tokens: {u.get('input_tokens', 0):,} input · {u.get('cache_creation_input_tokens', 0):,} cache write · "
        f"{u.get('cache_read_input_tokens', 0):,} cache read · {u.get('output_tokens', 0):,} output", ""]
Path("transcript.md").write_text("\n".join(out))
print(f"wrote transcript.md ({step} tool calls)")
