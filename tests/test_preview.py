"""`chronozarr preview`, the remote routes of `view` and `player`, and their diagnostics."""

from __future__ import annotations

import importlib
import io
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

import chronozarr
from chronozarr.cli import main
from chronozarr.view import (
    VIEWER_MOUNT,
    diagnose_view,
    jupyter_proxy_root,
    local_access,
    normalize_base_url,
    preview,
    preview_command,
    serve_store,
    view,
)
from tests.synthetic import make_da, make_truth

pytestmark = pytest.mark.unit

view_module = importlib.import_module("chronozarr.view")
HUB = {"JUPYTERHUB_SERVICE_PREFIX": "/user/ada/"}


def get(url: str) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, dict(exc.headers.items()), exc.read()


@pytest.fixture(autouse=True)
def _no_servers_left() -> Iterator[None]:
    yield
    for server in list(view_module._servers.values()):
        server.close()
    view_module._servers.clear()
    view_module._accesses.clear()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "my store"
    path.mkdir()
    (path / "zarr.json").write_text("{}")
    return path


@pytest.fixture
def viewer_dir(tmp_path: Path) -> Path:
    path = tmp_path / "viewer"
    (path / "demo").mkdir(parents=True)
    (path / "demo" / "index.html").write_text("<!doctype html><title>viewer</title>")
    (path / "chronozarr").mkdir()
    (path / "chronozarr" / "decoder.js").write_text("export const x = 1;")
    (path / "vendor").mkdir()
    (path / "vendor" / "codec.wasm").write_bytes(b"\0asm\1\0\0\0")
    return path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---- server ----


def test_server_listens_on_loopback_only(store):
    server = serve_store(store)
    assert server._server.server_address[0] == "127.0.0.1"
    assert server.url.startswith("http://127.0.0.1:")


def test_explicit_port_in_use_is_a_clear_error(store, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "zarr.json").write_text("{}")
    first = serve_store(store)
    with pytest.raises(OSError, match=rf"port {first.port} on 127\.0\.0\.1 is already in use"):
        serve_store(other, port=first.port)
    assert serve_store(other).port != first.port  # a free port when none is asked for


def test_running_server_refuses_a_different_port(store):
    first = serve_store(store)
    assert serve_store(store, port=first.port) is first
    with pytest.raises(OSError, match=r"already served on port"):
        serve_store(store, port=free_port())


def test_viewer_dir_is_served_next_to_the_store_with_module_types(store, viewer_dir):
    server = serve_store(store, viewer_dir=viewer_dir)
    base = f"{server.root_url}/{VIEWER_MOUNT}"
    status, headers, body = get(f"{base}/demo/index.html")
    assert (status, headers["Content-Type"]) == (200, "text/html; charset=utf-8")
    assert b"<title>viewer</title>" in body
    assert get(f"{base}/chronozarr/decoder.js")[1]["Content-Type"] == "text/javascript"
    assert get(f"{base}/vendor/codec.wasm")[1]["Content-Type"] == "application/wasm"
    assert get(f"{server.url}/zarr.json")[0] == 200
    (viewer_dir.parent / "secret.txt").write_text("secret")
    assert get(f"{base}/%2e%2e/secret.txt")[0] == 404
    assert get(f"{base}/..%2f..%2fsecret.txt")[0] == 404
    assert get(f"{base}/demo/missing.js")[0] == 404


def test_viewer_is_not_served_unless_asked_for(store):
    server = serve_store(store)
    assert get(f"{server.root_url}/{VIEWER_MOUNT}/demo/index.html")[0] == 404


def test_viewer_dir_can_be_added_to_a_running_server(store, viewer_dir):
    server = serve_store(store)
    assert serve_store(store, viewer_dir=viewer_dir) is server
    assert get(f"{server.root_url}/{VIEWER_MOUNT}/demo/index.html")[0] == 200


def test_viewer_dir_without_the_viewer_page_is_rejected(store, tmp_path):
    with pytest.raises(FileNotFoundError, match=r"not a viewer folder.*chronozarr-viewer"):
        serve_store(store, viewer_dir=tmp_path)


