"""Pre-execution static check (spec §10.6): syntax, imports of generated wrappers, unknown keyword arguments.

Runs in the gateway in a few milliseconds, so obvious mistakes come back without a runtime round trip.
"""

from __future__ import annotations

import ast
import difflib
from typing import Any


def _close(word: str, options: list[str]) -> str:
    matches = difflib.get_close_matches(word, options, n=3, cutoff=0.5)
    return f" Did you mean: {', '.join(matches)}?" if matches else ""


def check(code: str, index: dict[str, Any], known: dict[str, tuple[str, str]] | None = None) -> str | None:
    """Return a compact error message, or None if nothing obviously wrong was found.

    `known` maps names imported in earlier cells to (server, tool); new imports are added to it.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        line = (e.text or "").rstrip("\n")
        caret = " " * max(0, (e.offset or 1) - 1) + "^" if line else ""
        where = f"line {e.lineno}" if e.lineno else "unknown line"
        return f"SyntaxError: {e.msg} ({where})" + (f"\n    {line}\n    {caret}" if line else "")

    servers = index.get("servers", {})
    server_names = list(servers)
    problems: list[str] = []
    imported: dict[str, tuple[str, str]] = dict(known or {})  # local name → (server, tool)
    modules: dict[str, str] = {}  # local alias → server (for `from servers import github` / `import servers.github as gh`)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (node.module == "servers" or node.module.startswith("servers.")):
            parts = node.module.split(".")
            if len(parts) == 1:
                for alias in node.names:
                    if alias.name != "*" and alias.name not in servers:
                        problems.append(f"No server named {alias.name!r}.{_close(alias.name, server_names)}"
                                        f" Connected: {', '.join(server_names) or 'none'}.")
                    elif alias.name in servers:
                        modules[alias.asname or alias.name] = alias.name
                continue
            server = parts[1]
            if server not in servers:
                problems.append(f"No server named {server!r}.{_close(server, server_names)}"
                                f" Connected: {', '.join(server_names) or 'none'}.")
                continue
            tools = servers[server]["tools"]
            if len(parts) == 3 and parts[2] not in tools and parts[2] != "_types":
                problems.append(f"servers.{server} has no tool {parts[2]!r}.{_close(parts[2], list(tools))}")
                continue
            for alias in node.names:
                if alias.name == "*" or (len(parts) == 2 and alias.name in ("SCHEMA_HASH", "_types")):
                    continue
                if len(parts) == 2 and alias.name not in tools:
                    problems.append(f"servers.{server} has no tool {alias.name!r}.{_close(alias.name, list(tools))}"
                                    f" Search with search_tools(\"{alias.name.replace('_', ' ')}\", server=\"{server}\").")
                elif alias.name in tools:
                    imported[alias.asname or alias.name] = (server, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "servers" and len(parts) >= 2:
                    if parts[1] not in servers:
                        problems.append(f"No server named {parts[1]!r}.{_close(parts[1], server_names)}")
                    elif alias.asname:
                        modules[alias.asname] = parts[1]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target: tuple[str, str] | None = None
        f = node.func
        if isinstance(f, ast.Name) and f.id in imported:
            target = imported[f.id]
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in modules:
            server = modules[f.value.id]
            if f.attr not in servers[server]["tools"]:
                problems.append(f"servers.{server} has no tool {f.attr!r}.{_close(f.attr, list(servers[server]['tools']))}")
                continue
            target = (server, f.attr)
        if not target:
            continue
        meta = servers[target[0]]["tools"][target[1]]
        params = list(meta.get("params", {}))
        accepts_any = meta["signature"].endswith("**arguments)")
        for kw in node.keywords:
            if kw.arg is None or accepts_any:
                continue
            if kw.arg not in params:
                problems.append(f"{target[0]}.{target[1]}() has no argument {kw.arg!r}.{_close(kw.arg, params)}"
                                f" Signature: {meta['signature']}")
        if node.args and not accepts_any:
            problems.append(f"{target[0]}.{target[1]}() takes keyword arguments only: {meta['signature']}")
    if known is not None and not problems:
        known.update({k: v for k, v in imported.items() if v[0] in servers and v[1] in servers[v[0]]["tools"]})
    if problems:
        unique = list(dict.fromkeys(problems))
        return "Static check failed (code was not run):\n" + "\n".join(f"- {p}" for p in unique[:8])
    return None
