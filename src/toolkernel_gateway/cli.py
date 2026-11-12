"""Command line: toolkernel serve | generate | add | remove | connect | build-runtime."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import load_config, read_mcp_json, write_mcp_json


def _overrides(a: argparse.Namespace) -> dict[str, Any]:
    o: dict[str, Any] = {}
    if getattr(a, "host", None):
        o.setdefault("server", {})["host"] = a.host
    if getattr(a, "port", None):
        o.setdefault("server", {})["port"] = a.port
    if getattr(a, "mcp_json", None):
        o.setdefault("upstreams", {})["config_file"] = str(Path(a.mcp_json).resolve())
    if getattr(a, "data_dir", None):
        o["data_dir"] = str(Path(a.data_dir).resolve())
    if getattr(a, "driver", None):
        o.setdefault("runtime", {})["driver"] = a.driver
    if getattr(a, "state", None):
        o.setdefault("runtime", {})["state"] = a.state
    return o


def cmd_serve(a: argparse.Namespace) -> None:
    from .app import serve
    from .gateway import Gateway

    cfg = load_config(a.config, _overrides(a))
    c = cfg["server"]
    host = "localhost" if c["host"] in ("127.0.0.1", "0.0.0.0") else c["host"]
    print(f"\n  ToolKernel {__import__('toolkernel_gateway').__version__}")
    print(f"  ─ web UI      http://{host}:{c['port']}/")
    print(f"  ─ MCP         http://{host}:{c['port']}/mcp")
    print(f"  ─ servers     {cfg.mcp_json}")
    print(f"  ─ runtime     {cfg['runtime']['driver']} · {cfg['runtime']['state']}")
    if cfg["runtime"]["driver"] == "local":
        print("                (local processes are not isolated; use --driver docker for untrusted code)")
    print()
    asyncio.run(serve(Gateway(cfg)))


def cmd_generate(a: argparse.Namespace) -> None:
    from .gateway import Gateway

    async def run() -> None:
        cfg = load_config(a.config, _overrides(a))
        gw = Gateway(cfg)
        await gw.reload_mcp_json(force=True)
        deadline = asyncio.get_running_loop().time() + cfg["upstreams"]["upstream_wait_s"] + 15
        while asyncio.get_running_loop().time() < deadline:
            if all(u.status in ("ready", "error", "disabled") for u in gw.upstreams.upstreams.values()):
                break
            await asyncio.sleep(0.2)
        for up in gw.upstreams.upstreams.values():
            mark = {"ready": "✓", "error": "✗", "disabled": "–"}.get(up.status, "…")
            detail = f"{len(up.tools)} tools" if up.status == "ready" else (up.error or up.status)
            print(f"  {mark} {up.name}: {detail}")
        print(f"\n  wrappers: {gw.generator.root}")
        await gw.stop()

    asyncio.run(run())


def cmd_add(a: argparse.Namespace) -> None:
    cfg = load_config(a.config, _overrides(a))
    servers = read_mcp_json(cfg.mcp_json)
    entry: dict[str, Any] = {}
    if a.url:
        entry["url"] = a.url
        if a.header:
            entry["headers"] = dict(h.split("=", 1) for h in a.header)
    elif a.command:
        entry["command"] = a.command[0]
        entry["args"] = a.command[1:]
    else:
        sys.exit("give --url URL or --command CMD [ARGS...]")
    if a.env:
        entry["env"] = dict(e.split("=", 1) for e in a.env)
    servers[a.name] = entry
    write_mcp_json(cfg.mcp_json, servers)
    print(f"added {a.name} to {cfg.mcp_json}")


def cmd_remove(a: argparse.Namespace) -> None:
    cfg = load_config(a.config, _overrides(a))
    servers = read_mcp_json(cfg.mcp_json)
    if servers.pop(a.name, None) is None:
        sys.exit(f"{a.name} is not in {cfg.mcp_json}")
    write_mcp_json(cfg.mcp_json, servers)
    print(f"removed {a.name}")


def cmd_connect(a: argparse.Namespace) -> None:
    from .shim import run_shim

    asyncio.run(run_shim(a.url, a.key))


def cmd_build_runtime(a: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = root / "runtime" / "Dockerfile"
    if not dockerfile.exists():
        sys.exit(f"runtime/Dockerfile not found next to the package ({dockerfile})")
    with tempfile.TemporaryDirectory() as ctx:
        shutil.copy(dockerfile, Path(ctx) / "Dockerfile")
        cmd = ["docker", "build", "-t", a.tag, ctx]
        if a.extra:
            cmd[2:2] = ["--build-arg", "EXTRA_PACKAGES=" + " ".join(a.extra)]
        print(" ".join(cmd))
        raise SystemExit(subprocess.call(cmd))


def _bench_dirs(a: argparse.Namespace) -> tuple[Path, Path]:
    cfg = load_config(a.config, _overrides(a))
    data = Path(a.data).resolve() if getattr(a, "data", None) else cfg.data_dir / "bench-data"
    return data, cfg.data_dir / "bench"


def cmd_bench_data(a: argparse.Namespace) -> None:
    from .bench.data import generate

    data, _ = _bench_dirs(a)
    counts = generate(data, a.seed)
    print(f"  sample data written to {data}")
    print("  " + " · ".join(f"{v:,} {k}" for k, v in counts.items()))


def _has_anthropic_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()


def cmd_bench_run(a: argparse.Namespace) -> None:
    from .bench.harness import CONFIGS, PRICES, Plan, provider, run
    from .bench.report import write_report
    from .bench.tasks import get_tasks

    data, runs = _bench_dirs(a)
    if not (data / "sales" / "sales_2025.xlsx").exists():
        from .bench.data import generate

        print(f"  generating sample data in {data} …")
        generate(data)
    configs = [c.strip() for c in a.configs.split(",")]
    bad = [c for c in configs if c not in CONFIGS]
    if bad:
        sys.exit(f"unknown configuration(s): {', '.join(bad)}. Choose from: {', '.join(CONFIGS)}")
    if not a.models:
        if a.llm == "openai":
            sys.exit("  Pick the OpenAI model(s) to test with --models, e.g. --models <model-id>")
        a.models = "claude-opus-5-5"
    if a.llm in ("anthropic", "openai"):  # older spellings: each model now goes to its own provider
        a.llm = "api"
    models = [m.strip() for m in a.models.split(",")]
    if a.price:
        try:
            parts = [float(x) for x in a.price.split(",")]
            PRICES.update({m: (parts[0], parts[1], parts[2] if len(parts) > 2 else parts[0]) for m in models})
        except (ValueError, IndexError):
            sys.exit("  --price takes INPUT,OUTPUT[,CACHED_INPUT] in dollars per million tokens, e.g. --price 1.25,10,0.125")
    tasks = get_tasks(a.tasks)
    skipped = []
    has_search = bool(os.environ.get("TAVILY_API_KEY") or os.environ.get("BRAVE_API_KEY"))
    if not has_search:
        skipped += [t.id for t in tasks if "search" in t.needs]
    if a.no_web:
        skipped += [t.id for t in tasks if "web" in t.needs]
    tasks = [t for t in tasks if t.id not in skipped]
    if skipped:
        print(f"  skipping {', '.join(sorted(set(skipped)))}"
              + (" (set TAVILY_API_KEY or BRAVE_API_KEY for web search)" if not has_search else ""))
    if a.llm != "scripted":
        unknown = [m for m in models if m not in PRICES]
        if unknown:
            print(f"  note: no price on file for {', '.join(unknown)}; cost will show as $0"
                  + (" and --max-cost can't stop the run" if a.max_cost is not None else "")
                  + ". Pass --price INPUT,OUTPUT[,CACHED] ($ per million tokens) to fix that.")
    if a.llm == "api":
        for m in models:
            name, _, key_env = provider(m)
            ok = _has_anthropic_credentials() if name == "anthropic" else bool(os.environ.get(key_env))
            if not ok:
                sys.exit(f"  No credentials for {m}: set {key_env}, or use --llm scripted for a free plumbing check.")
    n = len(models) * len(configs) * len(tasks) * a.repeats
    print(f"  {n} task runs: {len(models)} model(s) × {len(configs)} configuration(s) × {len(tasks)} task(s) × {a.repeats}")
    if a.llm != "scripted" and not a.yes:
        apis = sorted({provider(m)[0] for m in models})
        print(f"  This calls the {', '.join(apis)} API and costs money. Cap it with --max-cost (dollars).")
        if input("  Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("  cancelled")
    plan = Plan(data_dir=data, out_dir=runs, configs=configs, models=models, tasks=tasks, repeats=a.repeats,
                effort=a.effort, llm=a.llm, driver=a.driver, direct_cap_chars=a.direct_cap,
                max_cost=a.max_cost, concurrency=a.concurrency)
    out = asyncio.run(run(plan))
    report = write_report(out)
    print(f"\n  report: {report}\n  also on the Benchmarks page of the web UI")


def cmd_bench_report(a: argparse.Namespace) -> None:
    from .bench.report import write_report

    _, runs = _bench_dirs(a)
    run_dir = Path(a.run) if a.run and Path(a.run).exists() else runs / (a.run or "")
    if not a.run:
        dirs = sorted(p for p in runs.iterdir() if (p / "meta.json").exists()) if runs.exists() else []
        if not dirs:
            sys.exit("no benchmark runs yet")
        run_dir = dirs[-1]
    print(write_report(run_dir).read_text())


def cmd_bench_list(a: argparse.Namespace) -> None:
    from .bench.tasks import TASKS

    for t in TASKS:
        extra = f"  (needs {', '.join(sorted(t.needs))})" if t.needs else ""
        print(f"  {t.id:20s} {t.category:13s} {t.title}{extra}")


def cmd_agent(a: argparse.Namespace) -> None:
    from .agent import Agent, chat

    if not _has_anthropic_credentials():
        sys.exit("  The agent calls Claude, so it needs Anthropic credentials. Set ANTHROPIC_API_KEY "
                 "(or run `ant auth login`) and try again.")
    url = a.url.rstrip("/")
    url = url if url.endswith("/mcp") else url + "/mcp"
    agent = Agent(gateway_url=url, api_key=a.key or os.environ.get("TOOLKERNEL_API_KEY"), model=a.model,
                  effort=a.effort, max_steps=a.max_steps)
    asyncio.run(chat(agent, " ".join(a.question) or None, Path(a.transcript) if a.transcript else None))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="toolkernel", description="Notebook-style code mode for MCP.")
    p.add_argument("-c", "--config", help="toolkernel.yaml (default: ./toolkernel.yaml if present)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--mcp-json", help="path to mcp.json (default ./mcp.json)")
        sp.add_argument("--data-dir", help="where wrappers, sessions and the database live (default ./.toolkernel)")

    s = sub.add_parser("serve", help="run the gateway")
    common(s)
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--driver", choices=["local", "docker"])
    s.add_argument("--state", choices=["stateful", "stateless"])
    s.set_defaults(fn=cmd_serve)

    g = sub.add_parser("generate", help="connect to upstreams, generate wrapper packages, exit")
    common(g)
    g.set_defaults(fn=cmd_generate)

    ad = sub.add_parser("add", help="add an upstream server to mcp.json")
    common(ad)
    ad.add_argument("name")
    ad.add_argument("--url")
    ad.add_argument("--header", action="append", metavar="K=V")
    ad.add_argument("--env", action="append", metavar="K=V")
    ad.add_argument("--command", nargs=argparse.REMAINDER, help="command and args (must be last)")
    ad.set_defaults(fn=cmd_add)

    rm = sub.add_parser("remove", help="remove an upstream server from mcp.json")
    common(rm)
    rm.add_argument("name")
    rm.set_defaults(fn=cmd_remove)

    cn = sub.add_parser("connect", help="stdio shim for clients that only speak stdio")
    cn.add_argument("url", help="gateway URL, e.g. http://localhost:7300")
    cn.add_argument("--key", help="API key, if the gateway requires one")
    cn.set_defaults(fn=cmd_connect)

    br = sub.add_parser("build-runtime", help="build the Docker runtime image")
    br.add_argument("--tag", default="toolkernel/runtime:1")
    br.add_argument("--extra", nargs="*", help="extra pip packages to bake in")
    br.set_defaults(fn=cmd_build_runtime)

    ag = sub.add_parser("agent", help="chat with Claude, using a running gateway's tools")
    ag.add_argument("question", nargs="*", help="ask once and exit; leave out for a chat")
    ag.add_argument("--url", default="http://localhost:7300", help="gateway URL")
    ag.add_argument("--key", help="gateway API key, if it requires one")
    ag.add_argument("--model", default="claude-opus-5-5")
    ag.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ag.add_argument("--max-steps", type=int, default=40)
    ag.add_argument("--transcript", help="write the session as Markdown to this file")
    ag.set_defaults(fn=cmd_agent)

    bench = sub.add_parser("bench", help="benchmark ToolKernel against direct MCP and other modes")
    bsub = bench.add_subparsers(dest="bench_cmd", required=True)
    bd = bsub.add_parser("data", help="generate the seeded sample data")
    common(bd)
    bd.add_argument("--data", help="where to write it (default: <data-dir>/bench-data)")
    bd.add_argument("--seed", type=int, default=7)
    bd.set_defaults(fn=cmd_bench_data)
    brun = bsub.add_parser("run", help="run tasks under each configuration and write a report")
    common(brun)
    brun.add_argument("--data", help="sample data folder (default: <data-dir>/bench-data, generated if missing)")
    brun.add_argument("--configs", default="direct,toolkernel,stateless,guard_off")
    brun.add_argument("--models", help="comma-separated, e.g. claude-opus-5-5,claude-haiku-5-5 "
                                       "(default claude-opus-5-5; required with --llm openai)")
    brun.add_argument("--tasks", default="all", help="task ids or categories, comma-separated (see `bench tasks`)")
    brun.add_argument("--repeats", type=int, default=1)
    brun.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    brun.add_argument("--llm", default="api", choices=["api", "anthropic", "openai", "scripted"],
                      help="api: each model through its provider (claude-* → ANTHROPIC_API_KEY, deepseek-* → "
                           "DEEPSEEK_API_KEY, others → OPENAI_API_KEY); scripted: a free plumbing check, no model calls")
    brun.add_argument("--price", help="INPUT,OUTPUT[,CACHED_INPUT] $ per million tokens for the models in this run "
                                      "(needed for cost figures on models without a built-in price)")
    brun.add_argument("--driver", default="local", choices=["local", "docker"])
    brun.add_argument("--direct-cap", type=int, default=100_000, help="max characters of one tool result in direct mode")
    brun.add_argument("--max-cost", type=float, help="stop once estimated spend reaches this many dollars")
    brun.add_argument("--concurrency", type=int, default=2)
    brun.add_argument("--no-web", action="store_true", help="skip tasks that need the internet")
    brun.add_argument("-y", "--yes", action="store_true", help="don't ask before spending")
    brun.set_defaults(fn=cmd_bench_run)
    brep = bsub.add_parser("report", help="print (and rewrite) the report of a run; latest by default")
    common(brep)
    brep.add_argument("run", nargs="?")
    brep.set_defaults(fn=cmd_bench_report)
    bl = bsub.add_parser("tasks", help="list the benchmark tasks")
    bl.set_defaults(fn=cmd_bench_list)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.cmd == "serve" else logging.WARNING,
                        format="  %(levelname).1s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    a.fn(a)


if __name__ == "__main__":
    main()
