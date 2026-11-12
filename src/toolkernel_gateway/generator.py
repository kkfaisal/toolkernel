"""Wrapper generator (spec §9): MCP tool schemas → a typed, readable Python package.

Deterministic: identical schemas produce byte-identical files. No LLM involved.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .naming import class_name, dedupe, param_name, py_name

RUNTIME_LIB = Path(__file__).parent / "runtime_lib" / "toolkernel"
GENERATOR_VERSION = 3  # bump when output format changes, so existing packages are regenerated
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ANNOTATION_LABELS = {
    "readOnlyHint": "read-only",
    "destructiveHint": "destructive",
    "idempotentHint": "idempotent",
    "openWorldHint": "open-world",
}


def schema_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:8]


def first_sentence(text: str | None, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _doc_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


# ---------------------------------------------------------------- JSON Schema → Python types


@dataclass
class TypeBuilder:
    max_depth: int = 4
    defs: dict[str, str] = field(default_factory=dict)  # class name → source
    by_signature: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    _resolving: dict[str, str] = field(default_factory=dict)

    def annotate(self, schema: Any, hint: str, root: dict[str, Any], depth: int = 0) -> str:
        if schema is True or schema is None or schema == {}:
            return "Any"
        if schema is False:
            return "None"
        if not isinstance(schema, dict):
            return "Any"
        if "$ref" in schema:
            return self._ref(schema["$ref"], root, depth)
        if "const" in schema:
            return self._literal([schema["const"]]) or "Any"
        if "enum" in schema:
            lit = self._literal(schema["enum"])
            if lit:
                return lit
        for key in ("anyOf", "oneOf"):
            if key in schema:
                return self._union([self.annotate(s, hint, root, depth) for s in schema[key]])
        if "allOf" in schema:
            parts = schema["allOf"]
            if len(parts) == 1:
                return self.annotate(parts[0], hint, root, depth)
            merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
            for p in parts:
                p = self._deref(p, root)
                if not isinstance(p, dict) or p.get("type", "object") != "object":
                    return "Any"
                merged["properties"].update(p.get("properties", {}))
                merged["required"] += p.get("required", [])
            return self.annotate(merged, hint, root, depth)
        t = schema.get("type")
        if isinstance(t, list):
            return self._union([self.annotate({**schema, "type": x}, hint, root, depth) for x in t])
        if t == "string":
            return "str"
        if t == "integer":
            return "int"
        if t == "number":
            return "float"
        if t == "boolean":
            return "bool"
        if t == "null":
            return "None"
        if t == "array" or "items" in schema:
            items = schema.get("items")
            if isinstance(items, list):  # tuple form
                return "list[" + self._union([self.annotate(i, hint + "Item", root, depth + 1) for i in items]) + "]"
            return f"list[{self.annotate(items or {}, hint + 'Item', root, depth + 1)}]"
        if t == "object" or "properties" in schema:
            props = schema.get("properties")
            if props and depth < self.max_depth:
                return self._typeddict(schema, hint, root, depth)
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict) and extra:
                return f"dict[str, {self.annotate(extra, hint + 'Value', root, depth + 1)}]"
            return "dict[str, Any]"
        if t is None:
            return "Any"
        self.warnings.append(f"unsupported schema type {t!r} at {hint}; using Any")
        return "Any"

    def _deref(self, schema: Any, root: dict[str, Any]) -> Any:
        if isinstance(schema, dict) and "$ref" in schema:
            return self._lookup(schema["$ref"], root)
        return schema

    @staticmethod
    def _lookup(ref: str, root: dict[str, Any]) -> Any:
        if not ref.startswith("#/"):
            return {}
        node: Any = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return {}
            node = node[part]
        return node

    def _ref(self, ref: str, root: dict[str, Any], depth: int) -> str:
        name = class_name(ref.rsplit("/", 1)[-1])
        if ref in self._resolving:
            return f'"{self._resolving[ref]}"'
        target = self._lookup(ref, root)
        self._resolving[ref] = name
        try:
            if isinstance(target, dict) and not target.get("title"):
                target = {**target, "title": name}
            return self.annotate(target, name, root, depth)
        finally:
            del self._resolving[ref]

    @staticmethod
    def _literal(values: list[Any]) -> str | None:
        if not values or not all(isinstance(v, (str, int, bool)) or v is None for v in values):
            return None
        non_null = [v for v in values if v is not None]
        parts = []
        if non_null:
            parts.append("Literal[" + ", ".join(json.dumps(v) if isinstance(v, str) else repr(v) for v in non_null) + "]")
        if len(non_null) != len(values):
            parts.append("None")
        return " | ".join(parts)

    @staticmethod
    def _union(types_: list[str]) -> str:
        seen: list[str] = []
        for t in types_:
            for piece in _split_union(t):
                if piece not in seen:
                    seen.append(piece)
        if "Any" in seen:
            return "Any"
        if "None" in seen:  # None last reads naturally
            seen = [s for s in seen if s != "None"] + ["None"]
        return " | ".join(seen) or "Any"

    def _typeddict(self, schema: dict[str, Any], hint: str, root: dict[str, Any], depth: int) -> str:
        title = schema.get("title")
        base = class_name(title) if title and not re.search(r"(Arguments|Output)$", title) else class_name(hint)
        required = set(schema.get("required", []))
        fields: list[tuple[str, str, str]] = []
        all_required = set(schema["properties"]) <= required
        for key, sub in schema["properties"].items():
            ann = self.annotate(sub, base + class_name(key), root, depth + 1)
            if key in required and not all_required:
                ann = f"Required[{ann}]"
            desc = first_sentence((sub or {}).get("description") if isinstance(sub, dict) else "", 90)
            fields.append((key, ann, desc))
        signature = json.dumps([fields, all_required])
        if signature in self.by_signature:
            return self.by_signature[signature]
        name = base
        n = 2
        while name in self.defs:
            name = f"{base}{n}"
            n += 1
        doc = first_sentence(schema.get("description"), 100)
        if all(_IDENT.match(k) and not _is_keyword(k) for k, _, _ in fields):
            lines = [f"class {name}(TypedDict{'' if all_required else ', total=False'}):"]
            if doc:
                lines.append(f'    """{_doc_escape(doc)}"""')
            for key, ann, desc in fields:
                lines.append(f"    {key}: {ann}" + (f"  # {desc}" if desc else ""))
            if not fields:
                lines.append("    pass")
        else:
            body = ", ".join(f"{json.dumps(k)}: {json.dumps(a)}" for k, a, _ in fields)
            total = "" if all_required else ", total=False"
            lines = [f"{name} = TypedDict({json.dumps(name)}, {{{body}}}{total})"]
        self.defs[name] = "\n".join(lines)
        self.by_signature[signature] = name
        return name


def _is_keyword(s: str) -> bool:
    import keyword

    return keyword.iskeyword(s)


def _split_union(t: str) -> list[str]:
    """Split 'A | list[B | C] | None' on top-level pipes only."""
    parts, depth, cur = [], 0, ""
    for ch in t:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "|" and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return [p for p in parts if p]


# ---------------------------------------------------------------- per-tool rendering


@dataclass
class ToolSpec:
    server: str  # original server name
    server_py: str
    name: str  # original tool name
    py: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any]
    hash: str = ""


def _is_wrapped_output(schema: dict[str, Any] | None) -> bool:
    """MCPServer/FastMCP wrap non-object returns as {"result": ...}; unwrap them for a natural Python type."""
    if not schema:
        return False
    props = schema.get("properties") or {}
    return list(props) == ["result"] and schema.get("required") == ["result"] and str(schema.get("title", "")).endswith("Output")


def render_tool(spec: ToolSpec, tb: TypeBuilder) -> tuple[str, dict[str, Any]]:
    schema = spec.input_schema or {}
    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    originals = list(props)
    py_params = dedupe([param_name(p) for p in originals])
    hint = class_name(spec.py)

    params_meta: dict[str, Any] = {}
    sig_req, sig_opt, arg_doc, call_items = [], [], [], []
    for orig, pyp in zip(originals, py_params):
        sub = props[orig] if isinstance(props[orig], dict) else {}
        ann = tb.annotate(sub, hint + class_name(orig), schema)
        is_req = orig in required
        desc = " ".join((sub.get("description") or "").split())
        if sub.get("default") is not None:
            desc = (desc + " " if desc else "") + f"Default: {json.dumps(sub['default'])}."
        if is_req:
            sig_req.append(f"{pyp}: {ann}")
        else:
            opt = ann if ann == "Any" or "None" in _split_union(ann) else f"{ann} | None"
            sig_opt.append(f"{pyp}: {opt} = None")
        if desc:
            arg_doc.append((pyp, desc))
        call_items.append(f"{json.dumps(orig)}: {pyp}")
        params_meta[pyp] = {"name": orig, "required": is_req, "type": ann, "description": desc}

    extra_kwargs = not props and bool(schema.get("additionalProperties"))
    sig_parts = sig_req + sig_opt
    if extra_kwargs:
        sig_parts.append("**arguments: Any")

    unwrap = _is_wrapped_output(spec.output_schema)
    if spec.output_schema:
        out_schema = spec.output_schema["properties"]["result"] if unwrap else spec.output_schema
        ret = tb.annotate(out_schema, hint + "Result", spec.output_schema)
        ret_doc = f"{ret} (validated against the tool's output schema)"
    else:
        ret = "Any"
        ret_doc = "Unstructured: parsed JSON when the tool returns JSON text, otherwise str."

    doc_lines = [_doc_escape(spec.description.strip()) or f"Call {spec.server}.{spec.name}."]
    if arg_doc:
        doc_lines += ["", "Args:"]
        for pyp, desc in arg_doc:
            wrapped = textwrap.wrap(_doc_escape(desc), 76 - len(pyp))
            doc_lines.append(f"    {pyp}: {wrapped[0]}")
            doc_lines += [" " * (6 + len(pyp)) + w for w in wrapped[1:]]
    doc_lines += ["", "Returns:", f"    {ret_doc}"]
    labels = [label for key, label in _ANNOTATION_LABELS.items() if spec.annotations.get(key)]
    if labels:
        doc_lines += ["", "Annotations: " + ", ".join(labels)]
    doc = "\n".join(("    " + line) if line else "" for line in doc_lines)

    if extra_kwargs and len(sig_parts) == 1:
        sig = f"def {spec.py}(**arguments: Any) -> {ret}:"
    elif sig_parts:
        sig = f"def {spec.py}(\n    *,\n    " + ",\n    ".join(sig_parts) + f",\n) -> {ret}:"
    else:
        sig = f"def {spec.py}() -> {ret}:"
    payload = "{" + ", ".join(call_items) + "}"
    if extra_kwargs:
        payload = "arguments" if not call_items else payload + " | arguments"

    used_types = sorted(n for n in tb.defs if re.search(rf"\b{n}\b", sig))
    imports = ["from __future__ import annotations", "", "from typing import Any, Literal  # noqa: F401", "",
               "from toolkernel._bridge import call_tool"]
    if used_types:
        imports.append(f"from ._types import {', '.join(used_types)}")
    header = f"# generated by toolkernel · server={spec.server} · tool={spec.name} · schema={spec.hash}"
    source = "\n".join([header, *imports, "", "", sig, f'    """{doc.lstrip()}\n    """',
                        f"    return call_tool({json.dumps(spec.server)}, {json.dumps(spec.name)}, {payload})", ""])

    shown = [p if params_meta[p]["required"] else p + "?" for p in py_params]
    if extra_kwargs:
        shown.append("**arguments")
    meta = {
        "name": spec.name,
        "summary": first_sentence(spec.description),
        "description": spec.description,
        "signature": f"{spec.py}({', '.join(shown)})",
        "returns": ret,
        "params": params_meta,
        "annotations": {k: v for k, v in spec.annotations.items() if v is not None},
        "unwrap_result": unwrap,
        "has_output_schema": bool(spec.output_schema),
        "used_types": used_types,
        "hash": spec.hash,
    }
    return source, meta


# ---------------------------------------------------------------- packages


@dataclass
class ServerInput:
    name: str
    title: str | None
    description: str | None
    instructions: str | None
    tools: list[dict[str, Any]]  # name, description, inputSchema, outputSchema, annotations


def build_server(server: ServerInput, server_py: str, max_depth: int = 4) -> tuple[dict[str, str], dict[str, Any]]:
    """Returns {relative file path: content} and the index entry for one server."""
    tb = TypeBuilder(max_depth=max_depth)
    tools = sorted(server.tools, key=lambda t: t["name"])  # stable regardless of upstream order
    py_names = dedupe([py_name(t["name"]) for t in tools])
    files: dict[str, str] = {}
    tools_meta: dict[str, Any] = {}
    for t, pyt in zip(tools, py_names):
        spec = ToolSpec(
            server=server.name,
            server_py=server_py,
            name=t["name"],
            py=pyt,
            description=t.get("description") or "",
            input_schema=t.get("inputSchema") or {},
            output_schema=t.get("outputSchema"),
            annotations=t.get("annotations") or {},
            hash=schema_hash(t),
        )
        source, meta = render_tool(spec, tb)
        files[f"{pyt}.py"] = source
        tools_meta[pyt] = meta

    server_hash = schema_hash([[k, v["hash"]] for k, v in tools_meta.items()]
                              + [server.instructions or "", server.description or "", GENERATOR_VERSION])
    types_src = "\n\n\n".join(tb.defs.values())
    files["_types.py"] = (
        f"# generated by toolkernel · server={server.name} · schema={server_hash}\n"
        "from __future__ import annotations\n\n"
        "from typing import Any, Literal, Required, TypedDict  # noqa: F401\n"
        + ("\n\n" + types_src + "\n" if types_src else "")
    )
    desc = server.description or server.title or f"MCP server {server.name}"
    init_lines = [
        f"# generated by toolkernel · server={server.name} · schema={server_hash}",
        f'"""{_doc_escape(first_sentence(desc, 200))}"""',
        "",
        *[f"from .{p} import {p}" for p in tools_meta],
        "",
        f"__all__ = {json.dumps(list(tools_meta))}",
        f"SCHEMA_HASH = {json.dumps(server_hash)}",
        "",
    ]
    files["__init__.py"] = "\n".join(init_lines)

    readme = [f"# {server_py}", "", desc.strip(), ""]
    if server.name != server_py:
        readme += [f"Upstream name: `{server.name}`", ""]
    if server.instructions:
        readme += ["## Notes from the server", "", server.instructions.strip(), ""]
    readme += [f"## Tools ({len(tools_meta)})", ""]
    for p, m in tools_meta.items():
        readme.append(f"- `{m['signature']}`: {m['summary'] or '(no description)'}")
    readme += ["", f"Use: `from servers.{server_py} import <tool>`. Details: `help(<tool>)` or "
               f"`describe_tool(\"{server_py}.<tool>\")`.", ""]
    files["README.md"] = "\n".join(readme)

    index = {
        "name": server.name,
        "title": server.title,
        "description": desc,
        "instructions": server.instructions,
        "hash": server_hash,
        "tools": tools_meta,
        "types": tb.defs,
        "warnings": tb.warnings,
    }
    return files, index


class Generator:
    def __init__(self, wrappers_dir: Path, max_depth: int = 4):
        self.root = wrappers_dir
        self.max_depth = max_depth
        self.index: dict[str, Any] = {"servers": {}}
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "servers").mkdir(exist_ok=True)
        init = self.root / "servers" / "__init__.py"
        if not init.exists():
            init.write_text('"""Generated MCP server packages. See README.md."""\n')
        self.install_runtime_lib()
        idx = self.root / "_index.json"
        if idx.exists():
            try:
                self.index = json.loads(idx.read_text())
            except json.JSONDecodeError:
                pass

    def install_runtime_lib(self) -> None:
        dest = self.root / "toolkernel"
        shutil.copytree(RUNTIME_LIB, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))

    def server_py_name(self, name: str) -> str:
        taken = {v["name"]: k for k, v in self.index["servers"].items()}
        if name in taken:
            return taken[name]
        base = py_name(name)
        candidate, n = base, 2
        while candidate in self.index["servers"]:
            candidate, n = f"{base}_{n}", n + 1
        return candidate

    def generate(self, server: ServerInput) -> tuple[str, bool]:
        """Write one server's package. Returns (python name, changed)."""
        server_py = self.server_py_name(server.name)
        files, index = build_server(server, server_py, self.max_depth)
        pkg = self.root / "servers" / server_py
        old = self.index["servers"].get(server_py)
        changed = not old or old.get("hash") != index["hash"] or not pkg.exists()
        if changed:
            tmp = self.root / "servers" / f".{server_py}.tmp"
            if tmp.exists():
                shutil.rmtree(tmp)
            tmp.mkdir()
            for rel, content in files.items():
                (tmp / rel).write_text(content)
            if pkg.exists():
                trash = self.root / "servers" / f".{server_py}.old"
                if trash.exists():
                    shutil.rmtree(trash)
                pkg.rename(trash)
                tmp.rename(pkg)
                shutil.rmtree(trash)
            else:
                tmp.rename(pkg)
        self.index["servers"][server_py] = index
        self.write_index()
        return server_py, changed

    def remove(self, name: str) -> None:
        for py, entry in list(self.index["servers"].items()):
            if entry["name"] == name:
                shutil.rmtree(self.root / "servers" / py, ignore_errors=True)
                del self.index["servers"][py]
        self.write_index()

    def write_index(self) -> None:
        servers = dict(sorted(self.index["servers"].items()))
        self.index["servers"] = servers
        (self.root / "_index.json").write_text(json.dumps(self.index, indent=1, sort_keys=True) + "\n")
        lines = [
            "# Tools available in this runtime",
            "",
            "Each server is a Python package. Import tools and call them with keyword arguments:",
            "",
            "    from servers.<server> import <tool>",
            "",
            "Read `servers/<server>/README.md` for a server's tools, or `help(<tool>)` for one tool.",
            "",
        ]
        for py, s in servers.items():
            names = list(s["tools"])
            shown = ", ".join(names[:40]) + (f", +{len(names) - 40} more" if len(names) > 40 else "")
            lines += [f"## {py} ({len(names)} tools)", "", first_sentence(s["description"], 200), "", shown, ""]
        if not servers:
            lines += ["No servers are connected yet.", ""]
        (self.root / "README.md").write_text("\n".join(lines))

    # -- lookups -------------------------------------------------------------

    def tool_entry(self, qualified: str) -> tuple[str, str, dict[str, Any]] | None:
        """Find a tool by 'server.tool' using python or original names."""
        if "." not in qualified:
            return None
        s, t = qualified.split(".", 1)
        for spy, entry in self.index["servers"].items():
            if s in (spy, entry["name"]):
                for tpy, meta in entry["tools"].items():
                    if t in (tpy, meta["name"]):
                        return spy, tpy, meta
        return None

    def tool_source(self, server_py: str, tool_py: str) -> str:
        path = self.root / "servers" / server_py / f"{tool_py}.py"
        return path.read_text() if path.exists() else ""
