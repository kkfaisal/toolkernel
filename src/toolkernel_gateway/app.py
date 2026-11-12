"""HTTP surfaces: MCP frontend (/mcp), REST for non-MCP agents (/v1), web UI + admin API (/, /api), and the bridge."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import datetime as dt
import hmac
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

import mcp_types as types
import uvicorn
from mcp.server import Server
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__
from .gateway import BridgeError, Gateway

UI_DIR = Path(__file__).parent / "ui"


# ---------------------------------------------------------------- MCP frontend


def _tool(t: dict[str, Any]) -> types.Tool:
    ann = t.get("annotations") or {}
    return types.Tool(
        name=t["name"],
        description=t["description"],
        input_schema=t["inputSchema"],
        annotations=types.ToolAnnotations(read_only_hint=ann.get("readOnlyHint")) if ann else None,
    )


def build_mcp_server(gw: Gateway) -> Server[Any]:
    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[_tool(t) for t in gw.meta_tools()])

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        req = ctx.request
        sid = None
        if req is not None and hasattr(req, "headers"):
            sid = req.headers.get("mcp-session-id")
        sid = sid or f"conn-{id(ctx.session):x}"
        client = None
        with contextlib.suppress(Exception):
            cp = ctx.session.client_params
            if cp and cp.client_info:
                client = f"{cp.client_info.name} {cp.client_info.version or ''}".strip()
        text, is_error = await gw.dispatch(sid, params.name, params.arguments or {}, client)
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)

    return Server("toolkernel", version=__version__, instructions=gw.instructions(),
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


# ---------------------------------------------------------------- auth


class AuthMiddleware:
    """When an API key is configured, /mcp, /v1 and /api require `Authorization: Bearer <key>`."""

    def __init__(self, app: Any, key: str | None):
        self.app, self.key = app, key

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self.key and scope["type"] == "http" and scope["path"].startswith(("/mcp", "/v1", "/api")):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            supplied = headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not supplied and scope["path"] == "/api/events":
                qs = dict(p.split("=", 1) for p in scope.get("query_string", b"").decode().split("&") if "=" in p)
                supplied = qs.get("key", "")
            if not hmac.compare_digest(supplied.encode(), self.key.encode()):
                resp = JSONResponse({"error": "unauthorized", "hint": "send Authorization: Bearer <api key>"}, 401)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------- REST (/v1)


def rest_routes(gw: Gateway) -> list[Route]:
    async def tools(request: Request) -> Response:
        fmt = request.query_params.get("format", "json-schema")
        defs = gw.meta_tools()
        if fmt == "openai":
            out: Any = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                          "parameters": t["inputSchema"]}} for t in defs]
        elif fmt == "anthropic":
            out = [{"name": t["name"], "description": t["description"], "input_schema": t["inputSchema"]} for t in defs]
        else:
            out = [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]} for t in defs]
        return JSONResponse(out)

    async def instructions(request: Request) -> Response:
        return PlainTextResponse(gw.instructions())

    async def new_session(request: Request) -> Response:
        import secrets

        body = await _json(request)
        sid = "rest-" + secrets.token_hex(8)
        gw.session(sid, body.get("client") or "REST client")
        return JSONResponse({"session_id": sid})

    async def call(request: Request) -> Response:
        sid = request.path_params["sid"]
        body = await _json(request)
        if not body.get("tool"):
            return JSONResponse({"error": "body needs {\"tool\": ..., \"arguments\": {...}}"}, 400)
        text, is_error = await gw.dispatch(sid, body["tool"], body.get("arguments") or {})
        return JSONResponse({"content": text, "is_error": is_error})

    return [
        Route("/v1/tools", tools),
        Route("/v1/instructions", instructions),
        Route("/v1/sessions", new_session, methods=["POST"]),
        Route("/v1/sessions/{sid}/call", call, methods=["POST"]),
    ]


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


# ---------------------------------------------------------------- admin API (/api)


def api_routes(gw: Gateway) -> list[Route]:
    def servers_view() -> list[dict[str, Any]]:
        index = gw.generator.index["servers"]
        out = []
        for up in gw.upstreams.upstreams.values():
            py = next((p for p, e in index.items() if e["name"] == up.name), None)
            item = up.summary()
            item["py"] = py
            item["warnings"] = index[py].get("warnings", []) if py else []
            out.append(item)
        return out

    async def overview(request: Request) -> Response:
        midnight = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
        ups = list(gw.upstreams.upstreams.values())
        return JSONResponse({
            "version": __version__,
            "started": gw.started,
            "driver": gw.cfg["runtime"]["driver"],
            "state_mode": gw.cfg["runtime"]["state"],
            "servers": {"total": len(ups), "ready": sum(u.status == "ready" for u in ups),
                        "error": sum(u.status == "error" for u in ups),
                        "tools": sum(len(e["tools"]) for e in gw.generator.index["servers"].values())},
            "sessions_active": sum(1 for s in gw.sessions.values() if s.runtime),
            "today": gw.store.totals(midnight),
            "all": gw.store.totals(0),
            "daily": gw.store.daily(14),
            "pending_approvals": sum(a["status"] == "pending" for a in gw.approvals.values()),
            "chars_per_token": gw.cfg["output"]["chars_per_token"],
            "budget": gw.cfg["output"]["budget_chars"],
            "connect": {"mcp_url": f"http://{_display_host(gw)}:{gw.cfg['server']['port']}/mcp",
                        "has_key": bool(gw.cfg.api_key)},
            "recent": gw.store.runs(limit=6),
        })

    async def servers(request: Request) -> Response:
        if request.method == "POST":
            body = await _json(request)
            try:
                if "mcpServers" in body:
                    for name, cfg in body["mcpServers"].items():
                        gw.add_server(name, cfg)
                else:
                    gw.add_server(str(body.get("name", "")).strip(), body.get("config") or {})
            except ValueError as e:
                return JSONResponse({"error": str(e)}, 400)
            await gw.reload_mcp_json(force=True)
            return JSONResponse({"ok": True})
        return JSONResponse(servers_view())

    async def server_item(request: Request) -> Response:
        name = request.path_params["name"]
        if request.method == "DELETE":
            gw.remove_server(name)
            await gw.reload_mcp_json(force=True)
            return JSONResponse({"ok": True})
        return JSONResponse(next((s for s in servers_view() if s["name"] == name), None) or {"error": "not found"})

    async def server_action(request: Request) -> Response:
        name, action = request.path_params["name"], request.path_params["action"]
        if name not in gw.upstreams.upstreams:
            return JSONResponse({"error": "not found"}, 404)
        if action == "reconnect":
            await gw.upstreams.restart(name)
        elif action in ("enable", "disable"):
            gw.set_server_enabled(name, action == "enable")
            await gw.reload_mcp_json(force=True)
        elif action == "log":
            p = gw.upstreams.upstreams[name].log_path
            text = p.read_text(errors="replace")[-20000:] if p.exists() else ""
            return PlainTextResponse(text)
        else:
            return JSONResponse({"error": "unknown action"}, 400)
        return JSONResponse({"ok": True})

    async def tools(request: Request) -> Response:
        q = request.query_params.get("q", "").strip()
        index = gw.generator.index["servers"]
        if q:
            hits = gw.search_index.search(q, None, 30)
            return JSONResponse([{"server": s, "tool": t, **_tool_brief(index[s]["tools"][t])} for s, t, _ in hits])
        return JSONResponse([
            {"server": spy, "name": e["name"], "description": e["description"],
             "tools": [{"tool": tpy, **_tool_brief(m)} for tpy, m in e["tools"].items()]}
            for spy, e in index.items()
        ])

    async def tool_detail(request: Request) -> Response:
        spy, tpy = request.path_params["server"], request.path_params["tool"]
        entry = gw.generator.index["servers"].get(spy)
        if not entry or tpy not in entry["tools"]:
            return JSONResponse({"error": "not found"}, 404)
        meta = entry["tools"][tpy]
        up = gw.upstreams.get(entry["name"])
        raw = next((t for t in (up.tools if up else []) if t["name"] == meta["name"]), {})
        return JSONResponse({"server": spy, "tool": tpy, "meta": meta, "source": gw.generator.tool_source(spy, tpy),
                             "input_schema": raw.get("inputSchema"), "output_schema": raw.get("outputSchema"),
                             "types": {t: entry.get("types", {}).get(t) for t in meta.get("used_types", [])}})

    async def sessions(request: Request) -> Response:
        stored = {s["id"]: s for s in gw.store.sessions(100)}
        out = []
        for sid, row in stored.items():
            live = gw.sessions.get(sid)
            out.append({**row, **(gw.session_view(live) if live else {"runtime": None}), "live": bool(live)})
        return JSONResponse(out)

    async def session_detail(request: Request) -> Response:
        sid = request.path_params["sid"]
        live = gw.sessions.get(sid)
        return JSONResponse({"session": gw.session_view(live) if live else {"id": sid},
                             "runs": gw.store.runs(sid, limit=200)})

    async def runs(request: Request) -> Response:
        qp = request.query_params
        return JSONResponse(gw.store.runs(qp.get("session"), int(qp.get("limit", 50)),
                                          int(qp["before"]) if qp.get("before") else None))

    async def run_detail(request: Request) -> Response:
        run = gw.store.run(int(request.path_params["rid"]))
        return JSONResponse(run or {"error": "not found"}, 200 if run else 404)

    async def approvals(request: Request) -> Response:
        items = sorted(gw.approvals.values(), key=lambda a: -a["created"])
        return JSONResponse([a for a in items if a["status"] == "pending"] + [a for a in items if a["status"] != "pending"][:20])

    async def decide(request: Request) -> Response:
        body = await _json(request)
        ok = gw.decide(request.path_params["aid"], bool(body.get("approve")))
        return JSONResponse({"ok": ok}, 200 if ok else 409)

    async def playground(request: Request) -> Response:
        body = await _json(request)
        sid = body.get("session") or "playground"
        tool = body.get("tool") or "execute_code"
        args = body.get("arguments") or {"code": body.get("code", "")}
        text, is_error = await gw.dispatch(sid, tool, args, "Playground")
        return JSONResponse({"text": text, "is_error": is_error, "session": sid})

    async def settings(request: Request) -> Response:
        exe = shutil.which("toolkernel") or str(Path(sys.argv[0]).resolve())
        return JSONResponse({"config": gw.cfg.public(), "config_file": str(gw.cfg.path) if gw.cfg.path else None,
                             "cli": exe,
                             "mcp_json": str(gw.cfg.mcp_json), "data_dir": str(gw.data_dir),
                             "instructions": gw.instructions(), "meta_tools": gw.meta_tools()})

    async def events(request: Request) -> Response:
        q = gw.events.subscribe()

        async def stream():
            try:
                yield "retry: 3000\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                gw.events.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    bench_root = gw.data_dir / "bench"

    def _run_dir(run_id: str) -> Path | None:
        d = (bench_root / run_id).resolve()
        return d if d.parent == bench_root.resolve() and (d / "meta.json").exists() else None

    async def bench_list(request: Request) -> Response:
        from .bench.report import load, summarize

        out = []
        if bench_root.exists():
            for d in sorted((p for p in bench_root.iterdir() if (p / "meta.json").exists()), reverse=True)[:100]:
                try:
                    s = summarize(*load(d))
                except (OSError, json.JSONDecodeError, KeyError):
                    continue
                out.append({"run_id": d.name, "meta": s["meta"], "simulated": s["simulated"], "rows": s["rows"],
                            "total_cost_usd": s["total_cost_usd"],
                            "headline": [{k: x[k] for k in ("model", "config", "label", "successes", "runs",
                                                            "mean_input_tokens")} for x in s["summary"]]})
        return JSONResponse({"runs": out, "commands": {
            "check": "toolkernel bench run --llm scripted",
            "run": "toolkernel bench run --max-cost 20",
            "models": "toolkernel bench run --models claude-opus-5-5,claude-haiku-5-5 --max-cost 40"},
            "has_brave_key": bool(os.environ.get("BRAVE_API_KEY"))})

    async def bench_detail(request: Request) -> Response:
        from .bench.report import load, summarize

        d = _run_dir(request.path_params["run"])
        if not d:
            return JSONResponse({"error": "not found"}, 404)
        s = summarize(*load(d))
        s["transcripts"] = sorted(p.name for p in (d / "transcripts").glob("*.json")) if (d / "transcripts").exists() else []
        return JSONResponse(s)

    async def bench_transcript(request: Request) -> Response:
        d = _run_dir(request.path_params["run"])
        name = request.path_params["name"]
        p = (d / "transcripts" / name) if d else None
        if not p or "/" in name or not p.exists():
            return JSONResponse({"error": "not found"}, 404)
        return JSONResponse(json.loads(p.read_text()))

    return [
        Route("/api/bench", bench_list),
        Route("/api/bench/{run}", bench_detail),
        Route("/api/bench/{run}/transcripts/{name}", bench_transcript),
        Route("/api/overview", overview),
        Route("/api/servers", servers, methods=["GET", "POST"]),
        Route("/api/servers/{name}", server_item, methods=["GET", "DELETE"]),
        Route("/api/servers/{name}/{action}", server_action, methods=["GET", "POST"]),
        Route("/api/tools", tools),
        Route("/api/tools/{server}/{tool}", tool_detail),
        Route("/api/sessions", sessions),
        Route("/api/sessions/{sid}", session_detail),
        Route("/api/runs", runs),
        Route("/api/runs/{rid:int}", run_detail),
        Route("/api/approvals", approvals),
        Route("/api/approvals/{aid}", decide, methods=["POST"]),
        Route("/api/playground", playground, methods=["POST"]),
        Route("/api/settings", settings),
        Route("/api/events", events),
    ]


def _tool_brief(m: dict[str, Any]) -> dict[str, Any]:
    return {"name": m["name"], "summary": m["summary"], "signature": m["signature"], "annotations": m["annotations"]}


def _display_host(gw: Gateway) -> str:
    h = gw.cfg["server"]["host"]
    return "localhost" if h in ("127.0.0.1", "0.0.0.0", "::") else h


def ui_routes() -> list[Any]:
    def asset_version() -> str:
        h = hashlib.sha256()
        for name in ("style.css", "app.js"):
            h.update((UI_DIR / name).read_bytes())
        return h.hexdigest()[:10]

    async def index(request: Request) -> Response:
        v = asset_version()  # cheap; makes browsers fetch new assets after an upgrade
        page = (UI_DIR / "index.html").read_text()
        page = page.replace("/ui/style.css", f"/ui/style.css?v={v}").replace("/ui/app.js", f"/ui/app.js?v={v}")
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    return [Route("/", index), Mount("/ui", StaticFiles(directory=UI_DIR), name="ui")]


def build_app(gw: Gateway) -> Any:
    server = build_mcp_server(gw)
    host = gw.cfg["server"]["host"]
    app = server.streamable_http_app(
        host=host,
        custom_starlette_routes=[*rest_routes(gw), *api_routes(gw), *ui_routes()],
    )
    return AuthMiddleware(app, gw.cfg.api_key)


# ---------------------------------------------------------------- bridge (runtime → gateway)


def build_bridge_app(gw: Gateway) -> Starlette:
    async def authed(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        return gw.session_for_token(token)

    def err(e: BridgeError, status: int = 200) -> JSONResponse:
        return JSONResponse({"ok": False, "error": {"type": e.kind, "message": e.message, **e.extra}}, status)

    async def call(request: Request) -> Response:
        s = await authed(request)
        if s is None:
            return err(BridgeError("ToolDenied", "invalid bridge token"), 401)
        body = await _json(request)
        try:
            result = await gw.bridge_call(s, str(body.get("server")), str(body.get("tool")),
                                          body.get("arguments") or {}, str(body.get("caller") or "code"))
            return JSONResponse({"ok": True, "result": result})
        except BridgeError as e:
            return err(e)

    async def stash(request: Request) -> Response:
        s = await authed(request)
        if s is None:
            return err(BridgeError("ToolDenied", "invalid bridge token"), 401)
        body = await _json(request)
        try:
            return JSONResponse({"ok": True, "result": gw.stash_op(s, body.get("op", ""), body.get("key"), body.get("value"))})
        except BridgeError as e:
            return err(e)

    async def search(request: Request) -> Response:
        if await authed(request) is None:
            return err(BridgeError("ToolDenied", "invalid bridge token"), 401)
        body = await _json(request)
        text = gw.bridge_search(str(body.get("query", "")), body.get("server"), body.get("detail") or "summary",
                                int(body.get("limit") or 10))
        return JSONResponse({"ok": True, "result": {"text": text}})

    return Starlette(routes=[
        Route("/bridge/call", call, methods=["POST"]),
        Route("/bridge/stash", stash, methods=["POST"]),
        Route("/bridge/search", search, methods=["POST"]),
    ])


# ---------------------------------------------------------------- serve


def _bind(host: str, port: int, what: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        sock.close()
        raise SystemExit(f"\n  Can't use port {port} for the {what} ({e.strerror}).\n"
                         f"  Is another ToolKernel running? Stop it, or pick another port in toolkernel.yaml.\n")
    sock.listen(2048)
    sock.set_inheritable(True)
    return sock


async def serve(gw: Gateway) -> None:
    c = gw.cfg["server"]
    main_sock = _bind(c["host"], c["port"], "web UI and MCP endpoint")
    bridge_sock = _bind(c.get("bridge_host", "127.0.0.1"), c["bridge_port"], "runtime bridge")
    # Live-update streams never end on their own, so shutdown must not wait for them indefinitely.
    main = uvicorn.Server(uvicorn.Config(build_app(gw), log_level="warning", timeout_graceful_shutdown=2))
    bridge = uvicorn.Server(uvicorn.Config(build_bridge_app(gw), log_level="warning", lifespan="off",
                                           timeout_graceful_shutdown=2))
    bridge.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]  # the main server owns signals
    gw.request_shutdown = lambda: setattr(main, "should_exit", True)  # type: ignore[attr-defined]
    try:
        await gw.start()
        bridge_task = asyncio.create_task(bridge.serve(sockets=[bridge_sock]))
        try:
            await main.serve(sockets=[main_sock])
        finally:
            bridge.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(bridge_task, 5)
    finally:
        await gw.stop()
