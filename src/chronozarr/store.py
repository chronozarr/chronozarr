"""Store normalization and standard-library HTTP transport for readers and validation."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from zarr.abc.store import (
    ByteRequest,
    OffsetByteRequest,
    RangeByteRequest,
    Store,
    SuffixByteRequest,
)
from zarr.core.buffer import Buffer, BufferPrototype, default_buffer_prototype
from zarr.storage import LocalStore, WrapperStore

# --- HTTP reading without fsspec --------------------------------------------------------------

_RETRY_DELAYS_S = (0.2, 0.6, 1.5)
# Some CDNs reject urllib's default "Python-urllib" agent with a 403.
_USER_AGENT = "chronozarr (+https://github.com/chronozarr/chronozarr)"
# Requests wait on the network, not the CPU, so they get threads of their own: asyncio's default
# pool has cores + 4 threads, which holds a 4-core machine to 8 requests at once.
REQUESTS_AT_ONCE = 32
_REQUESTS: ThreadPoolExecutor  # replaced in a forked child


def _start_request_threads() -> None:
    global _REQUESTS
    _REQUESTS = ThreadPoolExecutor(
        max_workers=REQUESTS_AT_ONCE, thread_name_prefix="chronozarr-http"
    )


_start_request_threads()
if hasattr(os, "register_at_fork"):  # POSIX
    # A forked child has none of the parent's threads, and a pool that counts them as idle never
    # starts new ones: the child's first request would wait forever.
    os.register_at_fork(after_in_child=_start_request_threads)


class HttpStatusError(OSError):
    """An HTTP error answer that retries did not clear. `status` is its code."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _base_url(url: str) -> str:
    """`url` minus trailing slashes on the path and any fragment; the query stays."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path.rstrip("/"), fragment=""))


def object_url(url: str, key: str) -> str:
    """URL of object `key` under store URL `url`: the key joins the path and the query is copied.

    A signed or token prefix (`https://host/store?token=abc`) keeps its query on every object,
    as the JS reader does: `https://host/store/zarr.json?token=abc`.
    """
    parts = urlsplit(_base_url(url))
    return urlunsplit(parts._replace(path=f"{parts.path}/{quote(key, safe='/')}"))


def redact_url(url: str) -> str:
    """`url` with its query replaced by `?<redacted>`, for messages. Local paths pass through."""
    if not url.startswith(("http://", "https://")):
        return url
    parts = urlsplit(url)
    if not parts.query:
        return url
    return urlunsplit(parts._replace(query="", fragment="")) + "?<redacted>"


def _range_header(byte_range: ByteRequest) -> str:
    if isinstance(byte_range, RangeByteRequest):
        return f"bytes={byte_range.start}-{byte_range.end - 1}"
    if isinstance(byte_range, OffsetByteRequest):
        return f"bytes={byte_range.offset}-"
    return f"bytes=-{byte_range.suffix}"


def _slice_body(body: bytes, byte_range: ByteRequest | None) -> bytes:
    """Apply a byte range locally, for a server that ignored the Range header."""
    if byte_range is None:
        return body
    if isinstance(byte_range, RangeByteRequest):
        return body[byte_range.start : byte_range.end]
    if isinstance(byte_range, OffsetByteRequest):
        return body[byte_range.offset :]
    return body[-byte_range.suffix :] if byte_range.suffix else b""


class HttpStore(Store):
    """A read-only Zarr store over HTTP(S), standard library only (no fsspec or aiohttp).

    Sharded arrays are read with `Range` requests, so the server must honour them (206). A
    missing object (404) is a missing key. 5xx, 429 and connection errors are retried three
    times; any other failure raises OSError naming the URL. The store cannot be listed.
    """

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        super().__init__(read_only=True)
        self.url = _base_url(url)
        self.timeout = timeout

    @property
    def supports_writes(self) -> bool:
        return False

    @property
    def supports_deletes(self) -> bool:
        return False

    @property
    def supports_listing(self) -> bool:
        return False

    def __eq__(self, value: object) -> bool:
        return isinstance(value, HttpStore) and value.url == self.url

    def __hash__(self) -> int:
        return hash(self.url)

    def __repr__(self) -> str:
        return f"HttpStore({redact_url(self.url)!r})"

    def _fetch(
        self, key: str, method: str, byte_range: ByteRequest | None = None
    ) -> tuple[bytes, Any] | None:
        """(body, headers) of one request, or None on 404. Retries transient failures."""
        url = object_url(self.url, key)
        shown = redact_url(url)
        headers = {"User-Agent": _USER_AGENT}
        if byte_range is not None:
            headers["Range"] = _range_header(byte_range)
        request = urllib.request.Request(url, method=method, headers=headers)
        for attempt in range(len(_RETRY_DELAYS_S) + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read() if method == "GET" else b""
                    if method == "GET" and response.status == 200:
                        body = _slice_body(body, byte_range)
                    return body, response.headers
            except urllib.error.HTTPError as error:
                code, reason = error.code, error.reason
                error.close()  # an HTTPError owns the response socket
                if code in (404, 416):
                    return None
                transient = code == 429 or code >= 500
                if not transient or attempt == len(_RETRY_DELAYS_S):
                    message = f"{method} {shown}: HTTP {code} {reason}"
                    raise HttpStatusError(message, code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if attempt == len(_RETRY_DELAYS_S):
                    raise OSError(f"{method} {shown}: {error}") from error
            time.sleep(_RETRY_DELAYS_S[attempt])
        raise AssertionError("unreachable")  # the loop returns or raises on its last attempt

    async def _request(
        self, key: str, method: str, byte_range: ByteRequest | None = None
    ) -> tuple[bytes, Any] | None:
        """`_fetch` on the request threads, up to `REQUESTS_AT_ONCE` at the same time."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_REQUESTS, self._fetch, key, method, byte_range)

    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        fetched = await self._request(key, "GET", byte_range)
        return None if fetched is None else prototype.buffer.from_bytes(fetched[0])

    async def get_partial_values(
        self, prototype: BufferPrototype, key_ranges: Iterable[tuple[str, ByteRequest | None]]
    ) -> builtins.list[Buffer | None]:
        return builtins.list(
            await asyncio.gather(*(self.get(k, prototype, r) for k, r in key_ranges))
        )

    async def exists(self, key: str) -> bool:
        return await self._request(key, "HEAD") is not None

    async def getsize(self, key: str) -> int:
        try:
            fetched = await self._request(key, "HEAD")
        except HttpStatusError as error:
            if error.status not in (403, 405):
                raise
            return await self._getsize_by_get(key)
        if fetched is None:
            raise FileNotFoundError(key)
        length = fetched[1].get("Content-Length")
        if length is None:
            raise OSError(
                f"HEAD {redact_url(object_url(self.url, key))}: no Content-Length header"
            )
        return int(length)

    async def _getsize_by_get(self, key: str) -> int:
        """The size from a one-byte GET, for a host that refuses HEAD (URLs presigned for GET).

        A 206 answer names the size in Content-Range (`bytes 0-0/12345`); a server that ignores
        Range answers 200 with the whole object, and its Content-Length is the size.
        """
        fetched = await self._request(key, "GET", RangeByteRequest(0, 1))
        if fetched is None:
            raise FileNotFoundError(key)
        headers = fetched[1]
        size = headers.get("Content-Range", "").rpartition("/")[2] or headers.get("Content-Length")
        if size is None or not size.isdigit():
            raise OSError(
                f"GET {redact_url(object_url(self.url, key))}: no size in Content-Range or "
                "Content-Length"
            )
        return int(size)

    async def set(self, key: str, value: Buffer) -> None:
        raise PermissionError("HttpStore is read-only")

    async def delete(self, key: str) -> None:
        raise PermissionError("HttpStore is read-only")

    async def list(self) -> AsyncIterator[str]:
        raise NotImplementedError("HttpStore cannot list keys")
        yield ""  # pragma: no cover - makes this an async generator

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        raise NotImplementedError("HttpStore cannot list keys")
        yield ""  # pragma: no cover

    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        raise NotImplementedError("HttpStore cannot list keys")
        yield ""  # pragma: no cover


