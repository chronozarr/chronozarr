"""Package adapter boundaries; browser checks exercise the installed leafmap renderer."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from chronozarr import add_chronozarr

pytestmark = pytest.mark.unit


class Map:
    _esm = "export default {render() {}};"
    _rendered = False

    def __init__(self):
        self.calls = []

    def add_call(self, *args):
        self.calls.append(args)


def test_packaged_bridge_and_layer_options():
    m = Map()
    assert (
        add_chronozarr(m, "https://example.com/store", product="band", band=1, range=[-25, 0]) is m
    )
    assert "renderBridge" in m._esm
    options = m.calls[0][1]["options"]
    assert (options["band"], options["range"]) == (1, [-25, 0])
    source = m._esm
    add_chronozarr(m, "https://example.com/second", name="second")
    assert m._esm == source
    assert len(m.calls) == 2


def test_local_path_uses_server(monkeypatch, tmp_path):
    import chronozarr.leafmap as adapter

    calls = []

    def serve(path, *, port):
        calls.append((path, port))
        return SimpleNamespace(url="http://127.0.0.1:9999/store")

    monkeypatch.setattr(adapter, "serve_store", serve)
    m = Map()
    add_chronozarr(m, tmp_path, port=9999)
    assert calls == [(str(tmp_path), 9999)]
    assert m.calls[0][1]["options"]["url"] == "http://127.0.0.1:9999/store"
    assert Path(adapter.__file__).with_name("leafmap.js").is_file()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"t": -1},
        {"band": True},
        {"opacity": 2},
        {"range": [0, 0]},
        {"range": [float("nan"), 1]},
        {"reader_url": "javascript:bad"},
    ],
)
def test_invalid_options_do_not_mutate_map(kwargs):
    m = Map()
    with pytest.raises(ValueError):
        add_chronozarr(m, "https://example.com/store", **kwargs)
    assert m.calls == []
    assert m._esm == Map._esm


def test_displayed_map_is_rejected():
    m = Map()
    m._rendered = True
    with pytest.raises(ValueError, match="before displaying"):
        add_chronozarr(m)


def test_base_url_replaces_the_loopback_address_of_a_local_store(tmp_path):
    (tmp_path / "zarr.json").write_text("{}")
    m = Map()
    try:
        add_chronozarr(m, tmp_path, base_url="https://forward.example:9000/")
        assert m.calls[0][1]["options"]["url"] == (f"https://forward.example:9000/{tmp_path.name}")
    finally:
        from chronozarr.view import _servers

        for server in list(_servers.values()):
            server.close()


def test_base_url_needs_a_local_store():
    m = Map()
    with pytest.raises(ValueError, match="local store"):
        add_chronozarr(m, "https://example.com/store", base_url="https://forward.example")
    assert m.calls == []
