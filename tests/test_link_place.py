"""`chronozarr link` sets the centre and zoom of the first view; `viewer_url` writes them."""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import zarr
from click.testing import CliRunner
from zarr.errors import ZarrUserWarning

import chronozarr
from chronozarr.cli import main
from chronozarr.view import serve_store, viewer_url
from tests.narrow import array_at
from tests.synthetic import make_da, make_truth

pytestmark = pytest.mark.unit

STORE = "https://h.example/s"


def _run(*args: str, ok: bool = True):
    result = CliRunner().invoke(main, list(args), catch_exceptions=False)
    assert (result.exit_code == 0) is ok, result.output
    return result


def _params(result) -> dict[str, list[str]]:
    return parse_qs(urlsplit(result.stdout.strip()).query)


def _serve(path: Path, crs: str, transform: tuple[float, ...], rows: int, cols: int):
    da = make_da(make_truth(2, 1, rows, cols), ["counts"])
    da.attrs.update(crs=crs, transform=transform)
    chronozarr.encode(da, path, chunk_size=16)
    return serve_store(path)


def _store_definition(path: Path, form: str, definition: str | dict[str, Any]) -> None:
    """Retain the code and mirrors while replacing every level's authoritative definition."""
    root = zarr.open_group(path, mode="r+", zarr_format=3, use_consolidated=False)
    for _, group in root.groups():
        data = array_at(group, "data")
        attrs = data.attrs.asdict()
        attrs.pop("proj:wkt2", None)
        attrs.pop("proj:projjson", None)
        attrs[f"proj:{form}"] = definition
        data.attrs.put(attrs)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ZarrUserWarning)
        zarr.consolidate_metadata(path)


@pytest.fixture
def utm(tmp_path):
    """EPSG:32631, x 499800..500300 and y -300..100: (3 E, 0 N) is (500000, 0), 50 px across."""
    server = _serve(
        tmp_path / "utm", "EPSG:32631", (10.0, 0.0, 499800.0, 0.0, -10.0, 100.0), 40, 50
    )
    yield server.url
    server.close()


@pytest.fixture
def degrees(tmp_path):
    """EPSG:4326 with longitudes 280..305 and latitudes 0..10, as NOAA OISST stores them."""
    server = _serve(tmp_path / "deg", "EPSG:4326", (0.5, 0.0, 280.0, 0.0, -0.5, 10.0), 20, 50)
    yield server.url
    server.close()


# --- viewer_url: the one place a viewer address is assembled -------------------------------------


def test_viewer_url_writes_zoom_and_centre_after_the_colormap():
    url = viewer_url(
        STORE, t=4, product="band", colormap="viridis", zoom=2.5, center=(684500, -3.25)
    )
    assert url.endswith("&cm=viridis&z=2.5&c=684500,-3.25")
    assert parse_qs(urlsplit(url).query)["c"] == ["684500,-3.25"]


def test_viewer_url_writes_a_centre_without_exponent_or_float_noise():
    url = viewer_url(STORE, center=(9123350.0, 500000.00000000012))
    assert url.endswith("&c=9123350,500000")


def test_viewer_url_without_a_place_is_unchanged():
    assert viewer_url(STORE) == "https://chronozarr.org/demo/?store=https%3A%2F%2Fh.example%2Fs"


@pytest.mark.parametrize("zoom", [0, -1, float("nan"), float("inf")])
def test_viewer_url_rejects_a_zoom_the_viewer_ignores(zoom):
    with pytest.raises(ValueError, match="zoom must be a positive number"):
        viewer_url(STORE, zoom=zoom)


@pytest.mark.parametrize(
    "center", [(1.0,), (1.0, 2.0, 3.0), (float("nan"), 0.0), (0.0, float("inf"))]
)
def test_viewer_url_rejects_a_centre_the_viewer_ignores(center):
    with pytest.raises(ValueError, match="center must be two finite numbers"):
        viewer_url(STORE, center=center)


# --- chronozarr link --lonlat / --xy / --zoom ----------------------------------------------------


