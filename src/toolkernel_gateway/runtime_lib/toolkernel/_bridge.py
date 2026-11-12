"""Bridge client: the only way code in the runtime reaches the outside world.

Every generated wrapper ends in `call_tool(server, tool, arguments)`. The call is
sent to the gateway's bridge, which applies policy, forwards it upstream and
returns a normalized value.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "call_tool",
    "ToolError",
    "ToolDenied",
    "ToolUnavailable",
    "MediaRef",
    "ResourceRef",
]

_BRIDGE_URL = os.environ.get("TOOLKERNEL_BRIDGE_URL", "http://127.0.0.1:7301")
_TOKEN = os.environ.get("TOOLKERNEL_BRIDGE_TOKEN", "")


class ToolError(Exception):
    """The upstream tool ran and reported an error."""

    def __init__(self, server: str, tool: str, message: str):
        self.server, self.tool, self.message = server, tool, message
        super().__init__(f"{server}.{tool}: {message}")


class ToolDenied(Exception):
    """A gateway policy blocked the call (deny rule, declined approval, rate limit)."""


class ToolUnavailable(Exception):
    """The upstream server is not reachable right now, or the call timed out."""


class MediaRef:
    """An image or audio result, saved to a file instead of being held in memory."""

    def __init__(self, path: str, mime_type: str):
        self.path, self.mime_type = path, mime_type

    def __repr__(self) -> str:
        return f"MediaRef(path={self.path!r}, mime_type={self.mime_type!r})"


class ResourceRef:
    """An embedded MCP resource. Text resources carry `text`; blobs are saved to `path`."""

    def __init__(self, uri: str, mime_type: str | None, text: str | None = None, path: str | None = None):
        self.uri, self.mime_type, self.text, self.path = uri, mime_type, text, path

    def __repr__(self) -> str:
        body = f"text[{len(self.text)}]" if self.text is not None else f"path={self.path!r}"
        return f"ResourceRef(uri={self.uri!r}, mime_type={self.mime_type!r}, {body})"


_ERRORS = {"ToolError": ToolError, "ToolDenied": ToolDenied, "ToolUnavailable": ToolUnavailable}


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        kind = value.get("__tk__")
        if kind == "media":
            return MediaRef(value["path"], value["mime_type"])
        if kind == "resource":
            return ResourceRef(value["uri"], value.get("mime_type"), value.get("text"), value.get("path"))
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _post(path: str, payload: dict[str, Any], timeout: float = 600) -> Any:
    req = urllib.request.Request(
        _BRIDGE_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            raise ToolUnavailable(f"bridge error {e.code}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ToolUnavailable(f"bridge unreachable: {e}") from None
    if not body.get("ok"):
        err = body.get("error") or {}
        cls = _ERRORS.get(err.get("type"), ToolUnavailable)
        if cls is ToolError:
            raise ToolError(err.get("server", "?"), err.get("tool", "?"), err.get("message", ""))
        raise cls(err.get("message", "unknown error"))
    return body.get("result")


def call_tool(server: str, tool: str, arguments: dict[str, Any], _caller: str = "code") -> Any:
    """Call `server.tool` through the gateway. `None` arguments are dropped."""
    args = {k: v for k, v in arguments.items() if v is not None}
    return _decode(_post("/bridge/call", {"server": server, "tool": tool, "arguments": args, "caller": _caller}))
