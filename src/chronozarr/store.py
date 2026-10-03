"""Store normalization and standard-library HTTP transport for readers and validation."""

from __future__ import annotations

import asyncio
import builtins
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterable
from typing import Any
from urllib.parse import quote

from zarr.abc.store import ByteRequest, OffsetByteRequest, RangeByteRequest, Store
from zarr.core.buffer import Buffer, BufferPrototype

# --- HTTP reading without fsspec --------------------------------------------------------------

_RETRY_DELAYS_S = (0.2, 0.6, 1.5)
# Some CDNs reject urllib's default "Python-urllib" agent with a 403.
_USER_AGENT = "chronozarr (+https://github.com/chronozarr/chronozarr)"


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
        self.url = url.rstrip("/")
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
        return f"HttpStore({self.url!r})"

    def _fetch(
        self, key: str, method: str, byte_range: ByteRequest | None = None
    ) -> tuple[bytes, Any] | None:
        """(body, headers) of one request, or None on 404. Retries transient failures."""
        url = f"{self.url}/{quote(key, safe='/')}"
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
                    raise OSError(f"{method} {url}: HTTP {code} {reason}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if attempt == len(_RETRY_DELAYS_S):
                    raise OSError(f"{method} {url}: {error}") from error
            time.sleep(_RETRY_DELAYS_S[attempt])
        raise AssertionError("unreachable")  # the loop returns or raises on its last attempt

    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        fetched = await asyncio.to_thread(self._fetch, key, "GET", byte_range)
        return None if fetched is None else prototype.buffer.from_bytes(fetched[0])

    async def get_partial_values(
        self, prototype: BufferPrototype, key_ranges: Iterable[tuple[str, ByteRequest | None]]
    ) -> builtins.list[Buffer | None]:
        return builtins.list(
            await asyncio.gather(*(self.get(k, prototype, r) for k, r in key_ranges))
        )

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._fetch, key, "HEAD") is not None

    async def getsize(self, key: str) -> int:
        fetched = await asyncio.to_thread(self._fetch, key, "HEAD")
        if fetched is None:
            raise FileNotFoundError(key)
        length = fetched[1].get("Content-Length")
        if length is None:
            raise OSError(f"HEAD {self.url}/{key}: no Content-Length header")
        return int(length)

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