def test_link_converts_a_longitude_and_latitude_to_the_store_crs(utm):
    result = _run("link", utm, "--lonlat", "3,0", "--zoom", "4", "--time", "1")
    params = _params(result)
    assert params["c"] == ["500000,0"]
    assert params["z"] == ["4"]
    assert params["t"] == ["1"]
    assert "x 500000, y 0 in EPSG:32631" in result.stderr


def test_link_accepts_negative_coordinates_after_the_option(utm, degrees):
    assert _params(_run("link", degrees, "--lonlat", "-64.6,3.25", "--zoom", "2"))["c"] == [
        "295.4,3.25"
    ]
    assert _params(_run("link", utm, "--lonlat=3.001,-0.002", "--zoom", "2"))["c"][0].startswith(
        "500111."
    )


def test_link_accepts_store_coordinates_as_they_are(utm):
    params = _params(_run("link", utm, "--xy", "500010,-20", "--zoom", "8"))
    assert params["c"] == ["500010,-20"]
    assert params["z"] == ["8"]


def test_link_zoom_alone_keeps_the_viewer_centre(utm):
    params = _params(_run("link", utm, "--zoom", "0.5"))
    assert params["z"] == ["0.5"]
    assert "c" not in params


def test_link_with_a_centre_and_no_zoom_asks_for_one_stored_pixel_per_screen_pixel(utm):
    result = _run("link", utm, "--xy", "500000,0")
    assert _params(result)["z"] == ["1"]
    assert "zoom 1" in result.stderr
    assert "--zoom" in result.stderr


def test_link_without_a_place_prints_no_zoom_or_centre(utm):
    params = _params(_run("link", utm, "--time", "1"))
    assert "z" not in params
    assert "c" not in params


def test_link_places_a_longitude_in_the_range_a_zero_to_360_store_uses(degrees):
    result = _run("link", degrees, "--lonlat", "-64.6,3.25")
    assert _params(result)["c"] == ["295.4,3.25"]


def test_link_names_the_extent_of_a_store_that_the_point_misses(utm):
    refused = _run("link", utm, "--lonlat", "3,5", "--zoom", "2", ok=False).output
    assert "Invalid value for --lonlat" in refused
    assert "longitude 3, latitude 5 is outside the store" in refused
    assert "longitude 2.9982 to 3.0027" in refused
    assert "EPSG:32631 x 499800 to 500300" in refused


def test_link_names_the_extent_of_a_store_that_store_coordinates_miss(utm):
    refused = _run("link", utm, "--xy", "0,0", ok=False).output
    assert "Invalid value for --xy" in refused
    assert "x 0, y 0 is outside the store" in refused
    assert "EPSG:32631 x 499800 to 500300, y -300 to 100" in refused


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--lonlat", "3,95"], "latitude from -90 to 90"),
        (["--lonlat", "3"], "--lonlat needs 2 comma-separated numbers, got 1"),
        (["--lonlat", "east,north"], "--lonlat needs 2 comma-separated numbers"),
        (["--xy", "1,2,3"], "--xy needs 2 comma-separated numbers, got 3"),
        (["--lonlat", "3,0", "--xy", "500000,0"], "--lonlat and --xy both set the centre"),
        (["--zoom", "0"], "zoom must be a positive number"),
        (["--zoom", "-2"], "zoom must be a positive number"),
        (["--zoom", "big"], "Invalid value for '--zoom'"),
    ],
)
def test_link_refuses_a_place_the_viewer_cannot_use(utm, args, message):
    assert message in _run("link", utm, *args, ok=False).output


@pytest.mark.parametrize("args", [["--lonlat", "3,0"], ["--xy", "1,2"], ["--zoom", "2"]])
def test_link_still_needs_a_hosted_store_for_a_place(args, tmp_path):
    assert "not an http(s) URL" in _run("link", str(tmp_path), *args, ok=False).output


