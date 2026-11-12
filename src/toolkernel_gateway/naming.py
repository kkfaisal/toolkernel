"""Name normalization (spec §9.6)."""

from __future__ import annotations

import builtins
import keyword
import re

_BUILTINS = set(dir(builtins))
_RESERVED_MODULES = {"_types", "__init__"}


def py_name(name: str, *, avoid_builtins: bool = True) -> str:
    """lowercase; non-[a-z0-9_] → _; leading digit → s_; keyword/builtin clash → trailing _."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)  # camelCase → camel_case
    s = re.sub(r"[^a-z0-9_]", "_", s.lower())
    s = re.sub(r"_+", "_", s).strip("_") or "x"
    if s[0].isdigit():
        s = "s_" + s
    if keyword.iskeyword(s) or keyword.issoftkeyword(s) or (avoid_builtins and s in _BUILTINS) or s in _RESERVED_MODULES:
        s += "_"
    return s


def param_name(name: str) -> str:
    """Parameters only avoid keywords: `id=` and `type=` read better than `id_=`."""
    return py_name(name, avoid_builtins=False)


def dedupe(names: list[str]) -> list[str]:
    """Collisions get _2, _3 … in original order."""
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            candidate = f"{n}_{seen[n]}"
            while candidate in seen:
                seen[n] += 1
                candidate = f"{n}_{seen[n]}"
            seen[candidate] = 1
            out.append(candidate)
        else:
            seen[n] = 1
            out.append(n)
    return out


def class_name(*parts: str) -> str:
    words: list[str] = []
    for p in parts:
        words += [w for w in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", p) if w]
    s = "".join(w[:1].upper() + w[1:] for w in words) or "Item"
    return ("T" + s) if s[0].isdigit() else s
