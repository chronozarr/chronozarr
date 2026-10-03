"""Notebook boundary checks; browser_check.mjs exercises the real embed protocol."""

import importlib
from pathlib import Path
from types import SimpleNamespace

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


def test_local_player_uses_range_server(monkeypatch, tmp_path):
    notebook = importlib.import_module("chronozarr.notebook")
    calls = []

    def serve(path, *, port):
        calls.append((path, port))
        return SimpleNamespace(url="http://127.0.0.1:1234/store")

    monkeypatch.setattr(notebook, "serve_store", serve)
    widget = player(tmp_path, port=1234)
    try:
        assert calls == [(str(tmp_path), 1234)]
        assert widget.store_url == "http://127.0.0.1:1234/store"
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
