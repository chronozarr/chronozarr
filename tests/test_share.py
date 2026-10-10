"""The disposable local-store sharing command (issue #80)."""

from __future__ import annotations

import importlib
import io
import socket
from pathlib import Path

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
        lambda _: [Check("root zarr.json", "ok", "public route works")],
    )
    monkeypatch.setattr(
        share_module, "_first_chunk_measurement", lambda *args: (2_000_000, 0.5, 2)
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
        lambda _: [Check("CORS", "fail", "blocked by the tunnel")],
    )
    lines: list[str] = []

    with pytest.raises(OSError, match="did not pass chronozarr doctor"):
        share_module.share(store, open_browser=False, echo=lines.append)

    assert not any(line.startswith("open: ") for line in lines)
    assert process.terminated
    assert not view_module._servers


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


def test_first_chunk_measurement_uses_an_isolated_synthetic_store(tmp_path):
    path = tmp_path / "synthetic"
    build_store(path, make_truth(2, 1, 20, 24), shard=False, chunk_size=16)
    server = serve_store(path)

    bytes_read, seconds, overview_cells = share_module._first_chunk_measurement(server.url, server)

    assert bytes_read > 0
    assert seconds >= 0
    assert overview_cells == 1
