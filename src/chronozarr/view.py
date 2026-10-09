"""Look at a local store in the chronozarr viewer: from a notebook (`view`, `player`) or with
`chronozarr preview`.

A small HTTP server on 127.0.0.1 serves the store with byte ranges and CORS, which sharded stores
need and `python -m http.server` lacks. By default the hosted viewer at chronozarr.org is pointed
at it (the viewer needs internet access, the store never leaves the machine), so the browser must
reach 127.0.0.1 on the machine that runs the server: a local Jupyter or VS Code session, or an SSH
port forward of the same port number. Recent Chrome versions ask permission before a public page
may reach local network addresses; allow it, or open the printed viewer URL in its own tab.

Remote notebooks (JupyterHub, jupyter-server-proxy, any reverse proxy) need a URL the browser can
reach instead. `base_url` names it explicitly. With `viewer_dir`, a self-hosted copy of the viewer
(see docs/viewer-distribution.md) is served by the same server, and on JupyterHub the route
through the notebook server (jupyter-server-proxy) is chosen automatically: the viewer and the
store then share the notebook's origin, so the notebook login protects both. The server only ever
listens on 127.0.0.1.
"""

from __future__ import annotations

import contextlib
import errno
import html
import importlib
import math
import importlib.util
import os
import posixpath
import re
import shlex
import socket
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, Literal