def test_link_tells_a_code_only_projected_store_without_pyproj_to_install_it_or_use_xy(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(sys.modules, "pyproj", None)
    path = tmp_path / "code-only"
    server = _serve(path, "EPSG:32631", (10.0, 0.0, 499800.0, 0.0, -10.0, 100.0), 40, 50)
    try:
        assert chronozarr.open_store(path).crs.wkt2 is None
        refused = _run("link", server.url, "--lonlat", "3,0", ok=False).output
        assert "Invalid value for --lonlat" in refused
        assert "a store in EPSG:32631 needs pyproj to place a longitude and latitude" in refused
        assert "chronozarr[geo]" in refused
        assert "--xy" in refused
        assert _params(_run("link", server.url, "--xy", "500000,0"))["c"] == ["500000,0"]
    finally:
        server.close()


def test_link_needs_no_pyproj_for_a_code_only_geographic_store(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyproj", None)
    path = tmp_path / "code-only"
    server = _serve(path, "EPSG:4326", (0.5, 0.0, 280.0, 0.0, -0.5, 10.0), 20, 50)
    try:
        assert chronozarr.open_store(path).crs.wkt2 is None
        assert _params(_run("link", server.url, "--lonlat", "-64.6,3.25"))["c"] == ["295.4,3.25"]
    finally:
        server.close()


@pytest.mark.parametrize("form", ["wkt2", "projjson"])
def test_link_needs_pyproj_for_a_redundant_geographic_definition(tmp_path, monkeypatch, form):
    pyproj = pytest.importorskip("pyproj")
    path = tmp_path / "full-definition"
    server = _serve(path, "EPSG:4326", (0.5, 0.0, 280.0, 0.0, -0.5, 10.0), 20, 50)
    try:
        known = pyproj.CRS.from_epsg(4326)
        definition = known.to_wkt() if form == "wkt2" else known.to_json_dict()
        _store_definition(path, form, definition)
        assert getattr(chronozarr.open_store(path).crs, form) == definition
        assert _params(_run("link", server.url, "--lonlat", "-64.6,3.25"))["c"] == ["295.4,3.25"]

        monkeypatch.setitem(sys.modules, "pyproj", None)
        refused = _run("link", server.url, "--lonlat", "-64.6,3.25", ok=False).output
        assert f'a store in {"WKT2" if form == "wkt2" else "PROJJSON"} "WGS 84"' in refused
        assert "needs pyproj" in refused and "chronozarr[geo]" in refused and "--xy" in refused
        raw = (
            definition
            if isinstance(definition, str)
            else json.dumps(definition, separators=(",", ":"), sort_keys=True)
        )
        assert raw not in refused
        assert _params(_run("link", server.url, "--xy", "295.4,3.25"))["c"] == ["295.4,3.25"]
        assert "c" not in _params(_run("link", server.url))
    finally:
        server.close()


@pytest.mark.parametrize("form", ["wkt2", "projjson"])
@pytest.mark.parametrize("parser_available", [True, False])
def test_link_never_inherits_the_geographic_shortcut_from_a_stale_code(
    tmp_path, monkeypatch, form, parser_available
):
    pyproj = pytest.importorskip("pyproj")
    path = tmp_path / "stale-code"
    server = _serve(path, "EPSG:4326", (10.0, 0.0, 499800.0, 0.0, -10.0, 100.0), 40, 50)
    try:
        projected = pyproj.CRS.from_epsg(32631)
        definition = projected.to_wkt() if form == "wkt2" else projected.to_json_dict()
        _store_definition(path, form, definition)
        if parser_available:
            with pytest.warns(UserWarning, match="describe different CRSs"):
                result = _run("link", server.url, "--lonlat", "3,0")
            assert _params(result)["c"] == ["500000,0"]
        else:
            monkeypatch.setitem(sys.modules, "pyproj", None)
            refused = _run("link", server.url, "--lonlat", "3,0", ok=False).output
            assert "needs pyproj" in refused and "--xy" in refused
            raw = (
                definition
                if isinstance(definition, str)
                else json.dumps(definition, separators=(",", ":"), sort_keys=True)
            )
            assert raw not in refused
            assert _params(_run("link", server.url, "--xy", "500000,0"))["c"] == ["500000,0"]
            assert "c" not in _params(_run("link", server.url))
    finally:
        server.close()


def test_link_help_explains_the_zoom_and_the_two_ways_to_give_a_centre():
    text = " ".join(_run("link", "--help").stdout.split())
    assert "--lonlat LON,LAT" in text
    assert "--xy X,Y" in text
    assert "--zoom" in text
    assert "Screen pixels per stored pixel" in text
    assert "code-only EPSG:4326" in text
    assert "stored WKT2/PROJJSON definition" in text
