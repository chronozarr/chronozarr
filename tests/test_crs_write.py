"""Writing a CRS that has no EPSG code with `encode`, and the version it declares.

The round trips use a Lambert conformal conic and the MODIS sinusoidal grid in synthetic
rasters. Their coordinates are not those of a real place; what matters is that the CRS the
store keeps is the CRS written and that every stored value equals the value written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import chronozarr
from chronozarr import schema
from chronozarr.crs import Crs
from tests.crs_samples import LCC_WKT2, SINUSOIDAL_WKT2
from tests.synthetic import BANDS, CRS, make_da, make_truth

pytestmark = pytest.mark.unit

HEIGHT, WIDTH, N_TIME = 40, 50, 4
CHUNK = 16
CASES = [pytest.param(LCC_WKT2, id="lcc"), pytest.param(SINUSOIDAL_WKT2, id="sinusoidal")]


@pytest.fixture(scope="module")
def truth() -> np.ndarray:
    return make_truth(N_TIME, 2, HEIGHT, WIDTH)


@pytest.fixture(scope="module")
def pyproj_crs():
    pyproj = pytest.importorskip("pyproj")
    return pyproj.CRS


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


# --- encode ---------------------------------------------------------------------------------


@pytest.mark.parametrize("wkt2", CASES)
def test_a_wkt2_crs_is_stored_as_it_is_in_a_0_4_0_store(tmp_path, truth, wkt2):
    out = tmp_path / "store"
    encode_with(truth, out, wkt2)

    assert schema.validate_report(out).problems == []
    assert schema.validate_report(out).notes == []
    opened = chronozarr.open_store(out)
    assert opened.attrs.spec_version == "0.4.0"
    assert (opened.crs.code, opened.crs.wkt2) == (None, wkt2)
    assert_values_equal(out, truth)

    root = on_disk(out, "")
    assert root["chronozarr"]["spec_version"] == "0.4.0"
    assert "crs" not in root["chronozarr"]
    for level in ("0", "1"):
        assert "crs" not in on_disk(out, level)
        for name in ("data",):
            attrs = on_disk(out, f"{level}/{name}")
            assert attrs["proj:wkt2"] == wkt2
            assert not {"proj:code", "proj:projjson", "crs"} & attrs.keys()
            assert attrs["_CRS"] == {"wkt": wkt2}


def test_an_epsg_crs_keeps_version_0_3_0_and_gains_the_wkt2(tmp_path, truth, pyproj_crs):
    out = tmp_path / "store"
    encode_with(truth, out, CRS)

    opened = chronozarr.open_store(out)
    assert opened.attrs.spec_version == "0.3.0"
    assert opened.attrs.crs == CRS
    wkt2 = pyproj_crs.from_epsg(32631).to_wkt("WKT2_2019")
    assert (opened.crs.code, opened.crs.wkt2) == (CRS, wkt2)
    attrs = on_disk(out, "0/data")
    assert (attrs["proj:code"], attrs["proj:wkt2"], attrs["crs"]) == (CRS, wkt2, CRS)
    assert attrs["_CRS"] == {"url": "http://www.opengis.net/def/crs/EPSG/0/32631", "wkt": wkt2}
    assert on_disk(out, "0")["crs"] == CRS
    assert on_disk(out, "")["chronozarr"]["crs"] == CRS
    assert schema.validate_report(out).problems == []
    assert schema.validate_report(out).notes == []


def test_the_wkt2_of_a_store_a_released_reader_opens_is_not_a_requirement(
    tmp_path, truth, monkeypatch
):
    """Without pyproj an EPSG code has no WKT2 to write: the code alone still names the CRS."""
    monkeypatch.setattr("chronozarr.crs._pyproj", lambda: None)
    out = tmp_path / "store"
    encode_with(truth, out, CRS)

    attrs = on_disk(out, "0/data")
    assert attrs["proj:code"] == CRS
    assert "proj:wkt2" not in attrs
    assert attrs["_CRS"] == {"url": "http://www.opengis.net/def/crs/EPSG/0/32631"}
    assert on_disk(out, "")["chronozarr"]["spec_version"] == "0.3.0"
    assert schema.validate(out) == []


def test_a_wkt2_crs_without_pyproj_is_still_written(tmp_path, truth, monkeypatch):
    monkeypatch.setattr("chronozarr.crs._pyproj", lambda: None)
    out = tmp_path / "store"
    encode_with(truth, out, LCC_WKT2)
    assert chronozarr.open_store(out).crs.wkt2 == LCC_WKT2
    assert schema.validate(out) == []


@pytest.mark.parametrize("form", ["proj4", "wkt1"])
def test_other_crs_forms_become_wkt2_through_pyproj(tmp_path, truth, pyproj_crs, form):
    parsed = pyproj_crs.from_wkt(SINUSOIDAL_WKT2)
    text = (
        "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
        if form == "proj4"
        else parsed.to_wkt("WKT1_GDAL")
    )
    out = tmp_path / "store"
    encode_with(truth, out, text)

    opened = chronozarr.open_store(out)
    assert opened.crs.code is None
    assert opened.crs.wkt2 is not None
    assert opened.crs.wkt2.startswith("PROJCRS[")
    assert pyproj_crs.from_wkt(opened.crs.wkt2) == parsed
    assert opened.attrs.spec_version == "0.4.0"
    assert_values_equal(out, truth)


def test_a_wkt2_that_pyproj_identifies_as_epsg_gets_its_code(tmp_path, truth, pyproj_crs):
    out = tmp_path / "store"
    encode_with(truth, out, pyproj_crs.from_epsg(32631).to_wkt("WKT2_2019"))
    assert chronozarr.open_store(out).attrs.spec_version == "0.3.0"
    assert chronozarr.open_store(out).crs.code == "EPSG:32631"


@pytest.mark.parametrize("text", ["", "not a crs", "+proj=nonsense"])
def test_a_crs_that_cannot_be_read_is_refused_with_what_to_pass(tmp_path, truth, text):
    with pytest.raises(ValueError, match=r"no crs|cannot read the CRS .*EPSG:32631"):
        encode_with(truth, tmp_path / "store", text)
    assert not (tmp_path / "store").exists()


def test_the_version_depends_on_whether_the_crs_has_an_epsg_code():
    assert schema.spec_version_for(Crs(code="EPSG:32631")) == "0.3.0"
    assert schema.spec_version_for(Crs(code="EPSG:32631", wkt2=LCC_WKT2)) == "0.3.0"
    assert schema.spec_version_for(Crs(code="ESRI:102003")) == "0.4.0"
    assert schema.spec_version_for(Crs(wkt2=LCC_WKT2)) == "0.4.0"
