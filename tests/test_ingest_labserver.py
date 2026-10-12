"""Lab range server of the Sentinel-2 ingest benchmark (examples/sentinel2_pc/labserver.py)."""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

MODULE_PATH = Path(__file__).resolve().parents[1] / "examples" / "sentinel2_pc" / "labserver.py"
_spec = importlib.util.spec_from_file_location("labserver", MODULE_PATH)
assert _spec is not None and _spec.loader is not None
labserver = importlib.util.module_from_spec(_spec)
sys.modules["labserver"] = labserver
_spec.loader.exec_module(labserver)

PAYLOAD = bytes(range(256)) * 40  # 10240 bytes


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.tif").write_bytes(PAYLOAD)
    return tmp_path


class Lab:
    def __init__(self, root: Path, config) -> None:
        self.server, self.base_url, self.thread = labserver.start_lab_server(root, config)
        self.port = self.server.server_address[1]

    def request(self, method: str, path: str, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            conn.close()

    def stats(self) -> dict:
        status, _, body = self.request("GET", "/_stats")
        assert status == 200
        return json.loads(body)

    def stop(self) -> None:
        labserver.stop_lab_server(self.server, self.thread)


@pytest.fixture
def make_lab(root: Path) -> Iterator:
    labs: list[Lab] = []

    def make(**config) -> Lab:
        lab = Lab(root, labserver.LabConfig(**config))
        labs.append(lab)
        return lab

    yield make
    for lab in labs:
        lab.stop()


def test_full_get_and_head(make_lab) -> None:
    lab = make_lab()
    status, headers, body = lab.request("GET", "/sub/file.tif")
    assert status == 200
    assert body == PAYLOAD
    assert headers["Content-Length"] == str(len(PAYLOAD))
    assert headers["Accept-Ranges"] == "bytes"

    status, headers, body = lab.request("HEAD", "/sub/file.tif")
    assert status == 200
    assert body == b""
    assert headers["Content-Length"] == str(len(PAYLOAD))


def test_range_requests(make_lab) -> None:
    lab = make_lab()
    status, headers, body = lab.request("GET", "/sub/file.tif", {"Range": "bytes=10-19"})
    assert status == 206
    assert body == PAYLOAD[10:20]
    assert headers["Content-Range"] == f"bytes 10-19/{len(PAYLOAD)}"

    status, _, body = lab.request("GET", "/sub/file.tif", {"Range": "bytes=10000-"})
    assert status == 206
    assert body == PAYLOAD[10000:]

    status, _, body = lab.request("GET", "/sub/file.tif", {"Range": "bytes=-16"})
    assert status == 206
    assert body == PAYLOAD[-16:]

    # A last byte past the end is clipped to the file.
    status, headers, body = lab.request("GET", "/sub/file.tif", {"Range": "bytes=10200-99999"})
    assert status == 206
    assert body == PAYLOAD[10200:]
    assert headers["Content-Range"] == f"bytes 10200-10239/{len(PAYLOAD)}"


def test_unsatisfiable_range_is_416(make_lab) -> None:
    lab = make_lab()
    status, headers, body = lab.request("GET", "/sub/file.tif", {"Range": "bytes=20000-20010"})
    assert status == 416
    assert body == b""
    assert headers["Content-Range"] == f"bytes */{len(PAYLOAD)}"


def test_missing_file_and_path_escape_are_404(make_lab) -> None:
    lab = make_lab()
    assert lab.request("GET", "/sub/none.tif")[0] == 404
    assert lab.request("GET", "/sub/../../etc/hosts")[0] == 404
    assert lab.request("GET", "/%2e%2e/%2e%2e/etc/hosts")[0] == 404


def test_alias_paths_serve_the_same_file(make_lab) -> None:
    lab = make_lab()
    for alias in ("/a0/sub/file.tif", "/a17/sub/file.tif"):
        status, _, body = lab.request("GET", alias, {"Range": "bytes=0-3"})
        assert status == 206
        assert body == PAYLOAD[:4]


def test_keep_alive_serves_several_requests_on_one_connection(make_lab) -> None:
    lab = make_lab()
    conn = http.client.HTTPConnection("127.0.0.1", lab.port, timeout=10)
    try:
        for i in range(3):
            conn.request("GET", "/sub/file.tif", headers={"Range": f"bytes={i}-{i}"})
            response = conn.getresponse()
            assert response.status == 206
            assert response.read() == PAYLOAD[i : i + 1]
    finally:
        conn.close()


def test_counters_and_reset(make_lab) -> None:
    lab = make_lab()
    lab.request("GET", "/sub/file.tif", {"Range": "bytes=0-99"})
    lab.request("HEAD", "/sub/file.tif")
    lab.request("GET", "/sub/none.tif")
    stats = lab.stats()
    assert stats["requests"] == {"GET": 2, "HEAD": 1}
    assert stats["status"] == {"206": 1, "200": 1, "404": 1}
    assert stats["bytes_sent"] == 100

    status, _, _ = lab.request("POST", "/_reset")
    assert status == 200
    stats = lab.stats()
    assert stats["requests"] == {}
    assert stats["bytes_sent"] == 0


def test_fail_rate_one_returns_503_with_retry_after(make_lab) -> None:
    lab = make_lab(fail_rate=1.0)
    status, headers, body = lab.request("GET", "/sub/file.tif")
    assert status == 503
    assert headers["Retry-After"] == "1"
    assert body == b""
    assert lab.stats()["status"] == {"503": 1}


def test_fail_rate_is_seeded(make_lab) -> None:
    def outcomes(seed: int) -> list[int]:
        lab = make_lab(fail_rate=0.5, seed=seed)
        return [lab.request("HEAD", "/sub/file.tif")[0] for _ in range(20)]

    first, again, other = outcomes(1), outcomes(1), outcomes(2)
    assert first == again
    assert first != other
    assert set(first) == {200, 503}


def test_fail_after_starts_a_burst_that_ends(make_lab) -> None:
    lab = make_lab(fail_after=2, fail_for=0.5)
    assert lab.request("GET", "/sub/file.tif", {"Range": "bytes=0-1"})[0] == 206
    assert lab.request("GET", "/sub/file.tif", {"Range": "bytes=0-1"})[0] == 206
    assert lab.request("GET", "/sub/file.tif", {"Range": "bytes=0-1"})[0] == 503
    assert lab.request("HEAD", "/sub/file.tif")[0] == 503
    time.sleep(0.6)
    assert lab.request("GET", "/sub/file.tif", {"Range": "bytes=0-1"})[0] == 206
    assert lab.stats()["bursts"] == 1


def test_max_inflight_throttles_concurrent_requests(make_lab) -> None:
    lab = make_lab(latency_ms=300, max_inflight=1)
    statuses: list[int] = []

    def get() -> None:
        statuses.append(lab.request("GET", "/sub/file.tif", {"Range": "bytes=0-1"})[0])

    threads = [threading.Thread(target=get) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(statuses) == [206, 503]
    get()  # alone again: served
    assert statuses[-1] == 206


def test_latency_delays_each_response(make_lab) -> None:
    lab = make_lab(latency_ms=100)
    t0 = time.perf_counter()
    lab.request("HEAD", "/sub/file.tif")
    assert time.perf_counter() - t0 >= 0.1


def test_bandwidth_limit_is_shared_across_requests(make_lab) -> None:
    # 10240 bytes at 0.02 MB/s is 0.51 s for one response; two in a row cannot finish sooner.
    lab = make_lab(bandwidth_mbps=0.02)
    t0 = time.perf_counter()
    lab.request("GET", "/sub/file.tif")
    lab.request("GET", "/sub/file.tif")
    assert time.perf_counter() - t0 >= 0.9