# ---- routes ----


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (
            "https://hub.example.org/user/ada/proxy/8765/",
            "https://hub.example.org/user/ada/proxy/8765",
        ),
        ("http://localhost:9000", "http://localhost:9000"),
        ("/user/ada/proxy/8765", "/user/ada/proxy/8765"),
        ("/", ""),
    ],
)
def test_normalize_base_url(given, expected):
    assert normalize_base_url(given) == expected


@pytest.mark.parametrize(
    "bad", ["", "proxy/8765", "//evil.example/x", "ftp://host/x", "https://h/x?a=1", "/x#frag"]
)
def test_normalize_base_url_rejects_ambiguous_values(bad):
    with pytest.raises(ValueError, match="base_url"):
        normalize_base_url(bad)


def test_jupyter_proxy_root_needs_the_hub_prefix():
    assert jupyter_proxy_root(8765, HUB) == "/user/ada/proxy/8765"
    assert jupyter_proxy_root(8765, {"JUPYTERHUB_SERVICE_PREFIX": "/user/ada/srv"}) == (
        "/user/ada/srv/proxy/8765"
    )
    assert jupyter_proxy_root(8765, {}) is None


def test_default_route_is_loopback_with_the_hosted_viewer(store, monkeypatch):
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/ada/")
    access = local_access(store)
    assert access.route == "loopback"
    assert access.store_url == f"http://127.0.0.1:{access.server.port}/my%20store"
    assert access.viewer_url == "https://chronozarr.org/demo/"
    assert access.viewer_source == "hosted"


def test_base_url_replaces_the_loopback_address(store, viewer_dir):
    access = local_access(store, base_url="https://forward.example:9000/", viewer_dir=viewer_dir)
    assert access.route == "base_url"
    assert access.store_url == "https://forward.example:9000/my%20store"
    assert access.viewer_url == f"https://forward.example:9000/{VIEWER_MOUNT}/demo/index.html"
    assert access.server.url.startswith("http://127.0.0.1:")  # the bind address is unchanged


def test_self_hosted_viewer_on_jupyterhub_goes_through_the_notebook_server(
    store, viewer_dir, monkeypatch
):
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/ada/")
    access = local_access(store, viewer_dir=viewer_dir)
    port = access.server.port
    assert access.route == "jupyter-proxy"
    assert access.store_url == f"/user/ada/proxy/{port}/my%20store"
    assert access.viewer_url == f"/user/ada/proxy/{port}/{VIEWER_MOUNT}/demo/index.html"


def test_viewer_and_viewer_dir_are_exclusive(store, viewer_dir):
    with pytest.raises(ValueError, match="either viewer"):
        local_access(store, viewer="https://v.example/", viewer_dir=viewer_dir)


# ---- notebook surfaces ----


def test_view_with_a_proxy_route_gives_the_iframe_a_same_origin_path(
    store, viewer_dir, monkeypatch
):
    pytest.importorskip("IPython")
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/ada/")
    page = view(store, viewer_dir=viewer_dir).data
    port = serve_store(store).port
    assert f'src="/user/ada/proxy/{port}/{VIEWER_MOUNT}/demo/index.html?store=' in page
    assert "%2Fuser%2Fada%2Fproxy%2F" in page
    assert "diagnose_view" in page


def test_view_rejects_local_options_for_a_hosted_store():
    pytest.importorskip("IPython")
    with pytest.raises(ValueError, match="local stores"):
        view("https://example.org/store", base_url="https://x.example")


def test_player_with_base_url_and_error_hint(store):
    pytest.importorskip("anywidget")
    widget = chronozarr.player(store, base_url="/user/ada/proxy/8765")
    try:
        assert widget.store_url == "/user/ada/proxy/8765/my%20store"
        assert widget.hint == ""
        widget.error = {"code": "no_response", "message": "The viewer did not answer"}
        assert "No request for the store has reached this server yet." in widget.hint
        assert "/user/ada/proxy/8765/my%20store/zarr.json" in widget.hint
        widget.error = {}
        assert widget.hint == ""
    finally:
        widget.close()


