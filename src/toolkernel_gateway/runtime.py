"""Runtime manager (spec §10): one persistent Python process per session.

Drivers:
- local:  a subprocess on this machine. Fast and convenient for development. NOT isolated.
- docker: a hardened container per session (read-only root, no capabilities, resource limits).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("toolkernel.runtime")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Runtime:
    session_id: str
    workdir: Path
    driver: str
    url: str = ""
    token: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    proc: asyncio.subprocess.Process | None = None
    container: str | None = None
    started: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    via_relay: bool = False
    container_name: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        h = {"X-Runtime-Token": self.token}
        if self.via_relay and self.container_name:
            h["X-Runtime-Target"] = self.container_name
        return h

    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.returncode is None
        return self.container is not None


class RuntimeError_(Exception):
    pass


class RuntimeManager:
    def __init__(self, cfg: dict[str, Any], output_cfg: dict[str, Any], wrappers: Path, sessions_dir: Path,
                 bridge_port: int):
        self.cfg = cfg
        self.output_cfg = output_cfg
        self.wrappers = wrappers
        self.sessions_dir = sessions_dir
        self.bridge_port = bridge_port
        self.http = httpx.AsyncClient(timeout=None)
        self.instance = f"p{bridge_port}"  # distinguishes this gateway's containers from another's
        self.relay_url: str | None = None
        self._relay_lock = asyncio.Lock()
        sessions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def isolated(self) -> bool:
        return self.cfg["driver"] == "docker" and self.cfg["network"] == "isolated"

    # -- workspace -----------------------------------------------------------------

    def workdir(self, session_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:64]
        wd = self.sessions_dir / safe
        (wd / "saved").mkdir(parents=True, exist_ok=True)
        (wd / ".outputs").mkdir(exist_ok=True)
        if self.cfg["driver"] == "local":
            for name in ("servers", "toolkernel", "README.md"):
                link = wd / name
                if not link.is_symlink() and not link.exists():
                    link.symlink_to(self.wrappers / name)
        return wd

    def _env(self, rt: Runtime, bridge_token: str, host: str, port: int, bridge_url: str) -> dict[str, str]:
        return {
            "TOOLKERNEL_BRIDGE_URL": bridge_url,
            "TOOLKERNEL_BRIDGE_TOKEN": bridge_token,
            "TOOLKERNEL_RUNTIME_TOKEN": rt.token,
            "TOOLKERNEL_RUNTIME_HOST": host,
            "TOOLKERNEL_RUNTIME_PORT": str(port),
            "TOOLKERNEL_DATA_DIR": "saved",
            "TOOLKERNEL_PREVIEW_THRESHOLDS": json.dumps(self.output_cfg["preview_threshold"]),
            "TOOLKERNEL_STATE_LINE": json.dumps(self.output_cfg["state_line"]),
            "PYTHONUNBUFFERED": "1",
        }

    async def cleanup_orphans(self) -> None:
        """Remove runtime containers left behind by a previous gateway that did not shut down cleanly."""
        if self.cfg["driver"] != "docker" or not shutil.which("docker"):
            return
        ids = []
        for label in (f"toolkernel.runtime={self.instance}", f"toolkernel.relay={self.instance}"):
            ids += (await self._docker("ps", "-aq", "--filter", f"label={label}", check=False)).split()
        if ids:
            await self._docker("rm", "-f", *ids, check=False)
            log.info("removed %d leftover container(s)", len(ids))

    # -- isolated network ------------------------------------------------------------

    NETWORK = "toolkernel-rt"

    async def _ensure_relay(self) -> None:
        """Internal network + relay container: runtimes can reach the bridge and nothing else."""
        async with self._relay_lock:
            if self.relay_url:
                return
            c = self.cfg
            if not await self._docker("network", "ls", "-q", "--filter", f"name=^{self.NETWORK}$", check=False):
                await self._docker("network", "create", "--internal", "--label", "toolkernel=1", self.NETWORK)
            name = f"toolkernel-relay-{self.instance}"
            await self._docker("rm", "-f", name, check=False)
            await self._docker(
                "run", "-d", "--rm", "--name", name, "--label", f"toolkernel.relay={self.instance}",
                "-p", "127.0.0.1::8890", "--add-host", "host.docker.internal:host-gateway",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "128m",
                "-v", f"{self.wrappers / 'toolkernel'}:/workspace/toolkernel:ro",
                "-e", f"TOOLKERNEL_RELAY_BRIDGE={c['bridge_host']}:{self.bridge_port}",
                c["image"], "python", "-m", "toolkernel._relay",
            )
            await self._docker("network", "connect", "--alias", f"toolkernel-bridge-{self.instance}", self.NETWORK, name)
            mapping = await self._docker("port", name, "8890/tcp")
            self.relay_url = "http://127.0.0.1:" + mapping.splitlines()[0].rsplit(":", 1)[-1]

    async def shutdown(self) -> None:
        if self.relay_url:
            await self._docker("rm", "-f", f"toolkernel-relay-{self.instance}", check=False)
            self.relay_url = None

    # -- start / stop ----------------------------------------------------------------

    async def start(self, session_id: str, bridge_token: str) -> Runtime:
        wd = self.workdir(session_id)
        rt = Runtime(session_id=session_id, workdir=wd, driver=self.cfg["driver"])
        if rt.driver == "docker":
            await self._start_docker(rt, bridge_token)
        else:
            await self._start_local(rt, bridge_token)
        await self._wait_healthy(rt)
        return rt

    async def _start_local(self, rt: Runtime, bridge_token: str) -> None:
        port = _free_port()
        env = {
            # A minimal environment: gateway secrets never reach the runtime.
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(rt.workdir),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "PYTHONPATH": str(rt.workdir),
            **self._env(rt, bridge_token, "127.0.0.1", port, f"http://127.0.0.1:{self.bridge_port}"),
        }
        logf = open(rt.workdir / ".runtime.log", "ab")  # noqa: SIM115
        rt.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "toolkernel._server",
            cwd=rt.workdir, env=env, stdout=logf, stderr=logf, start_new_session=True,
        )
        logf.close()
        rt.url = f"http://127.0.0.1:{port}"

    async def _docker(self, *args: str, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if check and proc.returncode != 0:
            raise RuntimeError_(f"docker {args[0]} failed: {err.decode().strip()}")
        return out.decode().strip()

    async def _start_docker(self, rt: Runtime, bridge_token: str) -> None:
        c = self.cfg
        if not shutil.which("docker"):
            raise RuntimeError_("runtime.driver is 'docker' but the docker CLI was not found")
        name = f"toolkernel-rt-{rt.workdir.name[:24].lower()}-{secrets.token_hex(2)}"
        if self.isolated:
            await self._ensure_relay()
            bridge_url = f"http://toolkernel-bridge-{self.instance}:7301"
            net = ["--network", self.NETWORK]
        else:
            bridge_url = f"http://{c['bridge_host']}:{self.bridge_port}"
            net = ["-p", "127.0.0.1::8888", "--network", c["network"], "--add-host", "host.docker.internal:host-gateway"]
        args = [
            "run", "-d", "--rm", "--name", name, *net,
            "--cpus", str(c["cpu"]), "--memory", str(c["memory"]), "--pids-limit", str(c["pids"]),
            "--read-only", "--tmpfs", "/tmp:size=256m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{self.wrappers / 'servers'}:/workspace/servers:ro",
            "-v", f"{self.wrappers / 'toolkernel'}:/workspace/toolkernel:ro",
            "-v", f"{self.wrappers / 'README.md'}:/workspace/README.md:ro",
            "-v", f"{rt.workdir / 'saved'}:/workspace/saved",
            "-v", f"{rt.workdir / '.outputs'}:/workspace/.outputs",
            "--label", f"toolkernel.runtime={self.instance}",
        ]
        if c.get("hardened"):
            args += ["--runtime", "runsc"]
        for k, v in self._env(rt, bridge_token, "0.0.0.0", 8888, bridge_url).items():
            args += ["-e", f"{k}={v}"]
        args.append(c["image"])
        rt.container = await self._docker(*args)
        rt.container_name = name
        if self.isolated:
            assert self.relay_url
            rt.url, rt.via_relay = self.relay_url, True
        else:
            mapping = await self._docker("port", rt.container, "8888/tcp")
            rt.url = "http://127.0.0.1:" + mapping.splitlines()[0].rsplit(":", 1)[-1]

    async def _wait_healthy(self, rt: Runtime, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            if rt.proc is not None and rt.proc.returncode is not None:
                break
            try:
                r = await self.http.get(rt.url + "/health", headers=rt.headers, timeout=2)
                if r.status_code == 200:
                    return
            except httpx.HTTPError as e:
                last = str(e)
            await asyncio.sleep(0.1)
        tail = ""
        logp = rt.workdir / ".runtime.log"
        if logp.exists():
            tail = logp.read_text(errors="replace")[-800:]
        await self.stop(rt)
        raise RuntimeError_(f"runtime did not start ({last}). {tail}".strip())

    async def interrupt(self, rt: Runtime) -> None:
        if rt.proc is not None and rt.proc.returncode is None:
            rt.proc.send_signal(signal.SIGINT)
        elif rt.container:
            await self._docker("kill", "-s", "INT", rt.container, check=False)

    async def stop(self, rt: Runtime) -> None:
        if rt.proc is not None and rt.proc.returncode is None:
            try:
                os.killpg(rt.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                rt.proc.kill()
            await rt.proc.wait()
        if rt.container:
            await self._docker("rm", "-f", rt.container, check=False)
            rt.container = None

    # -- exec --------------------------------------------------------------------------

    async def exec(self, rt: Runtime, code: str, timeout: float) -> dict[str, Any]:
        """Run code. Returns the runtime's result dict, plus 'crashed'/'timed_out' flags when relevant."""
        rt.last_used = time.time()
        task = asyncio.create_task(
            self.http.post(rt.url + "/exec", json={"code": code}, headers=rt.headers)
        )
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                await self.interrupt(rt)
                done, _ = await asyncio.wait({task}, timeout=10)
                if not done:
                    task.cancel()
                    await self.stop(rt)
                    return {"timed_out": True, "crashed": True,
                            "error": f"Execution exceeded {timeout:.0f}s and could not be interrupted; the runtime was restarted."}
            resp = task.result()
            resp.raise_for_status()
            out = resp.json()
            if "KeyboardInterrupt" in (out.get("error") or ""):
                out["error"] = f"Execution exceeded the {timeout:.0f}s time limit and was interrupted. " \
                               "Variables assigned before that point are kept."
                out["timed_out"] = True
            return out
        except (httpx.HTTPError, ConnectionError) as e:
            log.warning("runtime %s failed during exec: %s", rt.session_id, e)
            await self.stop(rt)
            return {"crashed": True, "error": "The runtime stopped unexpectedly while running this code "
                    "(possibly out of memory). It will be restarted on your next call."}
        finally:
            rt.last_used = time.time()

    async def close(self) -> None:
        await self.shutdown()
        await self.http.aclose()
