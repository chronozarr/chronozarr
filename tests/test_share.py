"""The disposable local-store sharing command (issue #80)."""

from __future__ import annotations

import importlib
import io
import json
import socket
import subprocess
from pathlib import Path
from typing import cast

import pytest
from click.testing import CliRunner

from chronozarr.cli import main
from chronozarr.doctor import Check
from chronozarr.view import serve_store
from tests.synthetic import build_store, make_truth

pytestmark = pytest.mark.unit

share_module = importlib.import_module("chronozarr.share")
view_module = importlib.import_module("chronozarr.view")


@pytest.fixture(autouse=True)
def _no_servers_left():
    yield
    for server in list(view_module._servers.values()):
        server.close()
    view_module._servers.clear()
    view_module._accesses.clear()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic store"
    path.mkdir()
    (path / "zarr.json").write_text("{}")
    return path


class FakeProcess:
    def __init__(self, output: str = "https://bright-sea.trycloudflare.com\n", *, exited=False):
        self.stdout = io.StringIO(output)
        self.stderr = io.StringIO()
        self.returncode = 7 if exited else None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _ready_share(monkeypatch, process: FakeProcess):
    commands: list[list[str]] = []
    monkeypatch.setattr(share_module.shutil, "which", lambda _: "/mock/cloudflared")

    def popen(args, **kwargs):
        commands.append(args)
        return process

    monkeypatch.setattr(share_module.subprocess, "Popen", popen)
    monkeypatch.setattr(share_module, "_wait_for_public_store", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        share_module,
        "diagnose",
        lambda _, **kwargs: [Check("root zarr.json", "ok", "public route works")],
    )
    monkeypatch.setattr(
        share_module,
        "_first_chunk_measurement",
        lambda *args: share_module._CellMeasurement(2_000_000, 0.5, 2, 1, False),
    )
    return commands


def _interrupt(_: object) -> int:
    raise KeyboardInterrupt


def test_share_prints_only_a_verified_public_viewer_link_and_cleans_up(store, monkeypatch):
    process = FakeProcess()
    commands = _ready_share(monkeypatch, process)
    monkeypatch.setattr(share_module, "_wait_for_tunnel", _interrupt)
    lines: list[str] = []

    share_module.share(store, open_browser=False, echo=lines.append)

    text = "\n".join(lines)
    assert commands and commands[0][1:3] == ["tunnel", "--url"]
    assert commands[0][-1].startswith("http://127.0.0.1:")
    assert "sharing" in text and "https://bright-sea.trycloudflare.com" in text
    assert "https://chronozarr.org/demo/?store=https%3A%2F%2Fbright-sea.trycloudflare.com" in text
    assert "synthetic%2520store" in text  # the URL is encoded inside the viewer's `store=` query.
    assert "127.0.0.1" not in next(line for line in lines if line.startswith("open: "))
    assert "tunnel throughput:" in text and "estimated overview step 1.00 s" in text
    assert "anyone who can open this link can read this store" in text
    assert lines[-1] == "stopped"
    assert process.terminated
    assert not view_module._servers


