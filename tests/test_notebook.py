"""Notebook boundary checks; browser_check.mjs exercises the real embed protocol."""

import html
import importlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from chronozarr import player

pytestmark = pytest.mark.unit
pytest.importorskip("anywidget")
TraitError = pytest.importorskip("traitlets").TraitError


def test_hosted_player_traits_and_serialization():
    widget = player("https://example.org/store", t=3, product="band", speed=10)
    try:
        assert widget.store_url == "https://example.org/store"
        assert (widget.t, widget.product, widget.speed) == (3, "band", 10)
        assert "chronozarr:set" in widget._esm
        bundle = widget._repr_mimebundle_()
        if isinstance(bundle, tuple):
            bundle = bundle[0]
        assert bundle["application/vnd.jupyter.widget-view+json"]["model_id"] == widget.model_id
    finally:
        widget.close()


def test_local_player_uses_range_server(tmp_path):
    view_module = importlib.import_module("chronozarr.view")
    (tmp_path / "zarr.json").write_text("{}")
    widget = player(tmp_path)
    try:
        server = view_module.serve_store(tmp_path)
        assert widget.store_url == server.url
        assert widget.viewer_url == "https://chronozarr.org/demo/"
        assert widget.access is not None and widget.access.server is server
    finally:
        widget.close()
        server.close()


def test_player_accepts_paths_on_the_notebook_origin():
    widget = player("https://example.org/store", viewer="/user/ada/proxy/8765/_viewer/demo/")
    try:
        assert widget.viewer_url == "/user/ada/proxy/8765/_viewer/demo/"
        for bad in ("//evil.example/x", "relative/path", "ftp://x/y"):
            with pytest.raises(TraitError, match="HTTP"):
                widget.viewer_url = bad
    finally:
        widget.close()


@pytest.mark.parametrize("kwargs", [{"t": -1}, {"height": 10}, {"theme": "other"}])
def test_invalid_traits(kwargs):
    with pytest.raises(TraitError):
        player("https://example.org/store", **kwargs)


def test_invalid_viewer_url():
    with pytest.raises(TraitError, match="HTTP"):
        player("https://example.org/store", viewer="javascript:alert(1)")


def test_frontend_is_package_asset():
    notebook = importlib.import_module("chronozarr.notebook")
    assert Path(notebook.__file__).with_name("player.js").is_file()


def test_scientific_display_traits():
    widget = player("https://example.org/store", product="band", band="HV_dB", range=[-25, 0])
    try:
        assert widget.controls is False
        widget.controls = True
        assert widget.controls is True
        assert widget.band == "HV_dB"
        assert widget.range == [-25, 0]
        widget.range = None
        for limits in ([1, 1], [2, 1], [float("nan"), 1], [1]):
            with pytest.raises(TraitError):
                widget.range = limits
    finally:
        widget.close()


def test_proxy_view_preserves_initial_display_options(tmp_path, monkeypatch):
    view_module = importlib.import_module("chronozarr.view")
    store = tmp_path / "store"
    store.mkdir()
    (store / "zarr.json").write_text("{}")
    viewer_dir = tmp_path / "viewer"
    (viewer_dir / "demo").mkdir(parents=True)
    (viewer_dir / "demo" / "index.html").write_text("<!doctype html>")
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/ada/")
    try:
        page = view_module.view(
            store, viewer_dir=viewer_dir, t=2, product="band", band="HV_dB", range=[-25, 0]
        ).data
        iframe = re.search(r'<iframe src="([^"]+)"', page)
        assert iframe is not None
        url = urlsplit(html.unescape(iframe.group(1)))
        server = view_module.serve_store(store)
        assert url.path == f"/user/ada/proxy/{server.port}/_viewer/demo/index.html"
        assert parse_qs(url.query) == {
            "store": [f"/user/ada/proxy/{server.port}/store"],
            "t": ["2"],
            "p": ["band"],
            "b": ["HV_dB"],
            "r": ["-25,0"],
        }
    finally:
        view_module.serve_store(store).close()
