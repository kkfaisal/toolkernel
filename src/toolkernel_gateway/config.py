"""Configuration: toolkernel.yaml (gateway settings) and mcp.json (upstream servers)."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "server": {"host": "127.0.0.1", "port": 7300, "bridge_port": 7301, "bridge_host": "127.0.0.1", "api_key": None},
    "data_dir": "./.toolkernel",
    "upstreams": {"config_file": "./mcp.json", "watch": True, "upstream_wait_s": 15, "call_timeout_s": 120},
    "generator": {"max_type_depth": 4, "validate_output": "warn"},
    "runtime": {
        "driver": "local",  # local | docker
        "state": "stateful",  # stateful | stateless
        "image": "toolkernel/runtime:1",
        "cpu": 1.0,
        "memory": "1g",
        "pids": 256,
        "exec_timeout_s": 120,
        "idle_timeout_s": 1800,
        "hardened": False,
        "network": "isolated",  # isolated (bridge-only) | any docker network name, e.g. bridge (internet access)
        "bridge_host": "host.docker.internal",
        "static_check": True,
    },
    "stash": {"max_bytes": 10 * 1024 * 1024, "ttl_s": 86400},
    "output": {
        "budget_chars": 4000,
        "head_ratio": 0.7,
        "preview_threshold": {"list": 20, "dict": 50, "str": 2000, "dataframe_rows": 20},
        "state_line": {"enabled": True, "max_items": 15, "max_chars": 300},
        "chars_per_token": 4,
    },
    "agent_interface": {"hybrid_call_tool": True, "max_servers_in_description": 30},
    "policies": {"default": "allow", "rules": [], "approval_timeout_s": 300},
}


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path | None = None
    base_dir: Path = field(default_factory=Path.cwd)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    @property
    def data_dir(self) -> Path:
        p = Path(self.raw["data_dir"])
        return (p if p.is_absolute() else self.base_dir / p).resolve()

    @property
    def mcp_json(self) -> Path:
        p = Path(self.raw["upstreams"]["config_file"])
        return (p if p.is_absolute() else self.base_dir / p).resolve()

    @property
    def api_key(self) -> str | None:
        return os.environ.get("TOOLKERNEL_API_KEY") or self.raw["server"].get("api_key")

    def public(self) -> dict[str, Any]:
        """Settings safe to show in the UI."""
        out = copy.deepcopy(self.raw)
        if out["server"].get("api_key"):
            out["server"]["api_key"] = "••••"
        return out


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    raw: dict[str, Any] = {}
    cfg_path: Path | None = None
    if path:
        cfg_path = Path(path).resolve()
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    elif Path("toolkernel.yaml").exists():
        cfg_path = Path("toolkernel.yaml").resolve()
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    merged = _merge(DEFAULTS, raw)
    merged = _merge(merged, overrides or {})
    return Config(merged, cfg_path, cfg_path.parent if cfg_path else Path.cwd())


# ---------------------------------------------------------------- mcp.json

_SECRET = re.compile(r"\$\{(secret|env):([A-Za-z0-9_.-]+)\}")


def read_mcp_json(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text() or "{}")
    return dict(data.get("mcpServers", {}))


def write_mcp_json(path: Path, servers: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")
    tmp.replace(path)


def resolve_secrets(value: Any, secrets: dict[str, str]) -> Any:
    """Expand ${secret:name} and ${env:NAME}. Secrets come from TOOLKERNEL_SECRET_<NAME> or the secrets file."""
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            kind, name = m.groups()
            if kind == "env":
                return os.environ.get(name, "")
            env_name = "TOOLKERNEL_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()
            if env_name in os.environ:
                return os.environ[env_name]
            if name in secrets:
                return secrets[name]
            raise KeyError(f"secret {name!r} is not set (set {env_name} or add it in Settings)")
        return _SECRET.sub(sub, value)
    if isinstance(value, dict):
        return {k: resolve_secrets(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_secrets(v, secrets) for v in value]
    return value
