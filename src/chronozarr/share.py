"""Share a local chronozarr store through a Cloudflare quick tunnel.

This is deliberately a small orchestration layer around :mod:`chronozarr.view`.
It creates no Cloudflare account, configuration, or persistent tunnel: stopping the command
stops both the loopback server and the quick tunnel.
"""

from __future__ import annotations

import contextlib
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from chronozarr.doctor import Check, diagnose
from chronozarr.store import object_url
from chronozarr.view import StoreServer, local_access, viewer_url

_QUICK_TUNNEL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\b", re.IGNORECASE)
_START_TIMEOUT_SECONDS = 30.0
_READY_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.2


@dataclass
class _Tunnel:
    """The cloudflared child and the pipes that report its assigned quick-tunnel URL."""

    process: subprocess.Popen[str]
    lines: queue.Queue[str]

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def _cloudflared() -> str:
    executable = shutil.which("cloudflared")
    if executable is None:
        raise OSError(
            "cloudflared is required to share a store but was not found. Install it from "
            "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/"
            "downloads/ and run `chronozarr share` again. No local server was started."
        )
    return executable


def _read_pipe(pipe: TextIO, lines: queue.Queue[str]) -> None:
    try:
        for line in pipe:
            lines.put(line.rstrip())
    finally:
        with contextlib.suppress(Exception):
            pipe.close()


