"""Runtime process entry point: `python -m toolkernel._server`.

Serves a tiny HTTP API to the gateway. Execution happens on the main thread so
SIGINT can interrupt a long-running cell without losing state.
"""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from ._executor import Executor

TOKEN = os.environ.get("TOOLKERNEL_RUNTIME_TOKEN", "")
HOST = os.environ.get("TOOLKERNEL_RUNTIME_HOST", "127.0.0.1")
PORT = int(os.environ.get("TOOLKERNEL_RUNTIME_PORT", "8888"))
THRESHOLDS = json.loads(os.environ.get("TOOLKERNEL_PREVIEW_THRESHOLDS", "{}"))
STATE = json.loads(os.environ.get("TOOLKERNEL_STATE_LINE", "{}"))

executor = Executor(THRESHOLDS, STATE.get("max_items", 15), STATE.get("max_chars", 300))


def _on_sigint(signum, frame):  # noqa: ARG001
    if executor.executing:
        raise KeyboardInterrupt


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return not TOKEN or self.headers.get("X-Runtime-Token") == TOKEN

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path == "/health":
            return self._send(200, {"ok": True, "pid": os.getpid(), "cells": executor.cells})
        if self.path == "/state":
            return self._send(200, {"state": executor.state_line()})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if self.path != "/exec":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        try:
            result = executor.run(payload.get("code", ""))
        except KeyboardInterrupt:
            result = {"stdout": "", "stdout_dropped": 0, "result": None,
                      "error": "KeyboardInterrupt: execution was interrupted.", "state": executor.state_line(),
                      "duration_ms": 0}
        self._send(200, result)


def _exit_with_parent() -> None:
    """Local driver: if the gateway dies, don't linger as an orphan."""
    parent = os.getppid()
    while True:
        time.sleep(2)
        if os.getppid() != parent:
            os._exit(0)


def main() -> None:
    sys.stdin = io.StringIO("")  # input() returns EOF instead of hanging
    if os.environ.get("TOOLKERNEL_RUNTIME_HOST", "127.0.0.1") == "127.0.0.1":
        threading.Thread(target=_exit_with_parent, daemon=True).start()
    signal.signal(signal.SIGINT, _on_sigint)
    server = HTTPServer((HOST, PORT), Handler)
    print(f"toolkernel runtime ready on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