def test_player_hosted_store_has_no_hint_source():
    pytest.importorskip("anywidget")
    widget = chronozarr.player("https://example.org/store")
    try:
        widget.error = {"code": "x", "message": "y"}
        assert widget.hint == ""
        with pytest.raises(ValueError, match="local stores"):
            chronozarr.player("https://example.org/store", viewer_dir=".")
    finally:
        widget.close()


# ---- diagnostics ----


def test_diagnose_counts_browser_requests(store, capsys):
    access = local_access(store)
    first = access.diagnose({})
    assert "0 for the store (last: never)" in first
    assert "No request for the store has reached this server yet." in first
    assert "Safari" in first  # the hosted viewer is an https page reaching http://127.0.0.1
    get(f"{access.store_url}/zarr.json")
    again = access.diagnose({})
    assert "1 for the store (last: 0 s ago)" in again
    assert "connectivity is not the problem" in again
    diagnose_view(store)
    assert "1 for the store" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        (HUB, "JupyterHub server"),
        ({"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"}, "ssh -L"),
        ({}, "Remote notebooks"),
    ],
)
def test_diagnose_names_the_environment(store, environ, expected):
    assert expected in local_access(store).diagnose(environ)


def test_diagnose_ssh_advice_names_the_bound_port(store):
    access = local_access(store)
    port = access.server.port
    assert f"ssh -L {port}:127.0.0.1:{port}" in access.diagnose({"SSH_TTY": "/dev/pts/0"})


def test_diagnose_of_the_proxy_route_checks_the_extension(store, viewer_dir, monkeypatch):
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/ada/")
    monkeypatch.setattr(view_module, "_proxy_installed", lambda: False)
    text = local_access(store, viewer_dir=viewer_dir).diagnose()
    assert "not found in this Python environment" in text
    assert "must show the viewer" in text
    assert "served from" in text and "chronozarr.org" not in text


def test_diagnose_of_an_unserved_store_says_what_to_call(store):
    with pytest.raises(ValueError, match=r"call view\(\) or player\(\) first"):
        diagnose_view(store)


def test_server_diagnose_defaults_to_the_loopback_route(store):
    text = serve_store(store).diagnose()
    assert "127.0.0.1 of the machine that runs the browser" in text


# ---- preview ----


class Browser:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.opened: list[str] = []

    def open(self, url: str) -> bool:
        self.opened.append(url)
        return self.result


def run_preview(store: Path, monkeypatch, browser: Browser, during=None, **kwargs):
    """Run `preview` with the browser mocked and a stand-in for the wait until Ctrl-C."""
    lines: list[str] = []
    seen: dict[str, object] = {}

    def block() -> None:
        seen["server"] = next(iter(view_module._servers.values()))
        if during is not None:
            during(seen["server"])
        raise KeyboardInterrupt

    monkeypatch.setattr(view_module.webbrowser, "open", browser.open)
    monkeypatch.setattr(view_module, "_block", block)
    preview(store, echo=lines.append, **kwargs)
    return lines, seen


def test_preview_serves_opens_the_hosted_viewer_and_stops_on_ctrl_c(store, monkeypatch):
    browser = Browser()
    reached: list[int] = []
    lines, seen = run_preview(
        store, monkeypatch, browser, during=lambda s: reached.append(get(f"{s.url}/zarr.json")[0])
    )
    server = seen["server"]
    assert reached == [200]
    assert browser.opened == [
        f"https://chronozarr.org/demo/?store=http%3A%2F%2F127.0.0.1%3A{server.port}%2Fmy%2520store"
    ]
    text = "\n".join(lines)
    assert "loaded from the internet" in text
    assert "127.0.0.1 only" in text
    assert lines[-1] == "stopped"
    assert not view_module._servers  # shut down cleanly
    with pytest.raises(urllib.error.URLError):
        get(f"{server.url}/zarr.json")


