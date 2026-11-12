"""Summaries and a Markdown report for a benchmark run."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from .harness import CONFIGS
from .tasks import TASKS

METRICS = ["input_tokens", "peak_context_tokens", "output_tokens", "api_calls", "tool_calls",
           "repeated_fetches", "bytes_from_tools", "chars_to_model", "wall_s", "cost_usd"]


def load(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta = json.loads((run_dir / "meta.json").read_text())
    rows = []
    p = run_dir / "results.jsonl"
    if p.exists():
        rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return meta, rows


def summarize(meta: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    excluded = [r for r in rows if r.get("excluded")]
    rows = [r for r in rows if not r.get("excluded")]  # provider load (rate limits) says nothing about a configuration
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r["model"], r["config"]), []).append(r)
    summary = []
    for (model, config), rs in groups.items():
        item: dict[str, Any] = {"model": model, "config": config, "label": CONFIGS[config].label if config in CONFIGS else config,
                                "runs": len(rs), "successes": sum(r["success"] for r in rs),
                                "errors": sum(1 for r in rs if r["error"])}
        item["success_rate"] = item["successes"] / len(rs)
        for m in METRICS:
            vals = [r[m] for r in rs]
            item[f"mean_{m}"] = statistics.fmean(vals) if vals else 0
            item[f"total_{m}"] = sum(vals)
        item["median_peak_context_tokens"] = statistics.median(r["peak_context_tokens"] for r in rs)
        summary.append(item)
    order = {c: i for i, c in enumerate(meta.get("configs", {}))}
    summary.sort(key=lambda s: (s["model"], order.get(s["config"], 99)))

    tasks: dict[str, dict[str, Any]] = {}
    for r in rows:
        t = tasks.setdefault(r["task"], {"task": r["task"], "category": r["category"], "cells": {}})
        cell = t["cells"].setdefault(f"{r['model']}|{r['config']}", {"runs": 0, "successes": 0, "input_tokens": 0,
                                                                     "errors": []})
        cell["runs"] += 1
        cell["successes"] += r["success"]
        cell["input_tokens"] += r["input_tokens"]
        if r["error"]:
            cell["errors"].append(r["error"][:160])
    titles = {t.id: t.title for t in TASKS}
    task_list = [{**v, "title": titles.get(k, k)} for k, v in tasks.items()]
    order_t = {t.id: i for i, t in enumerate(TASKS)}
    task_list.sort(key=lambda t: order_t.get(t["task"], 99))
    simulated = any(r.get("simulated") for r in rows)
    return {"meta": meta, "summary": summary, "tasks": task_list, "simulated": simulated, "excluded": len(excluded),
            "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4), "rows": len(rows)}


def _fmt(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e4:
        return f"{n / 1e3:.0f}k"
    return f"{n:,.0f}" if n >= 10 else f"{n:.1f}"


def markdown(s: dict[str, Any]) -> str:
    meta = s["meta"]
    lines = [f"# Benchmark {meta['run_id']}", ""]
    if s["simulated"]:
        lines += ["> **Plumbing check, not a benchmark.** This run used the scripted stand-in instead of a model: "
                  "answers come from the ground truth and token counts are estimated from text length. "
                  "Success rates here mean nothing; the run only proves the harness, tools and gateway work.", ""]
    lines += [f"- Models: {', '.join(meta['models'])} · effort `{meta['effort']}` · runtime driver `{meta['driver']}`",
              f"- Tasks: {len(meta['tasks'])} × {meta['repeats']} repeat(s) · status: {meta.get('status')}",
              f"- Direct MCP results capped at {meta['direct_cap_chars']:,} characters (about what Claude Code allows)",
              *([f"- {s['excluded']} task run(s) left out: the provider kept rate-limiting them even after waiting"]
                if s.get("excluded") else []),
              f"- Estimated cost: ${s['total_cost_usd']:.2f}", ""]
    for model in dict.fromkeys(x["model"] for x in s["summary"]):
        lines += [f"## {model}", "",
                  "| Configuration | Correct | Input tokens (mean) | Peak context (median) | Output tokens | Model calls | "
                  "Tool calls | Repeated fetches | Time (s) | Cost |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for x in [x for x in s["summary"] if x["model"] == model]:
            lines.append(
                f"| {x['label']} | {x['successes']}/{x['runs']} | {_fmt(x['mean_input_tokens'])} | "
                f"{_fmt(x['median_peak_context_tokens'])} | {_fmt(x['mean_output_tokens'])} | {x['mean_api_calls']:.1f} | "
                f"{x['mean_tool_calls']:.1f} | {x['total_repeated_fetches']} | {x['mean_wall_s']:.1f} | "
                f"${x['total_cost_usd']:.2f} |")
        lines.append("")
    cols = [f"{x['model']}|{x['config']}" for x in s["summary"]]
    lines += ["## Per task", "", "Each cell: correct runs / runs, and mean input tokens.", "",
              "| Task | " + " | ".join(c.split("|")[1] + (f" ({c.split('|')[0]})" if len(s["meta"]["models"]) > 1 else "")
                                     for c in cols) + " |",
              "|---|" + "---|" * len(cols)]
    for t in s["tasks"]:
        cells = []
        for c in cols:
            cell = t["cells"].get(c)
            if not cell:
                cells.append("–")
                continue
            mark = "✓" if cell["successes"] == cell["runs"] else ("✗" if cell["successes"] == 0 else "◐")
            cells.append(f"{mark} {cell['successes']}/{cell['runs']} · {_fmt(cell['input_tokens'] / cell['runs'])}")
        lines.append(f"| {t['title']} (`{t['task']}`) | " + " | ".join(cells) + " |")
    errors = [(t["task"], c, e) for t in s["tasks"] for c, cell in t["cells"].items() for e in cell["errors"]]
    if errors:
        lines += ["", "## Errors", ""] + [f"- `{t}` · {c.replace('|', ' · ')}: {e}" for t, c, e in errors[:40]]
    lines += ["", "## How to read this", "",
              "- **Input tokens** add up every model call in a task: tool definitions, the conversation, and every "
              "tool result the model had to read. It's the main driver of cost and latency.",
              "- **Peak context** is the largest single request. It shows how close a task came to the context limit.",
              "- **Repeated fetches** count identical tool calls made twice in one task: data fetched again because "
              "it wasn't kept.",
              "- Web tasks depend on live pages and search results, so they can vary between runs.",
              "- Costs are estimates from list prices, including prompt-cache discounts.", ""]
    return "\n".join(lines)


def write_report(run_dir: Path) -> Path:
    meta, rows = load(run_dir)
    s = summarize(meta, rows)
    (run_dir / "summary.json").write_text(json.dumps(s, indent=1))
    p = run_dir / "report.md"
    p.write_text(markdown(s))
    return p
