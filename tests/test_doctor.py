"""`chronozarr doctor`, the HTTP store, and the local range server behind `chronozarr.view`."""

from __future__ import annotations

import asyncio
import importlib
import itertools
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
import zarr
from zarr.abc.store import OffsetByteRequest, RangeByteRequest, SuffixByteRequest
from zarr.core.buffer import default_buffer_prototype

import chronozarr
import chronozarr.doctor as doctor_module
import chronozarr.schema as schema_module
import chronozarr.store as store_module
from chronozarr.decode import HttpStore
from chronozarr.doctor import DEFAULT_ORIGIN, Check, diagnose
from chronozarr.schema import validate
from chronozarr.view import StoreRequestHandler, serve_store, view
from tests.round_trips import RoundTrips
from tests.synthetic import build_store, make_da, make_truth

pytestmark = pytest.mark.unit

# `chronozarr.view` is the notebook function once the package exports it; the module lives in
# sys.modules either way.
view_module = importlib.import_module("chronozarr.view")


def with_headers(name: str, **changes: str | None) -> type[StoreRequestHandler]:
    """A handler whose response headers are the defaults with `changes` applied (None removes)."""
    headers = dict(StoreRequestHandler.response_headers)
    for key, value in changes.items():
        header = key.replace("_", "-")
        headers.pop(header, None)
        if value is not None:
            headers[header] = value
    return type(name, (StoreRequestHandler,), {"response_headers": headers})


NoCorsHandler = type("NoCorsHandler", (StoreRequestHandler,), {"response_headers": {}})
NoExposeHandler = with_headers("NoExposeHandler", Access_Control_Expose_Headers=None)
NoTimingHandler = with_headers("NoTimingHandler", Timing_Allow_Origin=None)
NoRangePreflightHandler = with_headers(
    "NoRangePreflightHandler", Access_Control_Allow_Headers=None
)
ImmutableHandler = with_headers(
    "ImmutableHandler",
    Cache_Control="public, max-age=31536000, immutable",
    CF_Cache_Status="DYNAMIC",
)


class IgnoreRangeHandler(StoreRequestHandler):
    def send_head(self):
        del self.headers["Range"]
        return super().send_head()


class FlakyHandler(StoreRequestHandler):
    """Answers 503 to the first request for every distinct key."""

    seen: set[str]

    def send_head(self):
        if self.path not in self.seen:
            self.seen.add(self.path)
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return None
        return super().send_head()


class NoHeadHandler(StoreRequestHandler):
    """Refuses HEAD, as a host behind URLs presigned for GET does. Records each GET's Range."""

    ranges: ClassVar[list[str | None]]

    def do_HEAD(self):
        self.send_error(HTTPStatus.FORBIDDEN)

    def do_GET(self):
        self.ranges.append(self.headers.get("Range"))
        super().do_GET()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128  # the default 5 drops connections from 32 requests at once


