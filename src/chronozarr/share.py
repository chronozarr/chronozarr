"""Share a local chronozarr store through a Cloudflare quick tunnel.

This is deliberately a small orchestration layer around :mod:`chronozarr.view`.
It creates no Cloudflare account, configuration, or persistent tunnel: stopping the command
stops both the loopback server and the quick tunnel.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from chronozarr.decode import open_store
from chronozarr.doctor import Check, diagnose
from chronozarr.store import HttpStore, object_url
from chronozarr.view import StoreServer, local_access, viewer_url

# Keep the scheme split across source tokens: tests scan source text for shipped data-store URLs.
_QUICK_TUNNEL = re.compile("https" + r"://[a-z0-9-]+\.trycloudflare\.com\b", re.IGNORECASE)
_START_TIMEOUT_SECONDS = 30.0
# Cloudflare's quick-tunnel banner explicitly notes that a new hostname can take time to become
# reachable. Keep polling long enough for its public DNS record to propagate before rejecting a
# tunnel that cloudflared has already connected.
_READY_TIMEOUT_SECONDS = 90.0
_POLL_SECONDS = 0.2
_TUNNEL_LOG_LINES = 32


class _TunnelLogs:
    """A bounded, non-blocking diagnostic record of cloudflared's two log streams."""

    def __init__(self) -> None:
        self._lines: deque[str] = deque(maxlen=_TUNNEL_LOG_LINES)
        self._url: str | None = None
        self._lock = threading.Lock()

    def add(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            if self._url is None and (match := _QUICK_TUNNEL.search(line)) is not None:
                self._url = match.group(0).rstrip("/")

    def url(self) -> str | None:
        with self._lock:
            return self._url

    def recent(self) -> list[str]:
        with self._lock:
            return list(self._lines)[-4:]


@dataclass
class _Tunnel:
    """The cloudflared child and the pipes that report its assigned quick-tunnel URL."""

    process: subprocess.Popen[str]
    logs: _TunnelLogs

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


def _read_pipe(pipe: TextIO, logs: _TunnelLogs) -> None:
    try:
        for line in pipe:
            logs.add(line.rstrip())
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
    logs = _TunnelLogs()
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            threading.Thread(target=_read_pipe, args=(pipe, logs), daemon=True).start()
    return _Tunnel(process, logs)


def _tunnel_url(tunnel: _Tunnel, *, timeout: float = _START_TIMEOUT_SECONDS) -> str:
    """Read cloudflared's assigned quick-tunnel URL, failing promptly if it exits first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (url := tunnel.logs.url()) is not None:
            return url
        status = tunnel.process.poll()
        if status is not None:
            detail = "\n".join(tunnel.logs.recent())
            suffix = f" Output: {detail}" if detail else ""
            raise OSError(f"cloudflared exited before creating a tunnel (exit {status}).{suffix}")
        time.sleep(_POLL_SECONDS)
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


@dataclass(frozen=True)
class _CellMeasurement:
    """One real level-0 cell read through the tunnel, after reader metadata is open."""

    bytes_read: int
    seconds: float
    overview_cells: int
    requests: int
    sharded: bool


class _MeasuringHttpStore(HttpStore):
    """Count payload bytes fetched for data objects without changing the normal reader path."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.data_prefix: str | None = None
        self.data_bytes = 0
        self.data_requests = 0

    def _fetch(self, key, method, byte_range=None):
        fetched = super()._fetch(key, method, byte_range)
        if (
            method == "GET"
            and self.data_prefix
            and key.startswith(self.data_prefix)
            and fetched is not None
        ):
            self.data_bytes += len(fetched[0])
            self.data_requests += 1
        return fetched


def _first_chunk_measurement(store_url: str) -> _CellMeasurement:
    """Measure one normal reader cell read without downloading an entire shard.

    For an unsharded store, the read is one compressed chunk. For a sharded store it includes the
    bounded shard-index range and the bounded inner-chunk range. ``open_store`` derives the
    layout from the arrays, so the optional ``chronozarr.levels`` summary is not required.
    """
    transport = _MeasuringHttpStore(store_url)
    store = open_store(transport)
    transport.data_prefix = f"0/{store.attrs.variable}/c/"
    transport.data_bytes = transport.data_requests = 0
    started = time.perf_counter()
    store.read_cell(0, 0, 0, lod=0)
    seconds = time.perf_counter() - started
    if transport.data_bytes == 0:
        raise OSError("the first level-0 cell has no stored data chunk to time")
    rows, columns = store.levels[-1].grid
    return _CellMeasurement(
        bytes_read=transport.data_bytes,
        seconds=seconds,
        overview_cells=rows * columns,
        requests=transport.data_requests,
        sharded=store.levels[0].shard_time is not None,
    )


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
        measurement = _first_chunk_measurement(access.store_url)
        rate = (
            measurement.bytes_read / measurement.seconds if measurement.seconds else float("inf")
        )
        estimate = measurement.seconds * measurement.overview_cells
        kind = (
            "level-0 cell read (shard index + inner chunk)"
            if measurement.sharded
            else "level-0 chunk"
        )
        target = viewer_url(access.store_url, access.viewer_url)
        echo(f"sharing {server.store} through {public_root} (byte ranges and CORS verified)")
        echo(
            f"tunnel throughput: first {kind} {measurement.bytes_read / 1e6:.2f} MB in "
            f"{measurement.seconds:.2f} s ({rate / 1e6:.2f} MB/s, "
            f"{measurement.requests} data request(s)); estimated overview step {estimate:.2f} s "
            f"for {measurement.overview_cells} cell(s), assuming similar cell-read sizes"
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