def _start_tunnel(executable: str, port: int) -> _Tunnel:
    """Start cloudflared and begin collecting both of its log streams without blocking it."""
    try:
        process = subprocess.Popen(
            [executable, "tunnel", "--url", f"http://127.0.0.1:{port}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise OSError(f"could not start cloudflared: {exc}") from exc
    lines: queue.Queue[str] = queue.Queue()
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            threading.Thread(target=_read_pipe, args=(pipe, lines), daemon=True).start()
    return _Tunnel(process, lines)


def _tunnel_url(tunnel: _Tunnel, *, timeout: float = _START_TIMEOUT_SECONDS) -> str:
    """Read cloudflared's assigned quick-tunnel URL, failing promptly if it exits first."""
    deadline = time.monotonic() + timeout
    output: list[str] = []
    while time.monotonic() < deadline:
        try:
            line = tunnel.lines.get(timeout=min(_POLL_SECONDS, deadline - time.monotonic()))
        except queue.Empty:
            line = ""
        if line:
            output.append(line)
            match = _QUICK_TUNNEL.search(line)
            if match is not None:
                return match.group(0).rstrip("/")
        status = tunnel.process.poll()
        if status is not None:
            detail = "\n".join(output[-4:])
            suffix = f" Output: {detail}" if detail else ""
            raise OSError(f"cloudflared exited before creating a tunnel (exit {status}).{suffix}")
    raise OSError(
        "cloudflared did not print a trycloudflare.com URL within "
        f"{timeout:.0f} seconds. Check its network connection and try again."
    )


def _direct_get(url: str, *, timeout: float = 10.0) -> bytes:
    """GET without inheriting an HTTP proxy intended for unrelated local traffic."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "chronozarr-share"})
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"HTTP {response.status}")
        return response.read()


def _wait_for_public_store(
    store_url: str, expected_root: bytes, *, timeout: float = _READY_TIMEOUT_SECONDS
) -> None:
    """Wait until the public route serves precisely the root we bound the tunnel to."""
    deadline = time.monotonic() + timeout
    last_error = "no response"
    root_url = object_url(store_url, "zarr.json")
    while time.monotonic() < deadline:
        try:
            if _direct_get(root_url) == expected_root:
                return
            last_error = "zarr.json did not match the local store"
        except OSError as exc:
            last_error = str(exc)
        time.sleep(_POLL_SECONDS)
    raise OSError(
        f"the tunnel did not serve this store within {timeout:.0f} seconds ({last_error}). "
        "The local server and tunnel were stopped."
    )


def _first_chunk_measurement(store_url: str, server: StoreServer) -> tuple[int, float, int]:
    """Return bytes, seconds, and cells per overview timestep from one level-0 chunk request."""
    root = json.loads((server.store / "zarr.json").read_bytes())
    variable = root["attributes"]["chronozarr"].get("variable", "data")
    target = object_url(store_url, f"0/{variable}/c/0/0/0/0")
    started = time.perf_counter()
    body = _direct_get(target, timeout=30)
    seconds = time.perf_counter() - started
    if not body:
        raise OSError("the first level-0 chunk was empty")

    # The viewer's fitted overview needs one chunk per cell at the coarsest level. The estimate
    # below intentionally reports its assumption: compression varies between cells and levels.
    overview = root["attributes"]["chronozarr"]["levels"][-1]
    rows, columns = overview["grid"]
    cells = int(rows) * int(columns)
    return len(body), seconds, cells


def _doctor_passes(checks: list[Check]) -> bool:
    return not any(check.status == "fail" for check in checks)


def _wait_for_tunnel(tunnel: _Tunnel) -> int:
    """Block until cloudflared exits; KeyboardInterrupt is handled by ``share``."""
    while tunnel.process.poll() is None:
        time.sleep(_POLL_SECONDS)
    return int(tunnel.process.returncode or 0)


def share(
    store: str | Path,
    *,
    port: int = 0,
    viewer: str | None = None,
    viewer_dir: str | Path | None = None,
    open_browser: bool = True,
    echo: Callable[[str], None] = print,
) -> None:
    """Serve ``store`` through one disposable Cloudflare quick tunnel until interrupted.

    The public viewer link is deliberately withheld until the tunnel returns the local store's
    root metadata and ``chronozarr doctor`` reports no reader-breaking failures.
    """
    if viewer is not None and viewer_dir is not None:
        raise ValueError("pass either viewer (a URL) or viewer_dir (a folder), not both")
    executable = _cloudflared()  # Do this first: a missing dependency must not start a server.
    server: StoreServer | None = None
    tunnel: _Tunnel | None = None
    stopped_by_user = False
    try:
        # Binding with port=0 avoids a find-free-port race. Verify the selected port before
        # cloudflared sees it, using the same byte-for-byte root check as explicit preview ports.
        server = local_access(store, port=port, viewer_dir=viewer_dir).server
        server._check_answers()
        tunnel = _start_tunnel(executable, server.port)
        public_root = _tunnel_url(tunnel)
        access = local_access(
            store, port=server.port, base_url=public_root, viewer=viewer, viewer_dir=viewer_dir
        )
        _wait_for_public_store(access.store_url, (server.store / "zarr.json").read_bytes())
        checks = diagnose(access.store_url)
        if not _doctor_passes(checks):
            failed = "; ".join(f"{c.name}: {c.detail}" for c in checks if c.status == "fail")
            raise OSError(f"the tunnel did not pass chronozarr doctor: {failed}")
        bytes_read, seconds, overview_cells = _first_chunk_measurement(access.store_url, server)
        rate = bytes_read / seconds if seconds else float("inf")
        estimate = seconds * overview_cells
        target = viewer_url(access.store_url, access.viewer_url)
        echo(f"sharing {server.store} through {public_root} (byte ranges and CORS verified)")
        echo(
            f"tunnel throughput: first level-0 chunk {bytes_read / 1e6:.2f} MB in "
            f"{seconds:.2f} s ({rate / 1e6:.2f} MB/s); estimated overview step "
            f"{estimate:.2f} s for {overview_cells} cell(s), assuming similar chunk sizes"
        )
        echo(f"open: {target}")
        echo("anyone who can open this link can read this store; press Ctrl-C to stop sharing")
        if open_browser and not webbrowser.open(target):
            echo("could not open a browser here; open the URL above in one.")
        try:
            status = _wait_for_tunnel(tunnel)
        except KeyboardInterrupt:
            stopped_by_user = True
        else:
            raise OSError(f"cloudflared stopped unexpectedly (exit {status}); sharing ended.")
    finally:
        if tunnel is not None:
            tunnel.close()
        if server is not None:
            server.close()
    if stopped_by_user:
        echo("stopped")