def as_store(path_or_url: Any) -> Any:
    """A zarr store argument: http(s) URL strings become an HttpStore, anything else is kept."""
    if isinstance(path_or_url, str) and path_or_url.startswith(("http://", "https://")):
        return HttpStore(path_or_url)
    return path_or_url


class IndexStore(WrapperStore):
    """Cache shard index bytes, retaining freshness for the mutable trailing time shard.

    The upstream sharding codec owns index representation and decoding. This wrapper only
    caches its index byte requests and turns HTTP suffix reads into bounded ranges.
    """

    def __init__(self, store: Store) -> None:
        super().__init__(store)
        self.indices: dict[str, tuple[int, str, int, dict[str, int]]] = {}
        self.cache: dict[str, tuple[object, bytes]] = {}
        self.locks: dict[str, asyncio.Lock] = {}

    def configure(
        self,
        prefix: str,
        size: int,
        location: str,
        mutable_time_shard: int,
        lengths: dict[str, int],
    ) -> None:
        self.indices[prefix] = size, location, mutable_time_shard, lengths

    def invalidate_indices(self) -> None:
        self.cache.clear()

    async def getsize(self, key: str) -> int:
        # WrapperStore does not pass getsize on; zarr's fallback would download the object.
        return await self._store.getsize(key)

    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        config = next((v for prefix, v in self.indices.items() if key.startswith(prefix)), None)
        if config is None or byte_range is None:
            result = await self._store.get(key, prototype, byte_range)
            if result is not None and key.endswith("zarr.json"):
                metadata = json.loads(result.to_bytes())
                check_extensions(metadata)
                result = prototype.buffer.from_bytes(json.dumps(metadata).encode())
            return result
        size, location, mutable, lengths = config
        index_request = (
            location == "end"
            and isinstance(byte_range, SuffixByteRequest)
            and byte_range.suffix == size
        ) or (
            location == "start"
            and isinstance(byte_range, RangeByteRequest)
            and byte_range.start == 0
            and byte_range.end == size
        )
        if not index_request:
            return await self._store.get(key, prototype, byte_range)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            # The first key component below c/ is the time shard in both array layouts.
            time_shard = int(key.split("/c/", 1)[1].split("/", 1)[0])
            underlying = self._store
            while hasattr(underlying, "_store"):
                underlying = underlying._store
            length = lengths.get(key)
            token: object = None
            if time_shard == mutable:
                if isinstance(underlying, HttpStore):
                    head = await underlying._request(key, "HEAD")
                    if head is None:
                        self.cache.pop(key, None)
                        return None
                    headers = head[1]
                    length = int(headers["Content-Length"])
                    version = headers.get("ETag") or headers.get("Last-Modified")
                    token = (length, version) if version else object()
                elif isinstance(underlying, LocalStore):
                    try:
                        stat = (underlying.root / key).stat()
                    except FileNotFoundError:
                        self.cache.pop(key, None)
                        return None
                    length, token = stat.st_size, (stat.st_size, stat.st_mtime_ns)
                else:
                    # Without a version signal, refetch this mutable index rather than risk
                    # applying cached offsets to replacement bytes of the same length.
                    length, token = await self._store.getsize(key), object()
            cached = self.cache.get(key)
            if cached is not None and cached[0] == token:
                return prototype.buffer.from_bytes(cached[1])
            request = byte_range
            if isinstance(underlying, HttpStore) and location == "end":
                if length is None:
                    try:
                        length = await self._store.getsize(key)
                    except FileNotFoundError:
                        return None
                request = RangeByteRequest(length - size, length)
            result = await self._store.get(key, prototype, request)
            if result is not None:
                self.cache[key] = token, result.to_bytes()
            return result


