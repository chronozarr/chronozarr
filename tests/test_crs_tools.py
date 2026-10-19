"""STAC, export-cog and append on a store whose CRS has no EPSG code."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import zarr

import chronozarr
from chronozarr import schema
from chronozarr.append import append
from chronozarr.export import export_cog
from chronozarr.stac import build_stac
from tests.crs_samples import LCC_WKT2, SINUSOIDAL_WKT2
from tests.narrow import array_at
from tests.synthetic import BANDS, CRS, make_da, make_truth

pytestmark = pytest.mark.unit

HEIGHT, WIDTH, N_TIME = 40, 50, 4
CHUNK = 16


@pytest.fixture(scope="module")
def truth() -> np.ndarray:
    return make_truth(N_TIME, 2, HEIGHT, WIDTH)


def encode_with(truth: np.ndarray, out: Path, crs: str, **options: Any) -> None:
    options.setdefault("chunk_size", CHUNK)
    chronozarr.encode(make_da(truth, BANDS), out, crs=crs, **options)


def on_disk(store: Path, path: str) -> dict[str, Any]:
    """The attributes in a node's own zarr.json."""
    return json.loads((store / path / "zarr.json").read_text())["attributes"]


def assert_values_equal(store_path: Path, truth: np.ndarray) -> None:
    opened = chronozarr.open_store(store_path)
    for t in range(len(truth)):
        np.testing.assert_array_equal(opened.read(t), truth[t])


# --- STAC and export-cog --------------------------------------------------------------------


def documents(store: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    return build_stac(chronozarr.open_store(store), href="store", id="test")


def test_stac_for_a_crs_without_a_code_has_a_null_code_and_the_wkt2(tmp_path, truth):
    out = tmp_path / "store"
    encode_with(truth, out, LCC_WKT2)
    collection, item = documents(out)

    properties = item["properties"]
    assert properties["proj:code"] is None
    assert properties["proj:wkt2"] == LCC_WKT2
    assert "proj:projjson" not in properties
    assert collection["summaries"] == {"proj:wkt2": [LCC_WKT2]}
    assert item["properties"]["cube:dimensions"]["x"]["reference_system"] == LCC_WKT2
    assert item["properties"]["cube:dimensions"]["y"]["reference_system"] == LCC_WKT2
    assert all(
        -180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in item["geometry"]["coordinates"][0]
    )


def test_stac_for_an_epsg_crs_is_unchanged(tmp_path, truth):
    out = tmp_path / "store"
    encode_with(truth, out, CRS)
    collection, item = documents(out)
    assert item["properties"]["proj:code"] == CRS
    assert "proj:wkt2" not in item["properties"]
    assert collection["summaries"] == {"proj:code": [CRS]}
    assert item["properties"]["cube:dimensions"]["x"]["reference_system"] == 32631


def test_export_cog_carries_a_crs_without_a_code(tmp_path, truth):
    import rasterio

    out = tmp_path / "store"
    encode_with(truth, out, LCC_WKT2)
    target = tmp_path / "cogs"
    (path, *_) = export_cog(out, target, level=0, times=[0])
    with rasterio.open(path) as src:
        assert src.crs == rasterio.crs.CRS.from_wkt(LCC_WKT2)
        np.testing.assert_array_equal(src.read(), truth[0])


# --- append ---------------------------------------------------------------------------------


def test_append_to_a_store_without_a_code_needs_the_same_crs(tmp_path, truth):
    out = tmp_path / "store"
    encode_with(truth[:2], out, LCC_WKT2)
    later = make_da(truth, BANDS).isel(time=slice(2, 4))

    with pytest.raises(
        ValueError, match=r"crs: the input is 'EPSG:32631', the store is 'WKT2 \"unknown\"'"
    ):
        append(out, later, crs=CRS)
    with pytest.raises(
        ValueError, match=r"crs: the input is 'WKT2 \"unknown\"', the store is 'WKT2 \"unknown\"'"
    ):
        append(out, later, crs=SINUSOIDAL_WKT2)

    report = append(out, later, crs=LCC_WKT2)
    assert report.n_appended == 2
    assert_values_equal(out, truth)
    assert schema.validate(out) == []
    assert chronozarr.open_store(out).attrs.spec_version == "0.4.0"


def test_append_a_store_with_a_code_still_compares_the_codes(tmp_path, truth):
    out = tmp_path / "store"
    encode_with(truth[:2], out, CRS)
    later = make_da(truth, BANDS).isel(time=slice(2, 4))
    with pytest.raises(
        ValueError, match=r"crs: the input is 'EPSG:32632', the store is 'EPSG:32631'"
    ):
        append(out, later, crs="EPSG:32632")
    assert append(out, later, crs=CRS).n_appended == 2


def test_the_wkt2_array_attribute_survives_an_append(tmp_path, truth):
    out = tmp_path / "store"
    encode_with(truth[:2], out, LCC_WKT2)
    append(out, make_da(truth, BANDS).isel(time=slice(2, 4)), crs=LCC_WKT2)
    assert on_disk(out, "0/data")["proj:wkt2"] == LCC_WKT2
    assert array_at(zarr.open_group(str(out), mode="r"), "0/data").shape[0] == 4