def test_preview_with_a_viewer_dir_needs_no_internet(store, viewer_dir, monkeypatch):
    browser = Browser()
    lines, seen = run_preview(store, monkeypatch, browser, viewer_dir=viewer_dir)
    server = seen["server"]
    assert browser.opened[0].startswith(
        f"http://127.0.0.1:{server.port}/{VIEWER_MOUNT}/demo/index.html?store="
    )
    assert "served locally, no internet needed" in "\n".join(lines)
    assert "chronozarr.org" not in "\n".join(lines)


def test_preview_says_when_no_browser_could_be_opened(store, monkeypatch):
    lines, _ = run_preview(store, monkeypatch, Browser(result=False))
    assert any("could not open a browser" in line for line in lines)


def test_preview_without_open_leaves_the_browser_alone(store, monkeypatch):
    browser = Browser()
    run_preview(store, monkeypatch, browser, open_browser=False)
    assert browser.opened == []


def test_preview_stops_the_server_when_waiting_fails(store, monkeypatch):
    def block() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(view_module.webbrowser, "open", Browser().open)
    monkeypatch.setattr(view_module, "_block", block)
    with pytest.raises(RuntimeError, match="boom"):
        preview(store, echo=lambda _: None)
    assert not view_module._servers


def test_preview_with_base_url_opens_that_address(store, monkeypatch):
    browser = Browser()
    run_preview(store, monkeypatch, browser, base_url="http://localhost:9000")
    assert browser.opened[0].endswith("?store=http%3A%2F%2Flocalhost%3A9000%2Fmy%2520store")


def test_preview_refuses_a_relative_base_url(store):
    with pytest.raises(ValueError, match="absolute http"):
        preview(store, base_url="/proxy/8765", echo=lambda _: None)
    assert not view_module._servers


def test_preview_port_conflict_is_an_error_not_a_hang(store, monkeypatch):
    monkeypatch.setattr(view_module.webbrowser, "open", Browser().open)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(OSError, match="already in use"):
            preview(store, port=int(taken.getsockname()[1]), echo=lambda _: None)


def test_preview_command_quotes_paths():
    assert preview_command("out") == "chronozarr preview out"
    assert preview_command(Path("my store")) == "chronozarr preview 'my store'"


# ---- CLI ----


def invoke(monkeypatch, browser: Browser, *args: str):
    def block() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(view_module.webbrowser, "open", browser.open)
    monkeypatch.setattr(view_module, "_block", block)
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_cli_preview_prints_urls_and_opens_the_viewer(store, monkeypatch):
    browser = Browser()
    result = invoke(monkeypatch, browser, "preview", str(store))
    assert result.exit_code == 0, result.output
    assert "serving" in result.output and "loaded from the internet" in result.output
    assert "stopped" in result.output
    assert len(browser.opened) == 1


def test_cli_preview_no_open_and_viewer_dir(store, viewer_dir, monkeypatch):
    browser = Browser()
    result = invoke(
        monkeypatch, browser, "preview", str(store), "--no-open", "--viewer-dir", str(viewer_dir)
    )
    assert result.exit_code == 0, result.output
    assert "no internet needed" in result.output
    assert browser.opened == []


def test_cli_preview_reports_a_busy_port_and_a_non_store(store, tmp_path, monkeypatch):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        busy = invoke(
            monkeypatch,
            Browser(),
            "preview",
            str(store),
            "--port",
            str(taken.getsockname()[1]),
        )
    assert busy.exit_code == 1
    assert "already in use" in busy.output and "Traceback" not in busy.output

    missing = invoke(monkeypatch, Browser(), "preview", str(tmp_path))
    assert missing.exit_code == 1
    assert "no zarr.json" in missing.output


def test_cli_preview_rejects_conflicting_viewers(store, viewer_dir, monkeypatch):
    result = invoke(
        monkeypatch,
        Browser(),
        "preview",
        str(store),
        "--viewer",
        "https://v.example/",
        "--viewer-dir",
        str(viewer_dir),
    )
    assert result.exit_code == 1
    assert "either viewer" in result.output