def test_share_reports_stages_before_their_corresponding_work(store, monkeypatch):
    process = FakeProcess()
    _ready_share(monkeypatch, process)
    monkeypatch.setattr(share_module, "_wait_for_tunnel", _interrupt)
    lines: list[str] = []
    stages = [
        "starting local server and quick tunnel...",
        "waiting for Cloudflare to assign a public address...",
        "waiting for https://bright-sea.trycloudflare.com to work; a new address usually takes "
        "15 to 30 seconds (at most 90). Use it after the open: line below: a lookup before then "
        "can keep it failing for a minute...",
        "checking browser access...",
        "measuring first transfer...",
    ]

    local_access = share_module.local_access
    local_access_calls = 0
    start_tunnel = share_module._start_tunnel

    def check_local_access(*args, **kwargs):
        nonlocal local_access_calls
        if local_access_calls == 0:
            assert stages[0] in lines
        local_access_calls += 1
        return local_access(*args, **kwargs)

    def check_start_tunnel(*args, **kwargs):
        assert stages[0] in lines
        return start_tunnel(*args, **kwargs)

    def check_tunnel_url(*args, **kwargs):
        assert stages[1] in lines
        return "https://bright-sea.trycloudflare.com"

    def check_public_store(*args, **kwargs):
        assert stages[2] in lines

    def check_doctor(*args, **kwargs):
        assert stages[3] in lines
        # Through the tunnel, decode a cell per level but read no whole timestep.
        assert kwargs.get("full_read_limit_mb") == 0
        return [Check("root zarr.json", "ok", "public route works")]

    def check_measurement(*args, **kwargs):
        assert stages[4] in lines
        return share_module._CellMeasurement(2_000_000, 0.5, 2, 1, False)

    monkeypatch.setattr(share_module, "local_access", check_local_access)
    monkeypatch.setattr(share_module, "_start_tunnel", check_start_tunnel)
    monkeypatch.setattr(share_module, "_tunnel_url", check_tunnel_url)
    monkeypatch.setattr(share_module, "_wait_for_public_store", check_public_store)
    monkeypatch.setattr(share_module, "diagnose", check_doctor)
    monkeypatch.setattr(share_module, "_first_chunk_measurement", check_measurement)

    share_module.share(store, open_browser=False, echo=lines.append)

    assert [line for line in lines if line in stages] == stages


def test_share_missing_cloudflared_never_starts_a_server(store, monkeypatch):
    monkeypatch.setattr(share_module.shutil, "which", lambda _: None)

    with pytest.raises(OSError, match="cloudflared is required"):
        share_module.share(store, open_browser=False)

    assert not view_module._servers


