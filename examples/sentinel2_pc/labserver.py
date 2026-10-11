"""Local HTTP range server for lab runs of the Sentinel-2 ingest benchmark.

Serves files from a directory with the parts of HTTP that COG readers use: GET, HEAD, a single
`Range: bytes=a-b`, keep-alive. On top of that it can shape the traffic so a run is repeatable
and cannot load a public host:

- `latency_ms`: sleep before every response.
- `bandwidth_mbps`: megabytes per second, one token bucket shared by all connections.
- `fail_rate`: fraction of responses that are 503 (seeded, so a run can be repeated).
- `fail_first`: 503 for the first N requests of every path (a deterministic transient error).
- `max_inflight`: 503 for every request beyond this many in flight at once, the way a host
  throttles one client that opens too many connections.
- `fail_after` / `fail_for`: after `fail_after` successful data responses, every response is 503
  for `fail_for` seconds, then the count starts over (a throttling burst).

A request path `/a<N>/<rest>` serves the file `<rest>`, so many distinct URLs can map to one
mirrored file (GDAL and the limiter see different files; the disk holds one).

`GET /_stats` returns the counters as JSON and `POST /_reset` zeroes them. Neither is counted.

Usage:
    uv run python examples/sentinel2_pc/labserver.py --root data/bench/mirror --port 8765 \\
        --latency-ms 40 --bandwidth-mbps 15 --fail-rate 0.01

In process (tests; not for benchmarks):
    server, base_url, thread = start_lab_server(root, LabConfig(latency_ms=40))
    ...
    stop_lab_server(server, thread)

A benchmark must run the server in its own process (bench.py does). With rasterio 1.5.0 a
`rasterio.open` of a URL served by a thread in the same process hung until killed.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

CHUNK_BYTES = 64 * 1024
ALIAS = re.compile(r"^/a(\d+)/(.+)$")
RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


@dataclass(frozen=True)
class LabConfig:
    """Traffic shaping. Zero or 0.0 turns a feature off."""

    latency_ms: float = 0.0
    bandwidth_mbps: float = 0.0
    fail_rate: float = 0.0
    fail_after: int = 0
    fail_for: float = 5.0
    max_inflight: int = 0
    fail_first: int = 0
    seed: int = 0


class LabState:
    """Counters, the shared bandwidth bucket, the failure dice and the throttling burst.

    The lock only guards short in-memory updates; nothing sleeps, writes or logs while holding it.
    """

    def __init__(self, config: LabConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._rng = random.Random(config.seed)
        self._next_free = 0.0
        self._burst_until = 0.0
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._requests: dict[str, int] = {}
            self._status: dict[str, int] = {}
            self._bytes_sent = 0
            self._successes_since_burst = 0
            self._bursts = 0
            self._next_free = 0.0
            self._burst_until = 0.0
            self._inflight = 0
            self._seen_paths: dict[str, int] = {}
            self._rng = random.Random(self.config.seed)

    def should_fail(self) -> bool:
        """Decide whether the next response is a 503 (random failure or throttling burst)."""
        now = time.monotonic()
        with self._lock:
            if now < self._burst_until:
                return True
            fail_after = self.config.fail_after
            if fail_after and self._successes_since_burst >= fail_after:
                self._burst_until = now + self.config.fail_for
                self._successes_since_burst = 0
                self._bursts += 1
                return True
            return self.config.fail_rate > 0 and self._rng.random() < self.config.fail_rate

    def first_failure(self, path: str) -> bool:
        """True for each of the first `fail_first` requests of a path."""
        if not self.config.fail_first:
            return False
        with self._lock:
            seen = self._seen_paths.get(path, 0)
            self._seen_paths[path] = seen + 1
            return seen < self.config.fail_first

    def enter(self) -> bool:
        """Count one request in flight; False when that exceeds `max_inflight` (throttle it)."""
        with self._lock:
            self._inflight += 1
            limit = self.config.max_inflight
            return not limit or self._inflight <= limit

    def leave(self) -> None:
        with self._lock:
            self._inflight -= 1

    def record(self, method: str, status: int) -> None:
        with self._lock:
            self._requests[method] = self._requests.get(method, 0) + 1
            self._status[str(status)] = self._status.get(str(status), 0) + 1
            if method == "GET" and status in (200, 206):
                self._successes_since_burst += 1

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self._bytes_sent += n

    def throttle(self, n: int) -> None:
        """Block until `n` bytes fit in the shared bandwidth budget."""
        rate = self.config.bandwidth_mbps * 1e6
        if rate <= 0:
            return
        now = time.monotonic()
        with self._lock:
            start = max(now, self._next_free)
            self._next_free = start + n / rate
        finish = start + n / rate
        if finish > now:
            time.sleep(finish - now)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "requests": dict(self._requests),
                "status": dict(self._status),
                "bytes_sent": self._bytes_sent,
                "bursts": self._bursts,
                "config": asdict(self.config),
            }


class LabServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], root: Path, config: LabConfig) -> None:
        super().__init__(address, LabHandler)
        self.root = root.resolve()
        self.state = LabState(config)

    def get_request(self):
        # Out of file descriptors, accept() fails at once and serve_forever would retry in a
        # busy loop at 100 % CPU; pausing lets open connections finish and free descriptors.
        try:
            return super().get_request()
        except OSError:
            time.sleep(0.05)
            raise


class LabHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60  # drop idle keep-alive connections

    server: LabServer

    def log_message(self, format: str, *args) -> None:
        """Per-request logging is off."""

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_POST(self) -> None:
        if urlsplit(self.path).path == "/_reset":
            self.server.state.reset()
            self._send_json({"reset": True})
        else:
            self._send_empty(404)

    # --- helpers ---

    def _send_empty(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve(self, url_path: str) -> Path | None:
        """The file a request path names, or None when it is missing or outside the root."""
        path = unquote(url_path)
        alias = ALIAS.match(path)
        if alias:
            path = "/" + alias.group(2)
        candidate = (self.server.root / path.lstrip("/")).resolve()
        if not candidate.is_relative_to(self.server.root) or not candidate.is_file():
            return None
        return candidate

    @staticmethod
    def _parse_range(header: str, size: int) -> tuple[int, int] | None | bool:
        """(first, last) inclusive; None when the header is not a single range (serve whole file);
        False when the range cannot be satisfied."""
        match = RANGE.match(header.strip())
        if not match or (match.group(1) == "" and match.group(2) == ""):
            return None
        if match.group(1) == "":  # suffix: the last n bytes
            n = int(match.group(2))
            if n == 0:
                return False
            return max(0, size - n), size - 1
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else size - 1
        if first >= size or last < first:
            return False
        return first, min(last, size - 1)

    def _serve(self, send_body: bool) -> None:
        method = "GET" if send_body else "HEAD"
        url_path = urlsplit(self.path).path
        state = self.server.state

        if send_body and url_path == "/_stats":
            self._send_json(state.snapshot())
            return

        allowed = state.enter()
        try:
            self._serve_counted(method, url_path, state, allowed, send_body)
        finally:
            state.leave()

    def _serve_counted(self, method, url_path, state, allowed: bool, send_body: bool) -> None:
        if state.config.latency_ms > 0:
            time.sleep(state.config.latency_ms / 1000)

        if not allowed or state.first_failure(url_path) or state.should_fail():
            state.record(method, 503)
            self._send_empty(503, {"Retry-After": "1"})
            return

        file_path = self._resolve(url_path)
        if file_path is None:
            state.record(method, 404)
            self._send_empty(404)
            return

        size = file_path.stat().st_size
        first, last, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            parsed = self._parse_range(range_header, size)
            if parsed is False:
                state.record(method, 416)
                self._send_empty(416, {"Content-Range": f"bytes */{size}"})
                return
            if isinstance(parsed, tuple):
                first, last = parsed
                status = 206

        length = last - first + 1 if size else 0
        state.record(method, status)
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        self.end_headers()
        if not send_body or length == 0:
            return

        try:
            with file_path.open("rb") as f:
                f.seek(first)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    state.throttle(len(chunk))
                    self.wfile.write(chunk)
                    state.add_bytes(len(chunk))
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # the client went away mid-body


def start_lab_server(
    root: Path, config: LabConfig | None = None, port: int = 0
) -> tuple[LabServer, str, threading.Thread]:
    """Start a server on 127.0.0.1 in a thread. `port=0` picks a free port."""
    server = LabServer(("127.0.0.1", port), root, config or LabConfig())
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="labserver", daemon=True
    )
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", thread


def stop_lab_server(server: LabServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--latency-ms", type=float, default=0.0)
    parser.add_argument("--bandwidth-mbps", type=float, default=0.0, help="megabytes per second")
    parser.add_argument("--fail-rate", type=float, default=0.0)
    parser.add_argument("--fail-after", type=int, default=0)
    parser.add_argument("--fail-for", type=float, default=5.0, help="seconds of 503s per burst")
    parser.add_argument(
        "--max-inflight",
        type=float,
        default=0,
        help="503 for requests beyond this many in flight at once (per-client throttling)",
    )
    parser.add_argument(
        "--fail-first", type=int, default=0, help="503 for the first N requests of every path"
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error(f"--root {args.root} is not a directory")
    config = LabConfig(
        latency_ms=args.latency_ms,
        bandwidth_mbps=args.bandwidth_mbps,
        fail_rate=args.fail_rate,
        fail_after=args.fail_after,
        fail_for=args.fail_for,
        max_inflight=int(args.max_inflight),
        fail_first=args.fail_first,
        seed=args.seed,
    )
    server, base_url, thread = start_lab_server(args.root, config, args.port)
    print(base_url, flush=True)  # first line: bench.py reads the port from it
    print(f"serving {args.root} with {config}", flush=True)
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        stop_lab_server(server, thread)


if __name__ == "__main__":
    main()