@contextmanager
def serving(handler: type[StoreRequestHandler], root: Path) -> Iterator[str]:
    server = LocalServer(("127.0.0.1", 0), partial(handler, root=root, prefix=root.name))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/{root.name}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def sharded_store(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("doctor") / "sharded"
    build_store(path, make_truth(5, 2, 40, 50), shard=True, chunk_size=16)
    return path


@pytest.fixture(scope="module")
def unsharded_store(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("doctor") / "unsharded"
    build_store(path, make_truth(5, 2, 40, 50), shard=False, chunk_size=16)
    return path


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == "fail"]


# --- diagnose -------------------------------------------------------------------------------


def test_local_store_passes_and_full_reads_match(sharded_store):
    checks = diagnose(str(sharded_store))
    assert not failures(checks), failures(checks)
    levels = [c for c in checks if c.name.startswith("decode level")]
    assert len(levels) >= 2
    assert all("full read" in c.detail and "matches" in c.detail for c in levels)
    assert all("plain-Zarr pixel at t=0 matches" in c.detail for c in levels)


def test_local_unsharded_store_passes(unsharded_store):
    assert not failures(diagnose(str(unsharded_store)))


def test_full_read_limit_skips_large_levels(sharded_store):
    checks = diagnose(str(sharded_store), full_read_limit_mb=0.0)
    level0 = by_name(checks)["decode level 0"]
    assert level0.status == "ok"
    assert "full read skipped" in level0.detail


def test_http_store_passes_browser_checks(sharded_store):
    with serving(StoreRequestHandler, sharded_store) as url:
        checks = by_name(diagnose(url))
    failed = [c for c in checks.values() if c.status == "fail"]
    assert not failed, failed
    for name in ("root zarr.json", "CORS on zarr.json", "suffix range", "CORS preflight", "HEAD"):
        assert checks[name].status == "ok", checks[name]
    range_check = next(c for n, c in checks.items() if n.startswith("byte range"))
    assert range_check.status == "ok"
    assert "206" in range_check.detail
    assert "sharded" in range_check.detail
    assert checks["edge cache"].status == "info"
    assert checks["timing-allow-origin"].status == "info"
    assert checks["timing-allow-origin"].detail == "*"
    assert checks["cache-control"].status == "info"  # no-cache, and the prefix is unversioned
    assert checks["decode level 0"].status == "ok"


def test_doctor_reports_version_rejection_with_conversion_guidance(sharded_store, tmp_path):
    old = tmp_path / "old"
    shutil.copytree(sharded_store, old)
    document = json.loads((old / "zarr.json").read_text())
    document["attributes"]["chronozarr"]["spec_version"] = "0.2.0"
    (old / "zarr.json").write_text(json.dumps(document))
    with serving(StoreRequestHandler, old) as url:
        checks = diagnose(url)
    assert any("chronozarr convert" in c.detail for c in failures(checks))


def test_missing_content_range_exposure_fails(sharded_store):
    with serving(NoExposeHandler, sharded_store) as url:
        checks = by_name(diagnose(url))
    assert checks["CORS expose Content-Range"].status == "fail"
    assert "Access-Control-Expose-Headers" in checks["CORS expose Content-Range"].hint


def test_cache_observations_are_never_failures(tmp_path):
    versioned = tmp_path / "chronozarr-2"
    build_store(versioned, make_truth(3, 2, 20, 20), shard=True, chunk_size=16)
    with serving(StoreRequestHandler, versioned) as url:
        advice = by_name(diagnose(url))
    assert advice["cache-control"].status == "warn"
    assert "versioned" in advice["cache-control"].detail
    assert "immutable" in advice["cache-control"].hint
    with serving(ImmutableHandler, versioned) as url:
        checks = by_name(diagnose(url))
    assert checks["cache-control"].status == "ok"
    assert checks["edge cache"].status == "info"
    edge = checks["edge cache"].detail
    assert "not caching (cf-cache-status DYNAMIC then DYNAMIC)" in edge
    assert "reads go to the bucket on every request" in edge
    assert "Field Hostname equals 127.0.0.1" in edge
    assert "Edge TTL override" in edge
    assert not failures(list(checks.values()))


def test_timing_allow_origin_absence_is_advice(sharded_store):
    with serving(StoreRequestHandler, sharded_store) as url:
        base = diagnose(url)
    assert by_name(base)["timing-allow-origin"].detail == "*"
    with serving(NoTimingHandler, sharded_store) as url:
        checks = by_name(diagnose(url))
    assert checks["timing-allow-origin"].status == "info"
    assert "transfer sizes read as 0" in checks["timing-allow-origin"].detail


def test_origin_is_sent_and_reported(sharded_store):
    with serving(StoreRequestHandler, sharded_store) as url:
        checks = by_name(diagnose(url, origin="https://example.org"))
    assert "https://example.org" in checks["CORS on zarr.json"].detail


def test_missing_cors_headers_fail_with_a_fix(sharded_store):
    with serving(NoCorsHandler, sharded_store) as url:
        checks = diagnose(url)
    cors = [c for c in failures(checks) if c.name.startswith("CORS on")]
    assert {c.name for c in cors} >= {"CORS on zarr.json"}
    assert "Access-Control-Allow-Origin" in cors[0].hint
    assert any(c.name == "CORS preflight" and c.status == "warn" for c in checks)


def test_ignored_range_fails_a_sharded_store(sharded_store):
    with serving(IgnoreRangeHandler, sharded_store) as url:
        checks = diagnose(url)
    bad = [c for c in failures(checks) if c.name.startswith("byte range")]
    assert len(bad) == 1
    assert "ignored the Range header" in bad[0].detail
    assert "Sharded stores need it" in bad[0].hint
    assert any(c.name == "suffix range" and c.status == "fail" for c in checks)


def test_ignored_range_only_warns_for_an_unsharded_store(unsharded_store):
    with serving(IgnoreRangeHandler, unsharded_store) as url:
        checks = diagnose(url)
    ranged = next(c for c in checks if c.name.startswith("byte range"))
    assert ranged.status == "warn"
    assert not failures(checks), failures(checks)
    suffix = by_name(checks)["suffix range"]
    assert suffix.status == "info"
    assert "not used by an unsharded store" in suffix.detail


def test_http_unsharded_store_passes_and_the_suffix_range_is_an_info_line(unsharded_store):
    with serving(StoreRequestHandler, unsharded_store) as url:
        checks = by_name(diagnose(url))
    assert not [c for c in checks.values() if c.status == "fail"], checks
    range_check = next(c for n, c in checks.items() if n.startswith("byte range"))
    assert range_check.status == "ok"
    assert "unsharded store" in range_check.detail
    assert checks["suffix range"].status == "info"
    assert checks["suffix range"].detail.startswith("206, Content-Range bytes ")
    assert "no shard index" in checks["suffix range"].detail
    levels = [c for n, c in checks.items() if n.startswith("decode level")]
    assert len(levels) >= 2
    assert all(c.status == "ok" for c in levels), levels
    assert all("plain-Zarr pixel at t=0 matches" in c.detail for c in levels)


def test_unsharded_store_does_not_need_the_range_header_allowed(unsharded_store):
    with serving(NoRangePreflightHandler, unsharded_store) as url:
        checks = by_name(diagnose(url))
    assert checks["CORS preflight"].status == "info"
    assert not [c for c in checks.values() if c.status == "fail"]


def test_sharded_store_warns_when_the_range_header_is_not_allowed(sharded_store):
    with serving(NoRangePreflightHandler, sharded_store) as url:
        checks = by_name(diagnose(url))
    assert checks["CORS preflight"].status == "warn"


def test_missing_root_group_fails_early(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with serving(StoreRequestHandler, empty) as url:
        checks = diagnose(url)
    assert [c.name for c in checks] == ["root zarr.json"]
    assert checks[0].status == "fail"
    assert "404" in checks[0].detail


def test_unreachable_host_fails_with_network_hint():
    checks = diagnose("http://127.0.0.1:9/nothing")
    assert len(checks) == 1
    assert checks[0].status == "fail"
    assert "failed" in checks[0].detail
    assert "reachable" in checks[0].hint


def test_local_path_that_is_not_a_directory_fails(tmp_path):
    checks = diagnose(str(tmp_path / "nope"))
    assert checks[0].status == "fail"
    assert "not a directory" in checks[0].detail


def test_corrupt_chunk_data_is_reported_per_level(tmp_path):
    path = tmp_path / "corrupt"
    build_store(path, make_truth(5, 2, 40, 50), shard=True, chunk_size=16)
    shard = sorted((path / "0" / "data" / "c").rglob("*"))
    target = next(p for p in shard if p.is_file())
    blob = bytearray(target.read_bytes())
    blob[: len(blob) - 64] = bytes(len(blob) - 64)
    target.write_bytes(bytes(blob))
    checks = by_name(diagnose(str(path)))
    assert checks["validate"].status == "ok"
    assert any(c.status == "fail" for n, c in checks.items() if n.startswith("decode level"))


def test_layout_problems_fail_validate(tmp_path):
    path = tmp_path / "broken"
    build_store(path, make_truth(3, 2, 20, 20), shard=True, chunk_size=16)
    (path / "0" / "time" / "c" / "0").write_bytes(b"")
    checks = diagnose(str(path))
    assert any(c.name == "validate" and c.status == "fail" for c in checks)


# --- round trips over a slow link -------------------------------------------------------------


@pytest.fixture(scope="module")
def masked_store(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("doctor") / "masked"
    mask = np.ones((4, 40, 50), dtype=np.uint8)
    mask[:, :5, :5] = 0
    build_store(path, make_truth(4, 1, 40, 50), shard=False, chunk_size=16, mask=mask)
    return path


def test_http_checks_take_three_round_trips(unsharded_store, monkeypatch):
    with serving(StoreRequestHandler, unsharded_store) as url:
        trips = RoundTrips(monkeypatch)
        checks, document = doctor_module._http_checks(url, DEFAULT_ORIGIN)
    assert document is not None
    assert not failures(checks), failures(checks)
    # The root names the data array; then the array metadata and the ranged read; then the
    # suffix range, the preflight and HEAD on the object the ranged read found.
    assert len(trips.requests) == 6
    assert trips.rounds() == 3, trips.paths()


def test_a_local_copy_is_validated_from_disk_and_the_levels_decode_together(
    masked_store, monkeypatch
):
    with serving(StoreRequestHandler, masked_store) as url:
        trips = RoundTrips(monkeypatch)
        checks = by_name(diagnose(url, full_read_limit_mb=0, local_copy=masked_store))
    assert not failures(list(checks.values())), failures(list(checks.values()))
    assert checks["validate"].status == "ok"
    assert str(masked_store) in checks["validate"].detail
    assert len([name for name in checks if name.startswith("decode level")]) == 3
    # Validation read every mask chunk and every coordinate array; none of them crossed HTTP.
    layout = [p for p in trips.paths() if re.search(r"/(mask|time|band|x|y)/", p)]
    assert not layout, layout
    # Three for the HTTP checks, one to open the store, then one level's four reads (two cells,
    # then the plain-Zarr array metadata and pixel) with every level read at the same time.
    assert trips.rounds() <= 8, trips.paths()


def test_layout_problems_in_the_local_copy_fail_validate(unsharded_store, tmp_path):
    copy = tmp_path / "copy"
    shutil.copytree(unsharded_store, copy)
    (copy / "0" / "time" / "c" / "0").write_bytes(b"")
    with serving(StoreRequestHandler, unsharded_store) as url:
        checks = by_name(diagnose(url, local_copy=copy))
    assert checks["validate"].status == "fail"
    assert not any(name.startswith("decode level") for name in checks)


def test_a_local_copy_does_not_excuse_a_failing_http_check(unsharded_store):
    with serving(NoCorsHandler, unsharded_store) as url:
        checks = by_name(diagnose(url, full_read_limit_mb=0, local_copy=unsharded_store))
    assert checks["validate"].status == "ok"
    assert checks["CORS on zarr.json"].status == "fail"


def test_a_local_copy_does_not_excuse_a_chunk_the_url_serves_corrupt(unsharded_store, tmp_path):
    served = tmp_path / "served"
    shutil.copytree(unsharded_store, served)
    for chunk in (served / "0" / "data" / "c").rglob("*"):
        if chunk.is_file():
            chunk.write_bytes(bytes(chunk.stat().st_size))
    with serving(StoreRequestHandler, served) as url:
        checks = by_name(diagnose(url, full_read_limit_mb=0, local_copy=unsharded_store))
    assert checks["validate"].status == "ok"  # the copy on disk is sound
    assert checks["decode level 0"].status == "fail"


# A doctor run whose server holds every request for `pattern` for a minute.
_HANGING_DOCTOR = """
import re, signal, sys, threading, time
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

from chronozarr.doctor import diagnose
from chronozarr.view import StoreRequestHandler

signal.signal(signal.SIGINT, signal.default_int_handler)
store, pattern = Path(sys.argv[1]), re.compile(sys.argv[2])


class Hang(StoreRequestHandler):
    def send_head(self):
        if pattern.search(self.path):
            print("hung", flush=True)
            time.sleep(60)
        return super().send_head()


class Quiet(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # a client that went away mid-request is the point of this server


server = Quiet(("127.0.0.1", 0), partial(Hang, root=store, prefix=store.name))
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    diagnose(f"http://127.0.0.1:{server.server_address[1]}/{store.name}", local_copy=store)
except KeyboardInterrupt:
    print("interrupted", flush=True)
    raise
print("finished", flush=True)
"""


@pytest.mark.parametrize(
    ("hang", "exits"),
    [("/0/data/zarr.json$", True), ("/1/data/c/", False)],
    ids=["HTTP checks", "level decode"],
)
def test_ctrl_c_during_a_hung_check_stops_doctor_at_once(masked_store, hang, exits):
    # share stops the tunnel and the server when doctor raises, so a check must not hold
    # Ctrl-C until its request ends. After an HTTP check the process also exits at once; a
    # level read leaves zarr's own reader thread, which the process waits for, as before.
    child = subprocess.Popen(
        [sys.executable, "-c", _HANGING_DOCTOR, str(masked_store), hang],
        stdout=subprocess.PIPE,
        text=True,
    )
    stdout = child.stdout
    assert stdout is not None
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        for line in stdout:
            lines.put(line)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert lines.get(timeout=60) == "hung\n"
        child.send_signal(signal.SIGINT)
        assert lines.get(timeout=5) == "interrupted\n"
        if exits:
            assert child.wait(timeout=5) == -signal.SIGINT  # not held by a thread, not a crash
    finally:
        child.kill()
        child.wait()
        reader.join(timeout=5)
        stdout.close()


# --- validate over HTTP: `chronozarr doctor <url>` and `chronozarr validate <url>` -----------


@pytest.fixture(scope="module")
def sharded_masked_store(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("doctor") / "sharded-masked"
    mask = np.ones((4, 40, 50), dtype=np.uint8)
    mask[:, :5, :5] = 0
    build_store(path, make_truth(4, 1, 40, 50), shard=True, shard_time=2, chunk_size=16, mask=mask)
    return path


def object_paths(store: Path, variable: str) -> set[str]:
    """URL path of every chunk object (shard, when sharded) of `variable`, stored or not."""
    paths: set[str] = set()
    for level in zarr.open_group(store, mode="r").group_keys():
        array = zarr.open_array(store / level / variable, mode="r")
        extent = array.shards or array.chunks
        grid = (range(math.ceil(n / e)) for n, e in zip(array.shape, extent, strict=True))
        paths |= {
            f"/{store.name}/{level}/{variable}/c/{'/'.join(map(str, index))}"
            for index in itertools.product(*grid)
        }
    return paths


def test_remote_validate_reads_each_metadata_object_once(unsharded_store, monkeypatch):
    with serving(StoreRequestHandler, unsharded_store) as url:
        trips = RoundTrips(monkeypatch)
        problems = validate(url)
    assert problems == []
    reads = Counter(path for path in trips.paths() if path.endswith("zarr.json"))
    assert max(reads.values()) == 1, {path: n for path, n in reads.items() if n > 1}
    # The root; every node it names, at once; each level's coordinates together (three levels);
    # the volatility array.
    assert trips.rounds() <= 6, trips.paths()


def test_remote_validate_reads_every_mask_chunk_once_and_many_at_a_time(masked_store, monkeypatch):
    with serving(StoreRequestHandler, masked_store) as url:
        trips = RoundTrips(monkeypatch)
        problems = validate(url)
    assert problems == []
    reads = Counter(path for path in trips.paths() if "/mask/c/" in path)
    assert set(reads) == object_paths(masked_store, "mask")  # the check still reads every chunk
    assert max(reads.values()) == 1
    # Six as for an unmasked store, plus the mask: level 0's 48 chunks in two rounds of up to 32
    # requests, then 16 and 4 chunks in one round each.
    assert trips.rounds() <= 10, trips.paths()


def test_remote_validate_reads_each_mask_shard_once(sharded_masked_store, monkeypatch):
    with serving(StoreRequestHandler, sharded_masked_store) as url:
        trips = RoundTrips(monkeypatch)
        problems = validate(url)
    assert problems == []
    reads = Counter(path for path in trips.paths() if "/mask/c/" in path)
    assert set(reads) == object_paths(sharded_masked_store, "mask")
    # A batch spans whole time shards, so each shard is read whole: no index read, and no read
    # per timestep.
    assert max(reads.values()) == 1, reads


def test_remote_validate_holds_no_more_mask_than_the_budget(masked_store, monkeypatch):
    monkeypatch.setattr(schema_module, "_MASK_BYTES", 16 * 16)  # one mask chunk
    with serving(StoreRequestHandler, masked_store) as url:
        trips = RoundTrips(monkeypatch)
        problems = validate(url)
    assert problems == []
    mask = [(started, ended) for started, ended, _, path in trips.requests if "/mask/c/" in path]
    assert len(mask) == 68
    overlapping = [
        (a, b) for a, b in itertools.combinations(mask, 2) if a[0] < b[1] and b[0] < a[1]
    ]
    assert not overlapping  # two mask chunks were never read at the same time


def test_remote_validate_stops_reading_a_mask_at_the_first_bad_chunk(
    masked_store, tmp_path, monkeypatch
):
    copy = tmp_path / masked_store.name
    shutil.copytree(masked_store, copy)
    plane = zarr.open_array(copy / "0" / "mask", mode="r+")
    plane[0, 0, 0] = 2  # in the first chunk read
    monkeypatch.setattr(schema_module, "_MASK_BYTES", 16 * 16)  # one chunk at a time
    with serving(StoreRequestHandler, copy) as url:
        trips = RoundTrips(monkeypatch)
        problems = validate(url)
    assert problems == ["level 0/mask: mask must contain only 0 and 1"]
    level_0 = [path for path in trips.paths() if f"/{copy.name}/0/mask/c/" in path]
    # The bad chunk, and at most the next one, already under way when the bad one was found.
    assert len(level_0) <= 2, level_0


@pytest.mark.parametrize("remote", [False, True], ids=["local", "http"])
def test_a_mask_value_other_than_0_or_1_fails_validate(masked_store, tmp_path, remote):
    copy = tmp_path / masked_store.name
    shutil.copytree(masked_store, copy)
    plane = zarr.open_array(copy / "0" / "mask", mode="r+")
    plane[3, 39, 49] = 2  # the last timestep, last chunk
    if remote:
        with serving(StoreRequestHandler, copy) as url:
            problems = validate(url)
    else:
        problems = validate(copy)
    assert problems == ["level 0/mask: mask must contain only 0 and 1"]


def test_remote_validate_sizes_shards_with_head_requests(sharded_store, monkeypatch):
    with serving(StoreRequestHandler, sharded_store) as url:
        trips = RoundTrips(monkeypatch)
        problems = validate(url)
    assert problems == []
    shards = [(method, path) for _, _, method, path in trips.requests if "/data/c/" in path]
    assert {path for _, path in shards} == object_paths(sharded_store, "data")
    assert {method for method, _ in shards} == {"HEAD"}  # shard_bytes: no shard downloaded
    # Six as for an unsharded store, plus every shard's HEAD at once.
    assert trips.rounds() <= 7, trips.paths()


def test_doctor_on_a_masked_store_over_http_takes_eighteen_round_trips(masked_store, monkeypatch):
    with serving(StoreRequestHandler, masked_store) as url:
        trips = RoundTrips(monkeypatch)
        checks = diagnose(url, full_read_limit_mb=0)
    assert not failures(checks), failures(checks)
    # Three for the HTTP checks, ten to validate, one to open the store, four to decode.
    assert trips.rounds() <= 18, trips.paths()


def test_remote_validate_sizes_shards_with_one_byte_reads_when_the_host_refuses_head(
    sharded_store, monkeypatch
):
    handler = type("NoHead", (NoHeadHandler,), {"ranges": []})
    with serving(handler, sharded_store) as url:
        trips = RoundTrips(monkeypatch)
        problems = validate(url)
    assert problems == []
    shards = [(method, path) for _, _, method, path in trips.requests if "/data/c/" in path]
    assert sorted(path for method, path in shards if method == "GET") == sorted(
        object_paths(sharded_store, "data")
    )
    assert handler.ranges.count("bytes=0-0") == len(object_paths(sharded_store, "data"))


# --- chronozarr.decode.HttpStore against the local range server -----------------------------


def test_http_store_reads_ranges_and_reports_missing_keys(sharded_store):
    async def read(store: HttpStore, key: str, rng=None):
        return await store.get(key, default_buffer_prototype(), rng)

    with serving(StoreRequestHandler, sharded_store) as url:
        store = HttpStore(url)
        whole = (sharded_store / "zarr.json").read_bytes()
        assert asyncio.run(read(store, "zarr.json")).to_bytes() == whole
        assert (
            asyncio.run(read(store, "zarr.json", RangeByteRequest(3, 10))).to_bytes()
            == whole[3:10]
        )
        assert (
            asyncio.run(read(store, "zarr.json", OffsetByteRequest(20))).to_bytes() == whole[20:]
        )
        assert asyncio.run(read(store, "zarr.json", SuffixByteRequest(7))).to_bytes() == whole[-7:]
        assert asyncio.run(read(store, "no/such/key")) is None
        assert asyncio.run(store.exists("zarr.json")) is True
        assert asyncio.run(store.exists("no/such/key")) is False


def test_http_store_sizes_an_object_when_the_host_refuses_head(sharded_store):
    key = "0/data/c/0/0/0/0"
    handler = type("NoHead", (NoHeadHandler,), {"ranges": []})
    with serving(handler, sharded_store) as url:
        store = HttpStore(url)
        assert asyncio.run(store.getsize(key)) == (sharded_store / key).stat().st_size
        with pytest.raises(FileNotFoundError):
            asyncio.run(store.getsize("no/such/key"))
    assert handler.ranges == ["bytes=0-0", "bytes=0-0"]


@pytest.mark.parametrize("status", [HTTPStatus.FORBIDDEN, HTTPStatus.METHOD_NOT_ALLOWED])
def test_partial_shards_and_planes_read_without_head_or_version_headers(tmp_path, status):
    class GetOnly(NoHeadHandler):
        ranges: ClassVar[list[str | None]] = []

        def do_HEAD(self):
            self.send_error(status)

        def send_header(self, keyword, value):
            if keyword.lower() not in {"etag", "last-modified"}:
                super().send_header(keyword, value)

    truth = make_truth(4, 1, 20, 22)
    data = make_da(truth)
    mask = np.ones((4, 20, 22), dtype=np.uint8)
    mask[:, 0, 0] = 0
    coverage = mask * 3
    path = tmp_path / "published"
    chronozarr.encode(
        data.isel(time=slice(0, 3)),
        path,
        chunk_size=16,
        n_lods=1,
        shard=True,
        shard_time=2,
        mask=mask[:3],
        coverage=coverage[:3],
    )
    with serving(GetOnly, path) as url:
        assert validate(url) == []
        old = chronozarr.open_store(url)
        np.testing.assert_array_equal(old.read(0), truth[0])
        np.testing.assert_array_equal(old.read(2), truth[2])
        np.testing.assert_array_equal(old.read_mask(2), mask[2])
        np.testing.assert_array_equal(old.read_coverage(2), coverage[2])
        probes = GetOnly.ranges.count("bytes=0-0")
        np.testing.assert_array_equal(old.read(2), truth[2])
        # No version header can establish index freshness, even at an unchanged length.
        assert GetOnly.ranges.count("bytes=0-0") > probes

        staging = tmp_path / "staging"
        shutil.copytree(path, staging)
        chronozarr.append(
            staging, data.isel(time=slice(3, 4)), mask=mask[3:], coverage=coverage[3:]
        )
        for source in sorted(staging.rglob("*"), key=lambda p: p == staging / "zarr.json"):
            if source.is_file():
                target = path / source.relative_to(staging)
                target.parent.mkdir(parents=True, exist_ok=True)
                upload = target.with_name(target.name + ".upload")
                shutil.copyfile(source, upload)
                os.replace(upload, target)
        np.testing.assert_array_equal(old.read(2), truth[2])
        assert len(old.times) == 3
        fresh = chronozarr.open_store(url)
        np.testing.assert_array_equal(fresh.read(3), truth[3])
        np.testing.assert_array_equal(fresh.read_mask(3), mask[3])
        np.testing.assert_array_equal(fresh.read_coverage(3), coverage[3])


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is POSIX only")
def test_http_store_reads_in_a_process_forked_after_a_read(sharded_store):
    async def read(store: HttpStore):
        return await store.get("zarr.json", default_buffer_prototype())

    with serving(StoreRequestHandler, sharded_store) as url:
        store = HttpStore(url)
        assert asyncio.run(read(store)) is not None  # the request threads exist now
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork with threads running
            pid = os.fork()
        if pid == 0:  # the child exits 0 only if its read returns
            code = 1
            try:
                code = 0 if asyncio.run(read(store)) is not None else 1
            finally:
                os._exit(code)
        deadline = time.monotonic() + 10
        while (waited := os.waitpid(pid, os.WNOHANG))[0] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        if waited[0] == 0:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail("the forked process's read hung")
    assert os.waitstatus_to_exitcode(waited[1]) == 0


def test_http_store_slices_locally_when_the_server_ignores_range(sharded_store):
    with serving(IgnoreRangeHandler, sharded_store) as url:
        store = HttpStore(url)
        got = asyncio.run(
            store.get("zarr.json", default_buffer_prototype(), RangeByteRequest(2, 9))
        )
    assert got is not None
    assert got.to_bytes() == (sharded_store / "zarr.json").read_bytes()[2:9]


def test_http_store_retries_transient_server_errors(sharded_store, monkeypatch):
    monkeypatch.setattr(store_module, "_RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    handler = type("Flaky", (FlakyHandler,), {"seen": set()})
    with serving(handler, sharded_store) as url:
        store = HttpStore(url)
        got = asyncio.run(store.get("zarr.json", default_buffer_prototype()))
    assert got is not None
    assert got.to_bytes() == (sharded_store / "zarr.json").read_bytes()


def test_http_store_names_the_url_when_the_host_is_unreachable(monkeypatch):
    monkeypatch.setattr(store_module, "_RETRY_DELAYS_S", (0.0,))
    store = HttpStore("http://127.0.0.1:9/x")
    with pytest.raises(OSError, match=r"GET http://127\.0\.0\.1:9/x/zarr\.json"):
        asyncio.run(store.get("zarr.json", default_buffer_prototype()))


def test_chronozarr_opens_over_http_with_the_standard_library_store(sharded_store):
    truth = chronozarr.open_store(str(sharded_store)).read(3)
    with serving(StoreRequestHandler, sharded_store) as url:
        remote = chronozarr.open_store(HttpStore(url))
        assert np.array_equal(remote.read(3), truth)


# --- local range server (chronozarr.view) ---------------------------------------------------


def _get(url: str, headers: dict[str, str] | None = None, method: str = "GET"):
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, dict(exc.headers.items()), exc.read()


def test_range_server_semantics(sharded_store):
    blob = (sharded_store / "zarr.json").read_bytes()
    size = len(blob)
    with serving(StoreRequestHandler, sharded_store) as url:
        key = f"{url}/zarr.json"
        status, headers, body = _get(key)
        assert (status, body) == (200, blob)
        assert headers["Accept-Ranges"] == "bytes"
        assert headers["Access-Control-Allow-Origin"] == "*"

        status, headers, body = _get(key, {"Range": "bytes=2-9"})
        assert (status, body) == (206, blob[2:10])
        assert headers["Content-Range"] == f"bytes 2-9/{size}"

        status, headers, body = _get(key, {"Range": "bytes=-5"})
        assert (status, body) == (206, blob[-5:])
        assert headers["Content-Range"] == f"bytes {size - 5}-{size - 1}/{size}"

        status, _, body = _get(key, {"Range": f"bytes={size - 3}-"})
        assert (status, body) == (206, blob[-3:])

        status, _, body = _get(key, {"Range": f"bytes=0-{size * 10}"})
        assert (status, body) == (206, blob)

        status, headers, _ = _get(key, {"Range": f"bytes={size}-"})
        assert status == 416
        assert headers["Content-Range"] == f"bytes */{size}"

        status, headers, body = _get(key, method="HEAD")
        assert (status, body) == (200, b"")
        assert headers["Content-Length"] == str(size)

        status, headers, _ = _get(
            key,
            {
                "Origin": "https://chronozarr.org",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "range",
            },
            method="OPTIONS",
        )
        assert status == 204
        assert "Range" in headers["Access-Control-Allow-Headers"]
        assert headers["Access-Control-Allow-Private-Network"] == "true"


def test_range_server_only_serves_files_under_its_prefix(sharded_store):
    with serving(StoreRequestHandler, sharded_store) as url:
        base = url.rsplit("/", 1)[0]
        assert _get(f"{base}/zarr.json")[0] == 404
        assert _get(f"{url}/0")[0] == 404  # a directory, not a file
        assert _get(f"{url}/nope.json")[0] == 404
        assert _get(f"{url}/../{sharded_store.name}/zarr.json")[0] in (200, 404)
        outside = sharded_store.parent / "secret.txt"
        outside.write_text("secret")
        assert _get(f"{url}/%2e%2e/secret.txt")[0] == 404


def test_serve_store_reuses_the_server_and_rejects_non_stores(sharded_store, tmp_path):
    first = serve_store(sharded_store)
    try:
        assert serve_store(sharded_store) is first
        assert first.url.startswith("http://127.0.0.1:")
        assert first.url.endswith(f"/{sharded_store.name}")
        assert _get(f"{first.url}/zarr.json")[0] == 200
    finally:
        first.close()
        view_module._servers.clear()
    with pytest.raises(FileNotFoundError, match=r"no zarr\.json"):
        serve_store(tmp_path)


def test_serve_store_restarts_after_close(sharded_store):
    first = serve_store(sharded_store)
    try:
        assert _get(f"{first.url}/zarr.json")[0] == 200
    finally:
        first.close()

    restarted = serve_store(sharded_store, port=first.port)
    try:
        assert restarted is not first
        assert restarted.port == first.port
        assert _get(f"{restarted.url}/zarr.json")[0] == 200
        # Closing the old handle again must not evict the replacement.
        first.close()
        assert serve_store(sharded_store) is restarted
        assert _get(f"{restarted.url}/zarr.json")[0] == 200
    finally:
        restarted.close()


def test_view_returns_an_iframe_pointing_the_viewer_at_the_local_store(sharded_store):
    pytest.importorskip("IPython")
    try:
        shown = view(sharded_store, height=480)
        page = shown.data
        assert "<iframe" in page
        assert 'height="480"' in page
        assert "https://chronozarr.org/demo/?store=http%3A%2F%2F127.0.0.1%3A" in page
        assert f"%2F{sharded_store.name}" in page
    finally:
        for server in list(view_module._servers.values()):
            server.close()
        view_module._servers.clear()


def test_view_passes_remote_urls_through_without_a_server():
    pytest.importorskip("IPython")
    shown = view("https://data.example.org/a/store", viewer="https://viewer.example/v/")
    assert (
        "https://viewer.example/v/?store=https%3A%2F%2Fdata.example.org%2Fa%2Fstore" in shown.data
    )
    assert not view_module._servers