VIEWER_URL = "https://chronozarr.org/demo/"
# The server mounts a self-hosted viewer folder under this path, next to the store.
VIEWER_MOUNT = "_viewer"
_VIEWER_PAGE = "demo/index.html"
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range, If-Match, If-None-Match, Content-Type",
    "Access-Control-Expose-Headers": "Content-Range, Content-Length, ETag, Accept-Ranges",
    "Access-Control-Allow-Private-Network": "true",
    "Timing-Allow-Origin": "*",
    "Access-Control-Max-Age": "600",
}
# Browsers refuse module scripts and WebAssembly with the wrong type, and the system mime table
# differs by platform (Windows maps .js to text/plain through the registry).
_VIEWER_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".wasm": "application/wasm",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str, size: int) -> tuple[int, int] | None | str:
    """Inclusive (start, end) for one satisfiable byte range, None to ignore the header, or
    "unsatisfiable"."""
    match = _RANGE.match(header.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        return None
    first, last = match.groups()
    if not first:  # suffix range: the final N bytes
        length = int(last)
        if length == 0:
            return "unsatisfiable"
        return max(size - length, 0), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        return "unsatisfiable"
    return start, end


def _unchanged_since(header: str | None, modified: int) -> bool:
    """Whether an If-Modified-Since header says the client's copy is as new as `modified`."""
    if not header:
        return False
    try:
        since = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return False
    return modified <= since.timestamp()


@dataclass
class ServerState:
    """What a server serves besides the store, and how many requests have reached it.

    The counters are how a notebook finds out whether the browser can reach the server at all.
    """

    viewer_root: Path | None = None
    store_requests: int = 0
    viewer_requests: int = 0
    last_store_request: float | None = None  # time.monotonic()
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reset_counts(self) -> None:
        with self._lock:
            self.store_requests = self.viewer_requests = 0
            self.last_store_request = None

    def record(self, *, store: bool, viewer: bool) -> None:
        with self._lock:
            if store:
                self.store_requests += 1
                self.last_store_request = time.monotonic()
            elif viewer:
                self.viewer_requests += 1


class StoreRequestHandler(SimpleHTTPRequestHandler):
    """Serves one directory under `/<prefix>/` with Range, HEAD, OPTIONS and CORS. No listings.

    A viewer folder (`ServerState.viewer_root`) is served the same way under `/_viewer/`.
    """

    # Added to every response. Subclasses may replace it to imitate other hosts.
    response_headers: ClassVar[dict[str, str]] = {**_CORS_HEADERS, "Cache-Control": "no-cache"}

    def __init__(
        self, *args: Any, root: Path, prefix: str, state: ServerState | None = None, **kwargs: Any
    ) -> None:
        self._root = root
        self._prefix = prefix
        self._state = state if state is not None else ServerState()
        self._remaining = 0
        super().__init__(*args, directory=str(root), **kwargs)

    def guess_type(self, path: Any) -> str:
        known = _VIEWER_TYPES.get(posixpath.splitext(str(path))[1].lower())
        return known if known is not None else super().guess_type(path)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-request logging; a notebook does not want a line per chunk."""

    def end_headers(self) -> None:
        for name, value in self.response_headers.items():
            self.send_header(name, value)
        super().end_headers()

    def _split(self, raw_path: str) -> list[str]:
        path = urllib.parse.unquote(urllib.parse.urlsplit(raw_path).path)
        return [p for p in posixpath.normpath(path).split("/") if p]

    def _count(self, raw_path: str) -> None:
        parts = self._split(raw_path)
        mount = parts[0] if parts else ""
        self._state.record(store=mount == self._prefix, viewer=mount == VIEWER_MOUNT)

    def do_OPTIONS(self) -> None:
        self._count(self.path)
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _file_for(self, raw_path: str) -> Path | None:
        parts = self._split(raw_path)
        if not parts or ".." in parts:
            return None
        if parts[0] == self._prefix:
            root = self._root
        elif parts[0] == VIEWER_MOUNT and self._state.viewer_root is not None:
            root = self._state.viewer_root
        else:
            return None
        target = root.joinpath(*parts[1:])
        return target if target.is_file() else None

    def send_head(self) -> BinaryIO | None:
        self._count(self.path)
        target = self._file_for(self.path)
        if target is None:
            self.send_error(HTTPStatus.NOT_FOUND, "no such object in this store")
            return None
        modified = int(target.stat().st_mtime)
        # `no-cache` makes a browser revalidate every object; answer that without the body.
        if "Range" not in self.headers and _unchanged_since(
            self.headers.get("If-Modified-Since"), modified
        ):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("Last-Modified", self.date_time_string(modified))
            self.end_headers()
            return None
        handle = target.open("rb")
        size = target.stat().st_size
        requested = _parse_range(self.headers.get("Range", ""), size)
        if requested == "unsatisfiable":
            handle.close()
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if isinstance(requested, tuple):
            start, end = requested
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            start, end = 0, size - 1
            self.send_response(HTTPStatus.OK)
        self._remaining = end - start + 1
        handle.seek(start)
        self.send_header("Content-Type", self.guess_type(str(target)))
        self.send_header("Content-Length", str(self._remaining))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Last-Modified", self.date_time_string(modified))
        self.end_headers()
        return handle

    def copyfile(self, source: Any, outputfile: Any) -> None:
        remaining = self._remaining
        while remaining > 0:
            block = source.read(min(remaining, 1 << 20))
            if not block:
                break
            outputfile.write(block)
            remaining -= len(block)


def _is_listening(port: int) -> bool:
    """Whether any process accepts connections on loopback `port`, over IPv4 or IPv6."""
    for address in ("127.0.0.1", "::1"):
        try:
            socket.create_connection((address, port), timeout=0.5).close()
        except OSError:  # refused, no IPv6, or filtered: nothing we can see is listening
            continue
        return True
    return False


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind a port that is in use, which would hide
    # a port conflict instead of reporting it.
    allow_reuse_address = os.name != "nt"


class StoreServer:
    """A running local HTTP server for one store directory. It listens on loopback only."""

    def __init__(
        self,
        store: Path,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        viewer_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.state = ServerState(viewer_root=viewer_dir)
        handler = partial(StoreRequestHandler, root=store, prefix=store.name, state=self.state)
        in_use = OSError(
            f"port {port} on {host} is already in use. Pass another port, or none to let the "
            "system pick a free one."
        )
        if port != 0 and _is_listening(port):
            # Binding alone can succeed next to a wildcard listener (SO_REUSEADDR on BSD and
            # macOS). A tunnel or port forward aimed at this port must reach this store, never
            # another service, so an occupied port is an error.
            raise in_use
        try:
            self._server = _Server((host, port), handler)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise in_use from exc
            raise OSError(f"cannot listen on {host}:{port}: {exc.strerror or exc}") from exc
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        if port != 0:
            self._check_answers()

    def _check_answers(self) -> None:
        """Fail unless this server, and not another process, answers on its port."""
        expected = (self.store / "zarr.json").read_bytes()
        try:
            direct = urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            )  # not via HTTP_PROXY
            with direct.open(f"{self.url}/zarr.json", timeout=5) as response:
                answered = response.read()
        except OSError as exc:  # URLError, HTTPError and timeouts
            self._stop()
            raise OSError(
                f"port {self.port} does not answer for {self.store}: {exc}. Another process may "
                "own it; pick another port."
            ) from exc
        if answered != expected:
            self._stop()
            raise OSError(
                f"port {self.port} answered with something other than {self.store}/zarr.json. "
                "Another process owns it; pick another port."
            )
        self.state.reset_counts()  # the check was not the browser

    def _stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def root_url(self) -> str:
        """The loopback address of the server; only a browser on this machine can use it."""
        host = self._server.server_address[0]
        return f"http://{host!s}:{self.port}"

    @property
    def url(self) -> str:
        """Loopback base URL of the store, suitable for `?store=` and `chronozarr.open_store`."""
        return f"{self.root_url}/{urllib.parse.quote(self.store.name)}"

    @property
    def viewer_dir(self) -> Path | None:
        return self.state.viewer_root

    def diagnose(self) -> str:
        """What this server has seen and what to try if the browser cannot reach it."""
        return access_for(self).diagnose()

    def close(self) -> None:
        with _lock:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            if _servers.get(self.store) is self:
                del _servers[self.store]
            known = _accesses.get(self.store)
            if known is not None and known.server is self:
                del _accesses[self.store]


_servers: dict[Path, StoreServer] = {}
_accesses: dict[Path, LocalAccess] = {}
_lock = threading.Lock()


def _check_viewer_dir(viewer_dir: str | Path) -> Path:
    path = Path(viewer_dir).expanduser().resolve()
    if not (path / _VIEWER_PAGE).is_file():
        raise FileNotFoundError(
            f"{path} is not a viewer folder: no {_VIEWER_PAGE}. Create one with "
            "`npx --package chronozarr chronozarr-viewer <new folder>` "
            "(see docs/viewer-distribution.md)."
        )
    return path


def serve_store(
    store: str | Path, *, port: int = 0, viewer_dir: str | Path | None = None
) -> StoreServer:
    """Serve a local store directory on 127.0.0.1 (free port by default).

    One server per store directory: a second call returns the running one. Stop it with
    `close()`; the next call starts a new server. Servers also die with the Python process.
    An explicit `port` that is in use raises OSError, and so does asking a running server for a
    different port. `viewer_dir` is a self-hosted viewer folder (`chronozarr-viewer` output) that
    the server also serves, under `/_viewer/`; a later call may add or replace it.
    """
    path = Path(store).expanduser().resolve()
    if not (path / "zarr.json").is_file():
        raise FileNotFoundError(
            f"{path} is not a chronozarr store: no zarr.json found. "
            "Pass the store directory that contains zarr.json."
        )
    viewer = _check_viewer_dir(viewer_dir) if viewer_dir is not None else None
    with _lock:
        running = _servers.get(path)
        if running is not None and port not in (0, running.port):
            raise OSError(
                f"{path} is already served on port {running.port}; call .close() on that server "
                f"before asking for port {port}."
            )
        if running is None:
            running = _servers[path] = StoreServer(path, port=port, viewer_dir=viewer)
        elif viewer is not None:
            running.state.viewer_root = viewer
        return running


def viewer_url(
    store_url: str,
    viewer: str = VIEWER_URL,
    *,
    t: int | None = None,
    product: str | None = None,
    band: str | None = None,
    range: Sequence[float] | None = None,
) -> str:
    """URL that opens `store_url` in the viewer, optionally at a timestep, product and display.

    `t` is a timestep index, `product` a viewer product id (`true_color`, `false_color`, `ndvi`,
    `ndwi`, `water`, `band`), `band` the band of the single-band product, and `range` its display
    limits (low, high) in physical units. They are presentation only: the store is not touched,
    and anyone who opens the URL sees the same initial view. The viewer ignores a value the
    store cannot honour, so check them first with `chronozarr link`.
    """
    query = [f"store={urllib.parse.quote(store_url, safe='')}"]
    if t is not None:
        if t < 0:
            raise ValueError(f"t must be a timestep index of 0 or more, got {t}")
        query.append(f"t={t}")
    if product is not None:
        query.append(f"p={urllib.parse.quote(product, safe='')}")
    if band is not None:
        query.append(f"b={urllib.parse.quote(band, safe='')}")
    if range is not None:
        low, high = range
        if not (math.isfinite(low) and math.isfinite(high) and low < high):
            raise ValueError(f"range must be two finite increasing limits, got {tuple(range)}")
        query.append(f"r={low:.12g},{high:.12g}")
    return f"{viewer}?{'&'.join(query)}"


def normalize_base_url(base_url: str) -> str:
    """`base_url` without a trailing slash; "/" becomes "" (the root of the page's own origin).

    Accepts an absolute http(s) URL or an origin-relative path such as "/user/ada/proxy/8765".
    """
    text = base_url.strip()
    parts = urllib.parse.urlsplit(text)
    absolute = parts.scheme in {"http", "https"} and bool(parts.netloc)
    rooted = text.startswith("/") and not text.startswith("//")
    if not (absolute or rooted) or parts.query or parts.fragment:
        raise ValueError(
            f"base_url must be an absolute http(s) URL or a path starting with '/', without a "
            f"query or fragment; got {base_url!r}. It is where the browser reaches this "
            "server's root, for example 'https://hub.example.org/user/ada/proxy/8765'."
        )
    return text.rstrip("/")


def jupyter_proxy_root(port: int, environ: Mapping[str, str] | None = None) -> str | None:
    """Origin-relative route to `port` through jupyter-server-proxy on JupyterHub, else None.

    JupyterHub gives every single-user server JUPYTERHUB_SERVICE_PREFIX, the one signal that is
    cheap and reliable from a kernel. A plain Jupyter server publishes no equivalent, so there
    the route is `base_url="<server base>/proxy/<port>"`.
    """
    prefix = (os.environ if environ is None else environ).get("JUPYTERHUB_SERVICE_PREFIX")
    return f"{prefix.rstrip('/')}/proxy/{port}" if prefix else None


def _proxy_installed() -> bool:
    return importlib.util.find_spec("jupyter_server_proxy") is not None


def _environment(environ: Mapping[str, str]) -> Literal["jupyterhub", "ssh", "local"]:
    if environ.get("JUPYTERHUB_SERVICE_PREFIX"):
        return "jupyterhub"
    if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        return "ssh"
    return "local"


Route = Literal["loopback", "base_url", "jupyter-proxy"]
ViewerSource = Literal["hosted", "served", "custom"]


@dataclass(frozen=True)
class LocalAccess:
    """How a browser is told to reach a local store, and what the server has seen of it."""

    server: StoreServer
    store_url: str
    viewer_url: str
    route: Route
    viewer_source: ViewerSource

    def diagnose(self, environ: Mapping[str, str] | None = None) -> str:
        server = self.server
        state = server.state
        last = state.last_store_request
        seen = "never" if last is None else f"{time.monotonic() - last:.0f} s ago"
        route = {
            "loopback": "127.0.0.1 of the machine that runs the browser",
            "base_url": "the base_url you gave",
            "jupyter-proxy": "through jupyter-server-proxy",
        }[self.route]
        source = {
            "hosted": "hosted at chronozarr.org, needs internet access",
            "served": f"served from {state.viewer_root}",
            "custom": "the viewer you gave",
        }[self.viewer_source]
        lines = [
            "chronozarr local store server",
            f"  store      {server.store}",
            f"  listens    127.0.0.1:{server.port} (this machine only)",
            f"  store URL  {self.store_url} ({route})",
            f"  viewer     {self.viewer_url} ({source})",
            f"  requests   {state.store_requests} for the store (last: {seen}), "
            f"{state.viewer_requests} for the viewer",
        ]
        if state.store_requests:
            lines.append(
                "The browser has reached this server, so connectivity is not the problem. Run "
                f"`chronozarr doctor {server.store}` and read the browser console."
            )
            return "\n".join(lines)
        lines.append("No request for the store has reached this server yet.")
        lines.extend(self._advice(os.environ if environ is None else environ))
        return "\n".join(lines)

    def _advice(self, environ: Mapping[str, str]) -> list[str]:
        port = self.server.port
        if self.route == "jupyter-proxy":
            installed = (
                "found in this Python environment"
                if _proxy_installed()
                else "not found in this Python environment; it must be installed where the "
                "Jupyter server runs"
            )
            return [
                f"The browser is sent through jupyter-server-proxy ({installed}).",
                f"Open {self.viewer_url} on your notebook's host in a new tab: it must show "
                "the viewer, not a 404 or a login page.",
            ]
        if self.route == "base_url":
            root = self.store_url.rsplit("/", 1)[0] or "/"
            return [
                f"Open {self.store_url}/zarr.json in the browser that shows the notebook: it "
                "must return JSON.",
                f"The proxy must map {root} to http://127.0.0.1:{port}/ (prefix removed) and "
                "pass Range headers.",
            ]
        kind = _environment(environ)
        advice: list[str]
        if kind == "jupyterhub":
            advice = [
                "This is a JupyterHub server: 127.0.0.1 in your browser is not this kernel. "
                "Serve a self-hosted viewer through jupyter-server-proxy with viewer_dir=... "
                "(docs/viewer-distribution.md), or forward the port.",
            ]
        elif kind == "ssh":
            advice = [
                "This looks like an SSH session. Forward the same port to your machine with "
                f"`ssh -L {port}:127.0.0.1:{port} <host>`; pass port={port} next time so the "
                "number stays the same.",
            ]
        else:
            advice = [
                "The browser must run on the machine that runs this kernel. For a remote "
                "kernel, container or VS Code remote, see 'Remote notebooks' in docs/python.md.",
            ]
        if self.viewer_source == "hosted":
            advice.append(
                "Chrome asks before a public page may reach 127.0.0.1; allow it. Safari can "
                "block an https page from loading http://127.0.0.1: use Chrome or Firefox, or "
                "a self-hosted viewer with viewer_dir=...."
            )
        return advice


def local_access(
    store: str | Path,
    *,
    port: int = 0,
    base_url: str | None = None,
    viewer: str | None = None,
    viewer_dir: str | Path | None = None,
) -> LocalAccess:
    """Serve `store` and decide the URLs the browser is given for it and for the viewer.

    `base_url` is where the browser reaches the server's root: an absolute http(s) URL or a path
    on the notebook's own origin. Without it the loopback address is used, unless `viewer_dir` is
    set on JupyterHub, where the route through the notebook server (jupyter-server-proxy) is
    chosen: only a viewer on the notebook's own origin sends the notebook login along with its
    requests. `viewer` is the viewer's URL; the default is the hosted viewer, or the one served
    from `viewer_dir`.
    """
    if viewer is not None and viewer_dir is not None:
        raise ValueError("pass either viewer (a URL) or viewer_dir (a folder), not both")
    server = serve_store(store, port=port, viewer_dir=viewer_dir)
    route: Route
    if base_url is not None:
        root, route = normalize_base_url(base_url), "base_url"
    elif viewer_dir is not None and (proxied := jupyter_proxy_root(server.port)) is not None:
        root, route = proxied, "jupyter-proxy"
    else:
        root, route = server.root_url, "loopback"
    source: ViewerSource = "custom" if viewer else "served" if viewer_dir is not None else "hosted"
    page = viewer or (
        f"{root}/{VIEWER_MOUNT}/{_VIEWER_PAGE}" if viewer_dir is not None else VIEWER_URL
    )
    access = LocalAccess(
        server=server,
        store_url=f"{root}/{urllib.parse.quote(server.store.name)}",
        viewer_url=page,
        route=route,
        viewer_source=source,
    )
    _accesses[server.store] = access
    return access


def access_for(server: StoreServer) -> LocalAccess:
    """The access last handed out for `server`, else the loopback one with the hosted viewer."""
    known = _accesses.get(server.store)
    if known is not None and known.server is server:
        return known
    return LocalAccess(server, server.url, VIEWER_URL, "loopback", "hosted")


def diagnose_view(store: str | Path) -> None:
    """Print what the local server of `store` has seen and what to try when the viewer is empty.

    Run it in a cell after `view(store)` or `player(store)`.
    """
    path = Path(store).expanduser().resolve()
    server = _servers.get(path)
    if server is None:
        raise ValueError(f"no local server is running for {path}: call view() or player() first")
    print(access_for(server).diagnose())




def view(
    store: str | Path,
    *,
    height: int = 640,
    viewer: str | None = None,
    port: int = 0,
    base_url: str | None = None,
    viewer_dir: str | Path | None = None,
    t: int | None = None,
    product: str | None = None,
    band: str | None = None,
    range: Sequence[float] | None = None,
) -> Any:
    """Show a store in the chronozarr viewer as a notebook iframe.

    `store` is a local directory (served from 127.0.0.1, see the module docstring for when that
    works) or an http(s) URL of a store that is already hosted. `base_url` and `viewer_dir` are
    for remote notebooks, see `local_access`. Returns an `IPython.display.HTML`; leave it as a
    cell's last expression or pass it to `display`. If the frame stays empty, call
    `chronozarr.diagnose_view(store)`.
    `t`, `product`, `band` and `range` set the initial view, as in `viewer_url`.
    """
    try:
        ipython_display = importlib.import_module("IPython.display")  # optional extra
    except ImportError as exc:
        raise ImportError(
            "chronozarr.view needs IPython: run `uv add 'chronozarr[notebook]'`"
        ) from exc
    text = str(store)
    if text.startswith(("http://", "https://")):
        if base_url is not None or viewer_dir is not None:
            raise ValueError("base_url and viewer_dir apply to local stores, not to a URL")
        store_url, page, note = text, viewer or VIEWER_URL, ""
    else:
        access = local_access(
            text, port=port, base_url=base_url, viewer=viewer, viewer_dir=viewer_dir
        )
        store_url, page = access.store_url, access.viewer_url
        note = " &middot; empty frame? run <code>chronozarr.diagnose_view(store)</code>"
    target = viewer_url(store_url, page, t=t, product=product, band=band, range=range)
    frame = (
        f'<iframe src="{html.escape(target, quote=True)}" width="100%" height="{int(height)}" '
        'style="border:0" allow="fullscreen; local-network-access" loading="lazy"></iframe>'
    )
    caption = (
        '<div style="font:12px sans-serif;color:#666">Store: '
        f"{html.escape(store_url)} &middot; "
        f'<a href="{html.escape(target, quote=True)}" target="_blank" rel="noopener">'
        f"open in a tab</a>{note}</div>"
    )
    return ipython_display.HTML(frame + caption)


def preview_command(store: str | Path) -> str:
    """The command that previews `store`, printed after `encode` and `convert` succeed."""
    return f"chronozarr preview {shlex.quote(os.fspath(store))}"


def _block() -> None:
    """Wait until interrupted. A bare Event().wait() is not interruptible on Windows."""
    while True:
        time.sleep(0.5)


def preview(
    store: str | Path,
    *,
    port: int = 0,
    viewer: str | None = None,
    viewer_dir: str | Path | None = None,
    base_url: str | None = None,
    open_browser: bool = True,
    echo: Callable[[str], None] = print,
) -> None:
    """Serve a local store and open it in the viewer until Ctrl-C (`chronozarr preview`).

    The server listens on 127.0.0.1 only. `port` 0 takes a free port. An explicit port that
    anything already listens on raises OSError (nothing falls through to another port, so a
    tunnel aimed at it never reaches another service), and the URL is printed only after the
    server is bound and has answered for this store. `viewer` or `viewer_dir` replace the hosted
    viewer at chronozarr.org, the only part that needs internet access. `base_url`, an absolute
    http(s) URL here, is the address a tunnel, proxy or port forward gives the server.
    """
    if base_url is not None and not normalize_base_url(base_url).startswith("http"):
        raise ValueError(
            f"preview opens a browser, so base_url must be an absolute http(s) URL; "
            f"got {base_url!r}"
        )
    access = local_access(
        store, port=port, base_url=base_url, viewer=viewer, viewer_dir=viewer_dir
    )
    server = access.server
    target = viewer_url(access.store_url, access.viewer_url)
    echo(f"serving {server.store} at {access.store_url} (byte ranges and CORS, 127.0.0.1 only)")
    if port != 0:
        echo(
            f"port {server.port} is bound to this store (checked with a local request); aim a "
            f"tunnel or port forward at http://127.0.0.1:{server.port}"
        )
    if base_url is not None:
        echo(f"anyone who can open {access.store_url} can read this store; stop sharing when done")
    if access.viewer_source == "hosted":
        echo(
            f"viewer: {access.viewer_url} (loaded from the internet; the store itself is read "
            "from this machine). For offline use see --viewer-dir and docs/viewer-distribution.md."
        )
    elif access.viewer_source == "served":
        echo(f"viewer: {server.viewer_dir} (served locally, no internet needed)")
    else:
        echo(f"viewer: {access.viewer_url}")
    echo(f"open: {target}")
    if open_browser and not webbrowser.open(target):
        echo("could not open a browser here; open the URL above in one.")
    echo("press Ctrl-C to stop")
    try:
        with contextlib.suppress(KeyboardInterrupt):
            _block()
    finally:
        server.close()
    echo("stopped")