def test_encode_and_convert_print_the_preview_command(tmp_path):
    source = tmp_path / "input.zarr"
    make_da(make_truth(2, 2, 40, 50)).to_dataset(name="reflectance").to_zarr(
        source, zarr_format=2, consolidated=False
    )
    encoded = tmp_path / "encoded"
    result = CliRunner().invoke(
        main, ["encode", str(source), str(encoded), "--chunk-size", "16"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert f"preview it: chronozarr preview {encoded}" in result.output

    converted = tmp_path / "converted"
    result = CliRunner().invoke(
        main,
        [
            "convert",
            str(source),
            str(converted),
            "--variable",
            "reflectance",
            "--chunk-size",
            "16",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert f"preview it: chronozarr preview {converted}" in result.output

    dry = CliRunner().invoke(
        main,
        ["convert", str(source), str(tmp_path / "dry"), "--variable", "reflectance", "--dry-run"],
        catch_exceptions=False,
    )
    assert "preview it" not in dry.output


# ---- sharing through a tunnel (#60) ----


def test_explicit_port_next_to_a_wildcard_listener_is_refused(store):
    # BSD and macOS let a loopback bind succeed beside a 0.0.0.0 listener when SO_REUSEADDR is
    # set; a tunnel aimed at the port must still never reach that other service.
    with socket.socket() as other:
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind(("0.0.0.0", 0))
        other.listen()
        taken = int(other.getsockname()[1])
        with pytest.raises(OSError, match="already in use"):
            serve_store(store, port=taken)
    assert not view_module._servers


class _Impostor:
    """Stands in for the opener of the self-check: whatever owns the port answers differently."""

    def open(self, url: str, timeout: float) -> io.BytesIO:
        return io.BytesIO(b"someone else")


def test_explicit_port_is_checked_to_answer_for_this_store(store, monkeypatch):
    monkeypatch.setattr(view_module.urllib.request, "build_opener", lambda *handlers: _Impostor())
    port = free_port()
    with pytest.raises(OSError, match="answered with something other"):
        serve_store(store, port=port)
    assert not view_module._servers
    with socket.socket() as probe:  # the port was released again
        probe.bind(("127.0.0.1", port))


def test_explicit_port_serves_and_the_check_is_not_counted_as_the_browser(store):
    port = free_port()
    server = serve_store(store, port=port)
    assert server.port == port
    assert server.state.store_requests == 0
    assert get(f"{server.url}/zarr.json")[0] == 200
    assert server.state.store_requests == 1


def conditional_get(url: str, since: str, **extra: str) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers={"If-Modified-Since": since, **extra})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, dict(exc.headers.items()), exc.read()


def test_revalidation_gets_304_without_a_body_and_ranges_still_work(store):
    url = f"{serve_store(store).url}/zarr.json"
    stamp = get(url)[1]["Last-Modified"]
    status, headers, body = conditional_get(url, stamp)
    assert (status, body) == (304, b"")
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert conditional_get(url, stamp, Range="bytes=0-0")[0] == 206  # never 304 for a range
    assert conditional_get(url, "Mon, 01 Jan 1990 00:00:00 GMT")[0] == 200
    assert conditional_get(url, "not a date")[0] == 200


def test_preview_with_a_port_says_what_a_tunnel_should_target(store, monkeypatch):
    port = free_port()
    lines, _ = run_preview(store, monkeypatch, Browser(), port=port)
    text = "\n".join(lines)
    assert f"port {port} is bound to this store" in text
    assert f"http://127.0.0.1:{port}" in text
    assert "can read this store" not in text


def test_preview_with_base_url_warns_that_the_link_is_readable(store, monkeypatch):
    lines, _ = run_preview(store, monkeypatch, Browser(), base_url="https://x.trycloudflare.com")
    assert any(
        "anyone who can open https://x.trycloudflare.com/my%20store can read this store" in line
        for line in lines
    )