class MetadataCache(WrapperStore):
    """Read each `zarr.json` once and serve it from memory after that, absent ones included.

    For validation, which opens a node from several checks: one read per node, and every check
    sees the same metadata. `prefetch` reads many nodes at the same time, so a remote store
    answers them in one round trip, not one each. Chunks are never cached.
    """

    def __init__(self, store: Store) -> None:
        super().__init__(store)
        self.metadata: dict[str, bytes | None] = {}

    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        if byte_range is not None or not key.endswith("zarr.json"):
            return await self._store.get(key, prototype, byte_range)
        if key not in self.metadata:
            result = await self._store.get(key, prototype)
            self.metadata[key] = None if result is None else result.to_bytes()
        cached = self.metadata[key]
        return None if cached is None else prototype.buffer.from_bytes(cached)

    async def getsize(self, key: str) -> int:
        return await self._store.getsize(key)  # WrapperStore does not pass getsize on

    async def prefetch(self, keys: Iterable[str]) -> None:
        missing = dict.fromkeys(key for key in keys if key not in self.metadata)
        await asyncio.gather(*(self.get(key, default_buffer_prototype()) for key in missing))


def check_extensions(metadata: dict[str, Any]) -> None:
    """Reject unknown required Zarr metadata extensions, including consolidated nodes."""
    group_keys = {"zarr_format", "node_type", "attributes", "consolidated_metadata"}
    array_keys = {
        "zarr_format",
        "node_type",
        "attributes",
        "shape",
        "data_type",
        "chunk_grid",
        "chunk_key_encoding",
        "fill_value",
        "codecs",
        "dimension_names",
        "storage_transformers",
    }
    if metadata.get("storage_transformers"):
        raise ValueError("unsupported storage_transformers")
    known = group_keys if metadata.get("node_type") == "group" else array_keys
    for name, value in list(metadata.items()):
        if name not in known and not (
            isinstance(value, dict) and value.get("must_understand") is False
        ):
            raise ValueError(f"unsupported mandatory Zarr extension {name!r}")
        if name not in known and metadata.get("node_type") == "group":
            metadata.pop(name)
    consolidated = metadata.get("consolidated_metadata", {})
    for node in consolidated.get("metadata", {}).values():
        check_extensions(node)
