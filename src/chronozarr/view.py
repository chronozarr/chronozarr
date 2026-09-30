"""Look at a local store in the TileRipper viewer from a notebook.

`view(store)` starts a small HTTP server on 127.0.0.1 (byte ranges and CORS, which sharded stores
need and `python -m http.server` lacks) and returns an IPython iframe that points the hosted
viewer at it. The iframe loads the viewer from the internet and the viewer fetches the store from
your machine, so it only works where the browser showing the notebook can reach 127.0.0.1 on the
machine running the kernel: a local Jupyter or VS Code session, not JupyterHub or a remote kernel
without port forwarding. Recent Chrome versions ask permission before a public page may reach
local network addresses; allow it, or open the printed viewer URL in its own tab.
"""

from __future__ import annotations

import html
import importlib
import posixpath
import re
import threading
import urllib.parse
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, ClassVar

VIEWER_URL = "https://tileripper.com/tileripper/"
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range, If-Match, If-None-Match, Content-Type",
    "Access-Control-Expose-Headers": "Content-Range, Content-Length, ETag, Accept-Ranges",
    "Access-Control-Allow-Private-Network": "true",
    "Timing-Allow-Origin": "*",
    "Access-Control-Max-Age": "600",
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


class StoreRequestHandler(SimpleHTTPRequestHandler):
    """Serves one directory under `/<prefix>/` with Range, HEAD, OPTIONS and CORS. No listings."""

    # Added to every response. Subclasses may replace it to imitate other hosts.
    response_headers: ClassVar[dict[str, str]] = {**_CORS_HEADERS, "Cache-Control": "no-cache"}

    def __init__(self, *args: Any, root: Path, prefix: str, **kwargs: Any) -> None:
        self._root = root
        self._prefix = prefix
        self._remaining = 0
        super().__init__(*args, directory=str(root), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-request logging; a notebook does not want a line per chunk."""

    def end_headers(self) -> None:
        for name, value in self.response_headers.items():
            self.send_header(name, value)
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _file_for(self, raw_path: str) -> Path | None:
        path = urllib.parse.unquote(urllib.parse.urlsplit(raw_path).path)
        parts = [p for p in posixpath.normpath(path).split("/") if p]
        if not parts or parts[0] != self._prefix or ".." in parts:
            return None
        target = self._root.joinpath(*parts[1:])
        return target if target.is_file() else None

    def send_head(self) -> BinaryIO | None:
        target = self._file_for(self.path)
        if target is None:
            self.send_error(HTTPStatus.NOT_FOUND, "no such object in this store")
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
        self.send_header("Last-Modified", self.date_time_string(int(target.stat().st_mtime)))
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


class StoreServer:
    """A running local HTTP server for one store directory."""

    def __init__(self, store: Path, host: str = "127.0.0.1", port: int = 0) -> None:
        self.store = store
        handler = partial(StoreRequestHandler, root=store, prefix=store.name)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """Base URL of the store, suitable for `?store=` and `chronozarr.open_store`."""
        host = self._server.server_address[0]
        return f"http://{host!s}:{self.port}/{urllib.parse.quote(self.store.name)}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


_servers: dict[Path, StoreServer] = {}
_lock = threading.Lock()


def serve_store(store: str | Path, *, port: int = 0) -> StoreServer:
    """Serve a local store directory on 127.0.0.1 (free port by default).

    One server per store directory: a second call returns the running one. Stop it with
    `close()`; servers also die with the Python process.
    """
    path = Path(store).expanduser().resolve()
    if not (path / "zarr.json").is_file():
        raise FileNotFoundError(
            f"{path} is not a chronozarr store: no zarr.json found. "
            "Pass the store directory that contains zarr.json."
        )
    with _lock:
        running = _servers.get(path)
        if running is None:
            running = _servers[path] = StoreServer(path, port=port)
        return running


def viewer_url(store_url: str, viewer: str = VIEWER_URL) -> str:
    """URL that opens `store_url` in the viewer."""
    return f"{viewer}?store={urllib.parse.quote(store_url, safe='')}"


def view(store: str | Path, *, height: int = 640, viewer: str = VIEWER_URL, port: int = 0) -> Any:
    """Show a store in the TileRipper viewer as a notebook iframe.

    `store` is a local directory (served from 127.0.0.1, see the module docstring for when that
    works) or an http(s) URL of a store that is already hosted. Returns an
    `IPython.display.HTML`; leave it as a cell's last expression or pass it to `display`.
    """
    try:
        ipython_display = importlib.import_module("IPython.display")  # optional extra
    except ImportError as exc:
        raise ImportError(
            "chronozarr.view needs IPython: run `uv add 'chronozarr[notebook]'`"
        ) from exc
    text = str(store)
    store_url = (
        text if text.startswith(("http://", "https://")) else serve_store(text, port=port).url
    )
    target = viewer_url(store_url, viewer)
    frame = (
        f'<iframe src="{html.escape(target, quote=True)}" width="100%" height="{int(height)}" '
        'style="border:0" allow="fullscreen; local-network-access" loading="lazy"></iframe>'
    )
    caption = (
        '<div style="font:12px sans-serif;color:#666">Store: '
        f"{html.escape(store_url)} &middot; "
        f'<a href="{html.escape(target, quote=True)}" target="_blank" rel="noopener">'
        "open in a tab</a></div>"
    )
    return ipython_display.HTML(frame + caption)