def test_cli_share_reports_the_install_path_without_starting_a_server(store, monkeypatch):
    monkeypatch.setattr(share_module.shutil, "which", lambda _: None)

    result = CliRunner().invoke(main, ["share", str(store), "--no-open"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "cloudflared is required" in result.output
    assert "developers.cloudflare.com" in result.output
    assert not view_module._servers


def test_share_does_not_print_a_link_when_doctor_fails(store, monkeypatch):
    process = FakeProcess()
    _ready_share(monkeypatch, process)
    monkeypatch.setattr(
        share_module,
        "diagnose",
        lambda _, **kwargs: [Check("CORS", "fail", "blocked by the tunnel")],
    )
    lines: list[str] = []

    with pytest.raises(OSError, match="did not pass chronozarr doctor"):
        share_module.share(store, open_browser=False, echo=lines.append)

    assert not any(line.startswith("open: ") for line in lines)
    assert "checking browser access..." in lines
    assert "measuring first transfer..." not in lines
    assert not any("chronozarr.org/demo/?store=" in line for line in lines)
    assert process.terminated
    assert not view_module._servers


def test_share_labels_a_sharded_measurement_as_a_cell_read(store, monkeypatch):
    process = FakeProcess()
    _ready_share(monkeypatch, process)
    monkeypatch.setattr(
        share_module,
        "_first_chunk_measurement",
        lambda *args: share_module._CellMeasurement(8_000, 0.25, 2, 2, True),
    )
    monkeypatch.setattr(share_module, "_wait_for_tunnel", _interrupt)
    lines: list[str] = []

    share_module.share(store, open_browser=False, echo=lines.append)

    throughput = next(line for line in lines if line.startswith("tunnel throughput:"))
    assert "level-0 cell read (shard index + inner chunk)" in throughput
    assert "first level-0 chunk" not in throughput


def test_share_stops_the_server_when_cloudflared_exits_during_startup(store, monkeypatch):
    process = FakeProcess(output="connection failed\n", exited=True)
    monkeypatch.setattr(share_module.shutil, "which", lambda _: "/mock/cloudflared")
    monkeypatch.setattr(share_module.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(OSError, match="exited before creating a tunnel"):
        share_module.share(store, open_browser=False)

    assert not view_module._servers


def test_share_stops_the_server_when_an_active_tunnel_exits(store, monkeypatch):
    process = FakeProcess()
    _ready_share(monkeypatch, process)
    monkeypatch.setattr(share_module, "_wait_for_tunnel", lambda _: 1)

    with pytest.raises(OSError, match="cloudflared stopped unexpectedly"):
        share_module.share(store, open_browser=False)

    assert process.terminated
    assert not view_module._servers


def test_share_explicit_port_is_the_port_given_to_cloudflared(store, monkeypatch):
    process = FakeProcess()
    commands = _ready_share(monkeypatch, process)
    monkeypatch.setattr(share_module, "_wait_for_tunnel", _interrupt)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    share_module.share(store, port=port, open_browser=False)

    assert commands[0][-1] == f"http://127.0.0.1:{port}"
    assert not view_module._servers


def test_public_tunnel_readiness_allows_delayed_dns_propagation(monkeypatch):
    now = [0.0]
    expected = b"synthetic root"

    def direct_get(*args, **kwargs):
        if now[0] < 31:
            raise OSError("temporary DNS lookup failure")
        return expected

    monkeypatch.setattr(share_module, "_direct_get", direct_get)
    monkeypatch.setattr(share_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        share_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )

    share_module._wait_for_public_store("https://delayed.trycloudflare.com/store", expected)

    assert now[0] >= 31


def test_the_first_lookup_waits_so_a_missing_record_is_not_cached(monkeypatch):
    now = [0.0]
    asked_at: list[float] = []
    expected = b"synthetic root"

    def direct_get(*args, **kwargs):
        asked_at.append(now[0])
        return expected

    monkeypatch.setattr(share_module, "_direct_get", direct_get)
    monkeypatch.setattr(share_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        share_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )

    share_module._wait_for_public_store(
        "https://new.trycloudflare.com/store", expected, first_lookup=15.0
    )

    assert asked_at and asked_at[0] >= 15.0, "no lookup before the address is likely published"


def test_first_chunk_measurement_uses_an_isolated_synthetic_store(tmp_path):
    path = tmp_path / "synthetic"
    build_store(path, make_truth(2, 1, 20, 24), shard=False, chunk_size=16)
    server = serve_store(path)

    measurement = share_module._first_chunk_measurement(server.url)

    assert measurement.bytes_read > 0
    assert measurement.seconds >= 0
    assert measurement.overview_cells == 1
    assert measurement.requests == 1
    assert not measurement.sharded


def test_sharded_measurement_reads_an_index_and_inner_chunk_not_a_whole_shard(tmp_path):
    path = tmp_path / "sharded"
    build_store(path, make_truth(8, 1, 32, 32), shard=True, chunk_size=16)
    server = serve_store(path)

    measurement = share_module._first_chunk_measurement(server.url)
    shard_bytes = (path / "0" / "data" / "c" / "0" / "0" / "0" / "0").stat().st_size

    assert measurement.sharded
    assert measurement.requests >= 2  # bounded shard index plus the requested inner chunk
    assert 0 < measurement.bytes_read < shard_bytes


def test_measurement_derives_levels_from_arrays_when_the_optional_mirror_is_absent(tmp_path):
    path = tmp_path / "no-levels-mirror"
    build_store(path, make_truth(2, 1, 20, 24), shard=False, chunk_size=16)
    root_path = path / "zarr.json"
    root = json.loads(root_path.read_text())
    del root["attributes"]["chronozarr"]["levels"]
    root_path.write_text(json.dumps(root))
    server = serve_store(path)

    measurement = share_module._first_chunk_measurement(server.url)

    assert measurement.overview_cells == 1


def test_the_api_host_in_a_cloudflared_error_is_not_taken_for_the_tunnel():
    logs = share_module._TunnelLogs()
    logs.add(
        'ERR Failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": '
        "dial tcp: no such host"
    )
    assert logs.url() is None
    logs.add("INF |  https://bright-sea.trycloudflare.com  |")
    assert logs.url() == "https://bright-sea.trycloudflare.com"


def test_waiting_for_the_public_address_stops_when_cloudflared_exits(monkeypatch):
    process = FakeProcess()
    process.returncode = 1
    # FakeProcess stands in for the Popen: the wait reads only poll() and the logs.
    tunnel = share_module._Tunnel(
        cast("subprocess.Popen[str]", process), share_module._TunnelLogs()
    )
    tunnel.logs.add("ERR failed to connect to the edge")
    monkeypatch.setattr(
        share_module, "_direct_get", lambda *a, **k: (_ for _ in ()).throw(OSError("dns"))
    )
    with pytest.raises(
        OSError, match=r"cloudflared exited \(exit 1\).*failed to connect to the edge"
    ):
        share_module._wait_for_public_store(
            "https://x.trycloudflare.com/s", b"root", tunnel=tunnel
        )


def test_tunnel_logs_keep_only_a_bounded_diagnostic_tail():
    logs = share_module._TunnelLogs()
    for number in range(share_module._TUNNEL_LOG_LINES * 3):
        logs.add(f"log line {number}")
    logs.add("created https://violet-rain.trycloudflare.com")

    assert logs.url() == "https://violet-rain.trycloudflare.com"
    assert len(logs._lines) <= share_module._TUNNEL_LOG_LINES
    assert len(logs.recent()) <= 4
    assert logs.recent()[-1] == "created https://violet-rain.trycloudflare.com"


# --- `chronozarr link` on the address `share` has just printed ----------------------------------

QUICK_HOST = "brand-new-words.trycloudflare.com"


@pytest.fixture
def served(tmp_path):
    path = tmp_path / "tunnelled"
    build_store(path, make_truth(2, 1, 20, 24), shard=False, chunk_size=16)
    server = serve_store(path)
    yield server
    server.close()


def _name_resolves_after(monkeypatch, host: str, seconds: float) -> list[float]:
    """`host` is unknown to this computer's resolver until `seconds` have passed on a fake clock,
    then it is this computer. Returns the clock."""
    now = [0.0]
    real = socket.getaddrinfo

    def getaddrinfo(name, *args, **kwargs):
        if name != host:
            return real(name, *args, **kwargs)
        if now[0] < seconds:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return real("127.0.0.1", *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(share_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        share_module.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    return now


def test_link_waits_for_a_new_quick_tunnel_address_to_resolve(served, monkeypatch):
    # Session 207 ran `link` on the address while `share` still waited for it to work.
    now = _name_resolves_after(monkeypatch, QUICK_HOST, 40.0)
    url = served.url.replace("127.0.0.1", QUICK_HOST)

    result = CliRunner().invoke(main, ["link", url], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("https://chronozarr.org/demo/?store=")
    assert f"{QUICK_HOST} does not resolve yet" in result.stderr
    assert "waiting up to 90 seconds" in result.stderr
    assert 40.0 <= now[0] < 45.0


def test_link_says_what_to_do_when_a_quick_tunnel_address_never_resolves(monkeypatch):
    now = _name_resolves_after(monkeypatch, QUICK_HOST, float("inf"))

    result = CliRunner().invoke(main, ["link", f"https://{QUICK_HOST}/s"])

    assert result.exit_code == 1
    assert f"{QUICK_HOST} did not resolve within 90 seconds" in result.output
    assert "only while the `chronozarr share` that printed it runs" in result.output
    assert now[0] >= 90.0


def test_link_does_not_wait_for_a_name_that_is_not_a_quick_tunnel(monkeypatch):
    now = _name_resolves_after(monkeypatch, "store.example.org", float("inf"))

    result = CliRunner().invoke(main, ["link", "https://store.example.org/s"])

    assert result.exit_code == 1
    assert "Name or service not known" in result.output
    assert "does not resolve yet" not in result.output
    assert now[0] < 5.0  # only the reader's own retries
